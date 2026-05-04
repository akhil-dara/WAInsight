"""
Message ingestion from msgstore.db.

Processes all 30+ WhatsApp message types in batched, multi-table
JOIN passes that gather text, quoted content, forwarding info,
view-once state, edit tracking, revocation, and private-reply
detection in a single sweep over the source data.

Handles:
    - All message types (text, image, video, audio, document, sticker, GIF,
      poll, call_log, system, newsletter, AI/bot, album, etc.)
    - Reply chain resolution (message_quoted → reply_to_msg_id)
    - Forward detection and forward_score
    - Edit tracking (message_edit_info)
    - Revocation/deletion for everyone (message_revoked)
    - View-once media state
    - Private reply detection (cross-chat replies)
    - Bot/AI message identification
    - Broadcast message flagging
    - Status reply detection
    - Device tracking (message_details.author_device_jid)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader
from app.models.enums import MessageType, get_type_label

logger = logging.getLogger(__name__)

# Batch size for processing messages
BATCH_SIZE = 50_000


def ingest_messages(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest all messages from msgstore.db into analysis.db.

    Uses a multi-table LEFT JOIN query to gather all message metadata in
    a single scan, then processes in batches of 50K for memory efficiency.

    Args:
        db_manager: Central database connection manager.
        analysis_conn: Write connection to analysis.db.
        progress_callback: Optional (processed, total) callback for progress tracking.

    Returns:
        Number of messages ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    # Get total message count for progress tracking
    total = reader.get_row_count("message")
    logger.info("Starting message ingestion: %d total messages", total)

    # Build the chat_id -> conversation_id lookup
    chat_map = _build_chat_map(analysis_conn)

    # Pre-load jid_row_id -> contact_id for fast sender resolution (avoids N+1)
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }
    logger.info("Pre-loaded %d JID-to-contact mappings", len(jid_to_contact))

    # Build chat_jid_row_id -> contact_id for personal chat sender resolution
    # When sender_jid_row_id=0 and from_me=0, the sender IS the chat partner
    chat_jid_rows = reader.execute_raw("SELECT _id, jid_row_id FROM chat")
    chat_to_jid: dict[int, int] = {r[0]: r[1] for r in chat_jid_rows}

    # Resolve device owner contact_id.  In groups, owner-content messages
    # land with from_me=1 + sender_jid_row_id=0 (WhatsApp's implicit
    # owner identity).  Without this lookup the owner gets NULL sender_id
    # and shows zero text/image/voice counts in participant exports even
    # though they sent dozens of messages.
    owner_cid: Optional[int] = None
    try:
        row = analysis_conn.fetchone(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'"
        )
        if row and row[0]:
            owner_phone = row[0]
            ow = analysis_conn.fetchone(
                "SELECT id FROM contact WHERE phone_number = ? OR phone_jid = ?",
                (owner_phone, f"{owner_phone}@s.whatsapp.net"),
            )
            if ow:
                owner_cid = ow[0]
                logger.info("Device owner resolved to contact_id=%d for sender attribution", owner_cid)
    except Exception as e:
        logger.warning("Could not resolve owner contact_id: %s", e)

    # Build the multi-table JOIN query
    query = _build_message_query(reader)

    insert_sql = """
        INSERT INTO message (
            source_msg_id, conversation_id, sender_id, from_me,
            timestamp, received_timestamp, receipt_server_timestamp, sort_id,
            message_type, type_label, text_content, status,
            is_starred, is_forwarded, forward_score,
            is_ephemeral, ephemeral_duration,
            is_revoked, revoke_timestamp, revoked_key_id,
            is_edited, edit_count, original_key_id, last_edit_timestamp,
            reply_to_key_id, quoted_text, quoted_type,
            is_view_once, view_once_state,
            is_bot_message, bot_model_type,
            is_private_reply, private_reply_source_chat_id,
            is_status_reply,
            broadcast, recipient_count,
            origin, origination_flags,
            source_key_id,
            sender_jid_row_id, source_chat_row_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    device_insert_sql = """
        INSERT OR IGNORE INTO message_device (
            message_id, device_jid_row_id, device_agent, device_number,
            is_primary, platform_label, platform_confidence
        ) VALUES (?,?,?,?,?,?,?)
    """

    processed = 0
    batch_num = 0

    # Get ID range
    min_max = reader.execute_raw(
        "SELECT MIN(_id), MAX(_id) FROM message"
    )
    if not min_max or not min_max[0][0]:
        logger.warning("No messages found in msgstore.db")
        return 0

    min_id, max_id = min_max[0]
    current_id = min_id

    while current_id <= max_id:
        batch_end = current_id + BATCH_SIZE
        batch_num += 1

        rows = reader.execute_raw(query, (current_id, batch_end))

        if not rows:
            current_id = batch_end
            continue

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for row in rows:
                msg = dict(zip(_QUERY_COLUMNS, row))

                # Resolve conversation
                conv_id = chat_map.get(msg["chat_row_id"])
                if conv_id is None:
                    continue

                # Resolve sender contact via pre-loaded mapping
                sender_id = None
                from_me = msg.get("from_me", 0)
                sender_jid_row = msg.get("sender_jid_row_id")

                if sender_jid_row and sender_jid_row != 0:
                    sender_id = jid_to_contact.get(sender_jid_row)
                elif from_me:
                    # from_me=1 with sender_jid_row=0/NULL is the device
                    # owner's own content (groups: WhatsApp's implicit
                    # owner identity; 1:1: same).  Attribute to owner so
                    # downstream sender_id queries don't lose it.
                    sender_id = owner_cid
                elif not sender_jid_row or sender_jid_row == 0:
                    # Incoming 1:1 with no sender_jid: sender IS the chat partner
                    chat_jid_row = chat_to_jid.get(msg["chat_row_id"])
                    if chat_jid_row:
                        sender_id = jid_to_contact.get(chat_jid_row)

                # Message type and label
                msg_type = msg.get("message_type", 0)
                type_label = get_type_label(msg_type)

                # Text content
                text = msg.get("text_data")

                # Forward info
                forward_score = msg.get("forward_score")
                is_forwarded = forward_score is not None and forward_score > 0

                # Edit tracking
                edit_version = msg.get("edit_version")
                is_edited = edit_version is not None and edit_version > 0
                edit_count = edit_version or 0
                original_key = msg.get("original_key_id")

                # Revocation
                revoked_key = msg.get("revoked_key_id")
                is_revoked = bool(revoked_key) or msg_type == 15

                # View-once
                is_view_once = bool(msg.get("view_once_state"))
                view_once_state = msg.get("view_once_state")

                # Quoted message (reply)
                quoted_key_id = msg.get("quoted_key_id")
                quoted_text = msg.get("quoted_text")
                quoted_type = msg.get("quoted_type")

                # Private reply detection: mq.chat_row_id is the QUOTED message's
                # original chat (the GROUP). If it differs from m.chat_row_id
                # (the DM where the reply lives), this is a cross-chat private reply.
                is_private_reply = False
                private_reply_source = None
                quoted_chat = msg.get("quoted_chat_row_id")
                if quoted_chat is not None and quoted_chat != 0 and quoted_chat != msg["chat_row_id"]:
                    is_private_reply = True
                    private_reply_source = quoted_chat  # The source group chat

                # Status reply detection (quoted_source=1 means reply to status)
                is_status_reply = msg.get("quoted_source") == 1

                # Bot/AI — determined by bot_message_info table (stage 16),
                # NOT by message_type. Type 66 is poll, not AI.
                is_bot = False  # Will be set in _stage_bot_messages
                bot_model = msg.get("bot_model_type")

                # Broadcast
                broadcast = bool(msg.get("broadcast"))

                # Build source_key_id from key_id
                key_id = msg.get("key_id", "")
                source_key_id = key_id or f"msg_{msg['_id']}"

                # Ephemeral
                is_ephemeral = bool(msg.get("ephemeral_duration"))
                ephemeral_duration = msg.get("ephemeral_duration")

                # Sort ID
                sort_id = msg.get("sort_id")

                # Status code
                status = msg.get("status")

                # Starred
                is_starred = bool(msg.get("starred"))

                # Timestamp
                timestamp = msg.get("timestamp", 0)

                # Edit timestamp (from message_edit_info)
                last_edit_ts = msg.get("edited_timestamp")

                # Revoke timestamp
                revoke_ts = msg.get("revoked_timestamp")

                # Additional timestamps
                received_ts = msg.get("received_timestamp")
                receipt_server_ts = msg.get("receipt_server_timestamp")

                cursor.execute(insert_sql, (
                    msg["_id"],         # source_msg_id
                    conv_id,            # conversation_id
                    sender_id,          # sender_id
                    from_me,            # from_me
                    timestamp,          # timestamp
                    received_ts,        # received_timestamp
                    receipt_server_ts,  # receipt_server_timestamp
                    sort_id,            # sort_id
                    msg_type,           # message_type
                    type_label,         # type_label
                    text,               # text_content
                    status,             # status
                    is_starred,         # is_starred
                    is_forwarded,       # is_forwarded
                    forward_score,      # forward_score
                    is_ephemeral,       # is_ephemeral
                    ephemeral_duration, # ephemeral_duration
                    is_revoked,         # is_revoked
                    revoke_ts,          # revoke_timestamp
                    revoked_key,        # revoked_key_id
                    is_edited,          # is_edited
                    edit_count,         # edit_count
                    original_key,       # original_key_id
                    last_edit_ts,       # last_edit_timestamp
                    quoted_key_id,      # reply_to_key_id
                    quoted_text,        # quoted_text
                    quoted_type,        # quoted_type
                    is_view_once,       # is_view_once
                    view_once_state,    # view_once_state
                    is_bot,             # is_bot_message
                    bot_model,          # bot_model_type
                    is_private_reply,   # is_private_reply
                    private_reply_source, # private_reply_source_chat_id
                    is_status_reply,    # is_status_reply
                    broadcast,          # broadcast
                    msg.get("recipient_count"), # recipient_count
                    msg.get("origin", 0) or 0,  # origin (0=primary phone for outgoing)
                    msg.get("origination_flags", 0) or 0,  # origination_flags
                    source_key_id,      # source_key_id
                    msg.get("sender_jid_row_id"),  # sender_jid_row_id (raw FK from msgstore)
                    msg.get("chat_row_id"),         # source_chat_row_id (raw FK from msgstore)
                ))

                msg_id = analysis_conn.raw_connection.last_insert_rowid()

                # Insert device tracking if available
                device_jid_row = msg.get("author_device_jid")
                if device_jid_row:
                    device_number = msg.get("device_number", 0) or 0
                    device_agent = msg.get("device_agent", 0) or 0
                    is_primary = 1 if device_number == 0 else 0
                    # Classify platform from key_id + device_number
                    from app.ingestion.keyid_classifier import classify_keyid
                    _plat, _conf = classify_keyid(
                        msg.get("key_id", ""),
                        from_me=bool(msg.get("from_me")),
                        device_number=device_number,
                    )
                    platform_label = _plat
                    platform_confidence = _conf
                    cursor.execute(device_insert_sql, (
                        msg_id,
                        device_jid_row,
                        device_agent,
                        device_number,
                        is_primary,
                        platform_label,
                        platform_confidence,
                    ))

                processed += 1

            analysis_conn.commit()

        except Exception:
            analysis_conn.rollback()
            raise

        if progress_callback:
            progress_callback(processed, total)

        logger.info(
            "Message batch %d: processed %d/%d (%.1f%%)",
            batch_num, processed, total, (processed / total * 100) if total else 0,
        )

        current_id = batch_end

    # Post-processing: resolve reply chains (quoted_key_id → reply_to_msg_id)
    _resolve_reply_chains(analysis_conn)

    # Post-processing: detect private replies and write private_reply table
    _write_private_replies(analysis_conn)

    # Post-processing: resolve revoke admins (admin_jid_row_id → revoked_by_admin_id)
    _resolve_revoke_admins(db_manager, analysis_conn)

    # Post-processing: enrich template/button messages with footer + button labels
    _enrich_template_messages(reader, analysis_conn)

    # Update conversation aggregate counts
    _update_conversation_counts(analysis_conn)

    logger.info("Message ingestion complete: %d messages", processed)
    return processed


# Query column names (must match the SELECT in _build_message_query)
_QUERY_COLUMNS = [
    "_id", "chat_row_id", "from_me", "key_id",
    "sender_jid_row_id", "timestamp", "message_type",
    "text_data", "status", "sort_id",
    "starred", "broadcast", "recipient_count",
    # From message_forwarded
    "forward_score",
    # From message_quoted
    "quoted_key_id", "quoted_text", "quoted_type",
    "parent_message_chat_row_id", "quoted_chat_row_id", "quoted_source",
    # From message_revoked
    "revoked_key_id", "revoked_timestamp",
    # From message_view_once_media
    "view_once_state",
    # From message_ephemeral
    "ephemeral_duration",
    # From message_edit_info
    "edit_version", "original_key_id", "edited_timestamp",
    # From message_details + jid resolution
    "author_device_jid", "device_agent", "device_number",
    # From message_add_on (bot)
    "bot_model_type",
    # Additional timestamps
    "received_timestamp",
    "receipt_server_timestamp",
    # Multi-device origin
    "origin",
    "origination_flags",
]


def _build_message_query(reader: SourceReader) -> str:
    """Build the multi-table JOIN query for message extraction.

    Joins message with satellite tables to gather all metadata in one pass.
    Uses LEFT JOINs since satellite data is sparse.
    """
    # Check which satellite tables exist
    has_forwarded = reader.table_exists("message_forwarded")
    has_quoted = reader.table_exists("message_quoted")
    has_revoked = reader.table_exists("message_revoked")
    has_view_once = reader.table_exists("message_view_once_media")
    has_ephemeral = reader.table_exists("message_ephemeral")
    has_edit = reader.table_exists("message_edit_info")
    has_details = reader.table_exists("message_details")
    has_template = reader.table_exists("message_template")

    # Check columns in message table
    msg_cols = reader.get_column_names("message")
    has_sort_id = "sort_id" in msg_cols
    has_starred = "starred" in msg_cols
    has_broadcast = "broadcast" in msg_cols
    has_recipient = "recipient_count" in msg_cols
    has_received_ts = "received_timestamp" in msg_cols
    has_receipt_server_ts = "receipt_server_timestamp" in msg_cols
    has_origin = "origin" in msg_cols
    has_origination_flags = "origination_flags" in msg_cols

    # Check message_edit_info columns
    edit_cols = reader.get_column_names("message_edit_info") if has_edit else []
    has_edit_version = "edit_version" in edit_cols

    # Check message_quoted columns (quoted_source added in newer WhatsApp)
    quoted_cols = reader.get_column_names("message_quoted") if has_quoted else []
    has_quoted_source = "quoted_source" in quoted_cols

    # Resolve device info from jid table via message_details.author_device_jid
    # message_details only has author_device_jid (FK to jid._id), so we JOIN
    # with jid to get the actual agent and device number.
    has_jid = reader.table_exists("jid")

    query = f"""
        SELECT
            m._id,
            m.chat_row_id,
            m.from_me,
            m.key_id,
            m.sender_jid_row_id,
            m.timestamp,
            m.message_type,
            {"COALESCE(NULLIF(m.text_data, ''), mt.content_text_data)" if has_template else "m.text_data"} AS text_data,
            m.status,
            {"m.sort_id" if has_sort_id else "NULL"} AS sort_id,
            {"m.starred" if has_starred else "0"} AS starred,
            {"m.broadcast" if has_broadcast else "0"} AS broadcast,
            {"m.recipient_count" if has_recipient else "NULL"} AS recipient_count,
            {"mf.forward_score" if has_forwarded else "NULL"} AS forward_score,
            {"mq.key_id" if has_quoted else "NULL"} AS quoted_key_id,
            {"mq.text_data" if has_quoted else "NULL"} AS quoted_text,
            {"mq.message_type" if has_quoted else "NULL"} AS quoted_type,
            {"mq.parent_message_chat_row_id" if has_quoted else "NULL"} AS parent_message_chat_row_id,
            {"mq.chat_row_id" if has_quoted else "NULL"} AS quoted_chat_row_id,
            {"mq.quoted_source" if has_quoted and has_quoted_source else "NULL"} AS quoted_source,
            {"mr.revoked_key_id" if has_revoked else "NULL"} AS revoked_key_id,
            {"mr.revoke_timestamp" if has_revoked else "NULL"} AS revoked_timestamp,
            {"mvo.state" if has_view_once else "NULL"} AS view_once_state,
            {"me.duration" if has_ephemeral else "NULL"} AS ephemeral_duration,
            {"mei.edit_version" if has_edit and has_edit_version else "CASE WHEN mei.message_row_id IS NOT NULL THEN 1 ELSE NULL END" if has_edit else "NULL"} AS edit_version,
            {"mei.original_key_id" if has_edit else "NULL"} AS original_key_id,
            {"mei.edited_timestamp" if has_edit else "NULL"} AS edited_timestamp,
            {"md.author_device_jid" if has_details else "NULL"} AS author_device_jid,
            {"dj.agent" if has_details and has_jid else "NULL"} AS device_agent,
            {"dj.device" if has_details and has_jid else "NULL"} AS device_number,
            NULL AS bot_model_type,
            {"m.received_timestamp" if has_received_ts else "NULL"} AS received_timestamp,
            {"m.receipt_server_timestamp" if has_receipt_server_ts else "NULL"} AS receipt_server_timestamp,
            {"m.origin" if has_origin else "NULL"} AS origin,
            {"m.origination_flags" if has_origination_flags else "NULL"} AS origination_flags
        FROM message m
        {"LEFT JOIN message_forwarded mf ON mf.message_row_id = m._id" if has_forwarded else ""}
        {"LEFT JOIN message_quoted mq ON mq.message_row_id = m._id" if has_quoted else ""}
        {"LEFT JOIN message_revoked mr ON mr.message_row_id = m._id" if has_revoked else ""}
        {"LEFT JOIN message_view_once_media mvo ON mvo.message_row_id = m._id" if has_view_once else ""}
        {"LEFT JOIN message_ephemeral me ON me.message_row_id = m._id" if has_ephemeral else ""}
        {"LEFT JOIN message_edit_info mei ON mei.message_row_id = m._id" if has_edit else ""}
        {"LEFT JOIN message_details md ON md.message_row_id = m._id" if has_details else ""}
        {"LEFT JOIN jid dj ON dj._id = md.author_device_jid" if has_details and has_jid else ""}
        {"LEFT JOIN message_template mt ON mt.message_row_id = m._id" if has_template else ""}
        WHERE m._id >= ? AND m._id < ?
        ORDER BY m._id
    """

    return query


def _build_chat_map(analysis_conn: AnalysisConnection) -> dict[int, int]:
    """Build mapping from source chat._id to analysis conversation.id."""
    rows = analysis_conn.fetchall(
        "SELECT source_chat_id, id FROM conversation"
    )
    return {row[0]: row[1] for row in rows}


def _resolve_reply_chains(analysis_conn: AnalysisConnection) -> None:
    """Resolve reply_to_key_id references to reply_to_msg_id.

    After all messages are ingested, we can cross-reference the
    ``reply_to_key_id`` (which contains the ``key_id`` of the quoted
    message) to find the actual ``message.id`` in our analysis.db.
    """
    logger.info("Resolving reply chains...")

    # Update messages that have reply_to_key_id but no reply_to_msg_id
    analysis_conn.execute("""
        UPDATE message SET reply_to_msg_id = (
            SELECT m2.id FROM message m2
            WHERE m2.source_key_id = message.reply_to_key_id
            LIMIT 1
        )
        WHERE reply_to_key_id IS NOT NULL
        AND reply_to_msg_id IS NULL
    """)

    resolved = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM message WHERE reply_to_msg_id IS NOT NULL"
    )
    count = resolved[0] if resolved else 0
    logger.info("Resolved %d reply chain references", count)


