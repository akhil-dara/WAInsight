"""
Poll ingestion from msgstore.db.

Processes ``message_poll``, ``message_poll_option``,
``message_add_on_poll_vote``, and
``message_add_on_poll_vote_selected_option`` to populate the
analysis ``poll``, ``poll_option``, ``poll_vote``, and
``poll_vote_option`` tables.

Pre-computes ``voter_names`` on each ``poll_option`` row so the
GUI never needs heavy joins at render time.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Optional

from app.db.connection import DatabaseManager, AnalysisConnection
from app.db.source_reader import SourceReader

logger = logging.getLogger(__name__)


def ingest_polls(
    db_manager: DatabaseManager,
    analysis_conn: AnalysisConnection,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> int:
    """Ingest polls, options, votes, and per-option voter selections from msgstore.db.

    Returns:
        Number of polls ingested.
    """
    msgstore = db_manager.get_msgstore()
    reader = SourceReader(msgstore)

    if not reader.table_exists("message_poll"):
        logger.warning("message_poll table not found")
        return 0

    # Build message lookup
    msg_map: dict[int, int] = {}
    rows = analysis_conn.fetchall("SELECT source_msg_id, id FROM message")
    msg_map = {row[0]: row[1] for row in rows}

    # ── 1. Ingest polls ──────────────────────────────────────────────────────
    poll_cols = reader.get_column_names("message_poll")
    poll_rows = reader.execute_raw(
        "SELECT message_row_id, selectable_options_count "
        "FROM message_poll"
    )

    poll_insert = """
        INSERT OR IGNORE INTO poll (message_id, selectable_count)
        VALUES (?,?)
    """

    poll_count = 0
    poll_id_map: dict[int, int] = {}  # message_row_id → analysis poll.id

    analysis_conn.begin_transaction()
    try:
        cursor = analysis_conn.get_cursor()

        for msg_row, selectable in poll_rows:
            msg_id = msg_map.get(msg_row)
            if msg_id is None:
                continue

            cursor.execute(poll_insert, (msg_id, selectable))
            poll_id = analysis_conn.raw_connection.last_insert_rowid()
            poll_id_map[msg_row] = poll_id
            poll_count += 1

        analysis_conn.commit()

    except Exception:
        analysis_conn.rollback()
        raise

    logger.info("Ingested %d polls", poll_count)

    # ── 2. Ingest poll options ────────────────────────────────────────────────
    source_option_to_analysis: dict[int, int] = {}  # source _id → analysis option id

    if reader.table_exists("message_poll_option"):
        option_rows = reader.execute_raw(
            "SELECT _id, message_row_id, option_name, option_sha256, vote_total "
            "FROM message_poll_option ORDER BY message_row_id, _id"
        )

        option_insert = """
            INSERT INTO poll_option (poll_id, option_name, option_hash, vote_total, option_index)
            VALUES (?,?,?,?,?)
        """

        option_count = 0
        option_index_tracker: dict[int, int] = {}  # poll_id → next index

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()
            for src_opt_id, msg_row, name, sha256, vote_total in option_rows:
                poll_id = poll_id_map.get(msg_row)
                if poll_id is None:
                    continue

                idx = option_index_tracker.get(poll_id, 0)
                option_index_tracker[poll_id] = idx + 1

                cursor.execute(option_insert, (
                    poll_id, name,
                    sha256.hex() if isinstance(sha256, bytes) else sha256,
                    vote_total or 0,
                    idx,
                ))
                analysis_opt_id = analysis_conn.raw_connection.last_insert_rowid()
                source_option_to_analysis[src_opt_id] = analysis_opt_id
                option_count += 1

            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        logger.info("Ingested %d poll options", option_count)

    # ── 3. Ingest poll votes ─────────────────────────────────────────────────
    # message_add_on_poll_vote → message_add_on → parent_message_row_id → poll
    # Also track message_add_on._id → analysis poll_vote.id for junction step

    # Pre-load jid_to_contact for voter resolution
    jid_contact_rows = analysis_conn.fetchall(
        "SELECT jid_row_id, contact_id FROM jid_to_contact"
    )
    jid_to_contact: dict[int, int] = {
        r[0]: r[1] for r in jid_contact_rows if r[0] is not None
    }

    # Map: source message_add_on._id → analysis poll_vote.id (for junction step)
    addon_to_vote: dict[int, int] = {}

    if reader.table_exists("message_add_on_poll_vote") and reader.table_exists("message_add_on"):
        vote_rows = reader.execute_raw(
            "SELECT mpv.message_add_on_row_id, mao.parent_message_row_id, "
            "mao.sender_jid_row_id, mao.from_me, mpv.sender_timestamp "
            "FROM message_add_on_poll_vote mpv "
            "JOIN message_add_on mao ON mao._id = mpv.message_add_on_row_id"
        )

        vote_insert = """
            INSERT OR IGNORE INTO poll_vote (poll_id, voter_id, from_me, timestamp)
            VALUES (?,?,?,?)
        """

        vote_count = 0
        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for addon_id, parent_msg_row, sender_jid_row, from_me, timestamp in vote_rows:
                poll_id = poll_id_map.get(parent_msg_row)
                if poll_id is None:
                    continue

                voter_id = jid_to_contact.get(sender_jid_row) if sender_jid_row else None

                cursor.execute(vote_insert, (poll_id, voter_id, from_me, timestamp))
                analysis_vote_id = analysis_conn.raw_connection.last_insert_rowid()
                addon_to_vote[addon_id] = analysis_vote_id
                vote_count += 1

            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        logger.info("Ingested %d poll votes", vote_count)

    # ── 4. Ingest per-option vote selections ─────────────────────────────────
    # message_add_on_poll_vote_selected_option links addon → specific option
    vote_option_count = 0
    if (reader.table_exists("message_add_on_poll_vote_selected_option")
            and source_option_to_analysis and addon_to_vote):
        selected_rows = reader.execute_raw(
            "SELECT message_add_on_row_id, message_poll_option_id "
            "FROM message_add_on_poll_vote_selected_option"
        )

        vo_insert = """
            INSERT OR IGNORE INTO poll_vote_option (poll_vote_id, poll_option_id)
            VALUES (?,?)
        """

        analysis_conn.begin_transaction()
        try:
            cursor = analysis_conn.get_cursor()

            for addon_id, src_option_id in selected_rows:
                vote_id = addon_to_vote.get(addon_id)
                option_id = source_option_to_analysis.get(src_option_id)
                if vote_id is None or option_id is None:
                    continue

                cursor.execute(vo_insert, (vote_id, option_id))
                vote_option_count += 1

            analysis_conn.commit()
        except Exception:
            analysis_conn.rollback()
            raise

        logger.info("Ingested %d poll vote-option selections", vote_option_count)

    # ── 5. Pre-compute voter_names per poll_option ───────────────────────────
    # Join poll_vote_option → poll_vote → contact to build display names,
    # then UPDATE poll_option.voter_names for each option. Done once at ingestion.
    if vote_option_count > 0:
        try:
            voter_rows = analysis_conn.fetchall(
                "SELECT pvo.poll_option_id,"
                " COALESCE("
                "   CASE WHEN pv.from_me = 1 THEN 'You' END,"
                "   NULLIF(c.resolved_name, ''),"
                "   NULLIF(c.display_name, ''),"
                "   CASE WHEN c.wa_name IS NOT NULL AND c.wa_name != ''"
                "        THEN '~' || c.wa_name END,"
                "   'Unknown')"
                " || CASE WHEN c.phone_number IS NOT NULL AND c.phone_number != ''"
                "         THEN ' (+' || c.phone_number || ')'"
                "         ELSE '' END"
                " AS voter_display"
                " FROM poll_vote_option pvo"
                " JOIN poll_vote pv ON pv.id = pvo.poll_vote_id"
                " LEFT JOIN contact c ON c.id = pv.voter_id"
                " ORDER BY pvo.poll_option_id"
            )

            # Group voter names by option
            option_voters: dict[int, list[str]] = defaultdict(list)
            for opt_id, voter_display in voter_rows:
                option_voters[opt_id].append(voter_display)

            analysis_conn.begin_transaction()
            cursor = analysis_conn.get_cursor()
            for opt_id, names in option_voters.items():
                voter_str = ", ".join(names)
                cursor.execute(
                    "UPDATE poll_option SET voter_names = ? WHERE id = ?",
                    (voter_str, opt_id),
                )
            analysis_conn.commit()

            logger.info("Pre-computed voter names for %d poll options", len(option_voters))

        except Exception as e:
            logger.warning("Failed to pre-compute poll voter names: %s", e)
            try:
                analysis_conn.rollback()
            except Exception:
                pass

    if progress_callback:
        progress_callback(poll_count, poll_count)

    return poll_count
