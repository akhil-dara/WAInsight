"""
System event ingestion from msgstore.db (60+ action types).

Parses system messages (``message_type = 7``) and their
associated detail tables to populate ``system_event``,
``number_change``, ``group_event_detail`` and
``group_past_participant``.

Covers all observed action categories:
    * Security — ``security_code_changed``,
      ``contact_blocked`` / ``unblocked``.
    * Group lifecycle — created, joined, left, subject / icon /
      description changes.
    * Disappearing messages — enabled / disabled / unsupported.
    * Account changes — ``device_linked``, ``number_changed``,
      ``device_changed``, ``username_changed``.
    * Admin actions — promoted, demoted.
    * Business — catalog info, callback enabled / disabled,
      opt-out.
    * Group calls — started, ended.
    * Community — link changes, sibling-group link changes.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader
from app.models.enums import MessageType, get_system_action_label

logger = logging.getLogger(__name__)


def ingest_system_events(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest system events from msgstore.db into analysis.db.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback.

    Returns:
        Number of system events ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_system"):
        logger.warning("message_system table not found")
        return 0

    total = reader.get_row_count("message_system")
    logger.info("Starting system event ingestion: %d total events", total)

    # Build lookups (single query for all message fields)
    msg_map: dict[int, int] = {}
    msg_conv_map: dict[int, int] = {}
    msg_ts_map: dict[int, int] = {}
    msg_sender_map: dict[int, int | None] = {}
    rows = analysis_conn.fetchall(
        "SELECT source_msg_id, id, conversation_id, timestamp, sender_id FROM message"
    )
    for row in rows:
        source_id, msg_id, conv_id, ts, sender_id = row
        msg_map[source_id] = msg_id
        msg_conv_map[msg_id] = conv_id
        msg_ts_map[msg_id] = ts
        msg_sender_map[msg_id] = sender_id

    # Pre-load jid_row_id -> contact_id for fast lookups
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    # Check which detail tables exist
    has_chat_participant = reader.table_exists("message_system_chat_participant")
    has_number_change = reader.table_exists("message_system_number_change")
    has_group = reader.table_exists("message_system_group")

    # Load detail data
    participant_details: dict[int, list[tuple]] = {}
    if has_chat_participant:
        part_rows = reader.execute_raw(
            "SELECT message_row_id, user_jid_row_id FROM message_system_chat_participant"
        )
        for msg_row, user_jid_row in part_rows:
            participant_details.setdefault(msg_row, []).append((user_jid_row,))

    number_changes: dict[int, tuple] = {}
    if has_number_change:
        nc_rows = reader.execute_raw(
            "SELECT message_row_id, old_jid_row_id, new_jid_row_id "
            "FROM message_system_number_change"
        )
        for msg_row, old_jid, new_jid in nc_rows:
            number_changes[msg_row] = (old_jid, new_jid)

    # Load group change details (subject, icon, description changes)
    group_details: dict[int, dict] = {}
    if has_group:
        group_cols = reader.get_column_names("message_system_group")
        logger.info("message_system_group columns: %s", group_cols)
        # Build dynamic SELECT based on available columns
        group_select = ["message_row_id"]
        for col in ("new_subject", "subject", "new_icon", "icon",
                     "new_description", "description", "new_ephemeral_setting"):
            if col in group_cols:
                group_select.append(col)
        if len(group_select) > 1:
            grp_rows = reader.execute_raw(
                f"SELECT {', '.join(group_select)} FROM message_system_group"
            )
            for grp_row in grp_rows:
                grp_dict = dict(zip(group_select, grp_row))
                msg_row_id = grp_dict.pop("message_row_id")
                # Only keep non-None values
                data = {k: v for k, v in grp_dict.items() if v is not None}
                if data:
                    group_details[msg_row_id] = data
            logger.info("Loaded %d group change detail records", len(group_details))

    # Load value change details (old subject names, old roles, etc.)
    value_changes: dict[int, str] = {}
    has_value_change = reader.table_exists("message_system_value_change")
    if has_value_change:
        vc_rows = reader.execute_raw(
            "SELECT message_row_id, old_data FROM message_system_value_change"
        )
        for msg_row, old_data in vc_rows:
            if old_data is not None:
                value_changes[msg_row] = old_data
        logger.info("Loaded %d value change records", len(value_changes))

    # Load ephemeral timer settings (duration for disappearing message events)
    ephemeral_settings: dict[int, dict] = {}
    has_ephemeral_setting = reader.table_exists("message_ephemeral_setting")
    if has_ephemeral_setting:
        es_cols = reader.get_column_names("message_ephemeral_setting")
        has_pre = "pre_setting_duration" in es_cols
        es_rows = reader.execute_raw(
            "SELECT message_row_id, setting_duration"
            + (", pre_setting_duration" if has_pre else ", NULL")
            + " FROM message_ephemeral_setting"
        )
        for msg_row, duration, pre_duration in es_rows:
            d: dict = {}
            if duration is not None:
                d["ephemeral_duration"] = duration
            if pre_duration is not None:
                d["ephemeral_pre_duration"] = pre_duration
            if d:
                ephemeral_settings[msg_row] = d
        logger.info("Loaded %d ephemeral setting records", len(ephemeral_settings))

    # Load community/group names from message_system_with_group_nodes
    community_names: dict[int, str] = {}
    has_group_nodes = reader.table_exists("message_system_with_group_nodes")
    if has_group_nodes:
        gn_cols = reader.get_column_names("message_system_with_group_nodes")
        has_group_subject = "group_subject" in gn_cols
        has_group_node_type = "group_node_type" in gn_cols
        if has_group_subject:
            # group_node_type=1 means community name, type=2 means group name
            # Prefer type=1 (community), fallback to any available group_subject
            gn_query = (
                "SELECT message_row_id, group_subject, group_node_type "
                "FROM message_system_with_group_nodes "
                "WHERE group_subject IS NOT NULL AND group_subject != ''"
            ) if has_group_node_type else (
                "SELECT message_row_id, group_subject, NULL "
                "FROM message_system_with_group_nodes "
                "WHERE group_subject IS NOT NULL AND group_subject != ''"
            )
            gn_rows = reader.execute_raw(gn_query)
            for msg_row, group_subject, node_type in gn_rows:
                # type 1 = community, type 2 = group; prefer community name
                if node_type == 1 or msg_row not in community_names:
                    community_names[msg_row] = group_subject
            logger.info("Loaded %d community/group names from message_system_with_group_nodes",
                        len(community_names))

    # Load text_data from messages for new subject names etc.
    msg_text_map: dict[int, str] = {}
    text_rows = reader.execute_raw(
        "SELECT ms.message_row_id, m.text_data "
        "FROM message_system ms "
        "JOIN message m ON m._id = ms.message_row_id "
        "WHERE m.text_data IS NOT NULL"
    )
    for msg_row, text_data in text_rows:
        msg_text_map[msg_row] = text_data
    logger.info("Loaded %d message text_data records for system events", len(msg_text_map))

    # Query system events
    ms_cols = reader.get_column_names("message_system")
    has_action_type = "action_type" in ms_cols

    sys_rows = reader.execute_raw(
        "SELECT message_row_id"
        + (", action_type" if has_action_type else "")
        + " FROM message_system"
    )

    insert_sql = """
        INSERT OR IGNORE INTO system_event (
            message_id, conversation_id, event_type, event_label,
            actor_id, target_id, event_data, community_name, timestamp
        ) VALUES (?,?,?,?,?,?,?,?,?)
    """

    nc_insert_sql = """
        INSERT INTO number_change (
            system_event_id, old_jid_row_id, new_jid_row_id,
            old_contact_id, new_contact_id
        ) VALUES (?,?,?,?,?)
    """

    detail_insert_sql = """
        INSERT INTO group_event_detail (
            system_event_id, participant_contact_id
        ) VALUES (?,?)
    """

    processed = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in sys_rows:
            source_msg_id = row[0]
            action_type = row[1] if has_action_type and len(row) > 1 else 0

            msg_id = msg_map.get(source_msg_id)
            if msg_id is None:
                continue

            conv_id = msg_conv_map.get(msg_id)
            if conv_id is None:
                continue

            timestamp = msg_ts_map.get(msg_id, 0)
            event_label = get_system_action_label(action_type or 0)

            # Resolve actor and target based on event type.
            #
            # For MOST event types with participants (12, 14, 81, etc.):
            # actor = message sender (the admin who performed the action)
            # target = first participant (the person acted upon)
            #
            # For role events WITHOUT participants (83, 84):
            # sender_jid = the person whose role changed (the TARGET)
            # The person who performed the action is implicit (phone owner)
            # So: actor = None, target = sender
            #
            # For number_changed events (10, 28):
            # sender_jid = the person whose number changed (TARGET)
            # actor = None (WhatsApp system event)
            SENDER_IS_TARGET_TYPES = {83, 84, 10, 28}

            participants = participant_details.get(source_msg_id, [])

            if action_type in SENDER_IS_TARGET_TYPES:
                # sender_jid is the subject/target of the event
                actor_id = None
                target_id = msg_sender_map.get(msg_id)
            else:
                # Standard: sender is the actor
                actor_id = msg_sender_map.get(msg_id)
                target_id = None
                if participants:
                    target_jid_row = participants[0][0]
                    target_id = jid_to_contact.get(target_jid_row)

            # Build event_data JSON for complex events
            event_data_dict: dict = {}

            # Attach group change details (subject/icon/description changes)
            if source_msg_id in group_details:
                event_data_dict.update(group_details[source_msg_id])

            # For group_subject_changed (type 1): old name from value_change, new name from text_data
            if action_type == 1:
                if source_msg_id in value_changes:
                    event_data_dict["old_subject"] = value_changes[source_msg_id]
                if source_msg_id in msg_text_map:
                    event_data_dict["new_subject"] = msg_text_map[source_msg_id]

            # Store value_change for types that have it (roles, join method, etc.)
            if source_msg_id in value_changes:
                event_data_dict["old_value"] = value_changes[source_msg_id]

            # Store text_data for types that have meaningful text
            # (disappearing timers, subject names, event names, descriptions, etc.)
            if source_msg_id in msg_text_map:
                event_data_dict["text_data"] = msg_text_map[source_msg_id]

            # Attach ephemeral timer duration for disappearing message events
            if source_msg_id in ephemeral_settings:
                event_data_dict.update(ephemeral_settings[source_msg_id])

            event_data = json.dumps(event_data_dict, ensure_ascii=False) if event_data_dict else None

            # Community/group name from message_system_with_group_nodes
            comm_name = community_names.get(source_msg_id)

            cursor.execute(insert_sql, (
                msg_id, conv_id, action_type or 0, event_label,
                actor_id, target_id, event_data, comm_name, timestamp,
            ))

            event_id = analysis_conn.raw_connection.last_insert_rowid()

            # Write number_change details
            if source_msg_id in number_changes:
                old_jid_row, new_jid_row = number_changes[source_msg_id]
                old_contact = jid_to_contact.get(old_jid_row) if old_jid_row else None
                new_contact = jid_to_contact.get(new_jid_row) if new_jid_row else None

                cursor.execute(nc_insert_sql, (
                    event_id, old_jid_row, new_jid_row, old_contact, new_contact,
                ))

            # Write group_event_detail for multi-participant events
            if len(participants) > 0:
                for (user_jid_row,) in participants:
                    contact_id = jid_to_contact.get(user_jid_row)
                    if contact_id:
                        cursor.execute(detail_insert_sql, (event_id, contact_id))

            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if progress_callback:
        progress_callback(processed, total)

    logger.info("System event ingestion complete: %d events", processed)
    return processed
