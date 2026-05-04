"""
Contact forensic report generator — self-contained HTML.

Generates a comprehensive forensic report for a single contact, including:
  - Contact identity (resolved name, phone, JID, LID, WA name, platform)
  - Messaging summary (sent/received/total across all conversations)
  - Per-group activity breakdown (messages, media, links, mentions per group)
  - Mention relationships (who this contact mentions / who mentions them)
  - Media statistics (images, videos, audio, documents, stickers, etc.)
  - Call statistics (calls made, received, duration)
  - Activity patterns (hourly, daily)
  - Groups in common (with roles, join dates)
  - Direct conversation stats

All identifiers (JIDs, LIDs, msgstore row IDs) are preserved for forensic traceability.
"""

from __future__ import annotations

import base64
import html
import logging
import sqlite3
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Section catalogue — exposed to the GUI so the user can tick / untick what
# they want included.  Keys here MUST stay stable: the dialog persists the
# user's choices keyed on them.  Add new sections at the bottom.
# ---------------------------------------------------------------------------

DEFAULT_SECTIONS: dict[str, bool] = {
    "identity":            True,   # Name / phone / JID / LID / business
    "overall_stats":       True,   # Sent / received totals + call totals
    "group_activity":      True,   # Per-group contribution breakdown
    "mentions":            True,   # @mentions given + received
    "activity_patterns":   True,   # Hourly + daily heatmaps
    "media_links":         True,   # Media counts by type + top link domains
    "reactions":           True,   # Reactions given + received
    "calls":               True,   # Call statistics + per-call detail
    "direct_conversation": True,   # 1-on-1 chat summary
    "groups_in_common":    True,   # Other groups this contact + owner share
}

# Display label for each section key — shown to the user in the dialog.
SECTION_LABELS: dict[str, str] = {
    "identity":            "Contact identity (name, phone, JID, LID, business info)",
    "overall_stats":       "Overall messaging totals",
    "group_activity":      "Per-group activity breakdown",
    "mentions":            "Mentions network (given & received)",
    "activity_patterns":   "Activity patterns (hourly / daily heatmap)",
    "media_links":         "Media types & top link domains",
    "reactions":           "Reactions given & received",
    "calls":               "Call statistics & per-call detail",
    "direct_conversation": "1-on-1 conversation summary",
    "groups_in_common":    "Groups in common",
}


def _safe_section(label: str, builder, *args, **kwargs) -> str:
    """Run a section-builder with fault isolation.

    A failure in one section (bad SQL, missing column, malformed
    row) must never nuke the entire report.  Exceptions are
    caught, logged, and rendered as a small inline error block
    so the rest of the report still surfaces partial data.
    """
    try:
        return builder(*args, **kwargs)
    except Exception as exc:
        logger.exception("Report section %r failed", label)
        tb = html.escape(traceback.format_exc())
        return (
            f'<div class="section section-error">'
            f'<h2>{html.escape(label)} — failed to render</h2>'
            f'<p class="error-text">'
            f'This section could not be generated.  '
            f'The rest of the report is unaffected.'
            f'</p>'
            f'<details><summary>Technical details</summary>'
            f'<pre style="font-size:11px;white-space:pre-wrap;'
            f'background:#fff5f5;border:1px solid #fcc;padding:8px;'
            f'border-radius:4px;color:#900;">{tb}</pre></details>'
            f'</div>'
        )