def _write_private_replies(analysis_conn: AnalysisConnection) -> None:
    """Write private_reply table entries for cross-chat replies.

    Private replies are messages where is_private_reply=1, meaning someone
    replied privately to a group message. We create entries linking the
    DM back to the original group conversation.
    """
    logger.info("Writing private reply records...")

    analysis_conn.execute("""
        INSERT OR IGNORE INTO private_reply (
            message_id, source_conversation_id,
            source_message_key_id, quoted_text
        )
        SELECT
            m.id,
            c.id,
            m.reply_to_key_id,
            m.quoted_text
        FROM message m
        JOIN conversation c ON c.source_chat_id = m.private_reply_source_chat_id
        WHERE m.is_private_reply = 1
        AND m.private_reply_source_chat_id IS NOT NULL
    """)

    count_row = analysis_conn.fetchone(
        "SELECT COUNT(*) FROM private_reply"
    )
    count = count_row[0] if count_row else 0
    logger.info("Wrote %d private reply records", count)


def _resolve_revoke_admins(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
) -> None:
    """Resolve admin_jid_row_id from message_revoked to contact.id.

    WhatsApp stores who performed the delete-for-everyone action in
    ``message_revoked.admin_jid_row_id`` (typically a LID JID).
    This resolves that JID row to a contact and updates
    ``message.revoked_by_admin_id``.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_revoked"):
        return

    mr_cols = reader.get_column_names("message_revoked")
    if "admin_jid_row_id" not in mr_cols:
        logger.info("message_revoked has no admin_jid_row_id column")
        return

    # Read all admin_jid_row_id entries
    admin_rows = reader.execute_raw(
        "SELECT message_row_id, admin_jid_row_id "
        "FROM message_revoked "
        "WHERE admin_jid_row_id IS NOT NULL AND admin_jid_row_id != 0"
    )

    if not admin_rows:
        logger.info("No admin revoke records found")
        return

    # Build jid_row_id -> contact_id lookup
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {r[0]: r[1] for r in jid_contact_rows if r[0] is not None}

    # Build source_msg_id -> analysis message.id lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    updated = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()
        for source_msg_id, admin_jid_row in admin_rows:
            msg_id = msg_map.get(source_msg_id)
            if msg_id is None:
                continue
            admin_contact_id = jid_to_contact.get(admin_jid_row)
            if admin_contact_id is None:
                continue
            cursor.execute(
                "UPDATE message SET revoked_by_admin_id = ? WHERE id = ?",
                (admin_contact_id, msg_id),
            )
            updated += 1
        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Resolved %d revoke admin references from %d total", updated, len(admin_rows))


def _update_conversation_counts(analysis_conn: AnalysisConnection) -> None:
    """Update pre-computed aggregate columns on the conversation table.

    Fills message_count, media_count, first_message_ts, and last_message_ts
    based on the ingested message data.
    """
    logger.info("Updating conversation aggregate counts...")

    analysis_conn.execute("""
        UPDATE conversation SET
            message_count = (
                SELECT COUNT(*) FROM message WHERE conversation_id = conversation.id
            ),
            media_count = (
                SELECT COUNT(*) FROM message
                WHERE conversation_id = conversation.id
                AND message_type IN (1,2,3,9,11,13,20)
            ),
            first_message_ts = (
                SELECT MIN(timestamp) FROM message WHERE conversation_id = conversation.id
            ),
            last_message_ts = (
                SELECT MAX(timestamp) FROM message WHERE conversation_id = conversation.id
            )
    """)

    # Update participant count for groups
    analysis_conn.execute("""
        UPDATE conversation SET
            participant_count = (
                SELECT COUNT(*) FROM group_member
                WHERE conversation_id = conversation.id AND is_current = 1
            )
        WHERE chat_type IN ('group', 'community')
    """)

    # Pre-compute last message info using CTEs (much faster than N correlated subqueries)
    logger.info("Pre-computing last message data per conversation...")

    # Step 1: Find last message per conversation using window function
    analysis_conn.execute("""
        WITH last_msg AS (
            SELECT conversation_id, id, text_content, type_label,
                   sender_id, status, from_me,
                   ROW_NUMBER() OVER (
                       PARTITION BY conversation_id
                       ORDER BY timestamp DESC, sort_id DESC
                   ) AS rn
            FROM message
        ),
        last_info AS (
            SELECT lm.conversation_id,
                   COALESCE(lm.text_content, lm.type_label, '') AS msg_text,
                   COALESCE(
                       NULLIF(c.resolved_name, ''),
                       NULLIF(c.display_name, ''),
                       CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''
                            THEN '+' || c.phone_number ELSE '' END,
                       NULLIF(c.wa_name, ''),
                       ''
                   ) AS msg_sender,
                   lm.status AS msg_status,
                   lm.from_me AS msg_from_me
            FROM last_msg lm
            LEFT JOIN contact c ON c.id = lm.sender_id
            WHERE lm.rn = 1
        )
        UPDATE conversation SET
            last_msg_text    = li.msg_text,
            last_msg_sender  = li.msg_sender,
            last_msg_status  = li.msg_status,
            last_msg_from_me = li.msg_from_me
        FROM last_info li
        WHERE conversation.id = li.conversation_id
    """)

    # Step 2: Ghost and unread counts (separate for clarity, still faster than per-row)
    analysis_conn.execute("""
        WITH ghost_counts AS (
            SELECT conversation_id, COUNT(*) AS cnt
            FROM ghost_message GROUP BY conversation_id
        )
        UPDATE conversation SET ghost_count = gc.cnt
        FROM ghost_counts gc
        WHERE conversation.id = gc.conversation_id
    """)

    analysis_conn.execute("""
        WITH unread_counts AS (
            SELECT conversation_id, COUNT(*) AS cnt
            FROM message
            WHERE from_me = 0 AND status < 6
            GROUP BY conversation_id
        )
        UPDATE conversation SET unread_count = uc.cnt
        FROM unread_counts uc
        WHERE conversation.id = uc.conversation_id
    """)

    logger.info("Conversation aggregate counts updated (including last_msg precompute)")


def _enrich_template_messages(reader: SourceReader, analysis_conn: AnalysisConnection):
    """Enrich template/button messages with footer text and button labels.

    WhatsApp stores template messages in a separate `message_template` table
    with content_text_data, footer_text_data, and buttons in `message_template_button`.
    The main text_data is populated via COALESCE in the query, but footer and buttons
    need a separate pass to append to text_content.
    """
    has_template = reader.table_exists("message_template")
    has_buttons = reader.table_exists("message_template_button")
    if not has_template:
        return

    # Get footer text for all template messages
    footers = {}
    try:
        rows = reader.execute_raw(
            "SELECT message_row_id, footer_text_data FROM message_template "
            "WHERE footer_text_data IS NOT NULL AND footer_text_data != ''"
        )
        for msg_row_id, footer in rows:
            footers[msg_row_id] = footer
    except Exception:
        pass

    # Get button data grouped by message: (text, url, type)
    # button_type: 1=quick_reply, 2=url, 3=phone_call
    buttons: dict[int, list[tuple[str, str, int]]] = {}
    if has_buttons:
        try:
            btn_rows = reader.execute_raw(
                "SELECT message_row_id, text_data, extra_data, button_type "
                "FROM message_template_button "
                "WHERE text_data IS NOT NULL AND text_data != '' "
                "ORDER BY message_row_id, _id"
            )
            for msg_row_id, btn_text, extra_data, btn_type in btn_rows:
                buttons.setdefault(msg_row_id, []).append(
                    (btn_text, extra_data or "", btn_type or 0))
        except Exception:
            pass

    if not footers and not buttons:
        return

    # Get source_msg_id → analysis message id mapping for all interactive message types
    template_msgs = analysis_conn.fetchall(
        "SELECT id, source_msg_id, text_content FROM message "
        "WHERE type_label IN ('button_message', 'list_message', 'carousel', "
        "'list_reply', 'product_catalog')"
    )

    count = 0
    analysis_conn.begin_transaction()
    try:
        for msg_id, source_id, text_content in template_msgs:
            parts = []
            if text_content:
                parts.append(text_content)

            footer = footers.get(source_id)
            if footer:
                parts.append(f"\n---\n{footer}")

            btns = buttons.get(source_id)
            if btns:
                btn_lines = []
                for btn_text, extra, btn_type in btns:
                    if btn_type == 2 and extra.startswith("http"):
                        # URL button — show text + link
                        btn_lines.append(f"[\U0001F517 {btn_text}] {extra}")
                    elif btn_type == 3 and extra:
                        # Phone call button
                        btn_lines.append(f"[\U0001F4DE {btn_text}] {extra}")
                    else:
                        # Quick reply or unknown
                        btn_lines.append(f"[\u25B6 {btn_text}]")
                parts.append("\n" + "\n".join(btn_lines))

            if len(parts) > 1 or (not text_content and parts):
                new_text = "\n".join(parts) if parts else None
                if new_text and new_text != text_content:
                    analysis_conn.execute(
                        "UPDATE message SET text_content = ? WHERE id = ?",
                        (new_text, msg_id),
                    )
                    count += 1

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    if count:
        logger.info("Enriched %d template/button messages with footer + button labels", count)
