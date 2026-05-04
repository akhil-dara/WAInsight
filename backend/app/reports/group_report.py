"""
Group forensic report generator — self-contained HTML.

Generates a comprehensive forensic report for a single WhatsApp group, including:
  - Group identity & metadata (name, JID, creation, creator, type, settings)
  - Group edit history timeline (subject, description, DP changes with thumbnails)
  - Member roster with roles, join dates, activity stats
  - Top contributors (messages, media, links, reactions)
  - Mention network (who mentions whom, most-mentioned, most-mentioning)
  - Activity patterns (hourly, daily heatmap)
  - Admin audit trail (promotions, demotions, settings changes)
  - Media & link statistics
  - Former members with departure info

All images (avatars, DP thumbnails) are base64-embedded for portability.
"""

from __future__ import annotations

import base64
import html
import sqlite3
import time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


def _detect_local_tz() -> tuple[str, timedelta]:
    """Detect local timezone name and offset from UTC."""
    local_dt = datetime.now().astimezone()
    tz_name = local_dt.strftime("%Z") or "LOCAL"
    utc_offset = local_dt.utcoffset() or timedelta(0)
    return tz_name, utc_offset


def _format_tz_display(tz_name: str, utc_offset: timedelta) -> str:
    """Build display like 'IST (UTC+5:30)'."""
    total_sec = int(utc_offset.total_seconds())
    sign = "+" if total_sec >= 0 else "-"
    h, remainder = divmod(abs(total_sec), 3600)
    m = remainder // 60
    offset_str = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return f"{tz_name} ({offset_str})"


def generate_group_report(
    analysis_db_path: str | Path,
    conversation_id: int,
    output_path: str | Path,
    timezone_offset_hours: float = 0,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    sections: dict[str, bool] | None = None,
    top_n: int = 20,
) -> Path:
    """Generate a forensic HTML report for a group conversation.

    Args:
        analysis_db_path: Path to analysis.db.
        conversation_id: The group conversation ID.
        output_path: Where to write the HTML file.
        timezone_offset_hours: Timezone offset from UTC for timestamps.
        date_from_ms: Optional Unix-ms lower bound — when set, message-derived
            statistics (top contributors, mentions, activity patterns, calls,
            locations, media, links, type breakdown, forwarders, devices,
            admin events) are restricted to this start timestamp inclusive.
            Roster-level data (members, past members, group identity & edit
            history) is always shown for context regardless of the window.
        date_to_ms: Optional Unix-ms upper bound (inclusive).

    Returns:
        Path to the generated HTML file.
    """
    output_path = Path(output_path)
    conn = sqlite3.connect(str(analysis_db_path))
    conn.row_factory = sqlite3.Row

    try:
        # ---- Gather all data ----
        group = _get_group_info(conn, conversation_id)
        members = _get_members(conn, conversation_id, date_from_ms, date_to_ms)
        past_members = _get_past_members(conn, conversation_id)
        edit_history = _get_edit_history(conn, conversation_id)
        mention_network = _get_mention_network(conn, conversation_id, date_from_ms, date_to_ms)
        top_mentioned = _get_top_mentioned(conn, conversation_id, date_from_ms, date_to_ms)
        top_mentioners = _get_top_mentioners(conn, conversation_id, date_from_ms, date_to_ms)
        hourly_activity = _get_hourly_activity(conn, conversation_id, date_from_ms, date_to_ms)
        daily_activity = _get_daily_activity(conn, conversation_id, date_from_ms, date_to_ms)
        admin_events = _get_admin_events(conn, conversation_id, date_from_ms, date_to_ms)
        media_stats = _get_media_stats(conn, conversation_id, date_from_ms, date_to_ms)
        link_domains = _get_top_link_domains(conn, conversation_id, date_from_ms, date_to_ms)
        message_type_stats = _get_message_type_stats(conn, conversation_id, date_from_ms, date_to_ms)

        device_stats = _get_device_platform_stats(conn, conversation_id, date_from_ms, date_to_ms)
        call_stats = _get_call_stats(conn, conversation_id, date_from_ms, date_to_ms)
        location_stats = _get_location_stats(conn, conversation_id, date_from_ms, date_to_ms)
        top_forwarders = _get_top_forwarders(conn, conversation_id, date_from_ms, date_to_ms)
        bot_activity = _get_bot_activity(conn, conversation_id, date_from_ms, date_to_ms)

        # ---- Build HTML ----
        date_range_str = _format_date_range(date_from_ms, date_to_ms)
        # Section toggle map — when None, include everything.  Each
        # _section_* call is gated by the matching key so toggling a
        # section off in the dialog completely omits it (no empty
        # placeholder card).
        secs = sections or {
            k: True for k in (
                "identity", "owner_policy", "summary", "edit_history",
                "members", "contributors", "forwarders", "devices",
                "mentions", "activity", "calls", "locations",
                "admin_audit", "media_links", "bot_activity", "past_members",
            )
        }

        out_sections: list[str] = []
        nav_items: list[tuple[str, str]] = []  # (anchor_id, label)

        def _add(key: str, anchor: str, label: str, html_str: str) -> None:
            if secs.get(key, True) and html_str:
                out_sections.append(html_str)
                nav_items.append((anchor, label))

        _add("identity",     "identity",     "Identity",
             _section_group_identity(group))
        _add("owner_policy", "owner-policy", "Owner & Policy",
             _section_owner_and_policy(conn, conversation_id, group))
        _add("summary",      "summary",      "Summary",
             _section_stats_summary(group, members, media_stats, conn,
                                    conversation_id, date_from_ms, date_to_ms))
        _add("edit_history", "edit-history", "Edit History",
             _section_edit_history(edit_history))
        # Resolve owner identity ONCE up-front so every section that
        # joins to ``contact`` and may surface the owner as "Unknown"
        # (sender_id NULL on from_me messages) can name/JID-stamp the
        # owner row consistently.
        _owner_meta = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM case_metadata "
                "WHERE key IN ('device_owner_name','device_owner_phone',"
                "              'device_owner_jid','device_owner_lid_jid')"
            ).fetchall()
        }
        _owner_phone_kv = (_owner_meta.get("device_owner_phone") or "").replace("@s.whatsapp.net", "")
        _owner_jid_kv = _owner_meta.get("device_owner_jid") or (
            f"{_owner_phone_kv}@s.whatsapp.net" if _owner_phone_kv else ""
        )
        _owner_name_kv = _owner_meta.get("device_owner_name") or ""
        _owner_lid_kv = _owner_meta.get("device_owner_lid_jid") or ""

        _add("members",      "members",      "Members",
             _section_members(members, group))
        _add("contributors", "contributors", f"Top {top_n} Contributors",
             _section_top_contributors(members, top_n=top_n))
        _add("forwarders",   "forwarders",   f"Top {top_n} Forwarders",
             _section_top_forwarders(top_forwarders, top_n=top_n))
        _add("devices",      "devices",      "Devices",
             _section_device_platforms(device_stats,
                                       owner_name=_owner_name_kv,
                                       owner_jid=_owner_jid_kv))
        _add("mentions",     "mentions",     "Mentions",
             _section_mention_network(mention_network, top_mentioned,
                                      top_mentioners, top_n=top_n,
                                      owner_name=_owner_name_kv,
                                      owner_jid=_owner_jid_kv,
                                      owner_lid=_owner_lid_kv))
        _add("activity",     "activity",     "Activity",
             _section_activity_patterns(hourly_activity, daily_activity))
        _add("calls",        "calls",        "Calls",
             _section_calls(call_stats, has_filter=bool(date_from_ms or date_to_ms)))
        _add("locations",    "locations",    "Locations",
             _section_locations(location_stats,
                                has_filter=bool(date_from_ms or date_to_ms),
                                owner_name=_owner_name_kv,
                                owner_jid=_owner_jid_kv,
                                owner_phone=_owner_phone_kv))
        _add("admin_audit",  "admin-audit",  "Admin Audit",
             _section_admin_audit(admin_events))
        _add("media_links",  "media-links",  "Media & Links",
             _section_media_links(media_stats, link_domains,
                                  message_type_stats, top_n=top_n))
        _add("bot_activity", "bot-activity", "Bot Activity",
             _section_bot_activity(bot_activity,
                                   has_filter=bool(date_from_ms or date_to_ms),
                                   top_n=top_n))
        _owner = _resolve_device_owner(conn)
        _add("past_members", "past-members", "Former Members",
             _section_past_members(past_members, _owner))

        case_info = _collect_case_info(conn, analysis_db_path)
        html_content = _wrap_html(
            title=f"Group Report — {group['display_name']}",
            group_name=group['display_name'],
            sections=out_sections,
            nav_items=nav_items,
            generated_at=_ts(int(datetime.now().timestamp() * 1000)),
            case_info=case_info,
            date_range_str=date_range_str,
        )

        output_path.write_text(html_content, encoding="utf-8")
        return output_path

    finally:
        conn.close()


def _date_filter_clause(prefix: str, date_from_ms: int | None,
                        date_to_ms: int | None) -> tuple[str, list]:
    """Return ('AND <prefix>.timestamp BETWEEN ? AND ?', [from_ms, to_ms])
    style fragments to splice into the message-derived queries.

    Returns ('', []) when both bounds are None so callers can append
    unconditionally.  ``prefix`` is the SQL alias of the table whose
    ``timestamp`` column the filter applies to (e.g. ``m`` for the
    message table, ``cr`` for call_record, etc.).
    """
    parts: list[str] = []
    params: list = []
    if date_from_ms is not None:
        parts.append(f" AND {prefix}.timestamp >= ?")
        params.append(int(date_from_ms))
    if date_to_ms is not None:
        parts.append(f" AND {prefix}.timestamp <= ?")
        params.append(int(date_to_ms))
    return ("".join(parts), params)


def _format_date_range(date_from_ms: int | None, date_to_ms: int | None) -> str:
    """Build a 'From: X — To: Y' display string for the report header."""
    if date_from_ms is None and date_to_ms is None:
        return ""
    parts = []
    if date_from_ms is not None:
        try:
            parts.append("From " + datetime.fromtimestamp(int(date_from_ms) / 1000).astimezone().strftime("%Y-%m-%d %H:%M"))
        except (ValueError, OSError):
            pass
    if date_to_ms is not None:
        try:
            parts.append("to " + datetime.fromtimestamp(int(date_to_ms) / 1000).astimezone().strftime("%Y-%m-%d %H:%M"))
        except (ValueError, OSError):
            pass
    return " ".join(parts) if parts else ""


# ====================================================================== #
# Data queries
# ====================================================================== #

def _collect_case_info(conn: sqlite3.Connection, analysis_db_path) -> dict:
    """Pull case/examiner/source metadata for the report header.

    Sources (in order of preference):
      1. metadata.json next to analysis.db      — examiner, case_id, notes
      2. case_metadata table inside analysis.db — source paths + hashes
    """
    info: dict = {
        "case_id": "",
        "examiner": "",
        "notes": "",
        "created": "",
        "source_paths": {},
        "hashes": {},
    }
    # metadata.json (GUI-side CaseManager writes this)
    try:
        from pathlib import Path
        import json
        meta_path = Path(analysis_db_path).parent / "metadata.json"
        if meta_path.exists():
            m = json.loads(meta_path.read_text(encoding="utf-8"))
            info["case_id"] = str(m.get("case_id") or "")
            info["examiner"] = str(m.get("examiner") or "")
            info["notes"] = str(m.get("notes") or "")
            info["created"] = str(m.get("created") or "")
            sp = m.get("source_paths") or {}
            # Handle both {"analysis_db": path, "databases": {name:{path,size,sha256}}}
            # and flat styles.
            if isinstance(sp, dict):
                info["source_paths"]["databases_dir"] = sp.get("databases_dir") or ""
                info["source_paths"]["analysis_db"] = sp.get("analysis_db") or str(analysis_db_path)
                dbs = sp.get("databases") or {}
                if isinstance(dbs, dict):
                    info["source_paths"]["databases"] = dbs
    except Exception:
        pass
    # case_metadata (ingester writes source paths + hashes + sizes here).
    # Compose a full per-database manifest so the banner can show, for
    # every source DB, its on-disk path, size, and SHA-256 — exactly
    # what the analyst needs for chain-of-custody verification.
    try:
        rows = conn.execute(
            "SELECT key, value FROM case_metadata "
            "WHERE key LIKE 'source_hash_%' OR key LIKE 'source_path_%' "
            "   OR key LIKE 'source_size_%' OR key = 'source_databases_dir' "
            "   OR key = 'hash_timestamp'"
        ).fetchall()
        # Bucket per-name details into the existing source_paths map.
        per_db: dict[str, dict] = info["source_paths"].setdefault("databases", {}) \
            if isinstance(info["source_paths"].get("databases"), dict) else {}
        info["source_paths"]["databases"] = per_db
        for k, v in rows:
            if k.startswith("source_hash_"):
                name = k[len("source_hash_"):]
                info["hashes"][name] = v
                per_db.setdefault(name, {})["sha256"] = v
            elif k.startswith("source_path_"):
                name = k[len("source_path_"):]
                per_db.setdefault(name, {})["path"] = v
            elif k.startswith("source_size_"):
                name = k[len("source_size_"):]
                try:
                    per_db.setdefault(name, {})["size_bytes"] = int(v)
                except (TypeError, ValueError):
                    per_db.setdefault(name, {})["size_bytes"] = v
            elif k == "source_databases_dir":
                info["source_paths"].setdefault("databases_dir", v)
    except Exception as e:
        print(f"[group_report] _collect_case_info case_metadata failed: {e}")

    # Make sure the analysis DB path is always recorded too.
    info["source_paths"].setdefault("analysis_db", str(analysis_db_path))
    return info