def generate_contact_report(
    analysis_db_path: str | Path,
    contact_id: int,
    output_path: str | Path,
    group_conversation_id: int | None = None,
    sections: dict[str, bool] | None = None,
) -> Path:
    """Generate a forensic HTML report for a single contact.

    Parameters
    ----------
    analysis_db_path:
        Path to ``analysis.db``.
    contact_id:
        The contact ID (``analysis.db.contact.id``).
    output_path:
        Where to write the HTML file.
    group_conversation_id:
        If set, focus the report on a single group context.
    sections:
        Optional mapping ``{section_key: bool}`` controlling which
        sections appear in the report.  Missing keys default to
        :data:`DEFAULT_SECTIONS`.  Use this to let the user opt out of
        slow / privacy-sensitive sections from the GUI.  Passing
        ``None`` includes every section.

    Returns
    -------
    pathlib.Path
        Path to the generated HTML file.
    """
    output_path = Path(output_path)
    conn = sqlite3.connect(str(analysis_db_path))
    conn.row_factory = sqlite3.Row

    # Resolve the effective section selection — caller-supplied keys
    # override defaults; unknown keys are ignored.
    effective: dict[str, bool] = {**DEFAULT_SECTIONS}
    if sections:
        for k, v in sections.items():
            if k in DEFAULT_SECTIONS:
                effective[k] = bool(v)

    def _on(key: str) -> bool:
        return effective.get(key, True)

    try:
        # ---- Resolve device-owner identity once.  Every section that
        # joins to ``contact`` and may surface the owner as blank
        # (sender_id NULL on from_me messages) uses this to stamp
        # owner rows with the real name + JID + LID instead. ----
        try:
            _owner_meta = {
                r["key"]: r["value"]
                for r in conn.execute(
                    "SELECT key, value FROM case_metadata "
                    "WHERE key IN ('device_owner_name','device_owner_phone',"
                    "              'device_owner_jid','device_owner_lid_jid')"
                ).fetchall()
            }
        except Exception:
            _owner_meta = {}
        _owner_phone_kv = (_owner_meta.get("device_owner_phone") or "").replace("@s.whatsapp.net", "")
        _owner_jid_kv = _owner_meta.get("device_owner_jid") or (
            f"{_owner_phone_kv}@s.whatsapp.net" if _owner_phone_kv else ""
        )
        _owner_name_kv = _owner_meta.get("device_owner_name") or ""
        _owner_lid_kv = _owner_meta.get("device_owner_lid_jid") or ""

        # ---- Identity is always fetched (needed for the report header). ----
        contact = _get_contact_info(conn, contact_id)
        contact_name = (
            contact.get("resolved_name")
            or contact.get("wa_name")
            or contact.get("phone_number")
            or f"Contact #{contact_id}"
        )

        # Determine report scope label (header)
        scope_label = "All Conversations"
        if group_conversation_id:
            try:
                grp = conn.execute(
                    "SELECT display_name FROM conversation WHERE id = ?",
                    (group_conversation_id,),
                ).fetchone()
                if grp:
                    scope_label = f"Group: {grp['display_name']}"
            except Exception:
                logger.exception("Failed to resolve group scope label")

        # ---- Fetch only the data the requested sections need. ----
        # Lazy fetches to avoid running expensive SQL the user opted out of.
        overall_stats = _get_overall_stats(conn, contact_id) if _on("overall_stats") or _on("calls") else None
        call_stats = _get_call_stats(conn, contact_id) if _on("calls") or _on("overall_stats") else None
        call_details = _get_call_details(conn, contact_id) if _on("calls") else []
        group_activity = _get_group_activity(conn, contact_id) if _on("group_activity") else []
        direct_stats = _get_direct_conversation(conn, contact_id) if _on("direct_conversation") else None
        mentions_given = _get_mentions_given(conn, contact_id, group_conversation_id) if _on("mentions") else []
        mentions_received = _get_mentions_received(conn, contact_id, group_conversation_id) if _on("mentions") else []
        hourly = _get_hourly_activity(conn, contact_id, group_conversation_id) if _on("activity_patterns") else []
        daily = _get_daily_activity(conn, contact_id, group_conversation_id) if _on("activity_patterns") else []
        media_stats = _get_media_stats(conn, contact_id, group_conversation_id) if _on("media_links") else None
        groups_in_common = _get_groups_in_common(conn, contact_id) if _on("groups_in_common") else []
        link_domains = _get_top_link_domains(conn, contact_id, group_conversation_id) if _on("media_links") else []
        reactions_given = _get_reactions_given(conn, contact_id, group_conversation_id) if _on("reactions") else []
        reactions_received = _get_reactions_received(conn, contact_id, group_conversation_id) if _on("reactions") else []

        # ---- Build sections — each one fault-isolated. ----
        rendered: list[str] = []
        if _on("identity"):
            rendered.append(_safe_section("Identity", _section_identity, contact))
        if _on("overall_stats"):
            rendered.append(_safe_section("Overall stats",
                _section_overall_stats, overall_stats, call_stats))
        if _on("group_activity"):
            rendered.append(_safe_section("Group activity",
                _section_group_activity, group_activity))
        if _on("mentions"):
            rendered.append(_safe_section("Mentions",
                _section_mentions, mentions_given, mentions_received,
                _owner_name_kv, _owner_jid_kv, _owner_lid_kv))
        if _on("activity_patterns"):
            rendered.append(_safe_section("Activity patterns",
                _section_activity_patterns, hourly, daily))
        if _on("media_links"):
            rendered.append(_safe_section("Media & links",
                _section_media_links, media_stats, link_domains))
        if _on("reactions"):
            rendered.append(_safe_section("Reactions",
                _section_reactions, reactions_given, reactions_received))
        if _on("calls"):
            rendered.append(_safe_section("Calls",
                _section_calls, call_stats, call_details))
        if _on("direct_conversation"):
            rendered.append(_safe_section("Direct conversation",
                _section_direct_conversation, direct_stats))
        if _on("groups_in_common"):
            rendered.append(_safe_section("Groups in common",
                _section_groups_in_common, groups_in_common))

        # Case header (best-effort — never fail the report on this)
        case_banner_html = ""
        try:
            from app.reports.group_report import _collect_case_info, _render_case_banner
            case_info = _collect_case_info(conn, analysis_db_path)
            case_banner_html = _render_case_banner(case_info or {})
        except Exception:
            logger.exception("Case banner failed to render")

        html_content = _wrap_html(
            title=f"Contact Report — {contact_name}",
            contact_name=contact_name,
            scope_label=scope_label,
            sections=rendered,
            generated_at=_ts(int(datetime.now().timestamp() * 1000)),
            contact_id=contact_id,
            case_banner=case_banner_html,
        )

        output_path.write_text(html_content, encoding="utf-8")
        return output_path

    finally:
        conn.close()


# ====================================================================== #
# Data queries
# ====================================================================== #

def _get_contact_info(conn: sqlite3.Connection, cid: int) -> dict:
    row = conn.execute("""
        SELECT id, resolved_name, wa_name, display_name, phone_number,
               phone_jid, lid_jid, lid_display_name, status_text,
               is_business, business_name, business_category,
               platform_estimate, avatar_blob,
               COALESCE(linked_device_count, 0) AS linked_device_count
        FROM contact WHERE id = ?
    """, (cid,)).fetchone()
    if not row:
        return {"id": cid, "resolved_name": f"Contact #{cid}"}
    return dict(row)


