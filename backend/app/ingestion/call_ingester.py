"""
Call record ingestion from msgstore.db.

Processes ``call_log`` and ``call_log_participant_v2`` to
populate the analysis ``call_record`` and ``call_participant``
tables.  Covers voice / video calls, group calls, and voice
chats.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader
from app.models.enums import get_call_result_label

logger = logging.getLogger(__name__)


def ingest_calls(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest call records from msgstore.db into analysis.db.

    Returns:
        Number of call records ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("call_log"):
        logger.warning("call_log table not found")
        return 0

    total = reader.get_row_count("call_log")
    logger.info("Starting call ingestion: %d total records", total)

    cl_cols = reader.get_column_names("call_log")

    # Read group_jid_row_id and call_type alongside is_joinable_group_call
    # to properly detect group calls (some group calls have is_joinable=0
    # but group_jid_row_id != 0, call_type IN (2,3), or many participants)
    has_call_type = "call_type" in cl_cols
    has_group_jid = "group_jid_row_id" in cl_cols
    has_offer_silence = "offer_silence_reason" in cl_cols
    has_creator_device = "call_creator_device_jid_row_id" in cl_cols

    extra_cols = ""
    if has_group_jid:
        extra_cols += ", group_jid_row_id"
    if has_call_type:
        extra_cols += ", call_type"
    if has_offer_silence:
        extra_cols += ", offer_silence_reason"
    if has_creator_device:
        extra_cols += ", call_creator_device_jid_row_id"

    call_rows = reader.execute_raw(
        "SELECT _id, jid_row_id, from_me, timestamp, video_call, "
        "duration, call_result, bytes_transferred, call_id, "
        f"is_joinable_group_call{extra_cols} "
        "FROM call_log ORDER BY _id"
    )

    # Pre-count participants per call_log row for group detection
    _participant_counts: dict[int, int] = {}
    _missed_counts: dict[int, int] = {}  # participants with call_result=2 (missed/declined)
    if reader.table_exists("call_log_participant_v2"):
        for r in reader.execute_raw(
            "SELECT call_log_row_id, COUNT(*) FROM call_log_participant_v2 GROUP BY call_log_row_id"
        ):
            _participant_counts[r[0]] = r[1]
        for r in reader.execute_raw(
            "SELECT call_log_row_id, COUNT(*) FROM call_log_participant_v2 "
            "WHERE call_result = 2 GROUP BY call_log_row_id"
        ):
            _missed_counts[r[0]] = r[1]

    # Pre-load unknown callers
    _unknown_callers: set[int] = set()
    if reader.table_exists("call_unknown_caller"):
        for r in reader.execute_raw("SELECT call_log_row_id FROM call_unknown_caller"):
            _unknown_callers.add(r[0])
        logger.info("Loaded %d unknown caller records", len(_unknown_callers))

    # Pre-load message_call_log -> message -> chat_row_id for group resolution
    # This is the most reliable way: call_log -> message_call_log -> message -> chat_row_id -> conversation
    _call_to_chat: dict[int, int] = {}  # call_log._id -> msgstore chat._id
    if reader.table_exists("message_call_log"):
        for r in reader.execute_raw(
            "SELECT mcl.call_log_row_id, m.chat_row_id "
            "FROM message_call_log mcl "
            "JOIN message m ON m._id = mcl.message_row_id"
        ):
            _call_to_chat[r[0]] = r[1]
        logger.info("Loaded %d call-to-chat mappings from message_call_log", len(_call_to_chat))

    # Pre-load conversation mapping: source_chat_id -> analysis conversation.id
    _chat_to_conv: dict[int, int] = {}
    for r in analysis_conn.fetchall("SELECT source_chat_id, id, chat_type FROM conversation"):
        _chat_to_conv[r[0]] = r[1]

    insert_sql = """
        INSERT OR IGNORE INTO call_record (
            source_call_id, contact_id, conversation_id, from_me, timestamp,
            is_video, duration_sec, call_result, result_label,
            bytes_transferred, call_id_text, is_group_call,
            group_conversation_id, call_type, offer_silence_reason, call_category,
            creator_jid, creator_contact_id, creator_device_type,
            is_unknown_caller
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """

    processed = 0
    call_id_map: dict[int, int] = {}  # source _id → analysis call_record.id

    # Pre-load jid_to_contact for batch lookups (faster than per-row SQL)
    _jid_to_contact: dict[int, int] = {}
    for r in analysis_conn.fetchall("SELECT jid_row_id, contact_id FROM jid_to_contact"):
        if r[0] is not None:
            _jid_to_contact[r[0]] = r[1]

    # Pre-load JID strings + phone_jid→contact for fallback resolution
    _jid_strings: dict[int, str] = {}
    try:
        for r in reader.execute_raw("SELECT _id, raw_string FROM jid WHERE raw_string IS NOT NULL"):
            _jid_strings[r[0]] = r[1]
    except Exception:
        pass
    _phone_to_contact: dict[str, int] = {}
    for r in analysis_conn.fetchall("SELECT phone_jid, id FROM contact WHERE phone_jid IS NOT NULL"):
        _phone_to_contact[r[0]] = r[1]

    def _resolve_contact(jid_row_id: int | None) -> int | None:
        if not jid_row_id:
            return None
        cid = _jid_to_contact.get(jid_row_id)
        if cid is None:
            jid_str = _jid_strings.get(jid_row_id)
            if jid_str:
                cid = _phone_to_contact.get(jid_str)
        return cid

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for row in call_rows:
            # Base 10 columns always present
            (call_id, jid_row, from_me, timestamp, video_call,
             duration, call_result, bytes_xfer, call_id_text,
             is_joinable) = row[:10]

            # Extra columns (variable position)
            idx = 10
            group_jid_row = 0
            call_type_val = None
            offer_silence_val = None
            if has_group_jid:
                group_jid_row = row[idx] or 0
                idx += 1
            if has_call_type:
                call_type_val = row[idx]
                idx += 1
            if has_offer_silence:
                offer_silence_val = row[idx]
                idx += 1
            creator_device_jid_row = 0
            if has_creator_device:
                creator_device_jid_row = row[idx] or 0
                idx += 1

            # Determine call category using validated detection hierarchy:
            # Voice Chat = call_type IN (2,3) AND no missed participants
            # (if there ARE missed participants, it's a group call silenced by DND,
            # not a voice chat — voice chats only record joiners, never missed)
            # Group Call = group_jid_row_id > 0
            # Multi-person = participants > 1 (from personal chat, added people)
            # Personal = everything else
            participant_cnt = _participant_counts.get(call_id, 0)
            missed_cnt = _missed_counts.get(call_id, 0)

            if call_type_val in (2, 3) and missed_cnt == 0:
                call_category = "voice_chat"
                is_group_call = True
            elif group_jid_row:
                call_category = "group_call"
                is_group_call = True
            elif participant_cnt > 1:
                call_category = "multi_person"
                is_group_call = True
            else:
                call_category = "personal"
                is_group_call = bool(is_joinable)

            # Resolve contact
            contact_id = _resolve_contact(jid_row)

            # Resolve conversation and group for this call
            # Priority 1: message_call_log -> message -> chat_row_id (most reliable)
            conv_id = None
            group_conv_id = None
            chat_id = _call_to_chat.get(call_id)
            if chat_id:
                conv_id = _chat_to_conv.get(chat_id)

            # Priority 2: group_jid_row_id -> conversation by JID
            if not conv_id and group_jid_row:
                jid_str = _jid_strings.get(group_jid_row, "")
                if jid_str:
                    conv_row = analysis_conn.fetchone(
                        "SELECT id FROM conversation WHERE jid_raw_string = ?",
                        (jid_str,),
                    )
                    if conv_row:
                        conv_id = conv_row[0]

            # For group calls, set group_conversation_id
            if is_group_call and conv_id:
                group_conv_id = conv_id

            result_label = get_call_result_label(call_result) if call_result is not None else None

            # Resolve call creator device info
            # JID format: "number.device_num:agent@domain"
            # device_num in jid table = actual companion device number (0=primary, >0=companion)
            # agent: 0=phone-number protocol, 1=LID protocol
            # Examples:
            # 15551234567.0:0@s.whatsapp.net → device=0, agent=0 → Primary phone (phone#)
            # 142945453908144.1:0@lid → device=0, agent=1 → Primary phone (LID)
            # 15551234567.0:1@s.whatsapp.net → device=1, agent=0 → Companion device
            creator_jid = None
            creator_contact_id = None
            creator_device_type = None
            if creator_device_jid_row:
                creator_jid = _jid_strings.get(creator_device_jid_row, "")
                if creator_jid:
                    # Get actual device number from jid table
                    jid_info = reader.execute_raw(
                        "SELECT device FROM jid WHERE _id = ?",
                        (creator_device_jid_row,),
                    )
                    device_num = 0
                    for r in jid_info:
                        device_num = r[0] or 0
                    creator_device_type = "primary_phone" if device_num == 0 else f"companion_{device_num}"

                    # Resolve to contact: strip device suffix to get base JID
                    # "15551234567.0:0@s.whatsapp.net" → "15551234567@s.whatsapp.net"
                    # "142945453908144.1:0@lid" → "142945453908144@lid"
                    base_part = creator_jid.split(".")[0] if "." in creator_jid else creator_jid.split("@")[0]
                    if "@s.whatsapp.net" in creator_jid:
                        base_jid = base_part + "@s.whatsapp.net"
                        creator_contact_id = _phone_to_contact.get(base_jid)
                    elif "@lid" in creator_jid:
                        base_jid = base_part + "@lid"
                        lid_row = analysis_conn.fetchone(
                            "SELECT id FROM contact WHERE lid_jid = ?", (base_jid,)
                        )
                        if lid_row:
                            creator_contact_id = lid_row[0]

            cursor.execute(insert_sql, (
                call_id, contact_id, conv_id, from_me, timestamp,
                1 if video_call else 0,
                duration, call_result, result_label,
                bytes_xfer, call_id_text,
                1 if is_group_call else 0,
                group_conv_id,
                call_type_val, offer_silence_val, call_category,
                creator_jid, creator_contact_id, creator_device_type,
                1 if call_id in _unknown_callers else 0,
            ))

            analysis_call_id = analysis_conn.raw_connection.last_insert_rowid()
            call_id_map[call_id] = analysis_call_id
            processed += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    # Ingest call participants
    participant_count = _ingest_call_participants(reader, analysis_conn, call_id_map, _resolve_contact)

    # Create synthetic messages for voice chats without message_call_log entries
    # These won't appear in group chat timeline otherwise
    synth_count = _create_voice_chat_messages(analysis_conn)

    # Create synthetic call messages in each participant's personal conversation
    # so group/multi-person calls appear in every relevant chat timeline
    participant_msg_count = _create_participant_call_messages(analysis_conn)

    logger.info("Call ingestion complete: %d calls, %d participants, "
                "%d synthetic voice chat msgs, %d synthetic participant msgs",
                processed, participant_count, synth_count, participant_msg_count)

    if progress_callback:
        progress_callback(processed, total)

    return processed


def _ingest_call_participants(
    reader: SourceReader,
    analysis_conn: AnalysisConnection,
    call_id_map: dict[int, int],
    resolve_contact,
) -> int:
    """Ingest call participants from call_log_participant_v2."""
    if not reader.table_exists("call_log_participant_v2"):
        return 0

    rows = reader.execute_raw(
        "SELECT call_log_row_id, jid_row_id, call_result "
        "FROM call_log_participant_v2"
    )

    insert_sql = """
        INSERT OR IGNORE INTO call_participant (call_id, contact_id, call_result)
        VALUES (?,?,?)
    """

    count = 0
    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for call_log_row, jid_row, call_result in rows:
            call_id = call_id_map.get(call_log_row)
            if call_id is None:
                continue

            contact_id = resolve_contact(jid_row)
            cursor.execute(insert_sql, (call_id, contact_id, call_result))
            count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    return count


def _create_voice_chat_messages(analysis_conn: AnalysisConnection) -> int:
    """Create synthetic message records for voice chats that have no message_call_log entry.

    Voice chats (call_category='voice_chat') almost never create a message in the
    group chat. To show them in the conversation timeline, we create synthetic
    message records with type_label='call_log' and message_type=90.

    Returns:
        Number of synthetic messages created.
    """
    # Find voice chats with group_conversation_id but no linked message
    orphan_calls = analysis_conn.fetchall("""
        SELECT cr.id, cr.group_conversation_id, cr.contact_id, cr.timestamp,
               cr.duration_sec, cr.is_video, cr.result_label, cr.call_id_text,
               cr.call_category
        FROM call_record cr
        WHERE cr.call_category = 'voice_chat'
          AND cr.group_conversation_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM message m
              WHERE m.source_key_id = REPLACE(cr.call_id_text, 'call:', '')
          )
    """)

    if not orphan_calls:
        logger.info("No orphan voice chats — all have linked messages")
        return 0

    logger.info("Found %d orphan voice chats needing synthetic messages", len(orphan_calls))

    # Get the max message ID to generate new unique IDs
    max_id_row = analysis_conn.fetchone("SELECT MAX(id) FROM message")
    next_id = (max_id_row[0] or 0) + 100000

    # source_msg_id has a UNIQUE constraint (see schema.py line 281), so we
    # CANNOT insert many rows with source_msg_id=0 — only the first succeeds,
    # the rest fail silently on UNIQUE violation and the synthesized voice
    # chats disappear. Use NEGATIVE ids starting well below the minimum
    # observed msgstore.db _id (msgstore _id is always positive), so there
    # is no collision with real rows. Track the next synthetic slot.
    min_synth = analysis_conn.fetchone(
        "SELECT MIN(source_msg_id) FROM message WHERE source_msg_id < 0"
    )
    next_synth_src_id = ((min_synth[0] or 0) - 1) if min_synth else -1

    count = 0
    skipped = 0
    failed = 0
    analysis_conn.begin_transaction()
    try:
        for cr in orphan_calls:
            cr_id, conv_id, contact_id, ts, dur, is_video, result, call_id_text, category = cr
            key_id = (call_id_text or "").replace("call:", "")
            if not key_id or not conv_id or not ts:
                skipped += 1
                continue

            # Find the correct sort_id by looking at nearby messages in this conversation
            # The sort_id should place this message in the right chronological position
            neighbor = analysis_conn.fetchone(
                "SELECT sort_id FROM message "
                "WHERE conversation_id = ? AND timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (conv_id, ts),
            )
            if neighbor and neighbor[0]:
                # Place just after the previous message
                sort_id = neighbor[0] + 1
            else:
                # No prior messages - use a sort_id based on timestamp
                # (dividing by 1000 to get a reasonable sort_id from ms timestamp)
                sort_id = ts // 1000

            next_id += 1
            synth_src_id = next_synth_src_id
            next_synth_src_id -= 1

            try:
                analysis_conn.execute("""
                    INSERT INTO message (
                        id, conversation_id, sender_id, from_me, timestamp,
                        message_type, type_label, source_key_id, sort_id,
                        source_msg_id, is_forwarded
                    ) VALUES (?, ?, ?, 0, ?, 90, 'call_log', ?, ?, ?, 0)
                """, (next_id, conv_id, contact_id, ts, key_id, sort_id, synth_src_id))
                count += 1
            except Exception as e:
                failed += 1
                logger.warning("Synthetic voice chat insert FAILED (id=%d, src=%d, key=%s, conv=%d): %s",
                               next_id, synth_src_id, key_id, conv_id, e)

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Synthetic voice-chat messages: %d created, %d skipped, %d failed",
                count, skipped, failed)
    return count


def _create_participant_call_messages(analysis_conn: AnalysisConnection) -> int:
    """Create synthetic call messages in each participant's personal conversation.

    For group, multi-person, and voice-chat calls, the original call message
    only exists in one conversation (the initiator's chat or the group chat).
    This creates corresponding messages in each participant's 1-on-1 conversation
    so the call appears in every relevant chat timeline.

    Returns:
        Number of synthetic messages created.
    """
    # Step 1: Pre-compute contact_id -> personal conversation_id mapping
    contact_conv_rows = analysis_conn.fetchall("""
        SELECT c.id, conv.id
        FROM contact c
        JOIN conversation conv ON (
            conv.jid_raw_string = c.phone_jid
            OR conv.jid_raw_string = c.lid_jid
        )
        WHERE conv.chat_type = 'personal'
    """)
    contact_to_conv: dict[int, int] = {r[0]: r[1] for r in contact_conv_rows}

    if not contact_to_conv:
        return 0

    # Step 2: Get all non-personal calls
    calls = analysis_conn.fetchall("""
        SELECT cr.id, cr.call_id_text, cr.contact_id, cr.timestamp,
               cr.from_me, cr.conversation_id, cr.call_category
        FROM call_record cr
        WHERE cr.call_category IN ('group_call', 'multi_person', 'voice_chat')
    """)

    if not calls:
        return 0

    # Step 3: Get all participants for these calls (batch lookup)
    participants = analysis_conn.fetchall("""
        SELECT cp.call_id, cp.contact_id
        FROM call_participant cp
        JOIN call_record cr ON cr.id = cp.call_id
        WHERE cr.call_category IN ('group_call', 'multi_person', 'voice_chat')
    """)

    # Build call_id -> list of participant contact_ids
    call_participants: dict[int, list[int]] = {}
    for call_id, contact_id in participants:
        call_participants.setdefault(call_id, []).append(contact_id)

    # Step 4: Find existing call messages to avoid duplicates (batch)
    existing_keys = set()
    existing_rows = analysis_conn.fetchall(
        "SELECT source_key_id, conversation_id FROM message WHERE message_type = 90"
    )
    for key, conv_id in existing_rows:
        existing_keys.add((key, conv_id))

    # Get the max message ID for unique synthetic IDs
    max_id_row = analysis_conn.fetchone("SELECT MAX(id) FROM message")
    next_id = (max_id_row[0] or 0) + 200000

    # ``source_msg_id`` is UNIQUE in the schema.  Using
    # ``next_id`` (the analysis.db ``message.id``) as the
    # synthetic source-id would risk collisions with real
    # msgstore ``_id`` values on large message tables.  Use
    # monotonically decreasing negative ids so synthetic rows
    # never clash with real msgstore ids (which are always
    # positive).
    min_synth = analysis_conn.fetchone(
        "SELECT MIN(source_msg_id) FROM message WHERE source_msg_id < 0"
    )
    next_synth_src_id = ((min_synth[0] or 0) - 1) if min_synth else -1

    count = 0
    failed = 0
    analysis_conn.begin_transaction()
    try:
        for cr in calls:
            cr_id, call_id_text, primary_contact_id, ts, from_me, conv_id, category = cr
            base_key = (call_id_text or "").replace("call:", "")
            if not base_key or not ts:
                continue

            # Collect all contacts to create messages for:
            # participants + the primary contact
            target_contacts: set[int] = set()
            for p_contact_id in call_participants.get(cr_id, []):
                target_contacts.add(p_contact_id)
            if primary_contact_id:
                target_contacts.add(primary_contact_id)

            for p_contact_id in target_contacts:
                personal_conv = contact_to_conv.get(p_contact_id)
                if not personal_conv:
                    continue

                # Skip if this conversation already has a message for this call
                # (either the original message or a previously created synthetic one)
                synth_key = f"{base_key}::p{p_contact_id}"
                if (base_key, personal_conv) in existing_keys:
                    continue
                if (synth_key, personal_conv) in existing_keys:
                    continue

                # Compute sort_id from nearest neighbor
                neighbor = analysis_conn.fetchone(
                    "SELECT sort_id FROM message "
                    "WHERE conversation_id = ? AND timestamp <= ? "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (personal_conv, ts),
                )
                if neighbor and neighbor[0]:
                    sort_id = neighbor[0] + 1
                else:
                    sort_id = ts // 1000

                next_id += 1
                synth_src_id = next_synth_src_id
                next_synth_src_id -= 1

                try:
                    analysis_conn.execute("""
                        INSERT INTO message (
                            id, conversation_id, sender_id, from_me, timestamp,
                            message_type, type_label, source_key_id, sort_id,
                            source_msg_id, is_forwarded
                        ) VALUES (?, ?, ?, ?, ?, 90, 'call_log', ?, ?, ?, 0)
                    """, (
                        next_id, personal_conv, primary_contact_id,
                        from_me, ts, synth_key, sort_id, synth_src_id,
                    ))
                    count += 1
                    existing_keys.add((synth_key, personal_conv))
                except Exception as e:
                    failed += 1
                    logger.warning(
                        "Synthetic participant-call insert FAILED "
                        "(id=%d, src=%d, key=%s, conv=%d): %s",
                        next_id, synth_src_id, synth_key, personal_conv, e,
                    )

        analysis_conn.commit()
    except Exception:
        analysis_conn.rollback()
        raise

    if count:
        logger.info("Created %d synthetic call messages in participant personal chats", count)
    return count