def _render_case_banner(ci: dict) -> str:
    """Render a prominent Case / Examiner banner at the top of the
    report.

    The banner sits inside ``.report-header`` which has
    ``color: white`` for the rest of the header.  Every text element
    here is given explicit dark colors so it stays readable against
    the cream-yellow background \u2014 without the explicit colors the
    inherited white-on-yellow rendered as invisible text in the
    previous version.
    """
    if not ci:
        return ""
    case_id  = ci.get("case_id") or ""
    examiner = ci.get("examiner") or ""
    notes    = ci.get("notes") or ""
    created  = ci.get("created") or ""
    sp       = ci.get("source_paths") or {}
    hashes   = ci.get("hashes") or {}

    # Colors that are forensically readable in BOTH on-screen HTML
    # AND printed-PDF rendering of the cream banner.
    K_LBL = "#5d4037"   # warm brown for the row labels
    K_VAL = "#1b1b1b"   # near-black for values
    K_DIM = "#6d4c41"   # secondary detail
    K_CODE_BG = "#fff3e0"
    K_CODE_FG = "#bf360c"

    def _row(label: str, value_html: str) -> str:
        return (
            f"<tr>"
            f"<td style='color:{K_LBL};font-weight:600;width:160px;'>{_h(label)}</td>"
            f"<td style='color:{K_VAL};'>{value_html}</td>"
            f"</tr>"
        )

    rows: list[str] = []
    if case_id:
        rows.append(_row("Case ID", f"<strong>{_h(case_id)}</strong>"))
    if examiner:
        rows.append(_row("Examiner", f"<strong>{_h(examiner)}</strong>"))
    if created:
        rows.append(_row("Case Created", _h(created)))
    if notes:
        rows.append(_row("Case Notes", _h(notes)))
    db_dir = sp.get("databases_dir") or ""
    if db_dir:
        rows.append(_row(
            "Source Directory",
            f"<code style='background:{K_CODE_BG};color:{K_CODE_FG};"
            f"padding:1px 4px;border-radius:3px;font-size:11px;'>{_h(db_dir)}</code>",
        ))
    analysis_db = sp.get("analysis_db") or ""
    if analysis_db:
        rows.append(_row(
            "Analysis DB",
            f"<code style='background:{K_CODE_BG};color:{K_CODE_FG};"
            f"padding:1px 4px;border-radius:3px;font-size:11px;'>{_h(analysis_db)}</code>",
        ))

    # Per-file source rows (path, size, sha256) \u2014 every byte of the
    # source DBs surfaced so the analyst can verify their copy.
    dbs = sp.get("databases") or {}
    if isinstance(dbs, dict) and dbs:
        lines = []
        for name in sorted(dbs.keys()):
            info = dbs.get(name) or {}
            if not isinstance(info, dict):
                continue
            size = info.get("size_bytes")
            size_str = f"{size:,} bytes" if isinstance(size, int) else ""
            sha = info.get("sha256") or hashes.get(name) or ""
            path = info.get("path") or ""
            line_parts = [
                f"<div style='margin:6px 0;padding:6px 8px;background:#fffaf0;"
                f"border:1px solid #ffe0b2;border-radius:4px;"
                f"font-family:Consolas,monospace;font-size:11px;color:{K_VAL};'>",
                f"<b style='color:{K_LBL};font-size:12px;'>{_h(name)}</b>",
            ]
            if size_str:
                line_parts.append(f" <span style='color:{K_DIM};'>\u00b7 {_h(size_str)}</span>")
            if path:
                line_parts.append(
                    f"<br><span style='color:{K_VAL};'>"
                    f"<b>path</b>: <code style='color:{K_CODE_FG};'>{_h(path)}</code>"
                    f"</span>"
                )
            if sha:
                line_parts.append(
                    f"<br><span style='color:{K_VAL};'>"
                    f"<b>sha256</b>: <code style='color:{K_CODE_FG};word-break:break-all;'>"
                    f"{_h(sha)}</code></span>"
                )
            line_parts.append("</div>")
            lines.append("".join(line_parts))
        if lines:
            rows.append(_row("Source Databases", "".join(lines)))
    elif hashes:
        # Fallback: just show hashes if source-paths section isn't populated.
        lines = []
        for name in sorted(hashes.keys()):
            sha = hashes[name]
            lines.append(
                f"<div style='margin:4px 0;padding:6px 8px;background:#fffaf0;"
                f"border:1px solid #ffe0b2;border-radius:4px;"
                f"font-family:Consolas,monospace;font-size:11px;color:{K_VAL};'>"
                f"<b style='color:{K_LBL};'>{_h(name)}</b><br>"
                f"<b>sha256</b>: <code style='color:{K_CODE_FG};word-break:break-all;'>"
                f"{_h(sha)}</code>"
                f"</div>"
            )
        if lines:
            rows.append(_row("Source Hashes", "".join(lines)))

    if not rows:
        return ""

    return (
        "<div style='background:#fffde7; border:1px solid #fff59d; "
        "border-left:6px solid #f57f17; padding:12px 16px; margin:0 0 12px 0; "
        f"border-radius:6px; color:{K_VAL};'>"
        "<div style='font-size:13px; font-weight:700; color:#e65100; "
        "margin-bottom:8px;'>\U0001F4CB CASE &amp; EVIDENCE PROVENANCE</div>"
        "<table class='info-table' style='margin:0;background:transparent;width:100%;'>"
        + "".join(rows)
        + "</table>"
        "</div>"
    )


def _resolve_device_owner(conn: sqlite3.Connection) -> int | None:
    """Resolve the device owner's contact_id from case_metadata."""
    try:
        row = conn.execute("SELECT value FROM case_metadata WHERE key = 'device_owner_contact_id'").fetchone()
        if row and row[0]:
            return int(row[0])
        row = conn.execute("SELECT value FROM case_metadata WHERE key = 'device_owner_jid'").fetchone()
        if row and row[0]:
            cr = conn.execute("SELECT id FROM contact WHERE phone_jid = ?", (row[0],)).fetchone()
            if cr:
                return cr[0]
        row = conn.execute("SELECT value FROM case_metadata WHERE key = 'device_owner_phone'").fetchone()
        if row and row[0]:
            cr = conn.execute("SELECT id FROM contact WHERE phone_number = ?", (row[0],)).fetchone()
            if cr:
                return cr[0]
        row = conn.execute("""
            SELECT m.sender_id FROM message m
            WHERE m.from_me = 1 AND m.sender_id IS NOT NULL AND m.message_type != 7
            LIMIT 1
        """).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return None


def _get_group_info(conn: sqlite3.Connection, conv_id: int) -> dict:
    # Defensively detect whether the newer forensic columns
    # exist — older analysis.db files were ingested before these
    # columns were added.
    conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversation)").fetchall()}
    extra_cols = [
        c for c in ("participation_status", "announcement_group",
                    "restrict_mode", "require_membership_approval",
                    "member_add_mode", "creator_jid_raw")
        if c in conv_cols
    ]
    extra_sql = ("," + ",".join(f"c.{x}" for x in extra_cols)) if extra_cols else ""

    row = conn.execute(f"""
        SELECT c.id, c.display_name, c.jid_raw_string, c.subject, c.description,
               c.chat_type, c.group_type, c.addressing_mode,
               c.created_timestamp, c.participant_count, c.message_count,
               c.media_count, c.ephemeral_duration,
               c.is_archived, c.is_pinned, c.is_muted, c.is_locked,
               c.first_message_ts, c.last_message_ts, c.avatar_blob,
               c.community_parent_id, c.source_chat_id{extra_sql}
        FROM conversation c WHERE c.id = ?
    """, (conv_id,)).fetchone()
    if not row:
        return {"display_name": f"Group #{conv_id}", "id": conv_id}

    d = dict(row)

    # Find creator
    creator = conn.execute("""
        SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
               c.phone_number, c.phone_jid, c.lid_jid
        FROM group_member gm
        JOIN contact c ON c.id = gm.contact_id
        WHERE gm.conversation_id = ? AND gm.role = 'superadmin'
        LIMIT 1
    """, (conv_id,)).fetchone()
    if creator:
        d["creator_name"] = creator["name"] or "Unknown"
        d["creator_phone"] = creator["phone_number"] or ""
        d["creator_jid"] = creator["phone_jid"] or ""
        d["creator_lid"] = creator["lid_jid"] or ""
    else:
        # Fallback: extract creator phone from group JID for older groups
        # Format: 15551234567-1419532254@g.us where the number before '-' is the creator
        jid = d.get("jid_raw_string") or ""
        if "@g.us" in jid:
            prefix = jid.split("@")[0]
            if "-" in prefix:
                creator_phone = prefix.split("-")[0]
                # Try to resolve this phone number to a contact
                cr = conn.execute("""
                    SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
                           c.phone_number, c.phone_jid, c.lid_jid
                    FROM contact c
                    WHERE c.phone_number = ? OR c.phone_jid = ?
                    LIMIT 1
                """, (creator_phone, f"{creator_phone}@s.whatsapp.net")).fetchone()
                if cr:
                    d["creator_name"] = cr["name"] or f"+{creator_phone}"
                    d["creator_phone"] = cr["phone_number"] or creator_phone
                    d["creator_jid"] = cr["phone_jid"] or f"{creator_phone}@s.whatsapp.net"
                    d["creator_lid"] = cr["lid_jid"] or ""
                else:
                    d["creator_name"] = f"+{creator_phone}"
                    d["creator_phone"] = creator_phone
                    d["creator_jid"] = f"{creator_phone}@s.whatsapp.net"
                    d["creator_lid"] = ""
            else:
                d["creator_name"] = "Unknown"
        else:
            d["creator_name"] = "Unknown"

    # Community parent name
    if d.get("community_parent_id"):
        parent = conn.execute(
            "SELECT display_name FROM conversation WHERE id = ?",
            (d["community_parent_id"],),
        ).fetchone()
        d["community_parent_name"] = parent["display_name"] if parent else None

    return d


def _get_members(conn: sqlite3.Connection, conv_id: int,
                 date_from_ms: int | None = None,
                 date_to_ms: int | None = None) -> list[dict]:
    """Roster of every contact who counts as a participant in this group.

    WhatsApp's own ``group_member`` table EXCLUDES the device owner
    (the owner is implicit — the database is theirs).  Without an
    explicit synth row for the owner, every downstream display
    treats them as missing — they don't appear in Members, Top
    Contributors, etc., even though they sent dozens of messages
    (``from_me = 1, sender_id IS NULL``).

    This function:
      * loads everyone in ``group_member`` (the explicit roster), and
      * appends a synthetic owner row when the device owner has any
        evidence of group membership (``participation_status`` 2/3/4
        OR at least one ``from_me = 1`` message in this chat).
    """
    rows = conn.execute("""
        SELECT gm.contact_id, gm.role, gm.label, gm.join_timestamp, gm.join_method,
               c.resolved_name, c.wa_name, c.phone_number, c.phone_jid, c.lid_jid,
               c.display_name AS c_display, c.avatar_blob,
               sca.total_messages, sca.total_text, sca.total_media,
               sca.total_images, sca.total_videos, sca.total_audio,
               sca.total_documents, sca.total_stickers, sca.total_gifs,
               sca.total_links, sca.total_reactions_given, sca.total_reactions_received,
               sca.total_mentions, sca.total_forwards, sca.total_edits, sca.total_deletes,
               sca.first_message_ts, sca.last_message_ts
        FROM group_member gm
        JOIN contact c ON c.id = gm.contact_id
        LEFT JOIN stats_contact_activity sca
            ON sca.contact_id = gm.contact_id AND sca.conversation_id = ?
        WHERE gm.conversation_id = ?
        ORDER BY
            CASE gm.role WHEN 'superadmin' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
            sca.total_messages DESC NULLS LAST
    """, (conv_id, conv_id)).fetchall()
    members = [dict(r) for r in rows]

    # ── Synthetic owner row ──
    owner_cid = _resolve_device_owner(conn)
    member_cids = {m["contact_id"] for m in members}
    if owner_cid and owner_cid not in member_cids:
        # Owner identity from case_metadata.
        meta_kv = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM case_metadata "
                "WHERE key IN ('device_owner_name','device_owner_phone',"
                "              'device_owner_jid','device_owner_lid_jid')"
            ).fetchall()
        }
        owner_phone = (meta_kv.get("device_owner_phone") or "")\
                        .replace("@s.whatsapp.net", "")
        owner_jid = meta_kv.get("device_owner_jid") or (
            f"{owner_phone}@s.whatsapp.net" if owner_phone else None
        )
        owner_lid = meta_kv.get("device_owner_lid_jid")
        owner_name = meta_kv.get("device_owner_name") or "Device Owner"

        # Owner role from chat.participation_status (2=member,
        # 3=admin, 4=creator).  Also captures the
        # member-status / admin / creator flags the renderer uses.
        try:
            ps_row = conn.execute(
                "SELECT participation_status FROM conversation WHERE id = ?",
                (conv_id,),
            ).fetchone()
            ps = int(ps_row[0]) if ps_row and ps_row[0] is not None else None
        except Exception:
            ps = None
        role = (
            "creator" if ps == 4 else
            "admin"   if ps == 3 else
            "member"  if ps == 2 else
            None
        )

        # Owner's message count for THIS conversation —
        # `from_me = 1, sender_id IS NULL` is how WhatsApp stores
        # owner-sent messages in groups, plus any rows that resolved
        # to the owner contact_id directly.
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n,"
                "       MIN(timestamp) AS first_ts,"
                "       MAX(timestamp) AS last_ts "
                "FROM message "
                "WHERE conversation_id = ? AND message_type != 7 "
                "  AND ((from_me = 1 AND sender_id IS NULL)"
                "       OR sender_id = ?)",
                (conv_id, owner_cid),
            ).fetchone()
            owner_msgs = int(row["n"] or 0)
            owner_first = row["first_ts"]
            owner_last = row["last_ts"]
        except Exception:
            owner_msgs = 0
            owner_first = None
            owner_last = None

        # Skip injection if there is genuinely no evidence the owner
        # was ever in this group (no participation_status entry AND
        # zero from_me messages).
        if role is not None or owner_msgs > 0:
            members.append({
                "contact_id": owner_cid,
                "role": role or "member",
                "label": "Device Owner",
                "join_timestamp": None,
                "join_method": None,
                "resolved_name": owner_name,
                "wa_name": None,
                "phone_number": owner_phone,
                "phone_jid": owner_jid,
                "lid_jid": owner_lid,
                "c_display": owner_name,
                "avatar_blob": None,
                "total_messages": owner_msgs,
                "total_text": None, "total_media": None,
                "total_images": None, "total_videos": None,
                "total_audio": None, "total_documents": None,
                "total_stickers": None, "total_gifs": None,
                "total_links": None, "total_reactions_given": None,
                "total_reactions_received": None, "total_mentions": None,
                "total_forwards": None, "total_edits": None,
                "total_deletes": None,
                "first_message_ts": owner_first,
                "last_message_ts": owner_last,
                "_is_owner": True,
            })
            # Re-sort so owner lands by role + msg count.
            role_rank = {"creator": -1, "superadmin": 0, "admin": 1, "member": 2}
            members.sort(key=lambda m: (
                role_rank.get((m.get("role") or "member"), 2),
                -(m.get("total_messages") or 0),
            ))

    return members


def _is_bot_contact_filter() -> str:
    """Return a SQL fragment that matches bot contacts so they can be
    excluded from the human roster sections.

    A "bot" here is anything that:
      * has ``business_name = 'Meta AI'``, or
      * has ``resolved_name`` / ``wa_name`` carrying "Meta AI"
        (Meta's canonical contact row in many DBs has
        ``business_name = NULL`` and the name only resolved on
        ``resolved_name``), or
      * lives on the ``@bot`` JID server
        (``phone_jid LIKE '%@bot'``), or
      * has a phone number in Meta's bot block (``1313555xxxx``).

    NULL-safe: every column gets a ``COALESCE(..., '')`` wrapper so
    plain NULL columns don't poison the OR chain via SQLite's
    three-valued logic.  Without the wrappers, a contact with NULL
    ``business_name`` + NULL ``wa_name`` (the common case for plain
    phone-number-only contacts) made the whole OR evaluate to NULL,
    and ``NOT NULL`` then excluded the row from any WHERE clause that
    used ``NOT _is_bot_contact_filter()`` — which silently dropped
    every former-member, former-mentioner, etc. with a stripped-down
    contact row.  Reproduced on conv 4 (DealBotz Hyderabad), where
    the table had 11 ``group_past_participant`` rows but the report
    rendered 0 former members.
    """
    return (
        "(COALESCE(c.business_name, '') = 'Meta AI'"
        " OR COALESCE(c.resolved_name, '') LIKE '%Meta AI%'"
        " OR COALESCE(c.wa_name, '') LIKE '%Meta AI%'"
        " OR COALESCE(c.phone_jid, '') LIKE '%@bot'"
        " OR (c.phone_number IS NOT NULL AND c.phone_number GLOB '1313555[0-9][0-9][0-9][0-9]'))"
    )