def _get_overall_stats(conn: sqlite3.Connection, cid: int) -> dict:
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_messages,
            SUM(CASE WHEN m.from_me = 1 THEN 1 ELSE 0 END) AS sent_by_owner,
            SUM(CASE WHEN m.from_me = 0 THEN 1 ELSE 0 END) AS sent_by_contact,
            MIN(m.timestamp) AS first_message_ts,
            MAX(m.timestamp) AS last_message_ts,
            COUNT(DISTINCT m.conversation_id) AS conversations_active_in
        FROM message m
        WHERE m.sender_id = ? AND m.message_type != 7
    """, (cid,)).fetchone()
    return dict(row) if row else {}


def _get_group_activity(conn: sqlite3.Connection, cid: int) -> list[dict]:
    rows = conn.execute("""
        SELECT c.id AS conv_id, c.display_name AS group_name,
               c.jid_raw_string AS group_jid, c.chat_type,
               sca.total_messages, sca.total_text, sca.total_media,
               sca.total_images, sca.total_videos, sca.total_audio,
               sca.total_documents, sca.total_links,
               sca.total_mentions, sca.total_forwards, sca.total_edits, sca.total_deletes,
               sca.total_reactions_given, sca.total_reactions_received,
               sca.first_message_ts, sca.last_message_ts,
               gm.role, gm.label, gm.join_timestamp, gm.join_method
        FROM stats_contact_activity sca
        JOIN conversation c ON c.id = sca.conversation_id
        LEFT JOIN group_member gm ON gm.conversation_id = sca.conversation_id AND gm.contact_id = sca.contact_id
        WHERE sca.contact_id = ?
          AND c.chat_type IN ('group', 'community', 'community_sub')
        ORDER BY sca.total_messages DESC
    """, (cid,)).fetchall()
    return [dict(r) for r in rows]


def _get_direct_conversation(conn: sqlite3.Connection, cid: int) -> dict | None:
    # ``conversation`` has no ``contact_id`` column; a personal
    # chat maps to a contact through ``jid_to_contact`` (the
    # chain is contact_id → jid_row_id → jid_raw_string →
    # conversation.jid_raw_string).  The chat-type enum value
    # for personal chats is ``'personal'`` (see schema).
    row = conn.execute("""
        SELECT c.id AS conv_id, c.display_name, c.jid_raw_string,
               c.message_count, c.media_count,
               c.first_message_ts, c.last_message_ts,
               c.is_archived, c.is_pinned, c.is_muted
        FROM conversation c
        JOIN jid_to_contact jtc ON jtc.jid_raw_string = c.jid_raw_string
        WHERE jtc.contact_id = ? AND c.chat_type = 'personal'
        LIMIT 1
    """, (cid,)).fetchone()
    return dict(row) if row else None


def _get_mentions_given(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[dict]:
    """Who this contact mentions most."""
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT mn.mentioned_id,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS mentioned_name,
               c.phone_jid AS mentioned_jid, c.lid_jid AS mentioned_lid,
               COUNT(*) AS cnt
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        LEFT JOIN contact c ON c.id = mn.mentioned_id
        WHERE m.sender_id = ? {where}
          AND mn.mentioned_id IS NOT NULL
        GROUP BY mn.mentioned_id
        ORDER BY cnt DESC
        LIMIT 20
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_mentions_received(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[dict]:
    """Who mentions this contact most.

    ``m.from_me`` is included in the SELECT + GROUP BY so the renderer
    can name-stamp owner-sent mentions with the device-owner identity
    from ``case_metadata``.  Without this, owner mentions surface as a
    blank "Mentioned By" row (sender_id IS NULL on from_me messages,
    so the LEFT JOIN against contact returns NULLs).
    """
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT m.sender_id,
               m.from_me,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS mentioner_name,
               c.phone_jid AS mentioner_jid, c.lid_jid AS mentioner_lid,
               COUNT(*) AS cnt
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        LEFT JOIN contact c ON c.id = m.sender_id
        WHERE mn.mentioned_id = ? {where}
        GROUP BY m.from_me, m.sender_id
        ORDER BY cnt DESC
        LIMIT 20
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_hourly_activity(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[tuple[int, int]]:
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT CAST(strftime('%H', m.timestamp/1000, 'unixepoch') AS INTEGER) AS hour,
               COUNT(*) AS cnt
        FROM message m
        WHERE m.sender_id = ? {where} AND m.message_type != 7
        GROUP BY hour ORDER BY hour
    """, params).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_daily_activity(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[tuple[str, int]]:
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT strftime('%Y-%m-%d', m.timestamp/1000, 'unixepoch') AS date_str,
               COUNT(*) AS cnt
        FROM message m
        WHERE m.sender_id = ? {where} AND m.message_type != 7
        GROUP BY date_str ORDER BY date_str
    """, params).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_call_stats(conn: sqlite3.Connection, cid: int) -> dict:
    # Column reference (analysis.db):
    #   cr.contact_id, cr.duration_sec,
    #   cr.is_video (1 = video, 0 = voice),
    #   cr.is_group_call,
    #   call_participant.call_id.
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_calls,
            SUM(CASE WHEN cr.from_me = 1 THEN 1 ELSE 0 END) AS calls_made,
            SUM(CASE WHEN cr.from_me = 0 THEN 1 ELSE 0 END) AS calls_received,
            SUM(CASE WHEN cr.is_video = 0 THEN 1 ELSE 0 END) AS voice_calls,
            SUM(CASE WHEN cr.is_video = 1 THEN 1 ELSE 0 END) AS video_calls,
            SUM(cr.duration_sec) AS total_duration_sec,
            MAX(cr.duration_sec) AS longest_call_sec
        FROM call_record cr
        WHERE cr.contact_id = ? OR cr.id IN (
            SELECT clp.call_id FROM call_participant clp WHERE clp.contact_id = ?
        )
    """, (cid, cid)).fetchone()
    return dict(row) if row else {}


def _get_call_details(conn: sqlite3.Connection, cid: int) -> list[dict]:
    rows = conn.execute("""
        SELECT cr.timestamp, cr.duration_sec AS duration,
               cr.is_video AS call_type, cr.call_result, cr.from_me,
               cr.is_group_call AS group_call_flag,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS caller_name,
               c.phone_jid AS caller_jid,
               cv.display_name AS conversation_name
        FROM call_record cr
        LEFT JOIN contact c ON c.id = cr.contact_id
        LEFT JOIN conversation cv ON cv.id = cr.conversation_id
        WHERE cr.contact_id = ? OR cr.id IN (
            SELECT clp.call_id FROM call_participant clp WHERE clp.contact_id = ?
        )
        ORDER BY cr.timestamp DESC
        LIMIT 50
    """, (cid, cid)).fetchall()
    return [dict(r) for r in rows]


