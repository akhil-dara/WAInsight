"""
Conversation (chat) ingestion from msgstore.db ``chat`` table.

Normalizes every conversation (personal, group, community,
broadcast, newsletter, status) into the analysis ``conversation``
and ``group_member`` tables.

Handles:
    * Chat type classification — personal / group / community /
      broadcast / newsletter / status.
    * Group type detection — regular, community announcement,
      community sub-group.
    * Community hierarchy linking — sub-groups linked to their
      parent announcement group.
    * Group membership with roles, labels, join / leave tracking.
    * Archive / pin / mute / lock state.
    * Ephemeral (disappearing) message settings.
    * Past participants (from ``group_past_participant_user``).
"""

from __future__ import annotations

import logging
from typing import Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader
from app.models.enums import GroupType, ChatType, JidServer
from app.utils.jid_parser import parse_jid

logger = logging.getLogger(__name__)


def ingest_conversations(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
) -> int:
    """Ingest all conversations from msgstore.db into analysis.db.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.

    Returns:
        Number of conversations ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    # Resolve JID raw strings from the jid table for chat classification
    jid_rows = reader.execute_raw("SELECT _id, raw_string, type, server FROM jid")
    jid_lookup: dict[int, tuple[str, int | None, str | None]] = {
        row[0]: (row[1] or "", row[2], row[3]) for row in jid_rows
    }

    # Check available columns in chat table
    chat_columns = reader.get_column_names("chat")
    logger.debug("chat table columns: %s", chat_columns)

    # Build SELECT for chat with available columns
    base_cols = ["_id", "jid_row_id"]
    optional_cols = {
        "subject": "subject",
        "created_timestamp": "created_timestamp",
        "display_message_row_id": "display_message_row_id",
        "sort_timestamp": "sort_timestamp",
        "archived": "archived",
        "unseen_earliest_message_received_time": "unseen_earliest",
        "unseen_message_count": "unseen_message_count",
        "hidden": "hidden",
        "group_type": "group_type",
        "chat_lock": "chat_lock",
        "description": "description",
        "ephemeral_expiration": "ephemeral_expiration",
        # Device-owner role per:
        # NULL=channel/broadcast, 0=personal, 1=no-longer-member,
        # 2=member, 3=admin, 4=creator+admin
        "participation_status": "participation_status",
    }

    select_cols = list(base_cols)
    available_optionals: list[str] = []
    for col, alias in optional_cols.items():
        if col in chat_columns:
            select_cols.append(col)
            available_optionals.append(col)

    chat_rows = reader.execute_raw(
        f"SELECT {', '.join(select_cols)} FROM chat ORDER BY sort_timestamp DESC"
    )

    # Load pin info from chat_pinned if available
    pin_lookup: dict[int, int] = {}
    if reader.table_exists("chat_pinned"):
        pin_rows = reader.execute_raw(
            "SELECT chat_row_id, timestamp FROM chat_pinned"
        )
        pin_lookup = {row[0]: row[1] for row in pin_rows}

    # Load mute info from chatsettings.db if available
    mute_lookup: dict[int, int] = {}
    try:
        cs_conn = db_manager.get_source("chatsettings.db")
        cs_reader = SourceReader(cs_conn)
        if cs_reader.table_exists("settings"):
            cs_cols = cs_reader.get_column_names("settings")
            if "chat_row_id" in cs_cols and "mute" in cs_cols:
                mute_rows = cs_reader.execute_raw(
                    "SELECT chat_row_id, mute FROM settings WHERE mute > 0"
                )
                mute_lookup = {row[0]: row[1] for row in mute_rows}
    except FileNotFoundError:
        logger.info("chatsettings.db not found - mute data unavailable")

    # Load group descriptions from wa.db (msgstore.db chat.description is always NULL)
    desc_lookup: dict[str, str] = {}
    addr_mode_lookup: dict[str, str] = {}
    admin_settings_lookup: dict[str, dict] = {}
    try:
        wa_conn = db_manager.get_wa_db()
        wa_reader = SourceReader(wa_conn)
        if wa_reader.table_exists("wa_group_descriptions"):
            desc_rows = wa_reader.execute_raw(
                "SELECT jid, description FROM wa_group_descriptions "
                "WHERE description IS NOT NULL AND description != ''"
            )
            desc_lookup = {row[0]: row[1] for row in desc_rows}
            logger.info("Loaded %d group descriptions from wa.db", len(desc_lookup))

        # Load community privacy settings (addressing_mode)
        if wa_reader.table_exists("wa_group_admin_settings"):
            admin_cols = wa_reader.get_column_names("wa_group_admin_settings")
            if "addressing_mode" in admin_cols:
                addr_rows = wa_reader.execute_raw(
                    "SELECT jid, addressing_mode FROM wa_group_admin_settings "
                    "WHERE addressing_mode IS NOT NULL AND addressing_mode != ''"
                )
                addr_mode_lookup = {row[0]: row[1] for row in addr_rows}
                logger.info(
                    "Loaded %d addressing_mode entries from wa.db "
                    "(lid=%d, pn=%d)",
                    len(addr_mode_lookup),
                    sum(1 for v in addr_mode_lookup.values() if v == "lid"),
                    sum(1 for v in addr_mode_lookup.values() if v == "pn"),
                )

            # Load full group admin settings:
            # announcement_group — 1 = only admins can send messages
            # restrict_mode       — 1 = only admins can edit subject/icon/desc
            # require_membership_approval — 1 = admin approval needed to join
            # member_add_mode     — 0 = anyone adds, 1 = admins only
            # creator_jid         — original creator's JID/LID
            admin_select_parts = ["jid"]
            for opt in ("announcement_group", "restrict_mode",
                        "require_membership_approval", "member_add_mode",
                        "creator_jid"):
                if opt in admin_cols:
                    admin_select_parts.append(opt)
            if len(admin_select_parts) > 1:
                rows = wa_reader.execute_raw(
                    f"SELECT {', '.join(admin_select_parts)} FROM wa_group_admin_settings"
                )
                for r in rows:
                    d = dict(zip(admin_select_parts, r))
                    jid = d.pop("jid", None)
                    if jid:
                        admin_settings_lookup[jid] = d
                logger.info(
                    "Loaded %d wa_group_admin_settings rows "
                    "(announcement-only=%d, restrict-only=%d)",
                    len(admin_settings_lookup),
                    sum(1 for v in admin_settings_lookup.values()
                        if v.get("announcement_group")),
                    sum(1 for v in admin_settings_lookup.values()
                        if v.get("restrict_mode")),
                )
    except FileNotFoundError:
        logger.info("wa.db not found - group descriptions unavailable")

    # Insert conversations
    insert_sql = """
        INSERT INTO conversation (
            source_chat_id, jid_raw_string, chat_type,
            display_name, subject, description,
            created_timestamp,
            is_hidden, sort_timestamp,
            is_archived, is_pinned, pin_timestamp,
            is_muted, mute_end_time, is_locked,
            ephemeral_duration, group_type, addressing_mode,
            source_unseen_count,
            participation_status,
            announcement_group, restrict_mode,
            require_membership_approval, member_add_mode,
            creator_jid_raw
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    count = 0
    chat_id_map: dict[int, int] = {}  # source chat._id -> analysis conversation.id
    jid_row_to_conv: dict[int, int] = {}  # jid._id -> analysis conversation.id

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in chat_rows:
            row_dict = dict(zip(select_cols, row))
            chat_id = row_dict["_id"]
            jid_row_id = row_dict["jid_row_id"]

            # Get JID info for classification
            jid_info = jid_lookup.get(jid_row_id, ("", None, None))
            raw_string, jid_type, server = jid_info

            # Classify chat type
            parsed = parse_jid(raw_string)
            if parsed:
                chat_type = parsed.chat_type.value
            elif server == JidServer.GROUP:
                chat_type = ChatType.GROUP.value
            elif server == JidServer.BROADCAST:
                if raw_string == "status@broadcast":
                    chat_type = ChatType.STATUS.value
                else:
                    chat_type = ChatType.BROADCAST.value
            elif server == JidServer.NEWSLETTER:
                chat_type = ChatType.NEWSLETTER.value
            else:
                chat_type = ChatType.PERSONAL.value

            # Group type
            group_type_val = row_dict.get("group_type")

            # Override chat_type for communities
            if group_type_val in (
                GroupType.COMMUNITY_ANNOUNCE,
                GroupType.COMMUNITY_META,
            ):
                chat_type = ChatType.COMMUNITY.value
            elif group_type_val in (
                GroupType.COMMUNITY_SUBGROUP,
                GroupType.COMMUNITY_DEFAULT,
            ):
                chat_type = ChatType.GROUP.value  # Sub-groups are still groups

            # Display name: use subject for groups, resolve for personal
            subject = row_dict.get("subject")
            display_name = subject if subject else None

            if not display_name and chat_type == ChatType.PERSONAL.value:
                # Try to get name from jid_to_contact
                contact_row = analysis_conn.fetchone(
                    "SELECT c.resolved_name FROM jid_to_contact j "
                    "JOIN contact c ON j.contact_id = c.id "
                    "WHERE j.jid_row_id = ?",
                    (jid_row_id,),
                )
                if contact_row:
                    display_name = contact_row[0]
                elif raw_string:
                    display_name = raw_string

            # Pinned status
            is_pinned = chat_id in pin_lookup
            pin_ts = pin_lookup.get(chat_id)

            # Mute status
            mute_end = mute_lookup.get(chat_id, 0)
            is_muted = mute_end > 0

            # Group admin settings (from wa.db.wa_group_admin_settings).
            # Only looked up for group/community chats; personal chats get NULLs.
            _admin = admin_settings_lookup.get(raw_string or "", {}) if raw_string else {}

            cursor.execute(insert_sql, (
                chat_id,
                raw_string,
                chat_type,
                display_name,
                subject,
                desc_lookup.get(raw_string) or row_dict.get("description"),
                row_dict.get("created_timestamp"),
                1 if row_dict.get("hidden") else 0,
                row_dict.get("sort_timestamp"),
                1 if row_dict.get("archived") else 0,
                1 if is_pinned else 0,
                pin_ts,
                1 if is_muted else 0,
                mute_end if is_muted else None,
                1 if row_dict.get("chat_lock") else 0,
                row_dict.get("ephemeral_expiration"),
                group_type_val,
                addr_mode_lookup.get(raw_string),
                row_dict.get("unseen_message_count"),
                # Forensic fields from chat / wa_group_admin_settings:
                row_dict.get("participation_status"),
                _admin.get("announcement_group"),
                _admin.get("restrict_mode"),
                _admin.get("require_membership_approval"),
                _admin.get("member_add_mode"),
                _admin.get("creator_jid"),
            ))

            conv_id = analysis_conn.raw_connection.last_insert_rowid()
            chat_id_map[chat_id] = conv_id
            jid_row_to_conv[jid_row_id] = conv_id
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d conversations", count)

    # Now ingest group members
    member_count = _ingest_group_members(
        reader, analysis_conn, jid_row_to_conv
    )
    logger.info("Ingested %d group members", member_count)

    # Ingest past participants
    past_count = _ingest_past_participants(
        reader, analysis_conn, jid_row_to_conv
    )
    logger.info("Ingested %d past group participants", past_count)

    # Link community sub-groups to parent
    _link_community_hierarchy(
        reader, analysis_conn, chat_id_map, jid_row_to_conv, db_manager
    )

    return count


def _ingest_group_members(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    jid_row_to_conv: dict[int, int],
) -> int:
    """Ingest group participant records into group_member table.

    Sources from ``group_participant_user`` table which tracks current
    group memberships with roles and per-group nicknames (labels).

    Args:
        jid_row_to_conv: Mapping from jid._id to analysis conversation.id.
    """
    if not reader.table_exists("group_participant_user"):
        logger.warning("group_participant_user table not found")
        return 0

    columns = reader.get_column_names("group_participant_user")
    has_label = "label" in columns
    has_rank = "rank" in columns
    has_admin = "admin" in columns
    has_add_timestamp = "add_timestamp" in columns
    has_join_method = "join_method" in columns
    has_pending = "pending" in columns

    select_parts = ["group_jid_row_id", "user_jid_row_id"]
    if has_rank:
        select_parts.append("rank")
    if has_admin:
        select_parts.append("admin")
    if has_label:
        select_parts.append("label")
    if has_add_timestamp:
        select_parts.append("add_timestamp")
    if has_join_method:
        select_parts.append("join_method")
    if has_pending:
        select_parts.append("pending")

    rows = reader.execute_raw(
        f"SELECT {', '.join(select_parts)} FROM group_participant_user"
    )

    # Pre-load jid_row_id -> contact_id mapping for fast lookups
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_row_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    insert_sql = """
        INSERT OR IGNORE INTO group_member (
            conversation_id, contact_id, role, label,
            join_timestamp, join_method, is_current
        ) VALUES (?,?,?,?,?,?,?)
    """

    count = 0
    skipped_conv = 0
    skipped_contact = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in rows:
            row_dict = dict(zip(select_parts, row))
            group_jid_row = row_dict["group_jid_row_id"]
            user_jid_row = row_dict["user_jid_row_id"]

            # Resolve group jid._id -> conversation.id via pre-built mapping
            conv_id = jid_row_to_conv.get(group_jid_row)
            if conv_id is None:
                skipped_conv += 1
                continue

            # Resolve user jid._id -> contact.id via pre-loaded mapping
            contact_id = jid_row_to_contact.get(user_jid_row)
            if contact_id is None:
                skipped_contact += 1
                continue

            # Determine role
            role = "member"
            rank_val = row_dict.get("rank", 0)
            admin_val = row_dict.get("admin", 0)
            if admin_val == 2 or rank_val == 2:
                role = "superadmin"
            elif admin_val == 1 or rank_val == 1:
                role = "admin"

            label = row_dict.get("label")
            join_ts = row_dict.get("add_timestamp")
            join_method = row_dict.get("join_method")
            # pending=1 means the user hasn't accepted the invite yet
            is_current = 0 if row_dict.get("pending") else 1

            cursor.execute(insert_sql, (
                conv_id, contact_id, role, label,
                join_ts, join_method, is_current,
            ))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    if skipped_conv or skipped_contact:
        logger.debug(
            "Group members: skipped %d (no conv), %d (no contact)",
            skipped_conv, skipped_contact,
        )

    return count


def _ingest_past_participants(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    jid_row_to_conv: dict[int, int],
) -> int:
    """Ingest past group participants from group_past_participant_user table.

    Records who left/was removed from groups and when.

    Args:
        jid_row_to_conv: Mapping from jid._id to analysis conversation.id.
    """
    if not reader.table_exists("group_past_participant_user"):
        logger.info("group_past_participant_user not found")
        return 0

    columns = reader.get_column_names("group_past_participant_user")
    has_state = "state" in columns
    has_timestamp = "timestamp" in columns

    rows = reader.execute_raw(
        "SELECT group_jid_row_id, user_jid_row_id"
        + (", state" if has_state else "")
        + (", timestamp" if has_timestamp else "")
        + " FROM group_past_participant_user"
    )

    # Pre-load jid_row_id -> contact_id mapping
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_row_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    insert_sql = """
        INSERT OR IGNORE INTO group_past_participant (
            conversation_id, contact_id, state, last_seen_ts
        ) VALUES (?,?,?,?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in rows:
            group_jid_row = row[0]
            user_jid_row = row[1]
            state = row[2] if has_state and len(row) > 2 else None
            timestamp = row[3] if has_timestamp and len(row) > 3 else None

            conv_id = jid_row_to_conv.get(group_jid_row)
            if conv_id is None:
                continue

            contact_id = jid_row_to_contact.get(user_jid_row)
            if contact_id is None:
                continue

            cursor.execute(insert_sql, (conv_id, contact_id, state, timestamp))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    return count


def _link_community_hierarchy(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    chat_id_map: dict[int, int],
    jid_row_to_conv: dict[int, int],
    db_manager: DatabaseManager | None = None,
) -> None:
    """Link community sub-groups to their parent community conversation.

    Uses multiple strategies in order of preference:

    1. **wa.db group_relationship** — definitive parent-child
       JID mapping stored in wa.db.  Each sub-group row has a
       ``parent_raw_jid`` pointing to the community.
    2. **Subject-name matching** — announcement groups
       (``group_type = 3``) share the same subject as the parent
       community (``group_type = 1``).
    3. **msgstore system messages** —
       ``message_system_with_group_nodes`` with
       ``action_type = 110`` (a hidden link event in the parent
       chat).
    """
    linked_count = 0

    # Build JID raw_string → conversation.id map for all conversations
    jid_to_conv: dict[str, int] = {}
    jid_conv_rows = analysis_conn.fetchall(
        "SELECT id, jid_raw_string FROM conversation "
        "WHERE jid_raw_string IS NOT NULL"
    )
    for cid, jid_str in jid_conv_rows:
        jid_to_conv[jid_str] = cid

    # Also build community (type=1) subject → conversation.id
    community_by_subject: dict[str, int] = {}
    comm_rows = analysis_conn.fetchall(
        "SELECT id, subject FROM conversation "
        "WHERE group_type = 1 AND subject IS NOT NULL"
    )
    for cid, subj in comm_rows:
        community_by_subject[subj] = cid

    # Start explicit transaction for all community updates
    analysis_conn.begin_transaction()

    # ------------------------------------------------------------------
    # Strategy 1: wa.db group_relationship (DEFINITIVE)
    #
    # ``group_relationship(parent_raw_jid, subgroup_raw_id)`` in
    # wa.db carries one row per sub-group that points at its
    # parent community JID — the authoritative source.
    # ------------------------------------------------------------------
    wadb_linked = 0
    wadb_available = False  # Track if wa.db was used (definitive source)
    if db_manager is not None:
        try:
            wa_conn = db_manager.get_source("wa.db")
            wa_reader = SourceReader(wa_conn)
            if wa_reader.table_exists("group_relationship"):
                gr_cols = wa_reader.get_column_names("group_relationship")
                if "parent_raw_jid" in gr_cols and "subgroup_raw_id" in gr_cols:
                    gr_rows = wa_reader.execute_raw(
                        "SELECT parent_raw_jid, subgroup_raw_id "
                        "FROM group_relationship"
                    )
                    # wa.db group_relationship is DEFINITIVE — if it exists
                    # and was readable, mark as available even if empty.
                    # Groups removed from communities will NOT appear here,
                    # so we must NOT re-link them via fallback strategies.
                    wadb_available = True
                    if gr_rows:
                        logger.info(
                            "Found %d rows in wa.db group_relationship",
                            len(gr_rows),
                        )
                        for parent_jid, sub_jid in gr_rows:
                            parent_conv = jid_to_conv.get(parent_jid)
                            child_conv = jid_to_conv.get(sub_jid)
                            if parent_conv and child_conv:
                                analysis_conn.execute(
                                    "UPDATE conversation "
                                    "SET community_parent_id = ? "
                                    "WHERE id = ? AND community_parent_id IS NULL",
                                    (parent_conv, child_conv),
                                )
                                wadb_linked += 1
                        if wadb_linked:
                            logger.info(
                                "Strategy 1 (wa.db group_relationship): "
                                "linked %d sub-groups", wadb_linked,
                            )
                            linked_count += wadb_linked
        except FileNotFoundError:
            logger.debug("wa.db not available for community linking")
        except Exception as exc:
            logger.warning("Error reading wa.db group_relationship: %s", exc)

    # ------------------------------------------------------------------
    # Strategies 2 & 3 are FALLBACKS — only used when wa.db is NOT
    # available. wa.db group_relationship is the single source of truth
    # for community membership. Groups that were removed from a
    # community are absent from wa.db but may still appear in old
    # system messages (action_type=110), which would incorrectly
    # re-link them.
    # ------------------------------------------------------------------
    if wadb_available:
        logger.info(
            "wa.db group_relationship is definitive — skipping "
            "fallback strategies 2 & 3 to avoid stale community links"
        )
    else:
        # Strategy 2: Subject-name matching for announcement (type=3)
        # groups → parent community (type=1)
        if community_by_subject:
            ann_rows = analysis_conn.fetchall(
                "SELECT id, subject FROM conversation "
                "WHERE group_type = 3 AND community_parent_id IS NULL "
                "AND subject IS NOT NULL"
            )
            name_linked = 0
            for ann_id, ann_subj in ann_rows:
                parent_id = community_by_subject.get(ann_subj)
                if parent_id and parent_id != ann_id:
                    analysis_conn.execute(
                        "UPDATE conversation SET community_parent_id = ? WHERE id = ?",
                        (parent_id, ann_id),
                    )
                    name_linked += 1
            if name_linked:
                logger.info(
                    "Strategy 2 (subject match): linked %d announcement groups",
                    name_linked,
                )
                linked_count += name_linked

        # Strategy 3: msgstore message_system action_type=110 (hidden msg
        # in parent community chat referencing added sub-groups)
        unlinked = analysis_conn.fetchall(
            "SELECT id FROM conversation "
            "WHERE group_type IN (2, 6) AND community_parent_id IS NULL"
        )
        if unlinked and reader.table_exists("message_system") and \
                reader.table_exists("message_system_with_group_nodes"):
            logger.info(
                "Strategy 3 (system messages): attempting to link %d "
                "remaining unlinked sub-groups", len(unlinked),
            )
            sys_rows = reader.execute_raw("""
                SELECT DISTINCT
                    gn.group_jid_row_id AS sub_jid_row_id,
                    m.chat_row_id       AS parent_chat_id
                FROM message_system ms
                JOIN message m ON m._id = ms.message_row_id
                JOIN chat parent ON parent._id = m.chat_row_id
                                AND parent.group_type = 1
                JOIN message_system_with_group_nodes gn
                     ON gn.message_row_id = ms.message_row_id
                     AND gn.group_node_type = 2
                WHERE ms.action_type = 110
            """)
            sys_linked = 0
            if sys_rows:
                for sub_jid_row, parent_chat_id in sys_rows:
                    parent_conv = chat_id_map.get(parent_chat_id)
                    child_conv = jid_row_to_conv.get(sub_jid_row)
                    if parent_conv and child_conv:
                        analysis_conn.execute(
                            "UPDATE conversation SET community_parent_id = ? "
                            "WHERE id = ? AND community_parent_id IS NULL",
                            (parent_conv, child_conv),
                        )
                        sys_linked += 1
                if sys_linked:
                    logger.info(
                        "Strategy 3 (system messages): linked %d sub-groups",
                        sys_linked,
                    )
                    linked_count += sys_linked

    analysis_conn.commit()

    # ------------------------------------------------------------------
    # Log final community statistics
    # ------------------------------------------------------------------
    comm_count = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM conversation WHERE group_type IN (1, 3)"
    )
    sub_count = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM conversation WHERE group_type IN (2, 6)"
    )
    linked_total = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM conversation WHERE community_parent_id IS NOT NULL"
    )
    if comm_count and comm_count[0]:
        logger.info(
            "Community chats: %d announcement/meta, %d sub-groups (%d linked)",
            comm_count[0], sub_count[0] if sub_count else 0,
            linked_total[0] if linked_total else 0,
        )