def _get_past_members(conn: sqlite3.Connection, conv_id: int) -> list[dict]:
    """Resolve former members from THREE sources, matching the GUI:

      1. ``group_past_participant`` — WhatsApp's authoritative
         left/removed list (state column = type of departure).
      2. ``group_member`` rows with ``is_current = 0`` — members the
         tool's ingestion saw join then leave (carries
         ``left_timestamp``).
      3. Message senders who aren't in either of the above —
         contacts who sent messages in this group but no longer
         appear in the roster (state = -2 "found via messages").

    The previous implementation used only #1 + #3 AND incorrectly
    filtered #3 against ALL ``group_member`` rows (not just
    ``is_current = 1``), so #2 contacts disappeared from the report
    even though the in-app group screen showed them.  This brought
    the report to parity with the GUI's "Former Members" panel.

    Bots (Meta AI etc.) are excluded throughout so they never pollute
    the human roster — they have their own Bot Activity section.
    """
    bot_filter = _is_bot_contact_filter()

    # ---- 1. group_past_participant ---------------------------------- #
    # Exclude anyone who is currently a member (left then rejoined).
    rows = conn.execute(f"""
        SELECT DISTINCT c.id AS contact_id,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number, c.phone_jid) AS name,
               c.phone_number, c.phone_jid, c.lid_jid,
               gpp.state, gpp.last_seen_ts,
               sca.total_messages, sca.total_media, sca.total_links
        FROM group_past_participant gpp
        JOIN contact c ON c.id = gpp.contact_id
        LEFT JOIN stats_contact_activity sca
            ON sca.contact_id = gpp.contact_id AND sca.conversation_id = ?
        WHERE gpp.conversation_id = ?
          AND gpp.contact_id NOT IN (
              SELECT gm.contact_id FROM group_member gm
              WHERE gm.conversation_id = ? AND gm.is_current = 1
          )
          AND NOT {bot_filter}
        ORDER BY gpp.last_seen_ts DESC
    """, (conv_id, conv_id, conv_id)).fetchall()
    result = [dict(r) for r in rows]
    known_ids = {r["contact_id"] for r in result}

    # ---- 2. group_member.is_current = 0 ---------------------------- #
    # Members the tool saw leave (and they're not in past_participant
    # — that pair is already covered by #1).
    left_rows = conn.execute(f"""
        SELECT DISTINCT c.id AS contact_id,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number, c.phone_jid) AS name,
               c.phone_number, c.phone_jid, c.lid_jid,
               -1 AS state, gm.left_timestamp AS last_seen_ts,
               sca.total_messages, sca.total_media, sca.total_links
        FROM group_member gm
        JOIN contact c ON c.id = gm.contact_id
        LEFT JOIN stats_contact_activity sca
            ON sca.contact_id = gm.contact_id AND sca.conversation_id = ?
        WHERE gm.conversation_id = ? AND gm.is_current = 0
          AND gm.contact_id NOT IN (
              SELECT gpp.contact_id FROM group_past_participant gpp
              WHERE gpp.conversation_id = ?
          )
          AND NOT {bot_filter}
        ORDER BY gm.left_timestamp DESC
    """, (conv_id, conv_id, conv_id)).fetchall()
    for r in left_rows:
        d = dict(r)
        if d["contact_id"] not in known_ids:
            result.append(d)
            known_ids.add(d["contact_id"])

    # ---- 3. Message senders not in any roster table ---------------- #
    # CRITICAL: filter against is_current = 1 only.  The previous
    # implementation filtered against ALL group_member rows, which
    # silently excluded the is_current=0 left-members from this
    # fallback path too — they vanished entirely.
    current_member_ids = {
        r[0] for r in conn.execute(
            "SELECT contact_id FROM group_member "
            "WHERE conversation_id = ? AND is_current = 1",
            (conv_id,)
        ).fetchall()
    }
    msg_only = conn.execute(f"""
        SELECT DISTINCT c.id AS contact_id,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number, c.phone_jid) AS name,
               c.phone_number, c.phone_jid, c.lid_jid,
               -2 AS state, MAX(m.timestamp) AS last_seen_ts,
               COUNT(*) AS total_messages, 0 AS total_media, 0 AS total_links
        FROM message m
        JOIN contact c ON c.id = m.sender_id
        WHERE m.conversation_id = ? AND m.sender_id IS NOT NULL AND m.message_type != 7
          AND NOT {bot_filter}
        GROUP BY m.sender_id
        ORDER BY MAX(m.timestamp) DESC
    """, (conv_id,)).fetchall()
    for r in msg_only:
        d = dict(r)
        cid = d["contact_id"]
        if cid not in known_ids and cid not in current_member_ids:
            result.append(d)
            known_ids.add(cid)

    return result


def _get_bot_activity(conn: sqlite3.Connection, conv_id: int,
                      date_from_ms: int | None = None,
                      date_to_ms: int | None = None) -> dict:
    """Return per-bot stats + Top-N humans who @-mention each bot.

    Returns a dict with two keys::

        {
          "bots":       [ {contact_id, name, business_name, phone,
                           phone_jid, total_messages, first_msg_ts,
                           last_msg_ts, is_meta_ai}, ... ],
          "summoners":  [ {bot_contact_id, bot_name, summoner_id,
                           summoner_name, summoner_jid, mention_count}, ... ]
        }

    Bots are everything matching :func:`_is_bot_contact_filter`.
    Summoners are humans whose messages contained an @-mention of one
    of those bot contacts (i.e. ``mention.mentioned_id`` points at a
    bot).  Date range applies to the message-level filters in both
    cases so the bot interaction window matches the rest of the report.
    """
    bot_filter = _is_bot_contact_filter()
    ext, eparams = _date_filter_clause("m", date_from_ms, date_to_ms)
    bots = conn.execute(f"""
        SELECT c.id AS contact_id,
               COALESCE(c.resolved_name, c.wa_name, c.business_name, c.phone_number) AS name,
               c.phone_number, c.phone_jid, c.lid_jid, c.business_name,
               COUNT(*) AS total_messages,
               MIN(m.timestamp) AS first_msg_ts,
               MAX(m.timestamp) AS last_msg_ts,
               CASE WHEN c.business_name = 'Meta AI'
                         OR c.resolved_name LIKE '%Meta AI%'
                         OR c.wa_name LIKE '%Meta AI%'
                    THEN 1 ELSE 0 END AS is_meta_ai
        FROM message m
        JOIN contact c ON c.id = m.sender_id
        WHERE m.conversation_id = ? AND m.message_type != 7 AND {bot_filter}{ext}
        GROUP BY c.id
        ORDER BY total_messages DESC
    """, (conv_id, *eparams)).fetchall()
    bots_list = [dict(r) for r in bots]

    # Top summoners — humans who @-mention each bot.  Bot identity
    # match uses the SAME unified filter as everywhere else (so a bot
    # whose ``business_name`` is NULL but ``resolved_name`` says
    # "Meta AI" still gets matched).  Sender exclusion strips bot
    # @-replies that include the user back so the human ranking
    # stays clean.
    bot_filter_target = _is_bot_contact_filter().replace("c.", "tc.")
    summ = conn.execute(f"""
        SELECT mn.mentioned_id AS bot_contact_id,
               COALESCE(tc.resolved_name, tc.wa_name, tc.business_name, tc.phone_number) AS bot_name,
               m.sender_id AS summoner_id,
               COALESCE(sc.resolved_name, sc.wa_name, sc.phone_number) AS summoner_name,
               sc.phone_jid AS summoner_jid,
               sc.lid_jid AS summoner_lid,
               COUNT(*) AS mention_count
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        JOIN contact tc ON tc.id = mn.mentioned_id
        LEFT JOIN contact sc ON sc.id = m.sender_id
        WHERE m.conversation_id = ?
          AND mn.mentioned_id IS NOT NULL
          AND {bot_filter_target}
          {_bot_exclude_for_alias("sc")}{ext}
        GROUP BY mn.mentioned_id, m.sender_id
        ORDER BY mention_count DESC
        LIMIT 100
    """, (conv_id, *eparams)).fetchall()
    return {"bots": bots_list, "summoners": [dict(r) for r in summ]}


def _get_edit_history(conn: sqlite3.Connection, conv_id: int) -> list[dict]:
    # Check if table exists
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='group_metadata_change'"
    ).fetchone()
    if not has_table:
        return []

    # Detect device owner for badge display
    owner_cid = _resolve_device_owner(conn)

    rows = conn.execute("""
        SELECT gmc.change_type, gmc.old_value, gmc.new_value,
               gmc.old_photo, gmc.new_photo,
               gmc.changed_by_id,
               gmc.source_msg_id, gmc.action_type, gmc.timestamp,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS changer_name,
               c.phone_number AS changer_phone,
               c.phone_jid AS changer_jid,
               c.lid_jid AS changer_lid
        FROM group_metadata_change gmc
        LEFT JOIN contact c ON c.id = gmc.changed_by_id
        WHERE gmc.conversation_id = ?
        ORDER BY gmc.timestamp ASC
    """, (conv_id,)).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d["is_owner"] = (d.get("changed_by_id") == owner_cid) if owner_cid else False
        result.append(d)
    return result


def _bot_exclude_for_alias(alias: str) -> str:
    """Return a NULL-safe SQL fragment that excludes Meta AI / bot
    contacts for the given table alias.

    A contact is a bot when ANY of these hold:
      * ``business_name = 'Meta AI'``
      * ``resolved_name`` contains ``Meta AI``
      * ``wa_name`` contains ``Meta AI``
      * ``phone_jid`` lives on the ``@bot`` server (every Meta-issued
        chat-bot sits there regardless of how the resolver named it)
      * the phone number is in Meta's bot block (``1313555xxxx``)

    Each clause is wrapped with ``COALESCE(..., '')`` (or an
    explicit ``IS NULL OR …`` for phone_number) so a row with a NULL
    value still satisfies the predicate — without the wrap, SQLite's
    3-valued logic would silently drop every row whose contact join
    didn't populate that column (the symptom: empty Mention Network
    table even though there were dozens of mentions).
    """
    a = alias
    return (
        f" AND COALESCE({a}.business_name, '') != 'Meta AI'"
        f" AND COALESCE({a}.resolved_name, '') NOT LIKE '%Meta AI%'"
        f" AND COALESCE({a}.wa_name, '') NOT LIKE '%Meta AI%'"
        f" AND COALESCE({a}.phone_jid, '') NOT LIKE '%@bot'"
        f" AND ({a}.phone_number IS NULL"
        f"      OR {a}.phone_number NOT GLOB '1313555[0-9][0-9][0-9][0-9]')"
    )


_BOT_EXCLUDE_HUMAN = _bot_exclude_for_alias("sc") + _bot_exclude_for_alias("mc")
_BOT_EXCLUDE_TARGET_ONLY = _bot_exclude_for_alias("c")


def _get_mention_network(conn: sqlite3.Connection, conv_id: int,
                         date_from_ms: int | None = None,
                         date_to_ms: int | None = None) -> list[dict]:
    """Mentions grouped by (sender, mentioned).

    Includes ``m.from_me`` so the renderer can name-stamp owner-sent
    mentions with the device-owner identity (otherwise the ``LEFT JOIN
    contact`` returns NULL for owner messages — they have no
    sender_id — and the row surfaces blank).
    """
    rows = conn.execute("""
        SELECT m.sender_id,
               m.from_me,
               COALESCE(sc.resolved_name, sc.wa_name, sc.phone_number) AS sender_name,
               sc.phone_jid AS sender_jid,
               mn.mentioned_id,
               COALESCE(mc.resolved_name, mc.wa_name, mc.phone_number) AS mentioned_name,
               mc.phone_jid AS mentioned_jid,
               COUNT(*) AS cnt
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        LEFT JOIN contact sc ON sc.id = m.sender_id
        LEFT JOIN contact mc ON mc.id = mn.mentioned_id
        WHERE m.conversation_id = ? AND mn.mentioned_id IS NOT NULL{ext}{bot_excl}
        GROUP BY m.from_me, m.sender_id, mn.mentioned_id
        ORDER BY cnt DESC
        LIMIT 50
    """.format(
        ext=_date_filter_clause("m", date_from_ms, date_to_ms)[0],
        bot_excl=_BOT_EXCLUDE_HUMAN,
    ),
    (conv_id, *_date_filter_clause("m", date_from_ms, date_to_ms)[1])).fetchall()
    return [dict(r) for r in rows]


def _get_top_mentioned(conn: sqlite3.Connection, conv_id: int,
                       date_from_ms: int | None = None,
                       date_to_ms: int | None = None) -> list[dict]:
    rows = conn.execute("""
        SELECT mn.mentioned_id,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
               c.phone_jid, c.lid_jid,
               COUNT(*) AS mention_count
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        LEFT JOIN contact c ON c.id = mn.mentioned_id
        WHERE m.conversation_id = ? AND mn.mentioned_id IS NOT NULL{ext}{bot_excl}
        GROUP BY mn.mentioned_id
        ORDER BY mention_count DESC
        LIMIT 50
    """.format(
        ext=_date_filter_clause("m", date_from_ms, date_to_ms)[0],
        bot_excl=_BOT_EXCLUDE_TARGET_ONLY,
    ),
    (conv_id, *_date_filter_clause("m", date_from_ms, date_to_ms)[1])).fetchall()
    return [dict(r) for r in rows]


def _get_top_mentioners(conn: sqlite3.Connection, conv_id: int,
                        date_from_ms: int | None = None,
                        date_to_ms: int | None = None) -> list[dict]:
    # Exclude bot SENDERS — a bot @-reply to the human sometimes
    # carries an @-back which would otherwise pollute the human
    # mentioners ranking.  Uses the unified bot filter for consistency.
    # ``m.from_me`` is included so the renderer can name-stamp the
    # owner mentioner row.
    rows = conn.execute("""
        SELECT m.sender_id,
               m.from_me,
               COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
               c.phone_jid, c.lid_jid,
               COUNT(*) AS mention_count
        FROM mention mn
        JOIN message m ON m.id = mn.message_id
        LEFT JOIN contact c ON c.id = m.sender_id
        WHERE m.conversation_id = ? AND mn.mentioned_id IS NOT NULL{ext}{bot_excl}
        GROUP BY m.from_me, m.sender_id
        ORDER BY mention_count DESC
        LIMIT 50
    """.format(
        ext=_date_filter_clause("m", date_from_ms, date_to_ms)[0],
        bot_excl=_bot_exclude_for_alias("c"),
    ),
    (conv_id, *_date_filter_clause("m", date_from_ms, date_to_ms)[1])).fetchall()
    return [dict(r) for r in rows]