def _get_media_stats(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> dict:
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    row = conn.execute(f"""
        SELECT
            SUM(CASE WHEN m.message_type = 1 THEN 1 ELSE 0 END) AS images,
            SUM(CASE WHEN m.message_type = 3 THEN 1 ELSE 0 END) AS videos,
            SUM(CASE WHEN m.message_type = 2 THEN 1 ELSE 0 END) AS audio,
            SUM(CASE WHEN m.message_type = 9 THEN 1 ELSE 0 END) AS documents,
            SUM(CASE WHEN m.message_type = 20 THEN 1 ELSE 0 END) AS stickers,
            SUM(CASE WHEN m.message_type = 13 THEN 1 ELSE 0 END) AS gifs,
            SUM(CASE WHEN m.message_type IN (5, 16) THEN 1 ELSE 0 END) AS locations,
            SUM(CASE WHEN m.message_type = 4 THEN 1 ELSE 0 END) AS contacts_shared,
            SUM(CASE WHEN m.message_type IN (42, 43) THEN 1 ELSE 0 END) AS view_once
        FROM message m
        WHERE m.sender_id = ? {where} AND m.message_type != 7 AND m.message_type != 0
    """, params).fetchone()
    return dict(row) if row else {}


def _get_groups_in_common(conn: sqlite3.Connection, cid: int) -> list[dict]:
    rows = conn.execute("""
        SELECT gm.conversation_id, c.display_name, c.jid_raw_string,
               c.chat_type, gm.role, gm.label,
               gm.join_timestamp, gm.join_method,
               sca.total_messages, sca.first_message_ts, sca.last_message_ts
        FROM group_member gm
        JOIN conversation c ON c.id = gm.conversation_id
        LEFT JOIN stats_contact_activity sca
            ON sca.conversation_id = gm.conversation_id AND sca.contact_id = gm.contact_id
        WHERE gm.contact_id = ?
        ORDER BY sca.total_messages DESC NULLS LAST
    """, (cid,)).fetchall()
    return [dict(r) for r in rows]


def _get_top_link_domains(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[dict]:
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT mld.domain, COUNT(*) AS cnt
        FROM message_link_detail mld
        JOIN message m ON m.id = mld.message_id
        WHERE m.sender_id = ? {where}
        GROUP BY mld.domain
        ORDER BY cnt DESC
        LIMIT 15
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_reactions_given(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[dict]:
    """Top emojis this contact reacts with."""
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT r.emoji, COUNT(*) AS cnt
        FROM reaction r
        JOIN message m ON m.id = r.message_id
        WHERE r.reactor_id = ? {where}
        GROUP BY r.emoji
        ORDER BY cnt DESC
        LIMIT 10
    """, params).fetchall()
    return [dict(r) for r in rows]


def _get_reactions_received(conn: sqlite3.Connection, cid: int, conv_id: int | None = None) -> list[dict]:
    """Top emojis reacted to this contact's messages."""
    where = "AND m.conversation_id = ?" if conv_id else ""
    params = (cid, conv_id) if conv_id else (cid,)
    rows = conn.execute(f"""
        SELECT r.emoji, COUNT(*) AS cnt
        FROM reaction r
        JOIN message m ON m.id = r.message_id
        WHERE m.sender_id = ? AND r.reactor_id != ? {where}
        GROUP BY r.emoji
        ORDER BY cnt DESC
        LIMIT 10
    """, (cid, cid, conv_id) if conv_id else (cid, cid)).fetchall()
    return [dict(r) for r in rows]


# ====================================================================== #
# HTML helpers
# ====================================================================== #

def _h(text) -> str:
    return html.escape(str(text)) if text else ""


def _detect_local_tz() -> tuple[str, timedelta]:
    """Detect local timezone name and offset."""
    local_dt = datetime.now().astimezone()
    tz_name = local_dt.strftime("%Z") or "LOCAL"
    utc_offset = local_dt.utcoffset() or timedelta(0)
    return tz_name, utc_offset


def _format_tz_display(tz_name: str, utc_offset: timedelta) -> str:
    total_sec = int(utc_offset.total_seconds())
    sign = "+" if total_sec >= 0 else "-"
    h, remainder = divmod(abs(total_sec), 3600)
    m = remainder // 60
    offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return f"{tz_name} ({offset_str})"


def _ts(ms) -> str:
    """Format Unix-ms timestamp with local time + UTC in brackets."""
    if not ms:
        return "—"
    try:
        local_dt = datetime.fromtimestamp(int(ms) / 1000).astimezone()
        utc_dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        tz_name = local_dt.strftime("%Z") or "LOCAL"
        local_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{local_str} {tz_name} [{utc_str} UTC]"
    except (ValueError, OSError):
        return "—"


def _ts_short(ms) -> str:
    """Format Unix-ms timestamp — short form with UTC in brackets."""
    if not ms:
        return "—"
    try:
        local_dt = datetime.fromtimestamp(int(ms) / 1000).astimezone()
        utc_dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        tz_name = local_dt.strftime("%Z") or "LOCAL"
        local_str = local_dt.strftime("%Y-%m-%d %H:%M")
        utc_str = utc_dt.strftime("%Y-%m-%d %H:%M")
        return f"{local_str} {tz_name} [{utc_str} UTC]"
    except (ValueError, OSError):
        return "—"


def _num(n) -> str:
    if n is None:
        return "0"
    return f"{int(n):,}"


def _blob_to_img(blob, size: int = 64, circle: bool = True) -> str:
    if not blob or len(blob) < 100:
        return ""
    b64 = base64.b64encode(blob).decode("ascii")
    style = f"width:{size}px;height:{size}px;object-fit:cover;"
    if circle:
        style += "border-radius:50%;"
    return f'<img src="data:image/jpeg;base64,{b64}" style="{style}" />'


def _duration_str(seconds) -> str:
    if not seconds:
        return "0s"
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds}s"


# ====================================================================== #
# Section builders
# ====================================================================== #

def _section_identity(c: dict) -> str:
    avatar_html = _blob_to_img(c.get("avatar_blob"), 80)
    if not avatar_html:
        name = c.get("resolved_name") or c.get("wa_name") or "#"
        initials = "".join(w[0] for w in name.split()[:2] if w and w[0].isalpha())
        avatar_html = f'<div class="avatar-placeholder">{_h(initials[:2].upper() or "#")}</div>'

    rows = []
    for label, key in [
        ("Resolved Name", "resolved_name"),
        ("WhatsApp Name", "wa_name"),
        ("Display Name", "display_name"),
        ("Phone Number", "phone_number"),
        ("Phone JID (msgstore)", "phone_jid"),
        ("LID JID (msgstore)", "lid_jid"),
        ("LID Display Name", "lid_display_name"),
        ("Status", "status_text"),
        ("Platform", "platform_estimate"),
        ("Business", "is_business"),
        ("Business Name", "business_name"),
        ("Business Category", "business_category"),
        ("Linked Devices", "linked_device_count"),
    ]:
        val = c.get(key)
        if val is None or val == "" or val == 0:
            if key in ("phone_jid", "lid_jid"):
                # Always show JID fields even if empty
                val = "—"
            else:
                continue
        if key == "is_business":
            val = "Yes" if val else "No"
        if key == "phone_number" and val and not str(val).startswith("+"):
            val = f"+{val}"
        display = f"<code>{_h(val)}</code>" if key in ("phone_jid", "lid_jid") else _h(val)
        rows.append(f"<tr><td>{label}</td><td>{display}</td></tr>")

    rows.append(f'<tr><td>Analysis DB contact.id</td><td><code>{c.get("id", "?")}</code></td></tr>')

    return f"""
    <div class="section" id="identity">
        <h2>Contact Identity</h2>
        <div class="identity-card">
            <div class="identity-avatar">{avatar_html}</div>
            <div class="identity-info">
                <h3>{_h(c.get('resolved_name') or c.get('wa_name') or c.get('phone_number') or 'Unknown')}</h3>
                <p class="dim">{_h(c.get('phone_jid', ''))}</p>
            </div>
        </div>
        <table class="info-table">{''.join(rows)}</table>
    </div>
    """


def _section_overall_stats(stats: dict, call_stats: dict) -> str:
    total = stats.get("total_messages") or 0
    sent = stats.get("sent_by_contact") or 0  # messages sent by this contact
    convos = stats.get("conversations_active_in") or 0
    total_calls = call_stats.get("total_calls") or 0
    total_dur = _duration_str(call_stats.get("total_duration_sec"))

    return f"""
    <div class="section" id="summary">
        <h2>Overall Activity</h2>
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{_num(total)}</div><div class="stat-label">Messages Sent</div></div>
            <div class="stat-card"><div class="stat-value">{_num(convos)}</div><div class="stat-label">Conversations</div></div>
            <div class="stat-card"><div class="stat-value">{_num(total_calls)}</div><div class="stat-label">Calls</div></div>
            <div class="stat-card"><div class="stat-value">{total_dur}</div><div class="stat-label">Call Duration</div></div>
        </div>
        <table class="info-table" style="margin-top:12px;">
            <tr><td>First Message</td><td>{_ts(stats.get('first_message_ts'))}</td></tr>
            <tr><td>Last Message</td><td>{_ts(stats.get('last_message_ts'))}</td></tr>
        </table>
    </div>
    """


def _section_group_activity(groups: list[dict]) -> str:
    if not groups:
        return '<div class="section" id="group-activity"><h2>Group Activity</h2><p class="empty">No group activity found.</p></div>'

    rows = []
    for g in groups:
        role = (g.get("role") or "member").lower()
        role_badge = f'<span class="role-{role}">{role.replace("superadmin", "Creator").title()}</span>'
        label_html = f' <span class="member-label">{_h(g.get("label"))}</span>' if g.get("label") else ""

        rows.append(f"""
        <tr>
            <td><strong>{_h(g.get('group_name'))}</strong>{label_html}</td>
            <td class="jid-cell"><code>{_h(g.get('group_jid', ''))}</code></td>
            <td>{role_badge}</td>
            <td class="num">{_num(g.get('total_messages'))}</td>
            <td class="num">{_num(g.get('total_media'))}</td>
            <td class="num">{_num(g.get('total_links'))}</td>
            <td class="num">{_num(g.get('total_mentions'))}</td>
            <td class="num">{_num(g.get('total_forwards'))}</td>
            <td class="num">{_num(g.get('total_edits'))}</td>
            <td class="num">{_num(g.get('total_deletes'))}</td>
            <td>{_ts_short(g.get('first_message_ts'))}</td>
            <td>{_ts_short(g.get('last_message_ts'))}</td>
        </tr>
        """)

    return f"""
    <div class="section" id="group-activity">
        <h2>Group Activity <span class="count">({len(groups)} groups)</span></h2>
        <table class="data-table">
            <thead><tr>
                <th>Group</th><th>Group JID</th><th>Role</th>
                <th>Messages</th><th>Media</th><th>Links</th><th>Mentions</th>
                <th>Forwards</th><th>Edits</th><th>Deletes</th>
                <th>First Msg</th><th>Last Msg</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_mentions(given: list[dict], received: list[dict],
                      owner_name: str = "", owner_jid: str = "",
                      owner_lid: str = "") -> str:
    """Render mention relationships — owner-aware.

    Rows where ``from_me = 1`` are rebadged with the device-owner
    identity from ``case_metadata`` so they don't surface as blank
    "Mentioned By" rows (the underlying ``LEFT JOIN contact`` returns
    NULLs for owner messages, since they have no sender_id).
    """
    if not given and not received:
        return '<div class="section" id="mentions"><h2>Mention Relationships</h2><p class="empty">No mentions found.</p></div>'

    owner_display = (
        f"{owner_name} (you)" if owner_name else "You (Device Owner)"
    )
    owner_tag_html = (
        ' <span style="background:#e0f2f1;color:#00695c;padding:1px 6px;'
        'border-radius:8px;font-size:9px;font-weight:600;'
        'text-transform:uppercase;letter-spacing:0.04em;">Owner</span>'
    )

    given_rows = ""
    for m in given:
        given_rows += f"""
        <tr>
            <td><strong>{_h(m.get('mentioned_name') or '—')}</strong></td>
            <td class="jid-cell"><code>{_h(m.get('mentioned_jid') or '')}</code></td>
            <td class="jid-cell"><code>{_h(m.get('mentioned_lid') or '')}</code></td>
            <td class="num">{_num(m.get('cnt'))}</td>
        </tr>
        """

    received_rows = ""
    for m in received:
        if m.get("from_me"):
            nm = owner_display
            jid = owner_jid
            lid = owner_lid
            tag = owner_tag_html
        else:
            nm = m.get("mentioner_name") or "—"
            jid = m.get("mentioner_jid") or ""
            lid = m.get("mentioner_lid") or ""
            tag = ""
        received_rows += f"""
        <tr>
            <td><strong>{_h(nm)}</strong>{tag}</td>
            <td class="jid-cell"><code>{_h(jid)}</code></td>
            <td class="jid-cell"><code>{_h(lid)}</code></td>
            <td class="num">{_num(m.get('cnt'))}</td>
        </tr>
        """

    return f"""
    <div class="section" id="mentions">
        <h2>Mention Relationships</h2>
        <div class="mention-grid">
            <div>
                <h3>This Contact Mentions <span class="count">({len(given)})</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Name</th><th>JID</th><th>LID</th><th>Times</th></tr></thead>
                    <tbody>{given_rows or '<tr><td colspan="4" class="empty">None</td></tr>'}</tbody>
                </table>
            </div>
            <div>
                <h3>Mentioned By <span class="count">({len(received)})</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Name</th><th>JID</th><th>LID</th><th>Times</th></tr></thead>
                    <tbody>{received_rows or '<tr><td colspan="4" class="empty">None</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    </div>
    """


def _section_activity_patterns(hourly: list[tuple], daily: list[tuple]) -> str:
    if not hourly:
        return ""

    max_h = max(c for _, c in hourly) if hourly else 1
    hour_bars = ""
    for h in range(24):
        count = dict(hourly).get(h, 0)
        pct = (count / max_h * 100) if max_h else 0
        hour_bars += f'<div class="hour-bar"><div class="hour-fill" style="height:{pct:.0f}%"></div><div class="hour-label">{h:02d}</div></div>'

    # Daily activity summary (recent 30 days mini-chart)
    daily_html = ""
    if daily and len(daily) > 7:
        recent = daily[-60:]  # last 60 days
        max_d = max(c for _, c in recent)
        daily_bars = ""
        for date_str, cnt in recent:
            pct = (cnt / max_d * 100) if max_d else 0
            daily_bars += f'<div class="day-bar" title="{date_str}: {cnt} msgs"><div class="day-fill" style="height:{pct:.0f}%"></div></div>'
        daily_html = f"""
        <h3>Daily Activity (last {len(recent)} days)</h3>
        <div class="daily-chart">{daily_bars}</div>
        """

    total_days = len(daily) if daily else 0
    total_msgs = sum(c for _, c in daily) if daily else 0
    avg = f"{total_msgs / total_days:.1f}" if total_days else "0"

    return f"""
    <div class="section" id="activity">
        <h2>Activity Patterns</h2>
        <p class="dim" style="margin-bottom:12px;">Active across {total_days} days &middot; Average {avg} messages/day</p>
        <h3>Messages by Hour of Day</h3>
        <div class="hourly-chart">{hour_bars}</div>
        {daily_html}
    </div>
    """


def _section_media_links(media: dict, domains: list[dict]) -> str:
    media_items = [
        ("Images", media.get("images")),
        ("Videos", media.get("videos")),
        ("Audio/Voice", media.get("audio")),
        ("Documents", media.get("documents")),
        ("Stickers", media.get("stickers")),
        ("GIFs", media.get("gifs")),
        ("Locations", media.get("locations")),
        ("Contacts Shared", media.get("contacts_shared")),
        ("View-Once", media.get("view_once")),
    ]
    media_rows = ""
    for label, val in media_items:
        if val:
            media_rows += f"<tr><td>{label}</td><td class='num'>{_num(val)}</td></tr>"

    domain_rows = ""
    for d in domains:
        domain_rows += f"<tr><td><code>{_h(d.get('domain'))}</code></td><td class='num'>{_num(d.get('cnt'))}</td></tr>"

    return f"""
    <div class="section" id="media-links">
        <h2>Media & Links</h2>
        <div class="mention-grid">
            <div>
                <h3>Media Sent</h3>
                <table class="data-table compact">
                    <thead><tr><th>Type</th><th>Count</th></tr></thead>
                    <tbody>{media_rows or '<tr><td colspan="2" class="empty">No media</td></tr>'}</tbody>
                </table>
            </div>
            <div>
                <h3>Top Link Domains</h3>
                <table class="data-table compact">
                    <thead><tr><th>Domain</th><th>Links</th></tr></thead>
                    <tbody>{domain_rows or '<tr><td colspan="2" class="empty">No links</td></tr>'}</tbody>
                </table>
            </div>
        </div>
    </div>
    """


def _section_reactions(given: list[dict], received: list[dict]) -> str:
    if not given and not received:
        return ""

    given_html = ""
    for r in given:
        given_html += f'<span class="emoji-stat">{r.get("emoji", "?")} <small>×{r.get("cnt", 0)}</small></span> '

    received_html = ""
    for r in received:
        received_html += f'<span class="emoji-stat">{r.get("emoji", "?")} <small>×{r.get("cnt", 0)}</small></span> '

    return f"""
    <div class="section" id="reactions">
        <h2>Reactions</h2>
        <div class="mention-grid">
            <div>
                <h3>Reactions Given</h3>
                <div class="emoji-row">{given_html or '<span class="empty">None</span>'}</div>
            </div>
            <div>
                <h3>Reactions Received</h3>
                <div class="emoji-row">{received_html or '<span class="empty">None</span>'}</div>
            </div>
        </div>
    </div>
    """


def _section_calls(stats: dict, details: list[dict]) -> str:
    total = stats.get("total_calls") or 0
    if not total:
        return '<div class="section" id="calls"><h2>Call History</h2><p class="empty">No calls found.</p></div>'

    call_type_labels = {0: "Voice", 1: "Video"}
    call_result_labels = {
        0: "Connected", 2: "Missed", 3: "Unavailable",
        4: "Rejected", 5: "Disconnected", 7: "Busy", 8: "Joined Voice Chat",
    }

    rows = []
    for c in details:
        ct = call_type_labels.get(c.get("call_type"), "?")
        cr = call_result_labels.get(c.get("call_result"), f"Code {c.get('call_result')}")
        direction = "Outgoing" if c.get("from_me") else "Incoming"
        is_group = "Group" if c.get("group_call_flag") else ""
        dur = _duration_str(c.get("duration"))
        conv = _h(c.get("conversation_name") or "")

        rows.append(f"""
        <tr>
            <td>{_ts_short(c.get('timestamp'))}</td>
            <td>{direction}</td>
            <td>{ct}{' ' + is_group if is_group else ''}</td>
            <td>{cr}</td>
            <td>{dur}</td>
            <td>{conv}</td>
        </tr>
        """)

    summary = (
        f"Made: {_num(stats.get('calls_made'))} · "
        f"Received: {_num(stats.get('calls_received'))} · "
        f"Voice: {_num(stats.get('voice_calls'))} · "
        f"Video: {_num(stats.get('video_calls'))} · "
        f"Total duration: {_duration_str(stats.get('total_duration_sec'))} · "
        f"Longest: {_duration_str(stats.get('longest_call_sec'))}"
    )

    return f"""
    <div class="section" id="calls">
        <h2>Call History <span class="count">({_num(total)} calls)</span></h2>
        <p class="dim" style="margin-bottom:12px;">{summary}</p>
        <table class="data-table compact">
            <thead><tr><th>Time</th><th>Direction</th><th>Type</th><th>Result</th><th>Duration</th><th>Conversation</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_direct_conversation(direct: dict | None) -> str:
    if not direct:
        return ""

    return f"""
    <div class="section" id="direct">
        <h2>Direct Conversation</h2>
        <table class="info-table">
            <tr><td>Conversation JID</td><td><code>{_h(direct.get('jid_raw_string'))}</code></td></tr>
            <tr><td>Total Messages</td><td>{_num(direct.get('message_count'))}</td></tr>
            <tr><td>Total Media</td><td>{_num(direct.get('media_count'))}</td></tr>
            <tr><td>First Message</td><td>{_ts(direct.get('first_message_ts'))}</td></tr>
            <tr><td>Last Message</td><td>{_ts(direct.get('last_message_ts'))}</td></tr>
            <tr><td>Archived</td><td>{'Yes' if direct.get('is_archived') else 'No'}</td></tr>
            <tr><td>Pinned</td><td>{'Yes' if direct.get('is_pinned') else 'No'}</td></tr>
            <tr><td>Muted</td><td>{'Yes' if direct.get('is_muted') else 'No'}</td></tr>
        </table>
    </div>
    """


def _section_groups_in_common(groups: list[dict]) -> str:
    if not groups:
        return ""

    rows = []
    for g in groups:
        role = (g.get("role") or "member").lower()
        role_badge = f'<span class="role-{role}">{role.replace("superadmin", "Creator").title()}</span>'
        label_html = f' <span class="member-label">{_h(g.get("label"))}</span>' if g.get("label") else ""

        rows.append(f"""
        <tr>
            <td><strong>{_h(g.get('display_name'))}</strong>{label_html}</td>
            <td class="jid-cell"><code>{_h(g.get('jid_raw_string', ''))}</code></td>
            <td>{role_badge}</td>
            <td class="num">{_num(g.get('total_messages'))}</td>
            <td>{_ts_short(g.get('join_timestamp'))}</td>
            <td>{_h(g.get('join_method', '—'))}</td>
            <td>{_ts_short(g.get('first_message_ts'))}</td>
            <td>{_ts_short(g.get('last_message_ts'))}</td>
        </tr>
        """)

    return f"""
    <div class="section" id="groups">
        <h2>Groups In Common <span class="count">({len(groups)})</span></h2>
        <table class="data-table">
            <thead><tr>
                <th>Group</th><th>Group JID</th><th>Role</th><th>Messages</th>
                <th>Joined</th><th>Join Method</th><th>First Msg</th><th>Last Msg</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


# ====================================================================== #
# HTML wrapper with embedded CSS
# ====================================================================== #

def _wrap_html(title: str, contact_name: str, scope_label: str,
               sections: list[str], generated_at: str, contact_id: int,
               case_banner: str = "") -> str:
    nav_items = [
        ("identity", "Identity"), ("summary", "Stats"),
        ("group-activity", "Groups"), ("mentions", "Mentions"),
        ("activity", "Activity"), ("media-links", "Media"),
        ("reactions", "Reactions"), ("calls", "Calls"),
        ("direct", "Direct Chat"), ("groups", "Common Groups"),
    ]
    nav_html = "".join(f'<a href="#{nid}">{nlabel}</a>' for nid, nlabel in nav_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_h(title)}</title>
<style>
@page {{ size: A4; margin: 1.5cm; }}
@media print {{ .no-print {{ display: none; }} .report-nav {{ display:none; }} .section {{ break-inside:avoid; }} }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; color: #1a1a1a; line-height: 1.5; background: #f8f9fa; }}

/* Header — matches Activity Report teal */
.report-header {{ background: #075e54; color: white; padding: 24px; border-radius: 0 0 12px 12px; margin: 0 16px; }}
.report-header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 8px; }}
.report-header .info-table td {{ border-color: rgba(255,255,255,0.2); color: white; padding: 4px 8px; font-size: 13px; }}
.report-header .info-table td:first-child {{ color: rgba(255,255,255,0.7); font-weight: 600; width: 160px; }}

/* Nav */
.report-nav {{ background: white; border-bottom: 2px solid #075e54; padding: 8px 16px; position: sticky; top: 0; z-index: 100; box-shadow: 0 2px 4px rgba(0,0,0,0.08); }}
.report-nav a {{ color: #555; text-decoration: none; font-size: 11px; font-weight: 600; padding: 6px 12px; border-radius: 4px; display: inline-block; }}
.report-nav a:hover {{ background: #e8f5e9; color: #075e54; }}

/* Sections */
.section {{ background: white; margin: 16px; padding: 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); page-break-inside: avoid; }}
.section h2 {{ color: #075e54; font-size: 18px; margin-bottom: 12px; padding-bottom: 6px; border-bottom: 2px solid #075e54; }}
.section h2 .count {{ color: #888; font-size: 13px; font-weight: normal; }}
.section h3 {{ color: #128c7e; font-size: 14px; margin: 16px 0 8px; }}
.empty {{ color: #999; font-style: italic; }}

/* Identity */
.identity-card {{ display: flex; gap: 16px; align-items: center; margin-bottom: 16px; }}
.identity-avatar img {{ width: 80px; height: 80px; border-radius: 50%; border: 3px solid #075e54; }}
.avatar-placeholder {{ width: 80px; height: 80px; border-radius: 50%; background: #075e54; display: flex; align-items: center; justify-content: center; font-size: 28px; font-weight: bold; color: white; }}
.identity-info h3 {{ font-size: 20px; color: #1a1a1a; }}
.identity-info .dim {{ margin-top: 4px; color: #888; font-size: 12px; }}

/* Info table */
.info-table {{ width: 100%; border-collapse: collapse; }}
.info-table td {{ padding: 4px 8px; border-bottom: 1px solid #eee; font-size: 13px; }}
.info-table td:first-child {{ color: #555; width: 200px; font-weight: 600; white-space: nowrap; }}
.info-table code {{ background: #f0f4f8; padding: 2px 6px; border-radius: 3px; font-size: 11px; color: #128c7e; font-family: 'Consolas', 'Courier New', monospace; }}

/* Stats grid */
.stats-grid {{ display: flex; gap: 12px; flex-wrap: wrap; }}
.stat-card {{ flex: 1; min-width: 120px; text-align: center; background: #f0f9f7; border: 1px solid #e0f2ef; border-radius: 8px; padding: 16px 12px; }}
.stat-value {{ font-size: 24px; font-weight: 700; color: #075e54; }}
.stat-label {{ font-size: 11px; color: #666; margin-top: 4px; }}

/* Data tables */
.data-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
.data-table th {{ background: #f5f5f5; padding: 8px; text-align: left; font-weight: 600; color: #333; border-bottom: 2px solid #ddd; font-size: 10px; text-transform: uppercase; }}
.data-table td {{ padding: 6px 8px; border-bottom: 1px solid #eee; }}
.data-table tbody tr:nth-child(even) {{ background: #fafafa; }}
.data-table tr:hover {{ background: #f0f9f7; }}
.data-table.compact {{ font-size: 11px; }}
.data-table .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.jid-cell code {{ font-size: 10px; color: #128c7e; background: #f0f4f8; padding: 1px 4px; border-radius: 2px; font-family: 'Consolas', monospace; }}

/* Roles */
.role-superadmin {{ background: #f4511e; color: white; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: bold; }}
.role-admin {{ background: #ffb300; color: #1a1a1a; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: bold; }}
.role-member {{ color: #999; font-size: 10px; }}
.member-label {{ background: #f0f4f8; color: #128c7e; padding: 1px 6px; border-radius: 4px; font-size: 10px; margin-left: 6px; }}

.dim {{ color: #888; font-size: 12px; }}

/* Mention grid */
.mention-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 768px) {{ .mention-grid {{ grid-template-columns: 1fr; }} }}

/* Hourly chart */
.hourly-chart {{ display: flex; align-items: flex-end; gap: 3px; height: 120px; padding: 0 4px; }}
.hour-bar {{ flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }}
.hour-fill {{ width: 100%; background: linear-gradient(0deg, #075e54, #43a047); border-radius: 2px 2px 0 0; min-height: 2px; }}
.hour-label {{ font-size: 9px; color: #999; margin-top: 4px; }}

/* Daily chart */
.daily-chart {{ display: flex; align-items: flex-end; gap: 1px; height: 80px; margin-top: 8px; }}
.day-bar {{ flex: 1; min-width: 2px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }}
.day-fill {{ width: 100%; background: #128c7e; border-radius: 1px 1px 0 0; min-height: 1px; opacity: 0.8; }}

/* Emoji */
.emoji-row {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 8px 0; }}
.emoji-stat {{ background: #f5f5f5; padding: 4px 10px; border-radius: 16px; font-size: 16px; }}
.emoji-stat small {{ font-size: 11px; color: #888; }}

/* Footer */
.report-footer {{ text-align: center; padding: 24px; color: #999; font-size: 11px; border-top: 1px solid #eee; margin: 16px; }}
</style>
</head>
<body>

<div class="report-header">
    <h1>Contact Forensic Report</h1>
    {case_banner}
    <table class="info-table">
        <tr><td>Contact Name</td><td><strong>{_h(contact_name)}</strong></td></tr>
        <tr><td>Scope</td><td>{_h(scope_label)}</td></tr>
        <tr><td>Contact ID (analysis.db)</td><td>{contact_id}</td></tr>
        <tr><td>Report Generated</td><td>{_h(generated_at)}</td></tr>
        <tr><td>Timezone</td><td>{_h(_format_tz_display(*_detect_local_tz()))}</td></tr>
    </table>
</div>

<div class="report-nav">{nav_html}</div>

{''.join(sections)}

<div class="report-footer">
    <strong>WAInsight</strong> &mdash; WhatsApp Forensic Analysis Suite<br>
    This report is auto-generated from forensic analysis of WhatsApp databases.<br>
    All JIDs and LIDs are original identifiers from the source database (msgstore.db / wa.db).
</div>

</body>
</html>"""
