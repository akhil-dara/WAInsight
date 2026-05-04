"""
Group metadata change ingestion from msgstore.db.

Extracts a forensic timeline of all group modifications:
  - Subject (name) changes: action_type 1 → old name from message_system_value_change,
    new name from message.text_data
  - Description changes: action_type 27 → new description from message.text_data
  - Icon/DP changes: action_type 6 → old/new photo BLOBs from message_system_photo_change
  - Settings changes: action_types 29-32, 56, 83-85 → admin-only, disappearing, invite link

Source tables in msgstore.db:
  message_system            — action_type for every system event
  message_system_value_change — old_data (previous subject, previous setting value)
  message_system_photo_change — old_photo, new_photo (JPEG thumbnails ≈2-5KB each)
  message                   — text_data (new subject or new description text), timestamp, sender_jid_row_id
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)

# Action type → change_type label mapping
ACTION_TYPE_MAP: dict[int, str] = {
    1:  "subject",
    6:  "icon",
    27: "description",
    29: "admin_only_edit_on",
    30: "admin_only_edit_off",
    31: "admin_only_send_on",
    32: "admin_only_send_off",
    56: "disappearing",
    83: "invite_link_reset",
    84: "approval_mode",
    85: "membership_approval",
}

# Group chat JID suffix
GROUP_JID_SUFFIX = "@g.us"


def ingest_group_metadata_changes(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest group metadata changes from msgstore.db into analysis.db.

    Reads from message_system joined with value_change and photo_change tables
    to build a complete forensic timeline of group modifications.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of group metadata change records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_system"):
        logger.warning("message_system table not found — skipping group metadata ingestion")
        return 0

    # ------------------------------------------------------------------ #
    # 1. Build lookup maps from analysis.db
    # ------------------------------------------------------------------ #

    # source_msg_id → (analysis_msg_id, conversation_id, timestamp, sender_contact_id)
    msg_rows = analysis_conn.fetchall(
        "SELECT source_msg_id, id, conversation_id, timestamp, sender_id FROM message"
    )
    msg_lookup: dict[int, tuple[int, int, int, int | None]] = {}
    for r in msg_rows:
        msg_lookup[r[0]] = (r[1], r[2], r[3], r[4])

    # jid_row_id → contact_id
    jid_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_rows if r[0] is not None
    }

    # conversation_id → chat_type (to filter groups only)
    conv_rows = analysis_conn.fetchall(
        "SELECT id, chat_type FROM conversation"
    )
    group_conv_ids: set[int] = {
        r[0] for r in conv_rows if r[1] in ("group", "community", "community_sub")
    }

    # ------------------------------------------------------------------ #
    # 2. Load detail tables from msgstore.db
    # ------------------------------------------------------------------ #

    # action_type filter as SQL IN clause
    action_types = tuple(sorted(ACTION_TYPE_MAP.keys()))
    placeholders = ",".join("?" * len(action_types))

    # Main system events for our target action types
    sys_rows = reader.execute_raw(
        f"SELECT ms.message_row_id, ms.action_type, "
        f"       m.text_data, m.sender_jid_row_id, m.from_me "
        f"FROM message_system ms "
        f"JOIN message m ON m._id = ms.message_row_id "
        f"WHERE ms.action_type IN ({placeholders})",
        action_types,
    )
    logger.info("Found %d group metadata system events for target action types", len(sys_rows))

    # Value changes (old subject, old settings value)
    value_changes: dict[int, str] = {}
    if reader.table_exists("message_system_value_change"):
        vc_rows = reader.execute_raw(
            "SELECT message_row_id, old_data FROM message_system_value_change "
            "WHERE old_data IS NOT NULL"
        )
        for msg_row, old_data in vc_rows:
            value_changes[msg_row] = old_data
        logger.info("Loaded %d value change records (old subjects/settings)", len(value_changes))

    # Photo changes (old/new group DP)
    photo_changes: dict[int, tuple[bytes | None, bytes | None, str | None]] = {}
    if reader.table_exists("message_system_photo_change"):
        pc_rows = reader.execute_raw(
            "SELECT message_row_id, old_photo, new_photo, new_photo_id "
            "FROM message_system_photo_change"
        )
        for msg_row, old_photo, new_photo, new_photo_id in pc_rows:
            photo_changes[msg_row] = (old_photo, new_photo, new_photo_id)
        logger.info("Loaded %d photo change records (group DP changes)", len(photo_changes))

    # ------------------------------------------------------------------ #
    # 3. Insert into group_metadata_change
    # ------------------------------------------------------------------ #

    insert_sql = """
        INSERT OR IGNORE INTO group_metadata_change (
            conversation_id, change_type, old_value, new_value,
            old_photo, new_photo, changed_by_id, message_id,
            source_msg_id, action_type, timestamp
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """

    total = len(sys_rows)
    processed = 0
    skipped = 0

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in sys_rows:
            source_msg_id = row[0]
            action_type = row[1]
            text_data = row[2]
            sender_jid_row_id = row[3]
            from_me = row[4]

            # Look up in analysis.db
            msg_info = msg_lookup.get(source_msg_id)
            if msg_info is None:
                skipped += 1
                continue

            analysis_msg_id, conv_id, timestamp, sender_contact_id = msg_info

            # Only process group conversations
            if conv_id not in group_conv_ids:
                skipped += 1
                continue

            change_type = ACTION_TYPE_MAP.get(action_type, f"unknown_{action_type}")

            # Determine who made the change
            changed_by_id = sender_contact_id
            if changed_by_id is None and sender_jid_row_id:
                changed_by_id = jid_to_contact.get(sender_jid_row_id)

            # Build old_value / new_value based on change type
            old_value = None
            new_value = None
            old_photo_blob = None
            new_photo_blob = None

            if action_type == 1:
                # Subject change: old from value_change, new from text_data
                old_value = value_changes.get(source_msg_id)
                new_value = text_data if text_data else None

            elif action_type == 27:
                # Description change: new description in text_data
                new_value = text_data if text_data else None

            elif action_type == 6:
                # Icon/DP change: photos from photo_change table
                if source_msg_id in photo_changes:
                    old_photo_blob, new_photo_blob, photo_id = photo_changes[source_msg_id]
                    if photo_id == "-1":
                        new_value = "removed"
                    elif photo_id:
                        new_value = f"photo_id:{photo_id}"

            elif action_type in (29, 30):
                # Admin-only edit on/off
                old_value = value_changes.get(source_msg_id)
                new_value = "on" if action_type == 29 else "off"

            elif action_type in (31, 32):
                # Admin-only send on/off
                old_value = value_changes.get(source_msg_id)
                new_value = "on" if action_type == 31 else "off"

            elif action_type == 56:
                # Disappearing messages changed
                old_value = value_changes.get(source_msg_id)
                new_value = text_data if text_data else None

            elif action_type == 83:
                # Invite link reset
                old_value = value_changes.get(source_msg_id)  # "invite_link"
                new_value = "reset"

            elif action_type in (84, 85):
                # Approval mode / membership approval changed
                old_value = value_changes.get(source_msg_id)
                new_value = text_data if text_data else None

            cursor.execute(insert_sql, (
                conv_id, change_type, old_value, new_value,
                old_photo_blob, new_photo_blob, changed_by_id,
                analysis_msg_id, source_msg_id, action_type, timestamp,
            ))
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if progress_callback:
        progress_callback(processed, total)

    logger.info(
        "Group metadata change ingestion complete: %d records ingested, %d skipped "
        "(non-group or missing message)",
        processed, skipped,
    )
    return processed