def _get_hourly_activity(conn: sqlite3.Connection, conv_id: int,
                         date_from_ms: int | None = None,
                         date_to_ms: int | None = None) -> list[tuple[int, int]]:
    extra, eparams = _date_filter_clause("message", date_from_ms, date_to_ms)
    rows = conn.execute(f"""
        SELECT CAST(strftime('%H', timestamp/1000, 'unixepoch') AS INTEGER) AS hour,
               COUNT(*) AS cnt
        FROM message
        WHERE conversation_id = ? AND message_type != 7{extra}
        GROUP BY hour ORDER BY hour
    """, (conv_id, *eparams)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_daily_activity(conn: sqlite3.Connection, conv_id: int,
                        date_from_ms: int | None = None,
                        date_to_ms: int | None = None) -> list[tuple[str, int]]:
    # Recompute when date range is set so the series respects bounds.
    if date_from_ms is not None or date_to_ms is not None:
        extra, eparams = _date_filter_clause("message", date_from_ms, date_to_ms)
        rows = conn.execute(f"""
            SELECT strftime('%Y-%m-%d', timestamp/1000, 'unixepoch') AS date_str,
                   COUNT(*) AS cnt
            FROM message
            WHERE conversation_id = ? AND message_type != 7{extra}
            GROUP BY date_str ORDER BY date_str
        """, (conv_id, *eparams)).fetchall()
        return [(r[0], r[1]) for r in rows]
    rows = conn.execute("""
        SELECT date_str, total_messages
        FROM stats_daily_activity
        WHERE conversation_id = ?
        ORDER BY date_str
    """, (conv_id,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _get_admin_events(conn: sqlite3.Connection, conv_id: int,
                      date_from_ms: int | None = None,
                      date_to_ms: int | None = None) -> list[dict]:
    ext, eparams = _date_filter_clause("se", date_from_ms, date_to_ms)
    rows = conn.execute(f"""
        SELECT se.event_label, se.timestamp, se.event_data,
               COALESCE(ac.resolved_name, ac.wa_name, ac.phone_number) AS actor_name,
               ac.phone_jid AS actor_jid,
               COALESCE(tc.resolved_name, tc.wa_name, tc.phone_number) AS target_name,
               tc.phone_jid AS target_jid
        FROM system_event se
        LEFT JOIN contact ac ON ac.id = se.actor_id
        LEFT JOIN contact tc ON tc.id = se.target_id
        WHERE se.conversation_id = ?{ext}
          AND se.event_label IN (
              'admin_promoted', 'admin_demoted',
              'participant_added', 'participant_removed',
              'participant_joined', 'participant_left',
              'group_created', 'subject_changed', 'icon_changed',
              'description_changed', 'admin_only_send_on', 'admin_only_send_off',
              'admin_only_edit_on', 'admin_only_edit_off',
              'disappearing_on', 'disappearing_off',
              'invite_link_reset', 'approval_mode_changed'
          )
        ORDER BY se.timestamp DESC
        LIMIT 200
    """, (conv_id, *eparams)).fetchall()
    return [dict(r) for r in rows]


def _get_media_stats(conn: sqlite3.Connection, conv_id: int,
                     date_from_ms: int | None = None,
                     date_to_ms: int | None = None) -> dict:
    row = conn.execute("""
        SELECT
            SUM(CASE WHEN m.message_type = 1 THEN 1 ELSE 0 END) AS images,
            SUM(CASE WHEN m.message_type = 3 THEN 1 ELSE 0 END) AS videos,
            SUM(CASE WHEN m.message_type = 2 THEN 1 ELSE 0 END) AS audio,
            SUM(CASE WHEN m.message_type = 9 THEN 1 ELSE 0 END) AS documents,
            SUM(CASE WHEN m.message_type = 20 THEN 1 ELSE 0 END) AS stickers,
            SUM(CASE WHEN m.message_type = 13 THEN 1 ELSE 0 END) AS gifs,
            SUM(CASE WHEN m.message_type IN (5, 16) THEN 1 ELSE 0 END) AS locations,
            SUM(CASE WHEN m.message_type = 4 THEN 1 ELSE 0 END) AS contacts_shared,
            COUNT(*) AS total
        FROM message m
        WHERE m.conversation_id = ? AND m.message_type != 7 AND m.message_type != 0
    """, (conv_id,)).fetchone()
    return dict(row) if row else {}


def _get_top_link_domains(conn: sqlite3.Connection, conv_id: int,
                          date_from_ms: int | None = None,
                          date_to_ms: int | None = None) -> list[dict]:
    rows = conn.execute("""
        SELECT mld.domain, COUNT(*) AS cnt,
               COUNT(DISTINCT m.sender_id) AS unique_senders
        FROM message_link_detail mld
        JOIN message m ON m.id = mld.message_id
        WHERE m.conversation_id = ?
        GROUP BY mld.domain
        ORDER BY cnt DESC
        LIMIT 15
    """, (conv_id,)).fetchall()
    return [dict(r) for r in rows]


def _get_message_type_stats(conn: sqlite3.Connection, conv_id: int,
                            date_from_ms: int | None = None,
                            date_to_ms: int | None = None) -> list[dict]:
    rows = conn.execute("""
        SELECT m.message_type, COUNT(*) AS cnt
        FROM message m
        WHERE m.conversation_id = ? AND m.message_type != 7
        GROUP BY m.message_type ORDER BY cnt DESC
    """, (conv_id,)).fetchall()
    return [dict(r) for r in rows]


def _get_top_forwarders(conn: sqlite3.Connection, conv_id: int,
                        date_from_ms: int | None = None,
                        date_to_ms: int | None = None) -> list[dict]:
    """Top 10 forwarders with breakdown by message type (category)."""
    try:
        rows = conn.execute("""
            SELECT m.sender_id,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
                   c.phone_jid, c.lid_jid,
                   COUNT(*) AS total_fwd,
                   SUM(CASE WHEN m.message_type = 0 THEN 1 ELSE 0 END) AS fwd_text,
                   SUM(CASE WHEN m.message_type = 1 THEN 1 ELSE 0 END) AS fwd_image,
                   SUM(CASE WHEN m.message_type = 3 THEN 1 ELSE 0 END) AS fwd_video,
                   SUM(CASE WHEN m.message_type = 2 THEN 1 ELSE 0 END) AS fwd_audio,
                   SUM(CASE WHEN m.message_type = 9 THEN 1 ELSE 0 END) AS fwd_doc,
                   SUM(CASE WHEN m.message_type = 20 THEN 1 ELSE 0 END) AS fwd_sticker,
                   SUM(CASE WHEN m.message_type = 13 THEN 1 ELSE 0 END) AS fwd_gif,
                   SUM(CASE WHEN m.message_type NOT IN (0,1,2,3,9,13,20) THEN 1 ELSE 0 END) AS fwd_other
            FROM message m
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE m.conversation_id = ? AND m.is_forwarded = 1 AND m.message_type != 7
            GROUP BY m.sender_id
            ORDER BY total_fwd DESC
            LIMIT 10
        """, (conv_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_device_platform_stats(conn: sqlite3.Connection, conv_id: int,
                               date_from_ms: int | None = None,
                               date_to_ms: int | None = None) -> list[dict]:
    """Who texts from which device (Android/iPhone/Web etc.).

    ``from_me`` is included in the SELECT so owner messages can be
    name-stamped with the device owner's identity at render time
    (otherwise they fall through ``COALESCE(...) AS name`` and end up
    in an "Unknown" bucket because the message row for the owner
    typically has ``sender_id = NULL``).
    """
    try:
        rows = conn.execute("""
            SELECT COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS name,
                   c.phone_jid,
                   m.from_me,
                   md.platform_label,
                   COUNT(*) AS cnt
            FROM message_device md
            JOIN message m ON m.id = md.message_id
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE m.conversation_id = ? AND m.message_type != 7
              AND md.platform_label IS NOT NULL
            GROUP BY m.from_me, m.sender_id, md.platform_label
            ORDER BY cnt DESC
        """, (conv_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_call_stats(conn: sqlite3.Connection, conv_id: int,
                    date_from_ms: int | None = None,
                    date_to_ms: int | None = None) -> list[dict]:
    """Call records for this group."""
    try:
        ext, eparams = _date_filter_clause("cr", date_from_ms, date_to_ms)
        rows = conn.execute(f"""
            SELECT cr.timestamp, cr.duration_sec AS duration,
                   cr.is_video, cr.call_result, cr.result_label,
                   cr.from_me, cr.is_group_call, cr.call_category,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS caller_name,
                   c.phone_jid AS caller_jid
            FROM call_record cr
            LEFT JOIN contact c ON c.id = cr.contact_id
            WHERE (cr.conversation_id = ? OR cr.group_conversation_id = ?){ext}
            ORDER BY cr.timestamp DESC
            LIMIT 200
        """, (conv_id, conv_id, *eparams)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _get_location_stats(conn: sqlite3.Connection, conv_id: int,
                        date_from_ms: int | None = None,
                        date_to_ms: int | None = None) -> list[dict]:
    """Location messages in this group.

    Schema uses ``place_address`` (not ``address``), live shares are
    encoded via ``is_live`` + ``live_duration``, and each row carries
    a ``thumbnail_blob`` (a small JPEG of the WhatsApp map preview)
    that is included so the report can render it inline.
    """
    try:
        ext, eparams = _date_filter_clause("m", date_from_ms, date_to_ms)
        rows = conn.execute(f"""
            SELECT m.timestamp, m.from_me, m.id AS message_id,
                   c.id           AS sender_cid,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS sender_name,
                   c.phone_jid    AS sender_jid,
                   c.lid_jid      AS sender_lid,
                   l.latitude, l.longitude,
                   l.place_name, l.place_address,
                   l.is_live, l.live_duration,
                   l.final_latitude, l.final_longitude,
                   l.final_timestamp,
                   l.thumbnail_blob,
                   l.map_preview_url
            FROM location l
            JOIN message m ON m.id = l.message_id
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE m.conversation_id = ?{ext}
            ORDER BY m.timestamp DESC
            LIMIT 500
        """, (conv_id, *eparams)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[group_report] _get_location_stats failed: {e}")
        return []


# ====================================================================== #
# HTML Section builders
# ====================================================================== #

def _h(text: str | None) -> str:
    """HTML-escape text."""
    return html.escape(str(text)) if text else ""


# Color palette used by _avatar_initial — matches the conversation
# list / contacts UI colors so the report is visually consistent
# with the in-app views.
_AVATAR_PALETTE = [
    "#26a69a", "#7e57c2", "#ec407a", "#5c6bc0", "#42a5f5",
    "#66bb6a", "#ffa726", "#8d6e63", "#ab47bc", "#ef5350",
]


def _avatar_initial(name: str, is_owner: bool = False) -> str:
    """Render a circular initial-bubble for a contact when no avatar
    blob is available — same visual rule as the conversations list.
    Owner avatars get an amber border so they stand out.
    """
    n = (name or "?").strip()
    initial = "?"
    for ch in n:
        if ch.isalnum():
            initial = ch.upper()
            break
    bg = (_AVATAR_PALETTE[(sum(ord(c) for c in n) % len(_AVATAR_PALETTE))]
          if n else _AVATAR_PALETTE[0])
    border = "#ff8f00" if is_owner else "#cfd8dc"
    return (
        f'<div style="width:42px;height:42px;border-radius:50%;'
        f'background:{bg};color:#ffffff;display:flex;align-items:center;'
        f'justify-content:center;font-weight:700;font-size:16px;'
        f'border:2px solid {border};">{html.escape(initial)}</div>'
    )


def _ts(ms: int | None) -> str:
    """Format Unix-ms timestamp with local time + UTC in brackets."""
    if not ms:
        return "—"
    try:
        local_dt = datetime.fromtimestamp(ms / 1000).astimezone()
        utc_dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        tz_name = local_dt.strftime("%Z") or "LOCAL"
        local_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        utc_str = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{local_str} {tz_name} [{utc_str} UTC]"
    except (ValueError, OSError):
        return "—"


def _ts_short(ms: int | None) -> str:
    """Format Unix-ms timestamp — short form with UTC in brackets."""
    if not ms:
        return "—"
    try:
        local_dt = datetime.fromtimestamp(ms / 1000).astimezone()
        utc_dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        tz_name = local_dt.strftime("%Z") or "LOCAL"
        local_str = local_dt.strftime("%Y-%m-%d %H:%M")
        utc_str = utc_dt.strftime("%Y-%m-%d %H:%M")
        return f"{local_str} {tz_name} [{utc_str} UTC]"
    except (ValueError, OSError):
        return "—"


def _num(n) -> str:
    """Format number with comma separator."""
    if n is None:
        return "0"
    return f"{int(n):,}"


def _blob_to_img(blob: bytes | None, size: int = 64, circle: bool = True) -> str:
    """Convert a BLOB to a base64 <img> tag."""
    if not blob or len(blob) < 100:
        return ""
    b64 = base64.b64encode(blob).decode("ascii")
    style = f"width:{size}px;height:{size}px;object-fit:cover;"
    if circle:
        style += "border-radius:50%;"
    return f'<img src="data:image/jpeg;base64,{b64}" style="{style}" />'


def _identity_html(name, phone=None, jid=None, lid=None, is_owner=False) -> str:
    """Build identity display with name, phone, JID, LID."""
    parts = []
    if is_owner:
        parts.append('<span class="owner-badge">Device Owner</span>')
    parts.append(f"<strong>{_h(name or 'Unknown')}</strong>")
    if phone:
        p = phone if phone.startswith("+") else f"+{phone}"
        parts.append(f'<span class="dim">{_h(p)}</span>')
    if jid:
        parts.append(f'<span class="jid">JID: {_h(jid)}</span>')
    if lid:
        parts.append(f'<span class="jid">LID: {_h(lid)}</span>')
    return " &middot; ".join(parts)


def _section_group_identity(g: dict) -> str:
    """Group identity & metadata section."""
    avatar_html = _blob_to_img(g.get("avatar_blob"), 80)
    if not avatar_html:
        initials = "".join(w[0] for w in (g.get("display_name") or "#").split()[:2] if w and w[0].isalpha())
        avatar_html = f'<div class="avatar-placeholder">{_h(initials[:2].upper() or "#")}</div>'

    group_type_names = {
        0: "Regular Group", 1: "Community", 2: "Community Sub-group",
        3: "Announcement Group", 4: "Newsletter", 5: "Announcement",
        6: "Community Sub-group",
    }
    gt = group_type_names.get(g.get("group_type"), "Group")

    rows = []
    rows.append(f"<tr><td>Group JID (msgstore.db)</td><td><code>{_h(g.get('jid_raw_string'))}</code></td></tr>")
    rows.append(f"<tr><td>chat._id (msgstore.db)</td><td><code>{g.get('source_chat_id', '—')}</code></td></tr>")
    rows.append(f"<tr><td>conversation.id (analysis.db)</td><td><code>{g.get('id', '—')}</code></td></tr>")
    rows.append(f"<tr><td>Group Type</td><td>{gt}</td></tr>")
    rows.append(f"<tr><td>Addressing Mode</td><td>{_h(g.get('addressing_mode', '—'))}</td></tr>")
    rows.append(f"<tr><td>Created</td><td>{_ts(g.get('created_timestamp'))}</td></tr>")

    # Creator identity
    creator_html = _identity_html(
        g.get("creator_name"), g.get("creator_phone"),
        g.get("creator_jid"), g.get("creator_lid"),
    )
    rows.append(f"<tr><td>Created By</td><td>{creator_html}</td></tr>")

    if g.get("community_parent_name"):
        rows.append(f"<tr><td>Community</td><td>{_h(g.get('community_parent_name'))}</td></tr>")

    eph = g.get("ephemeral_duration")
    if eph and eph > 0:
        if eph >= 86400:
            eph_str = f"{eph // 86400} days"
        elif eph >= 3600:
            eph_str = f"{eph // 3600} hours"
        else:
            eph_str = f"{eph // 60} min"
        rows.append(f"<tr><td>Disappearing Messages</td><td>{eph_str}</td></tr>")

    flags = []
    if g.get("is_archived"):
        flags.append("Archived")
    if g.get("is_pinned"):
        flags.append("Pinned")
    if g.get("is_muted"):
        flags.append("Muted")
    if g.get("is_locked"):
        flags.append("Locked")
    if flags:
        rows.append(f"<tr><td>Flags</td><td>{', '.join(flags)}</td></tr>")

    rows.append(f"<tr><td>First Message</td><td>{_ts(g.get('first_message_ts'))}</td></tr>")
    rows.append(f"<tr><td>Last Message</td><td>{_ts(g.get('last_message_ts'))}</td></tr>")

    desc = g.get("description") or ""
    desc_html = f'<div class="description">{_h(desc)}</div>' if desc else ""

    return f"""
    <div class="section" id="identity">
        <h2>Group Identity</h2>
        <div class="identity-card">
            <div class="identity-avatar">{avatar_html}</div>
            <div class="identity-info">
                <h3>{_h(g.get('display_name'))}</h3>
                {desc_html}
            </div>
        </div>
        <table class="info-table">{''.join(rows)}</table>
    </div>
    """


# ---------------------------------------------------------------------------
# Owner membership & admin-only-send policy.
# Primary sources:
#   * ``chat.participation_status`` (msgstore.db)
#   * ``wa_group_admin_settings.announcement_group /
#     restrict_mode`` (wa.db)
#   * ``message_system.action_type`` 31 / 32 (enable / disable
#     admins-only-send)
# ---------------------------------------------------------------------------

def _resolve_owner_phone_for_report(conn: sqlite3.Connection) -> str | None:
    try:
        r = conn.execute(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'"
        ).fetchone()
        if r and r[0]:
            return str(r[0])
        r = conn.execute(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_jid'"
        ).fetchone()
        if r and r[0]:
            v = str(r[0])
            return v.split("@", 1)[0] if "@" in v else v
        r = conn.execute(
            "SELECT c.phone_number FROM message m "
            "JOIN contact c ON c.id = m.sender_id "
            "WHERE m.from_me = 1 AND m.message_type != 7 "
            "GROUP BY m.sender_id ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if r and r[0]:
            return str(r[0])
    except Exception:
        pass
    return None


def _section_owner_and_policy(conn: sqlite3.Connection, conv_id: int,
                              g: dict) -> str:
    """Forensic summary of:
      • whether the device owner is/was a member of this group, AND
      • whether admins-only-send is enforced (and since when),
      • whether the owner has permission to send messages today.
    """
    chat_type = g.get("chat_type") or ""
    if chat_type not in ("group", "community"):
        return ""

    ps = g.get("participation_status")
    ann = g.get("announcement_group")
    restrict = g.get("restrict_mode")
    approval = g.get("require_membership_approval")
    add_mode = g.get("member_add_mode")

    owner_phone = _resolve_owner_phone_for_report(conn) or "device owner"

    # ---- Owner role (per chat.participation_status) ----
    PS_LABELS = {
        0: ("Individual chat (owner is the account holder)", "#1b5e20"),
        1: ("NO LONGER a member of this group", "#b71c1c"),
        2: ("Member (not an admin)", "#1b5e20"),
        3: ("Appointed admin", "#1565c0"),
        4: ("Creator + admin", "#6a1b9a"),
    }
    if ps in PS_LABELS:
        role_label, role_color = PS_LABELS[ps]
        role_source = "chat.participation_status (msgstore.db)"
    else:
        # Fallback: system-event timeline
        role = _owner_role_from_events(conn, conv_id)
        role_label = role.get("label") or "Unknown — no evidence"
        role_color = role.get("color") or "#455a64"
        role_source = role.get("source") or "no data"

    # ---- Admins-only-send timeline ----
    enforced_since = None
    last_toggle_by = None
    last_toggle_ts = None
    last_toggle_type = None
    try:
        row = conn.execute(
            "SELECT gmc.change_type, gmc.timestamp, "
            "       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS actor "
            "FROM group_metadata_change gmc "
            "LEFT JOIN contact c ON c.id = gmc.changed_by_id "
            "WHERE gmc.conversation_id = ? "
            "  AND gmc.change_type IN ('admin_only_send_on','admin_only_send_off') "
            "ORDER BY gmc.timestamp DESC LIMIT 1",
            (conv_id,),
        ).fetchone()
        if row:
            last_toggle_type = row["change_type"]
            last_toggle_ts = row["timestamp"]
            last_toggle_by = row["actor"]
            if last_toggle_type == "admin_only_send_on":
                enforced_since = row["timestamp"]
            else:
                prev = conn.execute(
                    "SELECT timestamp FROM group_metadata_change "
                    "WHERE conversation_id = ? AND change_type = 'admin_only_send_on' "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (conv_id,),
                ).fetchone()
                if prev:
                    enforced_since = prev["timestamp"]
    except Exception:
        pass

    # If we still don't know announcement_group (old ingestion), infer
    # from the last toggle.
    if ann is None and last_toggle_type:
        ann = 1 if last_toggle_type == "admin_only_send_on" else 0

    # ---- Derive owner's send permission ----
    if ann is None:
        can_send_html = (
            '<span style="color:#888;">Unknown (announcement_group '
            'not available; re-ingest with updated schema)</span>'
        )
    elif ann == 0:
        can_send_html = (
            '<span style="color:#1b5e20;font-weight:600;">'
            '\u2705 Yes \u2014 all members (including the device owner) may send</span>'
        )
    else:  # ann == 1
        if ps in (3, 4):
            can_send_html = (
                '<span style="color:#1565c0;font-weight:600;">'
                '\u2705 Yes \u2014 device owner is an admin and may send</span>'
            )
        elif ps == 1:
            can_send_html = (
                '<span style="color:#b71c1c;font-weight:600;">'
                '\u26D4 No \u2014 device owner is no longer a member</span>'
            )
        elif ps == 2:
            can_send_html = (
                f'<span style="color:#b71c1c;font-weight:600;">'
                f'\u26D4 No \u2014 <code>+{_h(owner_phone)}</code> (device owner) '
                f'does NOT have permission to send; only admins can send</span>'
            )
        else:
            # ps unknown — best-effort warning
            can_send_html = (
                f'<span style="color:#b71c1c;font-weight:600;">'
                f'\u26D4 Likely No \u2014 admins-only-send is enabled; the device '
                f'owner has no recorded admin role. (participation_status not '
                f'available; re-ingest to confirm.)</span>'
            )

    # ---- Additional group-rule rows ----
    rule_rows: list[str] = []
    rule_rows.append(
        f'<tr><td>Only admins can send messages</td><td>'
        f'{_bool_cell(ann)}<span style="opacity:0.7;font-size:11px;margin-left:6px">'
        f'wa_group_admin_settings.announcement_group</span></td></tr>'
    )
    if enforced_since:
        who = f" (by {_h(last_toggle_by)})" if last_toggle_by else ""
        rule_rows.append(
            f'<tr><td>\u00a0\u00a0\u00a0Enforced since</td>'
            f'<td>{_ts(enforced_since)}{who} '
            f'<span style="opacity:0.7;font-size:11px">'
            f'(message_system action_type 31)</span></td></tr>'
        )
    if last_toggle_ts and last_toggle_ts != enforced_since:
        verb = "ON" if last_toggle_type == "admin_only_send_on" else "OFF"
        who = f" (by {_h(last_toggle_by)})" if last_toggle_by else ""
        rule_rows.append(
            f'<tr><td>\u00a0\u00a0\u00a0Last toggle</td>'
            f'<td>{_ts(last_toggle_ts)} \u2014 turned {verb}{who}</td></tr>'
        )
    rule_rows.append(
        f'<tr><td>Only admins can edit group info</td><td>{_bool_cell(restrict)}'
        f'<span style="opacity:0.7;font-size:11px;margin-left:6px">'
        f'wa_group_admin_settings.restrict_mode</span></td></tr>'
    )
    rule_rows.append(
        f'<tr><td>Admin approval required to join</td><td>{_bool_cell(approval)}'
        f'<span style="opacity:0.7;font-size:11px;margin-left:6px">'
        f'wa_group_admin_settings.require_membership_approval</span></td></tr>'
    )
    rule_rows.append(
        f'<tr><td>Only admins can add members</td><td>{_bool_cell(add_mode)}'
        f'<span style="opacity:0.7;font-size:11px;margin-left:6px">'
        f'wa_group_admin_settings.member_add_mode (action_type 92)</span></td></tr>'
    )

    return f"""
    <div class="section" id="owner-policy">
        <h2>Device Owner &amp; Send Policy</h2>
        <table class="info-table">
            <tr><td style="width:260px">Device owner role in this group</td>
                <td><b style="color:{role_color}">{_h(role_label)}</b>
                    <span style="opacity:0.65;font-size:11px;margin-left:6px">
                    source: {_h(role_source)}</span></td></tr>
            <tr><td>Can the device owner send messages here?</td>
                <td>{can_send_html}</td></tr>
            {''.join(rule_rows)}
        </table>
    </div>
    """


def _bool_cell(v) -> str:
    if v is None:
        return '<span style="color:#888">Unknown</span>'
    if int(v):
        return '<span style="color:#b71c1c;font-weight:600">\u2705 Yes</span>'
    return '<span style="color:#546e7a">\u2013 No</span>'


def _owner_role_from_events(conn: sqlite3.Connection, conv_id: int) -> dict:
    """Fallback for analysis.db files ingested before participation_status
    was captured — re-uses the system-event logic from group_info_page.
    """
    YOU_J = {"you_were_added"}
    YOU_L = {"you_were_removed", "you_left"}
    SELF_J = {"participant_joined_via_link", "community_or_group_created"}
    GEN_J = {"participant_added"}
    GEN_L = {"participant_left"}
    # Find the owner cid
    ocid = None
    try:
        r = conn.execute(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_contact_id'"
        ).fetchone()
        if r and r[0]:
            ocid = int(r[0])
        else:
            r = conn.execute(
                "SELECT m.sender_id FROM message m "
                "WHERE m.from_me = 1 AND m.sender_id IS NOT NULL "
                "AND m.message_type != 7 LIMIT 1"
            ).fetchone()
            ocid = r[0] if r else None
    except Exception:
        pass
    if not ocid:
        return {"label": "Unknown (owner contact could not be resolved)",
                "color": "#455a64", "source": "no data"}
    events = conn.execute(
        "SELECT event_label, actor_id, target_id FROM system_event "
        "WHERE conversation_id = ? AND event_label IS NOT NULL "
        "  AND (actor_id = ? OR target_id = ? OR event_label LIKE 'you!_%' ESCAPE '!') "
        "ORDER BY timestamp ASC", (conv_id, ocid, ocid),
    ).fetchall()
    last = None
    for e in events:
        lbl, a, t = e["event_label"], e["actor_id"], e["target_id"]
        is_j = lbl in YOU_J or (lbl in SELF_J and a == ocid) or (lbl in GEN_J and t == ocid)
        is_l = lbl in YOU_L or (lbl in GEN_L and a == ocid)
        if is_j:
            last = "creator" if lbl == "community_or_group_created" else "join"
        elif is_l:
            last = "leave"
    if last == "creator":
        return {"label": "Creator (inferred from group-creation event)",
                "color": "#6a1b9a", "source": "system_event"}
    if last == "join":
        return {"label": "Currently a member (inferred from system events)",
                "color": "#1b5e20", "source": "system_event"}
    if last == "leave":
        return {"label": "No longer a member (inferred from leave/remove event)",
                "color": "#b71c1c", "source": "system_event"}
    # Last resort: from_me message check
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM message WHERE conversation_id = ? "
            "AND from_me = 1 AND message_type != 7", (conv_id,),
        ).fetchone()[0]
    except Exception:
        n = 0
    if n > 0:
        return {"label": f"Current (inferred from {n} messages sent by owner)",
                "color": "#1b5e20", "source": "inferred"}
    return {"label": "Unknown (no events, no sent messages)",
            "color": "#455a64", "source": "none"}


def _section_stats_summary(g: dict, members: list, media_stats: dict,
                            conn=None, conv_id: int = 0,
                            date_from_ms: int | None = None,
                            date_to_ms: int | None = None) -> str:
    total_msgs = g.get("message_count") or 0
    participant_count = len(members)
    admins = sum(1 for m in members if (m.get("role") or "").lower() in ("admin", "superadmin"))
    total_media = sum(
        (media_stats.get(k) or 0)
        for k in ("images", "videos", "audio", "documents", "stickers", "gifs")
    )
    # Use direct count from message_link_detail for accuracy (includes all senders)
    total_links = 0
    total_forwards = 0
    if conn and conv_id:
        try:
            total_links = conn.execute(
                "SELECT COUNT(*) FROM message_link_detail mld "
                "JOIN message m ON m.id = mld.message_id "
                "WHERE m.conversation_id = ?", (conv_id,)
            ).fetchone()[0] or 0
        except Exception:
            total_links = sum((m.get("total_links") or 0) for m in members)
        try:
            total_forwards = conn.execute(
                "SELECT COUNT(*) FROM message m "
                "WHERE m.conversation_id = ? AND m.is_forwarded = 1 AND m.message_type != 7",
                (conv_id,)
            ).fetchone()[0] or 0
        except Exception:
            total_forwards = sum((m.get("total_forwards") or 0) for m in members)
    else:
        total_links = sum((m.get("total_links") or 0) for m in members)
        total_forwards = sum((m.get("total_forwards") or 0) for m in members)

    return f"""
    <div class="section" id="summary">
        <h2>Summary</h2>
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{_num(total_msgs)}</div><div class="stat-label">Messages</div></div>
            <div class="stat-card"><div class="stat-value">{participant_count}</div><div class="stat-label">Members</div></div>
            <div class="stat-card"><div class="stat-value">{admins}</div><div class="stat-label">Admins</div></div>
            <div class="stat-card"><div class="stat-value">{_num(total_media)}</div><div class="stat-label">Media</div></div>
            <div class="stat-card"><div class="stat-value">{_num(total_links)}</div><div class="stat-label">Links</div></div>
            <div class="stat-card"><div class="stat-value">{_num(total_forwards)}</div><div class="stat-label">Forwards</div></div>
        </div>
    </div>
    """


def _section_edit_history(edits: list[dict]) -> str:
    if not edits:
        return '<div class="section" id="edit-history"><h2>Group Edit History</h2><p class="empty">No metadata changes recorded.</p></div>'

    n_subj = sum(1 for e in edits if e["change_type"] == "subject")
    n_icon = sum(1 for e in edits if e["change_type"] == "icon")
    n_desc = sum(1 for e in edits if e["change_type"] == "description")
    n_other = len(edits) - n_subj - n_icon - n_desc

    cards = []
    for e in edits:
        ct = e["change_type"]
        icon_map = {
            "subject": "&#x270E;", "description": "&#x1F4DD;", "icon": "&#x1F4F7;",
            "disappearing": "&#x23F1;", "invite_link_reset": "&#x1F517;",
        }
        color_map = {
            "subject": "#1565c0", "description": "#00897b", "icon": "#6a1b9a",
            "disappearing": "#ef6c00", "invite_link_reset": "#c62828",
        }
        label_map = {
            "subject": "Name Changed", "description": "Description Changed",
            "icon": "Profile Picture Changed", "disappearing": "Disappearing Messages",
            "admin_only_edit_on": "Admin-Only Edit: ON",
            "admin_only_edit_off": "Admin-Only Edit: OFF",
            "admin_only_send_on": "Admin-Only Send: ON",
            "admin_only_send_off": "Admin-Only Send: OFF",
            "invite_link_reset": "Invite Link Reset",
            "approval_mode": "Approval Mode Changed",
            "membership_approval": "Membership Approval",
        }
        icon = icon_map.get(ct, "&#x2699;")
        color = color_map.get(ct, "#607d8b")
        label = label_map.get(ct, ct.replace("_", " ").title())

        changer_html = _identity_html(
            e.get("changer_name"), e.get("changer_phone"),
            e.get("changer_jid"), e.get("changer_lid"),
            is_owner=bool(e.get("is_owner")),
        )

        content = ""
        if ct == "subject":
            old = _h(e.get("old_value") or "")
            new = _h(e.get("new_value") or "")
            content = f'<div class="change-values"><span class="old-val">{old}</span> <span class="arrow">&rarr;</span> <span class="new-val">{new}</span></div>'

        elif ct == "description":
            new = _h(e.get("new_value") or "(cleared)")
            content = f'<div class="desc-change">{new[:500]}{"..." if len(new) > 500 else ""}</div>'

        elif ct == "icon":
            old_img = _blob_to_img(e.get("old_photo"), 72, circle=True)
            new_img = _blob_to_img(e.get("new_photo"), 72, circle=True)
            nv = e.get("new_value") or ""
            if old_img or new_img:
                parts = []
                if old_img:
                    parts.append(f'<div class="dp-box"><div class="dp-label">Previous</div>{old_img}</div>')
                if old_img and new_img:
                    parts.append('<div class="dp-arrow">&rarr;</div>')
                if new_img:
                    parts.append(f'<div class="dp-box"><div class="dp-label">New</div>{new_img}</div>')
                elif nv == "removed":
                    parts.append('<div class="dp-box"><div class="dp-label">Removed</div><div class="dp-removed">&#x1F6AB;</div></div>')
                content = f'<div class="dp-change">{"".join(parts)}</div>'
            elif nv == "removed":
                content = '<div class="dp-removed-text">Profile picture removed</div>'

        else:
            nv = _h(e.get("new_value") or "")
            if nv:
                content = f'<div class="setting-val">{nv}</div>'

        forensic = f'<span class="forensic">msgstore._id: {e.get("source_msg_id", "?")} &middot; action_type: {e.get("action_type", "?")}</span>'

        cards.append(f"""
        <div class="edit-card">
            <div class="edit-header">
                <span class="edit-icon" style="color:{color}">{icon}</span>
                <span class="edit-label" style="color:{color}">{label}</span>
                <span class="edit-ts">{_ts_short(e.get("timestamp"))}</span>
            </div>
            <div class="edit-by">Changed by: {changer_html}</div>
            {content}
            <div class="edit-forensic">{forensic}</div>
        </div>
        """)

    summary = f"{len(edits)} changes: {n_subj} name, {n_icon} DP, {n_desc} description, {n_other} settings"

    return f"""
    <div class="section" id="edit-history">
        <h2>Group Edit History <span class="count">({summary})</span></h2>
        <div class="edit-timeline">{''.join(cards)}</div>
    </div>
    """


def _section_members(members: list[dict], group: dict) -> str:
    if not members:
        return ""

    # Compact timestamp helper — full timestamps blow up the row
    # height of the Members table because each cell wraps "YYYY-MM-DD
    # HH:MM India Standard Time [YYYY-MM-DD HH:MM UTC]" across 6+
    # lines, dominating the page.  Use a single-line "YYYY-MM-DD"
    # form here; the chain-of-custody banner already records the case
    # timezone for unambiguous interpretation.
    def _short(ms):
        if not ms:
            return "—"
        try:
            return datetime.fromtimestamp(int(ms) / 1000)\
                           .astimezone().strftime("%Y-%m-%d")
        except (ValueError, OSError):
            return "—"

    # Sort: device owner first, then admins, then by message count desc.
    # Analyst always sees themselves at the top of the roster.
    def _sort_key(m: dict) -> tuple:
        is_owner = bool(m.get("_is_owner"))
        role = (m.get("role") or "member").lower()
        role_rank = 0 if role == "superadmin" else (1 if role == "admin" else 2)
        return (
            0 if is_owner else 1,            # owner first
            role_rank,                       # then creator → admin → member
            -(m.get("total_messages") or 0), # then by activity
            (m.get("resolved_name") or m.get("c_display") or "").lower(),
        )
    members = sorted(members, key=_sort_key)

    rows = []
    for m in members:
        is_owner = bool(m.get("_is_owner"))
        name = m.get("resolved_name") or m.get("c_display") or m.get("wa_name") \
                or m.get("phone_number") or m.get("phone_jid") or "Unknown"
        phone = m.get("phone_number") or ""
        jid = m.get("phone_jid") or ""
        lid = m.get("lid_jid") or ""
        role = (m.get("role") or "member").lower()
        role_badge = (
            f'<span class="role-{role}">'
            f'{role.replace("superadmin", "Creator").title()}'
            f'</span>'
        )
        label_html = (
            f' <span class="member-label">{_h(m.get("label"))}</span>'
            if m.get("label") else ""
        )
        owner_badge = (
            ' <span style="background:#ff8f00;color:white;padding:1px 6px;'
            'border-radius:3px;font-size:9px;font-weight:bold;">DEVICE OWNER</span>'
            if is_owner else ""
        )
        # Owner row gets a subtle amber background so the analyst
        # spots the case-phone identity at a glance.
        row_style = (
            ' style="background:#fff8e1; border-left:3px solid #ff8f00;"'
            if is_owner else ""
        )

        total = m.get("total_messages") or 0
        join_ts = _short(m.get("join_timestamp"))
        first_msg = _short(m.get("first_message_ts"))
        last_msg = _short(m.get("last_message_ts"))

        # Avatar — circular profile picture if WhatsApp had one
        # cached, or a coloured-initial placeholder so every row
        # always has a visual.  Embedded as base64 so the report
        # stays a single self-contained file.
        avatar_blob = m.get("avatar_blob")
        if avatar_blob and isinstance(avatar_blob, (bytes, bytearray)) and len(avatar_blob) > 100:
            try:
                b64 = base64.b64encode(avatar_blob).decode("ascii")
                avatar_html = (
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'style="width:42px;height:42px;border-radius:50%;'
                    f'object-fit:cover;border:2px solid '
                    f'{"#ff8f00" if is_owner else "#cfd8dc"};display:block;" '
                    f'alt="dp"/>'
                )
            except Exception:
                avatar_html = _avatar_initial(name, is_owner)
        else:
            avatar_html = _avatar_initial(name, is_owner)

        # Combined identity cell: name + owner/role badges on top,
        # phone + JID + LID stacked below as small monospace lines so
        # the analyst sees the full identity in ONE column instead of
        # four (which is what was clipping the table off the PDF).
        identity_cell = (
            f'<div style="font-weight:600;">{_h(name)}{owner_badge}{label_html}</div>'
            + (f'<div style="font-size:10px;color:#5d6770;margin-top:2px;">'
               f'<b>{_h(phone)}</b></div>' if phone else '')
            + (f'<div style="font-size:9px;color:#128c7e;font-family:Consolas,monospace;'
               f'word-break:break-all;">{_h(jid)}</div>' if jid else '')
            + (f'<div style="font-size:9px;color:#7b1fa2;font-family:Consolas,monospace;'
               f'word-break:break-all;">{_h(lid)}</div>' if lid else '')
        )

        # Combined activity range: joined / first / last on three small
        # lines so the analyst sees the full lifecycle in one column.
        activity_cell = (
            f'<div style="font-size:10px;color:#5d6770;">'
            f'<b>Joined:</b> {join_ts}</div>'
            f'<div style="font-size:10px;color:#5d6770;">'
            f'<b>First:</b> {first_msg}</div>'
            f'<div style="font-size:10px;color:#5d6770;">'
            f'<b>Last:</b> {last_msg}</div>'
        )

        rows.append(f"""
        <tr{row_style}>
            <td style="width:48px;">{avatar_html}</td>
            <td>{identity_cell}</td>
            <td>{role_badge}</td>
            <td class="num">{_num(total)}</td>
            <td class="num">{_num(m.get('total_media'))}</td>
            <td class="num">{_num(m.get('total_links'))}</td>
            <td class="num">{_num(m.get('total_mentions'))}</td>
            <td style="white-space:nowrap;">{activity_cell}</td>
        </tr>
        """)

    owner_in_list = any(m.get("_is_owner") for m in members)
    owner_note = (
        '<p style="font-size:11px;color:#666;margin:4px 0 8px;">'
        '<span style="background:#fff8e1;border:1px solid #ffe082;padding:1px 6px;'
        'border-radius:3px;color:#e65100;">DEVICE OWNER</span>'
        ' — case-phone identity, injected into the roster.  WhatsApp\'s own '
        '<code>group_member</code> table never lists the owner (the owner is implicit), '
        'so the row here is reconstructed from <code>case_metadata</code> + '
        '<code>chat.participation_status</code>.'
        '</p>'
        if owner_in_list else ""
    )

    return f"""
    <div class="section" id="members">
        <h2>Current Members <span class="count">({len(members)})</span></h2>
        {owner_note}
        <table class="data-table members-table">
            <thead><tr>
                <th style="width:48px;">DP</th>
                <th>Identity (Name · Phone · JID · LID)</th>
                <th style="width:80px;">Role</th>
                <th style="width:64px;" class="num">Msgs</th>
                <th style="width:54px;" class="num">Media</th>
                <th style="width:54px;" class="num">Links</th>
                <th style="width:54px;" class="num">@</th>
                <th style="width:130px;">Activity</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_top_contributors(members: list[dict], top_n: int = 20) -> str:
    sorted_members = sorted(members, key=lambda m: m.get("total_messages") or 0, reverse=True)[:top_n]
    if not sorted_members:
        return ""

    max_msgs = max((m.get("total_messages") or 0) for m in sorted_members) or 1
    total_in_group = len([m for m in members if (m.get("total_messages") or 0) > 0])

    bars = []
    for m in sorted_members:
        is_owner = bool(m.get("_is_owner"))
        name = m.get("resolved_name") or m.get("wa_name") or m.get("phone_number") or "Unknown"
        jid = m.get("phone_jid") or ""
        total = m.get("total_messages") or 0
        pct = (total / max_msgs) * 100
        jid_html = f' <code class="jid">{_h(jid)}</code>' if jid else ""
        owner_badge = (
            ' <span style="background:#ff8f00;color:white;padding:0 5px;'
            'border-radius:3px;font-size:9px;font-weight:bold;">OWNER</span>'
            if is_owner else ""
        )
        # Owner bar in amber so the case-phone identity is obvious.
        bar_fill_style = (
            "width:{pct:.0f}%;background:linear-gradient(90deg,#ff8f00,#ffb300);"
            if is_owner else "width:{pct:.0f}%;"
        ).format(pct=pct)
        bars.append(f"""
        <div class="bar-row">
            <div class="bar-name">{_h(name)}{owner_badge}{jid_html}</div>
            <div class="bar-track"><div class="bar-fill" style="{bar_fill_style}"></div></div>
            <div class="bar-value">{_num(total)}</div>
        </div>
        """)

    shown = len(sorted_members)
    if total_in_group > shown:
        cap_label = f" <span class='count'>(showing top {shown} of {total_in_group} contributors)</span>"
    else:
        cap_label = f" <span class='count'>({shown} contributors)</span>"

    return f"""
    <div class="section" id="contributors">
        <h2>Top Contributors{cap_label}</h2>
        <div class="bar-chart">{''.join(bars)}</div>
    </div>
    """


def _section_mention_network(network: list[dict], top_mentioned: list[dict],
                             top_mentioners: list[dict], top_n: int = 20,
                             owner_name: str = "", owner_jid: str = "",
                             owner_lid: str = "") -> str:
    """Mentions render — owner-aware.

    The underlying queries don't resolve a contact for owner-sent
    messages (sender_id NULL on from_me) so the "Most Active
    Mentioners" + "Who Mentions Whom" rows for the owner come back
    blank.  We name/JID-stamp them here using the device owner
    identity from ``case_metadata``.
    """
    if not network and not top_mentioned:
        return '<div class="section" id="mentions"><h2>Mention Network</h2><p class="empty">No mentions found in the selected window.</p></div>'

    owner_display = (
        f"{owner_name} (you)" if owner_name else "You (Device Owner)"
    )
    owner_tag_html = (
        ' <span style="background:#e0f2f1;color:#00695c;padding:1px 6px;'
        'border-radius:8px;font-size:9px;font-weight:600;'
        'text-transform:uppercase;letter-spacing:0.04em;">Owner</span>'
    )

    def _stamp_sender(name: str | None, jid: str | None, lid: str | None,
                      from_me: int | None) -> tuple[str, str, str, bool]:
        """Return (display_name, phone_jid, lid_jid, is_owner)."""
        if from_me:
            return (owner_display, owner_jid, owner_lid, True)
        return ((name or "—"), (jid or ""), (lid or ""), False)

    # Top mentioned table — these are TARGETS of @-mentions, never the
    # owner-as-sender path, so no owner stamping needed here.
    mentioned_rows = ""
    for tm in top_mentioned[:top_n]:
        mentioned_rows += f"""
        <tr>
            <td><strong>{_h(tm.get('name') or '—')}</strong></td>
            <td class="jid-cell"><code>{_h(tm.get('phone_jid') or '')}</code></td>
            <td class="jid-cell"><code>{_h(tm.get('lid_jid') or '')}</code></td>
            <td class="num">{_num(tm.get('mention_count'))}</td>
        </tr>
        """

    mentioner_rows = ""
    for tm in top_mentioners[:top_n]:
        nm, jid, lid, is_owner = _stamp_sender(
            tm.get("name"), tm.get("phone_jid"), tm.get("lid_jid"),
            tm.get("from_me"))
        owner_html = owner_tag_html if is_owner else ""
        mentioner_rows += f"""
        <tr>
            <td><strong>{_h(nm)}</strong>{owner_html}</td>
            <td class="jid-cell"><code>{_h(jid)}</code></td>
            <td class="jid-cell"><code>{_h(lid)}</code></td>
            <td class="num">{_num(tm.get('mention_count'))}</td>
        </tr>
        """

    # Who mentions whom — top 30 edges
    edge_cap = max(30, top_n)
    network_rows = ""
    for n in network[:edge_cap]:
        nm, jid, _, is_owner = _stamp_sender(
            n.get("sender_name"), n.get("sender_jid"), None,
            n.get("from_me"))
        owner_html = owner_tag_html if is_owner else ""
        network_rows += f"""
        <tr>
            <td>{_h(nm)}{owner_html}</td>
            <td class="jid-cell"><code>{_h(jid)}</code></td>
            <td class="arrow-cell">&rarr;</td>
            <td>{_h(n.get('mentioned_name') or '—')}</td>
            <td class="jid-cell"><code>{_h(n.get('mentioned_jid') or '')}</code></td>
            <td class="num">{n.get('cnt', 0)}</td>
        </tr>
        """

    mentioned_count = len(top_mentioned)
    mentioner_count = len(top_mentioners)
    edges_count = len(network)
    mentioned_label = (
        f"(showing top {min(top_n, mentioned_count)} of {mentioned_count})"
        if mentioned_count > top_n else f"({mentioned_count})"
    )
    mentioner_label = (
        f"(showing top {min(top_n, mentioner_count)} of {mentioner_count})"
        if mentioner_count > top_n else f"({mentioner_count})"
    )
    edges_label = (
        f"(showing top {min(edge_cap, edges_count)} of {edges_count} edges)"
        if edges_count > edge_cap else f"({edges_count} edges)"
    )

    return f"""
    <div class="section" id="mentions">
        <h2>Mention Network</h2>

        <div class="mention-grid">
            <div>
                <h3>Most Mentioned <span class="count">{mentioned_label}</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Name</th><th>JID</th><th>LID</th><th>Times</th></tr></thead>
                    <tbody>{mentioned_rows}</tbody>
                </table>
            </div>
            <div>
                <h3>Most Active Mentioners <span class="count">{mentioner_label}</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Name</th><th>JID</th><th>LID</th><th>Times</th></tr></thead>
                    <tbody>{mentioner_rows}</tbody>
                </table>
            </div>
        </div>

        <h3>Who Mentions Whom <span class="count">{edges_label}</span></h3>
        <table class="data-table compact">
            <thead><tr><th>Sender</th><th>Sender JID</th><th></th><th>Mentioned</th><th>Mentioned JID</th><th>Count</th></tr></thead>
            <tbody>{network_rows}</tbody>
        </table>
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

    return f"""
    <div class="section" id="activity">
        <h2>Activity Patterns</h2>
        <h3>Messages by Hour of Day</h3>
        <div class="hourly-chart">{hour_bars}</div>
    </div>
    """


def _section_top_forwarders(forwarders: list[dict], top_n: int = 20) -> str:
    """Top forwarders with category breakdown — capped at top_n."""
    total_count = len(forwarders)
    capped = forwarders[:top_n]
    if not capped:
        return (
            '<div class="section" id="forwarders">'
            '<h2>Top Forwarders</h2>'
            '<p class="empty">No forwarded messages in the selected window.</p>'
            '</div>'
        )

    rows = []
    for f in capped:
        name = _h(f.get("name") or "Unknown")
        jid = _h(f.get("phone_jid") or "")
        lid = _h(f.get("lid_jid") or "")
        total = f.get("total_fwd") or 0

        # Build category breakdown
        cats = []
        for label, key in [
            ("Text", "fwd_text"), ("Image", "fwd_image"), ("Video", "fwd_video"),
            ("Audio", "fwd_audio"), ("Doc", "fwd_doc"), ("Sticker", "fwd_sticker"),
            ("GIF", "fwd_gif"), ("Other", "fwd_other"),
        ]:
            v = f.get(key) or 0
            if v > 0:
                cats.append(f"{label}: {v}")
        cat_str = ", ".join(cats)

        rows.append(f"""
        <tr>
            <td><strong>{name}</strong></td>
            <td class="jid-cell"><code>{jid}</code></td>
            <td class="jid-cell"><code>{lid}</code></td>
            <td class="num"><strong>{_num(total)}</strong></td>
            <td style="font-size:10px;color:#666;">{cat_str}</td>
        </tr>
        """)

    cap_label = (
        f"(showing top {len(capped)} of {total_count} forwarders)"
        if total_count > top_n else f"({total_count})"
    )

    return f"""
    <div class="section" id="forwarders">
        <h2>Top Forwarders <span class="count">{cap_label}</span></h2>
        <table class="data-table">
            <thead><tr><th>Name</th><th>JID</th><th>LID</th><th>Forwards</th><th>Category Breakdown</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_device_platforms(stats: list[dict],
                               owner_name: str = "",
                               owner_jid: str = "") -> str:
    """Device platform usage — who texts from which device.

    Owner-aware: rows where ``from_me=1`` are rebadged with the device
    owner's identity from ``case_metadata`` so they no longer surface
    as "Unknown" (the owner's message rows have no sender_id, so the
    LEFT JOIN against ``contact`` returns NULLs).
    """
    if not stats:
        return ""

    # Build a pivot: name -> {platform: count}
    by_person: dict[str, dict] = {}
    all_platforms: set[str] = set()
    OWNER_KEY = "__owner__"
    for s in stats:
        if s.get("from_me"):
            # Owner row — name-stamp with case_metadata identity
            display_name = (
                f"{owner_name} (you)" if owner_name else "You (Device Owner)"
            )
            jid = owner_jid or ""
            key = OWNER_KEY
        else:
            display_name = s.get("name") or "Unknown"
            jid = s.get("phone_jid") or ""
            key = f"{display_name}||{jid}"
        plat = (s.get("platform_label") or "unknown").title()
        all_platforms.add(plat)
        by_person.setdefault(
            key, {"name": display_name, "jid": jid, "platforms": {},
                  "is_owner": key == OWNER_KEY}
        )
        # When two contact rows resolve to the same display+JID (rare
        # schema dups), ``+= cnt`` is the right merge.
        by_person[key]["platforms"][plat] = (
            by_person[key]["platforms"].get(plat, 0) + (s.get("cnt") or 0)
        )

    platforms = sorted(all_platforms)
    rows = []
    for key, info in sorted(
        by_person.items(),
        key=lambda x: (not x[1].get("is_owner"),    # owner first
                       -sum(x[1]["platforms"].values()))
    ):
        total = sum(info["platforms"].values())
        owner_tag = (
            ' <span style="background:#e0f2f1;color:#00695c;padding:1px 6px;'
            'border-radius:8px;font-size:9px;font-weight:600;'
            'text-transform:uppercase;letter-spacing:0.04em;">Owner</span>'
            if info.get("is_owner") else ''
        )
        cells = f"<td><strong>{_h(info['name'])}</strong>{owner_tag}</td>"
        cells += f'<td class="jid-cell"><code>{_h(info["jid"])}</code></td>'
        for p in platforms:
            cnt = info["platforms"].get(p, 0)
            pct = f" ({cnt*100//total}%)" if total > 0 and cnt > 0 else ""
            cells += f'<td class="num">{_num(cnt)}{pct}</td>'
        cells += f'<td class="num"><strong>{_num(total)}</strong></td>'
        rows.append(f"<tr>{cells}</tr>")

    hdr = "<th>Name</th><th>JID</th>" + "".join(f"<th>{_h(p)}</th>" for p in platforms) + "<th>Total</th>"

    return f"""
    <div class="section" id="devices">
        <h2>Device Platform Usage</h2>
        <table class="data-table">
            <thead><tr>{hdr}</tr></thead>
            <tbody>{''.join(rows[:30])}</tbody>
        </table>
    </div>
    """


def _section_calls(calls: list[dict], has_filter: bool = False) -> str:
    """Call history for this group — always renders the section so the
    analyst can see whether call data was empty or simply absent in
    the selected timeline window."""
    if not calls:
        msg = (
            "No calls in the selected timeline window."
            if has_filter else
            "No calls have been recorded for this group."
        )
        return (
            '<div class="section" id="calls">'
            '<h2>Call History</h2>'
            f'<p class="empty">{msg}</p>'
            '</div>'
        )

    total = len(calls)
    total_dur = sum(c.get("duration") or 0 for c in calls)
    dur_str = (
        f"{total_dur // 3600}h {(total_dur % 3600) // 60}m"
        if total_dur >= 3600 else f"{total_dur // 60}m {total_dur % 60}s"
    )

    # Category breakdown so analysts see at a glance whether voice
    # chats / group calls / multi-person calls dominate.
    cat_counts: dict[str, int] = {}
    video_count = sum(1 for c in calls if c.get("is_video"))
    voice_count = total - video_count
    missed_count = sum(1 for c in calls
                       if (c.get("result_label") or "").lower() == "missed")
    for c in calls:
        cat = c.get("call_category") or "personal"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    breakdown_pills = " · ".join(
        f"<span class='pill'>{_h(k)}: {v}</span>"
        for k, v in sorted(cat_counts.items(), key=lambda kv: -kv[1])
    )

    rows = []
    cap = 50
    for c in calls[:cap]:
        ct = "Video" if c.get("is_video") else "Voice"
        cr = _h(c.get("result_label") or c.get("call_result") or "—")
        cat = _h(c.get("call_category") or "")
        direction = "Outgoing" if c.get("from_me") else "Incoming"
        dur = c.get("duration") or 0
        dur_display = (
            f"{dur // 60}m {dur % 60}s" if dur >= 60
            else f"{dur}s" if dur else "—"
        )
        rows.append(f"""
        <tr>
            <td>{_ts_short(c.get('timestamp'))}</td>
            <td>{_h(c.get('caller_name', '—'))}</td>
            <td class="jid-cell"><code>{_h(c.get('caller_jid', ''))}</code></td>
            <td>{direction}</td>
            <td>{ct}{' · ' + cat if cat else ''}</td>
            <td>{cr}</td>
            <td class="num">{dur_display}</td>
        </tr>
        """)

    cap_label = (
        f" (showing first {cap} of {total} calls)" if total > cap else ""
    )
    return f"""
    <div class="section" id="calls">
        <h2>Call History <span class="count">({total} calls · total duration {dur_str}{cap_label})</span></h2>
        <div style="margin:6px 0 12px;font-size:11px;color:#444;">
          <span class='pill'>Voice: {voice_count}</span>
          <span class='pill'>Video: {video_count}</span>
          <span class='pill pill-warn'>Missed: {missed_count}</span>
          {breakdown_pills}
        </div>
        <table class="data-table compact">
            <thead><tr><th>Time</th><th>Caller</th><th>Caller JID</th><th>Direction</th><th>Type</th><th>Result</th><th>Duration</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_locations(locations: list[dict], has_filter: bool = False,
                       owner_name: str = "", owner_jid: str = "",
                       owner_phone: str = "") -> str:
    """Location messages shared in this group — always rendered so the
    section appears in the report navigation even when empty.

    Owner-shared locations carry ``from_me = 1`` with ``sender_id IS
    NULL`` in WhatsApp's storage (the owner is implicit), so the
    sender column needs an owner-aware fallback.  ``owner_name`` /
    ``owner_jid`` / ``owner_phone`` come from ``case_metadata`` and
    are stamped on owner rows so the analyst sees a real identity
    instead of "Unknown".
    """
    if not locations:
        msg = (
            "No locations shared in the selected timeline window."
            if has_filter else
            "No locations have been shared in this group."
        )
        return (
            '<div class="section" id="locations">'
            '<h2>Location Messages</h2>'
            f'<p class="empty">{msg}</p>'
            '</div>'
        )

    rows = []
    live_count = 0
    for loc in locations:
        lat = loc.get("latitude") or 0
        lng = loc.get("longitude") or 0
        place = _h(loc.get("place_name") or "")
        addr = _h(loc.get("place_address") or "")
        is_live = bool(loc.get("is_live"))
        if is_live:
            live_count += 1
        live_dur = loc.get("live_duration") or 0

        # Type column — Live vs Static, with live duration when known.
        if is_live:
            if live_dur:
                hrs = live_dur // 3600
                mins = (live_dur % 3600) // 60
                dur_str = f"{hrs}h {mins}m" if hrs else f"{mins}m"
                type_cell = (f'<span style="background:#e8f5e9;color:#1b5e20;'
                             f'padding:1px 6px;border-radius:3px;font-size:10px;'
                             f'font-weight:600;">LIVE · {dur_str}</span>')
            else:
                type_cell = ('<span style="background:#e8f5e9;color:#1b5e20;'
                             'padding:1px 6px;border-radius:3px;font-size:10px;'
                             'font-weight:600;">LIVE</span>')
        else:
            type_cell = ('<span style="background:#eceff1;color:#37474f;'
                         'padding:1px 6px;border-radius:3px;font-size:10px;'
                         'font-weight:600;">Static</span>')

        place_cell = place or "<span style='color:#999;'>(no name)</span>"
        if addr and addr != place:
            place_cell += f'<br><small style="color:#888">{addr}</small>'

        # Map link for the SHARE-START position — clicking the
        # coordinates opens a Google Maps tab so the analyst can verify
        # the location in seconds.
        try:
            map_url = f"https://www.google.com/maps?q={float(lat):.6f},{float(lng):.6f}"
            start_label = (
                'Start' if is_live else 'Position'
            )
            coord_cell = (
                f'<div style="margin-bottom:3px;">'
                f'<span style="color:#666;font-size:9px;text-transform:uppercase;'
                f'letter-spacing:0.04em;">{start_label}</span><br>'
                f'<a href="{map_url}" target="_blank" rel="noopener noreferrer">'
                f'<code>{lat:.6f}, {lng:.6f}</code></a></div>'
            )
        except (TypeError, ValueError):
            coord_cell = "—"

        # Live-location FINAL position — displayed as its own labelled
        # row so the analyst sees both the start and the last reported
        # coordinate in the same cell.  ``final_timestamp`` (if
        # available) tells the analyst when the live share stopped
        # broadcasting.
        final_lat = loc.get("final_latitude")
        final_lng = loc.get("final_longitude")
        final_ts = loc.get("final_timestamp")
        if is_live and final_lat and final_lng:
            try:
                fl = float(final_lat)
                fg = float(final_lng)
                final_url = f"https://www.google.com/maps?q={fl:.6f},{fg:.6f}"
                final_block = (
                    f'<div style="margin-top:4px;border-top:1px dashed #cfd8dc;'
                    f'padding-top:3px;">'
                    f'<span style="color:#1b5e20;font-size:9px;font-weight:700;'
                    f'text-transform:uppercase;letter-spacing:0.04em;">'
                    f'Final (last reported)</span><br>'
                    f'<a href="{final_url}" target="_blank" rel="noopener noreferrer">'
                    f'<code>{fl:.6f}, {fg:.6f}</code></a>'
                )
                if final_ts:
                    final_block += (
                        f'<br><small style="color:#888;">'
                        f'@ {_h(_ts_short(final_ts))}</small>'
                    )
                final_block += '</div>'
                coord_cell += final_block
            except (TypeError, ValueError):
                pass
        elif is_live:
            # Live but no final captured yet — make it explicit so the
            # analyst doesn't think the data is missing by mistake.
            coord_cell += (
                '<div style="margin-top:4px;color:#888;font-size:10px;'
                'font-style:italic;">no final position captured</div>'
            )

        # Sender resolution — owner-aware.  Owner-shared locations
        # carry ``from_me = 1`` with ``sender_id IS NULL`` (WhatsApp's
        # implicit-owner convention).  Fall back to the owner identity
        # carried in case_metadata so the column shows the real name +
        # JID instead of "—" / "Unknown".
        if loc.get("from_me") and not loc.get("sender_name"):
            owner_label = (
                f"<strong>{_h(owner_name) if owner_name else 'You (Device Owner)'}</strong>"
                f' <span style="background:#ff8f00;color:white;padding:1px 5px;'
                f'border-radius:3px;font-size:9px;font-weight:bold;">DEVICE OWNER</span>'
            )
            if owner_phone:
                owner_label += f'<br><small style="color:#888;">+{_h(owner_phone)}</small>'
            sender_cell = owner_label
            jid_cell = (f'<code>{_h(owner_jid)}</code>'
                        if owner_jid else '<span style="color:#999">—</span>')
            lid_cell = '<span style="color:#999">—</span>'
        else:
            sender_cell = _h(loc.get('sender_name') or '—')
            jid_cell = (f'<code>{_h(loc.get("sender_jid", ""))}</code>'
                        if loc.get("sender_jid") else '<span style="color:#999">—</span>')
            lid_cell = (f'<code>{_h(loc.get("sender_lid", ""))}</code>'
                        if loc.get("sender_lid") else '<span style="color:#999">—</span>')

        # Map preview thumbnail — WhatsApp captures a small JPEG of
        # the map at share time and stores it in ``location.thumbnail_blob``.
        # Embed as base64 so the report stays a single self-contained file.
        thumb_blob = loc.get("thumbnail_blob")
        if thumb_blob and len(thumb_blob) > 100:
            try:
                b64 = base64.b64encode(thumb_blob).decode("ascii")
                map_url_thumb = (
                    f'https://www.google.com/maps?q={float(lat):.6f},{float(lng):.6f}'
                )
                thumb_cell = (
                    f'<a href="{map_url_thumb}" target="_blank" rel="noopener noreferrer">'
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'style="width:96px;height:64px;object-fit:cover;'
                    f'border-radius:4px;border:1px solid #d0d7de;display:block;" '
                    f'alt="map preview"/></a>'
                )
            except (TypeError, ValueError):
                thumb_cell = '<span style="color:#999;font-size:10px;">—</span>'
        else:
            thumb_cell = '<span style="color:#999;font-size:10px;">no preview</span>'

        rows.append(f"""
        <tr>
            <td>{thumb_cell}</td>
            <td style="white-space:nowrap;">{_ts_short(loc.get('timestamp'))}</td>
            <td>{sender_cell}</td>
            <td class="jid-cell">{jid_cell}</td>
            <td class="jid-cell">{lid_cell}</td>
            <td>{coord_cell}</td>
            <td>{place_cell}</td>
            <td>{type_cell}</td>
        </tr>
        """)

    static_count = len(locations) - live_count
    summary = (
        f'<span class="pill">Total: {len(locations)}</span>'
        f'<span class="pill">Static: {static_count}</span>'
        f'<span class="pill">Live: {live_count}</span>'
    )
    return f"""
    <div class="section" id="locations">
        <h2>Location Messages <span class="count">({len(locations)})</span></h2>
        <div style="margin:6px 0 12px;">{summary}</div>
        <table class="data-table compact">
            <thead><tr>
                <th style="width:108px;">Map preview</th>
                <th>Time</th><th>Sender</th><th>JID</th><th>LID</th>
                <th>Coordinates (click to map)</th><th>Place</th><th>Type</th>
            </tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_admin_audit(events: list[dict]) -> str:
    if not events:
        return ""

    rows = []
    for e in events:
        actor = _h(e.get("actor_name") or "—")
        actor_jid = _h(e.get("actor_jid") or "")
        target = _h(e.get("target_name") or "—")
        target_jid = _h(e.get("target_jid") or "")
        label = _h(e.get("event_label") or "").replace("_", " ").title()

        rows.append(f"""
        <tr>
            <td>{_ts_short(e.get('timestamp'))}</td>
            <td><strong>{label}</strong></td>
            <td>{actor} <code class="jid">{actor_jid}</code></td>
            <td>{target} <code class="jid">{target_jid}</code></td>
        </tr>
        """)

    return f"""
    <div class="section" id="admin-audit">
        <h2>Admin Audit Trail <span class="count">({len(events)} events)</span></h2>
        <table class="data-table compact">
            <thead><tr><th>Timestamp</th><th>Event</th><th>Actor</th><th>Target</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


def _section_media_links(media_stats: dict, link_domains: list[dict],
                         type_stats: list[dict], top_n: int = 20) -> str:
    # WhatsApp's message_type taxonomy — distilled from the Android
    # ``messages`` table, message_type integer column.  Older codes
    # have been stable for years; newer codes (60+, 80+, 100+, 110+)
    # came in with Meta AI / Channels / Communities / Newsletter and
    # were inferred from official docs + observed cases.  Anything we
    # genuinely don't recognise still falls through to "Type N" so
    # the analyst can spot patterns without being misled by a guessed
    # label.
    type_labels = {
        0:   "Text",
        1:   "Image",
        2:   "Audio",
        3:   "Video",
        4:   "Contact Card",
        5:   "Location",
        7:   "System Notification",
        8:   "Voice Note",
        9:   "Document",
        10:  "Call Log",
        11:  "URL Preview",
        13:  "GIF",
        14:  "Live Location (start)",
        15:  "Newsletter Update",
        16:  "Group Notification",
        18:  "Settings Change",
        19:  "Group Description",
        20:  "Sticker",
        21:  "Disappearing Settings",
        22:  "Status Reply",
        23:  "Ephemeral Setting",
        25:  "Contact Card (multi)",
        27:  "Payment Request",
        28:  "Payment Sent",
        29:  "Group Invite",
        33:  "Reaction",
        35:  "Poll",
        36:  "Poll Vote",
        38:  "Order Confirmation",
        41:  "Channel Update",
        42:  "View-Once Image",
        43:  "View-Once Video",
        44:  "Photo Album (image)",
        45:  "Photo Album (video)",
        46:  "Keep In Chat",
        49:  "Chat Lock Settings",
        53:  "Sticker (animated)",
        57:  "Payment Invitation",
        64:  "Interactive Button Reply",
        65:  "Interactive List Reply",
        66:  "View-Once Viewed",
        67:  "Quoted Status Reply",
        68:  "Status Video",
        81:  "Status Mention",
        82:  "AI Reply (text)",
        90:  "AI Reply (image)",
        91:  "AI Imagine Update",
        92:  "Meta AI Conversation",
        94:  "AI Reply (other)",
        100: "Native Flow Response",
        102: "Poll (v3)",
        110: "Channel Reaction",
        112: "AI Voice Response",
        113: "AI Stream Update",
        116: "Status Mention (channel)",
    }

    # Type breakdown — show all rows with non-trivial counts.  Cap
    # display only when the list is huge.
    type_total = len(type_stats)
    type_cap = max(15, top_n)
    type_rows = ""
    for t in type_stats[:type_cap]:
        mt = t.get("message_type", 0)
        label = type_labels.get(mt, f"Type {mt}")
        type_rows += f"<tr><td>{label}</td><td class='num'>{_num(t.get('cnt'))}</td></tr>"

    domain_total = len(link_domains)
    domain_cap = top_n
    domain_rows = ""
    for d in link_domains[:domain_cap]:
        domain_rows += f"""
        <tr>
            <td><code>{_h(d.get('domain'))}</code></td>
            <td class='num'>{_num(d.get('cnt'))}</td>
            <td class='num'>{_num(d.get('unique_senders'))}</td>
        </tr>
        """

    type_label_str = (
        f"(showing first {type_cap} of {type_total})"
        if type_total > type_cap else f"({type_total})"
    )
    domain_label_str = (
        f"(showing top {min(domain_cap, domain_total)} of {domain_total})"
        if domain_total > domain_cap else f"({domain_total})"
    )

    type_rows_html = type_rows or '<tr><td colspan="2" class="empty">No data in window.</td></tr>'
    domain_rows_html = domain_rows or '<tr><td colspan="3" class="empty">No links shared in window.</td></tr>'

    return f"""
    <div class="section" id="media-links">
        <h2>Media &amp; Links</h2>
        <div class="mention-grid">
            <div>
                <h3>Message Types <span class="count">{type_label_str}</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Type</th><th>Count</th></tr></thead>
                    <tbody>{type_rows_html}</tbody>
                </table>
            </div>
            <div>
                <h3>Top Link Domains <span class="count">{domain_label_str}</span></h3>
                <table class="data-table compact">
                    <thead><tr><th>Domain</th><th>Links</th><th>Senders</th></tr></thead>
                    <tbody>{domain_rows_html}</tbody>
                </table>
            </div>
        </div>
    </div>
    """


def _section_bot_activity(bot_data: dict, has_filter: bool = False,
                          top_n: int = 20) -> str:
    """Render the dedicated Meta AI / bot interaction section.

    Bots are split out of the human Members and Former Members
    sections because they are not group participants in the human-
    roster sense — they are shared system identities.  Mixing them
    into "former members" misleads the analyst into treating Meta AI
    as someone who left the group.

    Two sub-tables are shown:
      * **Bot interactions** — every bot that replied in this chat,
        with message count + first/last seen.
      * **Top summoners** — humans who @-mentioned each bot most.
        This answers "who pulls Meta AI into conversations the most"
        without polluting the regular Mention Network table.
    """
    bots = bot_data.get("bots", []) if isinstance(bot_data, dict) else []
    summoners = bot_data.get("summoners", []) if isinstance(bot_data, dict) else []

    if not bots and not summoners:
        msg = (
            "No bot interactions in the selected timeline window."
            if has_filter else
            "No Meta-AI / bot interactions in this group."
        )
        return (
            '<div class="section" id="bot-activity">'
            '<h2>Bot Activity</h2>'
            f'<p class="empty">{msg}</p>'
            '</div>'
        )

    # ---- Bots table ----
    bot_rows = []
    for b in bots:
        is_meta_ai = bool(b.get("is_meta_ai"))
        name = b.get("name") or b.get("business_name") or "Bot"
        badge = (
            ' <span style="background:#9c27b0;color:white;padding:1px 6px;'
            'border-radius:3px;font-size:9px;font-weight:bold;">META AI</span>'
            if is_meta_ai else
            ' <span style="background:#607d8b;color:white;padding:1px 6px;'
            'border-radius:3px;font-size:9px;font-weight:bold;">BOT</span>'
        )
        biz = (
            f'<small style="color:#888;display:block;font-size:10px;">'
            f'business: {_h(b.get("business_name"))}</small>'
            if b.get("business_name") else ""
        )
        bot_rows.append(f"""
        <tr>
            <td><strong>{_h(name)}</strong>{badge}{biz}</td>
            <td>{_h(b.get('phone_number', '')) or '—'}</td>
            <td class="jid-cell"><code>{_h(b.get('phone_jid', ''))}</code></td>
            <td class="num"><strong>{_num(b.get('total_messages'))}</strong></td>
            <td>{_ts_short(b.get('first_msg_ts'))}</td>
            <td>{_ts_short(b.get('last_msg_ts'))}</td>
        </tr>
        """)
    bot_rows_html = (
        ''.join(bot_rows) or
        '<tr><td colspan="6" class="empty">No bot replies in window.</td></tr>'
    )

    # ---- Summoners table — grouped by bot, top-N humans per bot ----
    # Bucket summoners by bot then keep the top-N per bucket so the
    # table reads "for Meta AI, top X people pulled it in".
    by_bot: dict[int, dict] = {}
    for s in summoners:
        bid = s.get("bot_contact_id")
        if bid is None:
            continue
        bucket = by_bot.setdefault(bid, {"bot_name": s.get("bot_name") or "Bot", "rows": []})
        bucket["rows"].append(s)

    summon_blocks = []
    for bid, info in by_bot.items():
        rows = info["rows"][:top_n]
        total_for_bot = sum((r.get("mention_count") or 0) for r in info["rows"])
        body = ""
        for r in rows:
            body += f"""
            <tr>
                <td><strong>{_h(r.get('summoner_name') or 'Unknown')}</strong></td>
                <td class="jid-cell"><code>{_h(r.get('summoner_jid', ''))}</code></td>
                <td class="jid-cell"><code>{_h(r.get('summoner_lid', ''))}</code></td>
                <td class="num"><strong>{_num(r.get('mention_count'))}</strong></td>
            </tr>
            """
        cap_note = (
            f"showing top {len(rows)} of {len(info['rows'])} summoners · "
            f"{_num(total_for_bot)} total mentions"
        )
        summon_blocks.append(f"""
        <h3>Who @-mentions <span style='color:#7b1fa2;'>{_h(info['bot_name'])}</span> the most
            <span class="count">({cap_note})</span>
        </h3>
        <table class="data-table compact">
            <thead><tr><th>Summoner</th><th>Phone JID</th><th>LID</th><th>Times</th></tr></thead>
            <tbody>{body}</tbody>
        </table>
        """)
    summoners_html = (
        ''.join(summon_blocks) or
        '<p class="empty">No human-to-bot @-mentions in this window.</p>'
    )

    total_bots = len(bots)
    meta_ai = sum(1 for b in bots if b.get("is_meta_ai"))
    bot_msgs = sum((b.get("total_messages") or 0) for b in bots)
    total_mentions = sum((s.get("mention_count") or 0) for s in summoners)

    summary_pills = (
        f'<span class="pill">Bots seen: {total_bots}</span>'
        f'<span class="pill">Meta AI: {meta_ai}</span>'
        f'<span class="pill">Bot replies: {_num(bot_msgs)}</span>'
        f'<span class="pill">Bot @-mentions by humans: {_num(total_mentions)}</span>'
    )

    return f"""
    <div class="section" id="bot-activity">
        <h2>Bot Activity</h2>
        <div style="margin:6px 0 12px;">{summary_pills}</div>
        <p style="font-size:11px;color:#666;margin-bottom:8px;">
            Meta AI / bot interactions are tracked here separately
            from the human roster and the regular Mention Network.
            These contacts are not group members in the participant
            sense — they are system identities that reply to invocations
            within the group.  The Mention Network section excludes them
            so the human-to-human communication graph stays clean.
        </p>
        <h3>Bot Interactions <span class="count">({total_bots} bot{'s' if total_bots != 1 else ''})</span></h3>
        <table class="data-table compact">
            <thead><tr>
                <th>Bot</th><th>Phone</th><th>JID</th>
                <th>Messages</th><th>First seen</th><th>Last seen</th>
            </tr></thead>
            <tbody>{bot_rows_html}</tbody>
        </table>
        {summoners_html}
    </div>
    """


def _section_past_members(past: list[dict], owner_cid: int | None = None) -> str:
    """Former Members — always renders the section (with an explicit
    empty-state placeholder when there are no past participants) so the
    analyst sees that the source table was checked.  Previously this
    returned ``""`` when empty, which made the section silently vanish
    from the report and looked like the rendering had broken.
    """
    if not past:
        return (
            '<div class="section" id="past-members">'
            '<h2>Former Members <span class="count">(0)</span></h2>'
            '<p class="empty" style="color:#666;font-style:italic;">'
            'No former members recorded for this group.  '
            'WhatsApp\'s <code>group_past_participant</code> table was '
            'checked and returned no rows for this conversation.'
            '</p>'
            '</div>'
        )

    rows = []
    for p in past:
        name_html = f"<strong>{_h(p.get('name'))}</strong>"
        if owner_cid and p.get("contact_id") == owner_cid:
            name_html += ' <span class="owner-badge">Device Owner</span>'
        state = p.get("state")
        if state == -2:
            name_html += ' <span style="color:#ff9800;font-size:9px;">(found via messages)</span>'
        rows.append(f"""
        <tr>
            <td>{name_html}</td>
            <td>{_h(p.get('phone_number', ''))}</td>
            <td class="jid-cell"><code>{_h(p.get('phone_jid', ''))}</code></td>
            <td class="jid-cell"><code>{_h(p.get('lid_jid', ''))}</code></td>
            <td class="num">{_num(p.get('total_messages'))}</td>
            <td>{_ts_short(p.get('last_seen_ts'))}</td>
        </tr>
        """)

    return f"""
    <div class="section" id="past-members">
        <h2>Former Members <span class="count">({len(past)})</span></h2>
        <table class="data-table compact">
            <thead><tr><th>Name</th><th>Phone</th><th>JID</th><th>LID</th><th>Messages</th><th>Last Seen</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
        </table>
    </div>
    """


# ====================================================================== #
# HTML wrapper with embedded CSS
# ====================================================================== #

def _wrap_html(title: str, group_name: str, sections: list[str],
               generated_at: str,
               nav_items: list[tuple[str, str]] | None = None,
               case_info: dict | None = None,
               date_range_str: str = "") -> str:
    if nav_items is None:
        nav_items = [
            ("identity", "Identity"),
            ("owner-policy", "Owner & Policy"),
            ("summary", "Summary"),
            ("edit-history", "Edit History"), ("members", "Members"),
            ("contributors", "Contributors"), ("forwarders", "Forwarders"),
            ("devices", "Devices"),
            ("mentions", "Mentions"), ("activity", "Activity"),
            ("calls", "Calls"), ("locations", "Locations"),
            ("admin-audit", "Admin Audit"),
            ("media-links", "Media & Links"), ("past-members", "Former Members"),
        ]
    nav_html = "".join(
        f'<a href="#{nid}">{nlabel}</a>' for nid, nlabel in nav_items
    )

    date_range_row = (
        f"<tr><td>Timeline Window</td>"
        f"<td><strong style='color:#fff59d;'>{_h(date_range_str)}</strong>"
        f" <span style='color:rgba(255,255,255,0.6);font-size:11px;'>"
        f"&nbsp;(filtered)</span></td></tr>"
        if date_range_str else ""
    )

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
.description {{ color: #666; margin-top: 8px; font-size: 13px; white-space: pre-wrap; max-height: 120px; overflow: auto; background: #f5f5f5; padding: 8px; border-radius: 4px; }}

/* Info table */
.info-table {{ width: 100%; border-collapse: collapse; }}
.info-table td {{ padding: 4px 8px; border-bottom: 1px solid #eee; font-size: 13px; }}
.info-table td:first-child {{ color: #555; width: 180px; font-weight: 600; white-space: nowrap; }}
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
.arrow-cell {{ text-align: center; color: #999; font-weight: bold; }}

/* Roles */
.role-superadmin {{ background: #f4511e; color: white; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: bold; }}
.role-admin {{ background: #ffb300; color: #1a1a1a; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: bold; }}
.role-member {{ color: #999; font-size: 10px; }}
.member-label {{ background: #f0f4f8; color: #128c7e; padding: 1px 6px; border-radius: 4px; font-size: 10px; margin-left: 6px; }}

/* Bar chart */
.bar-chart {{ max-width: 600px; }}
.bar-row {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
.bar-name {{ width: 280px; font-size: 12px; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.bar-track {{ flex: 1; height: 18px; background: #eee; border-radius: 3px; overflow: hidden; }}
.bar-fill {{ height: 100%; background: linear-gradient(90deg, #075e54, #128c7e); border-radius: 3px; }}
.bar-value {{ width: 60px; font-size: 11px; color: #666; font-variant-numeric: tabular-nums; }}

/* Hourly chart */
.hourly-chart {{ display: flex; align-items: flex-end; gap: 3px; height: 120px; padding: 0 4px; }}
.hour-bar {{ flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }}
.hour-fill {{ width: 100%; background: linear-gradient(0deg, #075e54, #43a047); border-radius: 2px 2px 0 0; min-height: 2px; }}
.hour-label {{ font-size: 9px; color: #999; margin-top: 4px; }}

/* Edit history */
.edit-timeline {{ display: flex; flex-direction: column; gap: 8px; }}
.edit-card {{ background: #fafafa; border: 1px solid #eee; border-radius: 8px; padding: 12px 16px; }}
.edit-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.edit-icon {{ font-size: 16px; }}
.edit-label {{ font-weight: bold; font-size: 13px; }}
.edit-ts {{ color: #999; font-size: 11px; margin-left: auto; }}
.edit-by {{ font-size: 11px; color: #666; margin-bottom: 6px; }}
.edit-forensic {{ margin-top: 6px; }}
.forensic {{ font-size: 9px; color: #aaa; font-style: italic; }}

/* Change values */
.change-values {{ background: #e3f2fd; border: 1px solid #bbdefb; border-radius: 6px; padding: 8px 12px; font-size: 13px; }}
.old-val {{ color: #c62828; text-decoration: line-through; }}
.new-val {{ color: #1565c0; font-weight: bold; }}
.arrow {{ color: #999; font-weight: bold; margin: 0 8px; }}
.desc-change {{ background: #e0f2f1; border: 1px solid #b2dfdb; border-radius: 6px; padding: 8px 12px; font-size: 12px; white-space: pre-wrap; max-height: 200px; overflow: auto; }}
.dp-change {{ display: flex; align-items: center; gap: 16px; background: #f3e5f5; border: 1px solid #e1bee7; border-radius: 6px; padding: 12px; }}
.dp-box {{ text-align: center; }}
.dp-label {{ font-size: 10px; color: #999; margin-bottom: 4px; }}
.dp-box img {{ border-radius: 50%; border: 2px solid #ddd; }}
.dp-arrow {{ font-size: 20px; color: #999; font-weight: bold; }}
.dp-removed {{ font-size: 32px; }}
.setting-val {{ background: #fff3e0; border: 1px solid #ffe0b2; border-radius: 6px; padding: 8px 12px; font-size: 12px; }}

/* Identity helpers */
.owner-badge {{ background: #ff8f00; color: white; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold; }}
.dim {{ color: #888; }}
.jid {{ font-size: 10px; color: #128c7e; font-family: 'Consolas', monospace; }}

/* Mention grid */
.mention-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 768px) {{ .mention-grid {{ grid-template-columns: 1fr; }} }}

/* Footer */
.report-footer {{ text-align: center; padding: 24px; color: #999; font-size: 11px; border-top: 1px solid #eee; margin: 16px; }}

/* Inline pills for category breakdowns */
.pill {{ display:inline-block; background:#e8f5e9; color:#1b5e20; padding:2px 8px;
        border-radius:10px; font-size:10px; font-weight:600; margin-right:4px; }}
.pill-warn {{ background:#ffebee; color:#c62828; }}
a {{ color:#075e54; }}
</style>
</head>
<body>

<div class="report-header">
    <h1>Group Forensic Report</h1>
    {_render_case_banner(case_info or {})}
    <table class="info-table">
        <tr><td>Group Name</td><td><strong>{_h(group_name)}</strong></td></tr>
        {date_range_row}
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
