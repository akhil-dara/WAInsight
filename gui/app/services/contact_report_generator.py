"""
Comprehensive WhatsApp Contact Activity Report Generator.

Produces a self-contained HTML report (convertible to PDF via weasyprint)
with full forensic details: device history, group participation, calls,
media breakdown, heatmaps, business info, and more.
"""
from __future__ import annotations

import base64
import logging
from datetime import date, datetime, time as dtime
from typing import Optional

from app.config import (
    date_range_to_timestamps,
    format_timestamp,
    format_timestamp_with_utc,
    get_current_timezone_display,
    sqlite_localtime_modifiers,
)

logger = logging.getLogger(__name__)


class ContactReportGenerator:
    """Generate a comprehensive forensic activity report for a WhatsApp contact."""

    def __init__(self, contact_id: int,
                 date_from: Optional[date] = None,
                 date_to: Optional[date] = None,
                 case_info: Optional[dict] = None):
        self.contact_id = contact_id
        self.date_from = date_from
        self.date_to = date_to
        self.case_info = case_info or {}
        self._ts_from, self._ts_to = date_range_to_timestamps(date_from, date_to)

    def generate_html(self) -> str:
        from app.services.database import Database
        db = Database.get()

        overview = self._fetch_overview(db)
        devices = self._fetch_devices(db)
        heatmap = self._fetch_heatmap(db)
        daily = self._fetch_daily(db)
        groups_current, groups_past = self._fetch_groups(db)
        personal = self._fetch_personal(db)
        calls = self._fetch_calls(db)
        locations = self._fetch_locations(db)
        media = self._fetch_media(db)
        reactions = self._fetch_reactions(db)
        mentions = self._fetch_mentions(db)
        keyid_stats = self._fetch_keyid_stats(db)

        body = ""
        body += self._render_case_header(overview)
        body += self._render_overview(overview)
        body += self._render_devices(devices, keyid_stats)
        body += self._render_heatmap(heatmap)
        body += self._render_calendar(daily)
        body += self._render_groups(groups_current, groups_past)
        body += self._render_personal(personal)
        body += self._render_calls(calls)
        body += self._render_locations(locations)
        body += self._render_media(media)
        body += self._render_interactions(reactions, mentions)

        return self._wrap_html(body, overview.get("name", "Unknown"))

    # ================================================================
    # DATA FETCHING
    # ================================================================

    def _ts_filter(self, col: str = "m.timestamp") -> str:
        parts = []
        if self._ts_from:
            parts.append(f"{col} >= {self._ts_from}")
        if self._ts_to:
            parts.append(f"{col} <= {self._ts_to}")
        return (" AND " + " AND ".join(parts)) if parts else ""

    def _fetch_overview(self, db) -> dict:
        row = db.fetchone("""
            SELECT c.resolved_name, c.phone_jid, c.lid_jid, c.phone_number,
                   c.wa_name, c.display_name, c.status_text,
                   c.platform_estimate, c.platform_confidence,
                   c.is_business, c.is_meta_verified, c.trust_tier,
                   c.business_name, c.business_category, c.business_description,
                   c.business_address, c.business_email, c.business_website,
                   c.business_member_since,
                   c.fb_linked_name, c.fb_linked_likes,
                   c.ig_linked_name, c.ig_linked_followers,
                   c.avatar_blob, c.message_count,
                   c.personal_msg_count, c.group_msg_count,
                   c.linked_device_count
            FROM contact c WHERE c.id = ?
        """, (self.contact_id,))
        if not row:
            return {"name": "Unknown", "contact_id": self.contact_id}

        return {
            "name": row[0] or row[4] or row[3] or "Unknown",
            "phone_jid": row[1] or "",
            "lid_jid": row[2] or "",
            "phone_number": row[3] or "",
            "wa_name": row[4] or "",
            "display_name": row[5] or "",
            "status_text": row[6] or "",
            "platform": row[7] or "",
            "platform_confidence": row[8] or 0,
            "is_business": bool(row[9]),
            "is_meta_verified": bool(row[10]),
            "trust_tier": row[11] or "",
            "business_name": row[12] or "",
            "business_category": row[13] or "",
            "business_description": row[14] or "",
            "business_address": row[15] or "",
            "business_email": row[16] or "",
            "business_website": row[17] or "",
            "business_member_since": row[18] or "",
            "fb_linked_name": row[19] or "",
            "fb_linked_likes": row[20] or 0,
            "ig_linked_name": row[21] or "",
            "ig_linked_followers": row[22] or 0,
            "avatar_blob": row[23],
            "total_msgs": row[24] or 0,
            "personal_msgs": row[25] or 0,
            "group_msgs": row[26] or 0,
            "linked_devices": row[27] or 0,
            "contact_id": self.contact_id,
        }

        # Add owner message count (messages sent by owner TO this contact in personal chat)
        try:
            owner_msgs = db.fetchone("""
                SELECT COUNT(*), cm.value, cm2.value
                FROM message m
                JOIN conversation cv ON cv.id = m.conversation_id
                LEFT JOIN case_metadata cm ON cm.key = 'device_owner_name'
                LEFT JOIN case_metadata cm2 ON cm2.key = 'device_owner_jid'
                WHERE cv.jid_raw_string = ? AND cv.chat_type = 'personal'
                  AND (m.from_me = 1 OR m.sender_id IS NULL)
            """, (result["phone_jid"],))
            if owner_msgs and owner_msgs[0]:
                result["owner_msgs_to_contact"] = owner_msgs[0] or 0
                result["owner_name"] = owner_msgs[1] or "Owner"
                result["owner_jid"] = owner_msgs[2] or ""
        except Exception:
            pass

        return result

    def _fetch_devices(self, db) -> list:
        return db.fetchall("""
            SELECT md.device_number, md.platform_label,
                   COUNT(*) AS msgs, MIN(m.timestamp) AS first_ts, MAX(m.timestamp) AS last_ts,
                   ROUND(AVG(md.platform_confidence), 2) AS avg_conf,
                   SUM(CASE WHEN cv.chat_type = 'personal' THEN 1 ELSE 0 END) AS personal,
                   SUM(CASE WHEN cv.chat_type != 'personal' THEN 1 ELSE 0 END) AS grp
            FROM message_device md
            JOIN message m ON m.id = md.message_id
            JOIN conversation cv ON cv.id = m.conversation_id
            WHERE m.sender_id = ? AND m.message_type NOT IN (7, 64)
              AND md.platform_label NOT IN ('newsletter', 'channel_bot')
            """ + self._ts_filter() + """
            GROUP BY md.device_number, md.platform_label
            ORDER BY first_ts
        """, (self.contact_id,))

    def _fetch_heatmap(self, db) -> list:
        sqlite_mods = ", ".join(f"'{m}'" for m in sqlite_localtime_modifiers())
        if self._ts_from or self._ts_to:
            rows = db.fetchall("""
                SELECT CAST(strftime('%%w', m.timestamp/1000, """ + sqlite_mods + """) AS INT) as dow,
                       CAST(strftime('%%H', m.timestamp/1000, """ + sqlite_mods + """) AS INT) as hour,
                       COUNT(*) as cnt
                FROM message m WHERE m.sender_id = ? AND m.message_type NOT IN (7, 64)
                """ + self._ts_filter() + """
                GROUP BY dow, hour
            """, (self.contact_id,))
        else:
            rows = db.fetchall("""
                SELECT day_of_week, hour_of_day, SUM(message_count)
                FROM stats_hourly_heatmap
                WHERE contact_id = ?
                GROUP BY day_of_week, hour_of_day
            """, (self.contact_id,))
        grid = [[0]*24 for _ in range(7)]
        for r in rows:
            dow, hour, cnt = r[0] or 0, r[1] or 0, r[2] or 0
            if 0 <= dow < 7 and 0 <= hour < 24:
                grid[dow][hour] += cnt
        return grid

    def _fetch_daily(self, db) -> dict:
        sqlite_mods = ", ".join(f"'{m}'" for m in sqlite_localtime_modifiers())
        rows = db.fetchall("""
            SELECT DATE(m.timestamp/1000, """ + sqlite_mods + """) as d, COUNT(*)
            FROM message m WHERE m.sender_id = ? AND m.message_type NOT IN (7, 64)
            """ + self._ts_filter() + """
            GROUP BY d ORDER BY d
        """, (self.contact_id,))
        return {r[0]: r[1] for r in rows if r[0]}

    def _fetch_groups(self, db) -> tuple:
        current = db.fetchall("""
            SELECT cv.id, cv.display_name, cv.avatar_blob, cv.participant_count,
                   gm.role, gm.join_timestamp,
                   COALESCE(sca.total_messages, 0), COALESCE(sca.total_images, 0),
                   COALESCE(sca.total_videos, 0), COALESCE(sca.total_audio, 0),
                   COALESCE(sca.total_documents, 0),
                   sca.first_message_ts, sca.last_message_ts,
                   gm.label
            FROM group_member gm
            JOIN conversation cv ON cv.id = gm.conversation_id
            LEFT JOIN stats_contact_activity sca ON sca.contact_id = ? AND sca.conversation_id = cv.id
            WHERE gm.contact_id = ? AND gm.is_current = 1 AND cv.chat_type = 'group'
            ORDER BY COALESCE(sca.total_messages, 0) DESC
        """, (self.contact_id, self.contact_id))
        past = db.fetchall("""
            SELECT cv.id, cv.display_name, gm.left_timestamp, gm.left_reason, gm.role,
                   COALESCE(sca.total_messages, 0), sca.first_message_ts, sca.last_message_ts
            FROM group_member gm
            JOIN conversation cv ON cv.id = gm.conversation_id
            LEFT JOIN stats_contact_activity sca ON sca.contact_id = ? AND sca.conversation_id = cv.id
            WHERE gm.contact_id = ? AND gm.is_current = 0 AND cv.chat_type = 'group'
            ORDER BY gm.left_timestamp DESC
        """, (self.contact_id, self.contact_id))
        return current, past

    def _fetch_personal(self, db) -> dict | None:
        row = db.fetchone("""
            SELECT cv.id, cv.display_name, sca.total_messages,
                   sca.total_text, sca.total_media, sca.total_images, sca.total_videos,
                   sca.total_audio, sca.total_documents,
                   sca.first_message_ts, sca.last_message_ts
            FROM stats_contact_activity sca
            JOIN conversation cv ON cv.id = sca.conversation_id
            WHERE sca.contact_id = ? AND cv.chat_type = 'personal'
            LIMIT 1
        """, (self.contact_id,))
        if not row:
            return None
        return {"conv_id": row[0], "name": row[1], "total": row[2] or 0,
                "text": row[3] or 0, "media": row[4] or 0, "images": row[5] or 0,
                "videos": row[6] or 0, "audio": row[7] or 0, "docs": row[8] or 0,
                "first_ts": row[9], "last_ts": row[10]}

    def _fetch_calls(self, db) -> dict:
        rows = db.fetchall("""
            SELECT cr.from_me, cr.is_video, cr.duration_sec, cr.result_label,
                   cr.timestamp, COALESCE(cr.call_category, 'personal') AS call_category
            FROM call_record cr
            WHERE cr.contact_id = ?
            """ + self._ts_filter("cr.timestamp") + """
            ORDER BY cr.timestamp DESC
        """, (self.contact_id,))
        total = len(rows)
        voice = sum(1 for r in rows if not r[1])
        video = sum(1 for r in rows if r[1])
        made = sum(1 for r in rows if r[0])
        received = total - made
        total_dur = sum((r[2] or 0) for r in rows)
        voice_chats = sum(1 for r in rows if r[5] == "voice_chat")
        group_calls = sum(1 for r in rows if r[5] == "group_call")
        multi_person = sum(1 for r in rows if r[5] == "multi_person")
        return {"total": total, "voice": voice, "video": video,
                "made": made, "received": received, "total_duration": total_dur,
                "voice_chats": voice_chats, "group_calls": group_calls,
                "multi_person": multi_person, "calls": rows}

    def _fetch_locations(self, db) -> list:
        """Fetch location messages sent by this contact."""
        try:
            return db.fetchall("""
                SELECT l.latitude, l.longitude, l.place_name, l.place_address,
                       l.is_live, l.live_duration, l.map_preview_url,
                       l.thumbnail_blob, m.timestamp, m.from_me,
                       COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name
                FROM location l
                JOIN message m ON m.id = l.message_id
                LEFT JOIN conversation conv ON conv.id = m.conversation_id
                WHERE m.sender_id = ?
                """ + self._ts_filter("m.timestamp") + """
                ORDER BY m.timestamp DESC
            """, (self.contact_id,))
        except Exception:
            return []

    def _fetch_media(self, db) -> dict:
        rows = db.fetchall("""
            SELECT cv.chat_type,
                   SUM(sca.total_images), SUM(sca.total_videos), SUM(sca.total_audio),
                   SUM(sca.total_documents), SUM(sca.total_stickers), SUM(sca.total_gifs)
            FROM stats_contact_activity sca
            JOIN conversation cv ON cv.id = sca.conversation_id
            WHERE sca.contact_id = ?
            GROUP BY cv.chat_type
        """, (self.contact_id,))
        result = {}
        for r in rows:
            result[r[0] or "other"] = {
                "images": r[1] or 0, "videos": r[2] or 0, "audio": r[3] or 0,
                "docs": r[4] or 0, "stickers": r[5] or 0, "gifs": r[6] or 0,
            }
        return result

    def _fetch_reactions(self, db) -> dict:
        given = db.fetchall("""
            SELECT r.emoji, COUNT(*) FROM reaction r
            WHERE r.reactor_id = ? GROUP BY r.emoji ORDER BY COUNT(*) DESC LIMIT 15
        """, (self.contact_id,))
        received = db.fetchall("""
            SELECT r.emoji, COUNT(*) FROM reaction r
            JOIN message m ON m.id = r.message_id
            WHERE m.sender_id = ? AND r.reactor_id != ? GROUP BY r.emoji ORDER BY COUNT(*) DESC LIMIT 15
        """, (self.contact_id, self.contact_id))
        return {"given": given, "received": received}

    def _fetch_mentions(self, db) -> dict:
        # Get device owner info for "You" display
        _owner_name = "Device Owner"
        _owner_jid = ""
        try:
            _orow = db.fetchone("SELECT value FROM case_metadata WHERE key = 'device_owner_name'")
            if _orow:
                _owner_name = _orow[0]
            _ojid = db.fetchone("SELECT value FROM case_metadata WHERE key = 'device_owner_jid'")
            if _ojid:
                _owner_jid = _ojid[0]
        except Exception:
            pass

        mentioned_by = db.fetchall("""
            SELECT COALESCE(c.resolved_name, c.phone_number,
                   CASE WHEN m.from_me = 1 THEN ? ELSE 'Unknown' END),
                   COUNT(*),
                   COALESCE(c.phone_jid, c.lid_jid,
                   CASE WHEN m.from_me = 1 THEN ? ELSE '' END)
            FROM mention mn
            JOIN message m ON m.id = mn.message_id
            LEFT JOIN contact c ON c.id = m.sender_id
            WHERE mn.mentioned_id = ? GROUP BY m.sender_id ORDER BY COUNT(*) DESC LIMIT 10
        """, (_owner_name, _owner_jid, self.contact_id))
        mentioning = db.fetchall("""
            SELECT COALESCE(c.resolved_name, c.phone_number,
                   CASE WHEN mn.mention_type IN (1, 2) THEN '@everyone'
                        WHEN mn.display_name IS NOT NULL AND mn.display_name != ''
                             THEN REPLACE(mn.display_name, '|', ' ')
                        ELSE 'Unknown' END),
                   COUNT(*),
                   COALESCE(c.phone_jid, c.lid_jid, '')
            FROM mention mn
            JOIN message m ON m.id = mn.message_id
            LEFT JOIN contact c ON c.id = mn.mentioned_id
            WHERE m.sender_id = ? GROUP BY COALESCE(mn.mentioned_id, mn.display_name, mn.mention_type)
            ORDER BY COUNT(*) DESC LIMIT 10
        """, (self.contact_id,))
        return {"mentioned_by": mentioned_by, "mentioning": mentioning}

    def _fetch_keyid_stats(self, db) -> list:
        return db.fetchall("""
            SELECT length(m.source_key_id) AS key_len,
                   substr(upper(m.source_key_id), 1, 2) AS prefix2,
                   md.device_number, md.platform_label,
                   COUNT(*) AS cnt
            FROM message m
            JOIN message_device md ON md.message_id = m.id
            WHERE m.sender_id = ? AND m.message_type NOT IN (7, 64)
            GROUP BY key_len, prefix2, md.device_number, md.platform_label
            ORDER BY md.device_number, cnt DESC
        """, (self.contact_id,))

    # ================================================================
    # HTML RENDERING
    # ================================================================

    def _esc(self, s: str) -> str:
        if not s:
            return ""
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def _fmt_ts(self, ts_ms) -> str:
        if not ts_ms:
            return "-"
        return format_timestamp_with_utc(ts_ms, "datetime") or "-"

    def _fmt_date(self, ts_ms) -> str:
        if not ts_ms:
            return "-"
        return format_timestamp(ts_ms, "date") or "-"

    def _fmt_dur(self, seconds: int) -> str:
        if not seconds:
            return "0s"
        h, r = divmod(seconds, 3600)
        m, s = divmod(r, 60)
        parts = []
        if h:
            parts.append(f"{h}h")
        if m:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    def _avatar_b64(self, blob) -> str:
        if not blob or len(blob) < 100:
            return ""
        return f"data:image/jpeg;base64,{base64.b64encode(blob).decode()}"

    def _render_case_header(self, overview: dict) -> str:
        h = '<div class="report-section case-header">'
        h += '<h1>WhatsApp Contact Activity Report</h1>'
        if self.case_info:
            h += '<table class="info-table">'
            if self.case_info.get("case_id"):
                h += f'<tr><td class="key">Case ID</td><td>{self._esc(self.case_info["case_id"])}</td></tr>'
            if self.case_info.get("examiner"):
                h += f'<tr><td class="key">Examiner</td><td>{self._esc(self.case_info["examiner"])}</td></tr>'
            if self.case_info.get("device_owner"):
                h += f'<tr><td class="key">Device Owner</td><td>{self._esc(self.case_info["device_owner"])}</td></tr>'
            if self.case_info.get("device_owner_jid"):
                h += f'<tr><td class="key">Owner JID</td><td class="mono">{self._esc(self.case_info["device_owner_jid"])}</td></tr>'
            h += f'<tr><td class="key">Report Generated</td><td>{format_timestamp_with_utc(int(datetime.now().timestamp() * 1000), "full")}</td></tr>'
            h += f'<tr><td class="key">Timezone</td><td>{self._esc(get_current_timezone_display())}</td></tr>'
            if self.date_from or self.date_to:
                h += f'<tr><td class="key">Date Range</td><td>{self.date_from or "start"} to {self.date_to or "end"}</td></tr>'
            h += '</table>'
        h += '</div>'
        return h

    def _render_overview(self, o: dict) -> str:
        avatar_html = ""
        avatar_src = self._avatar_b64(o.get("avatar_blob"))
        if avatar_src:
            avatar_html = f'<img src="{avatar_src}" class="avatar-large">'
        else:
            initials = "".join(w[0] for w in (o["name"] or "?").split()[:2] if w and w[0].isalpha())[:2].upper() or "?"
            avatar_html = f'<div class="avatar-placeholder">{initials}</div>'

        verified = ""
        if o.get("is_meta_verified"):
            verified = '<span class="verified-badge">Meta Verified</span>'
        elif o.get("is_business"):
            verified = '<span class="biz-badge">WhatsApp Business</span>'

        platform_text = ""
        plat = o.get("platform", "")
        conf = o.get("platform_confidence", 0)
        _p = {"android": "Android", "iphone": "iPhone", "phone": "Phone", "multi_device": "Multi-Device"}.get(plat, plat)
        if _p:
            platform_text = f'{_p} ({conf*100:.0f}% confidence)' if conf else _p

        h = '<div class="report-section">'
        h += '<h2>Contact Overview</h2>'
        h += '<div class="overview-grid">'
        h += f'<div class="overview-avatar">{avatar_html}</div>'
        h += '<div class="overview-info">'
        h += f'<h3>{self._esc(o["name"])} {verified}</h3>'
        h += '<table class="info-table">'
        h += f'<tr><td class="key">Contact ID</td><td>{o["contact_id"]}</td></tr>'
        if o["phone_number"]:
            h += f'<tr><td class="key">Phone</td><td>+{self._esc(o["phone_number"])}</td></tr>'
        if o["phone_jid"]:
            h += f'<tr><td class="key">Phone JID</td><td class="mono">{self._esc(o["phone_jid"])}</td></tr>'
        if o["lid_jid"]:
            h += f'<tr><td class="key">LID JID</td><td class="mono">{self._esc(o["lid_jid"])}</td></tr>'
        if o["wa_name"]:
            h += f'<tr><td class="key">WhatsApp Name</td><td>{self._esc(o["wa_name"])}</td></tr>'
        if o["status_text"]:
            h += f'<tr><td class="key">Status</td><td>{self._esc(o["status_text"])}</td></tr>'
        if platform_text:
            h += f'<tr><td class="key">Primary Device</td><td><strong>{platform_text}</strong></td></tr>'
        _name = o["name"].split()[0] if o["name"] else "This contact"
        h += f'<tr><td class="key">Messages by {self._esc(_name)}</td><td>{o["total_msgs"]:,}</td></tr>'
        h += f'<tr><td class="key">&nbsp;&nbsp;In Personal Chats</td><td>{o["personal_msgs"]:,}</td></tr>'
        h += f'<tr><td class="key">&nbsp;&nbsp;In Group Chats</td><td>{o["group_msgs"]:,}</td></tr>'
        owner_msgs = o.get("owner_msgs_to_contact", 0)
        owner_name = o.get("owner_name", "Owner")
        owner_jid = o.get("owner_jid", "")
        if owner_msgs:
            _owner_label = f"{self._esc(owner_name)}"
            if owner_jid:
                _owner_label += f" <span style='color:#667781;font-size:10px'>({self._esc(owner_jid)})</span>"
            h += f'<tr><td class="key">Messages by {_owner_label} <span style="color:#667781;font-size:9px">(device owner)</span></td><td>{owner_msgs:,}</td></tr>'
        if o["linked_devices"]:
            h += f'<tr><td class="key">Companion Devices</td><td>{o["linked_devices"]} seen historically</td></tr>'
        # Business info
        if o["is_business"]:
            if o["trust_tier"]:
                _tier = {"TIER_2": "Meta Verified (Blue Tick)", "TIER_1": "Official Business", "TIER_0": "WhatsApp Business User"}.get(o["trust_tier"], o["trust_tier"])
                h += f'<tr><td class="key">Trust Tier</td><td>{_tier}</td></tr>'
            if o["business_category"]:
                h += f'<tr><td class="key">Category</td><td>{self._esc(o["business_category"])}</td></tr>'
            if o["business_description"]:
                h += f'<tr><td class="key">Description</td><td>{self._esc(o["business_description"][:200])}</td></tr>'
            if o["business_address"]:
                h += f'<tr><td class="key">Address</td><td>{self._esc(o["business_address"])}</td></tr>'
            if o["business_email"]:
                h += f'<tr><td class="key">Email</td><td>{self._esc(o["business_email"])}</td></tr>'
            if o["business_website"]:
                h += f'<tr><td class="key">Website</td><td><a href="{self._esc(o["business_website"])}">{self._esc(o["business_website"])}</a></td></tr>'
            if o["business_member_since"]:
                h += f'<tr><td class="key">Member Since</td><td>{self._esc(o["business_member_since"])}</td></tr>'
            if o["fb_linked_name"]:
                h += f'<tr><td class="key">Facebook</td><td>{self._esc(o["fb_linked_name"])} ({o["fb_linked_likes"]:,} likes)</td></tr>'
            if o["ig_linked_name"]:
                h += f'<tr><td class="key">Instagram</td><td>@{self._esc(o["ig_linked_name"])} ({o["ig_linked_followers"]:,} followers)</td></tr>'
        h += '</table></div></div></div>'
        return h

    def _render_devices(self, devices: list, keyid_stats: list) -> str:
        if not devices:
            return ""
        _P = {"android": "Android", "iphone": "iPhone", "companion": "Web/Desktop",
              "android_linked": "Android (Linked)", "iphone_linked": "iPhone (Linked)",
              "business_api": "Business API"}

        h = '<div class="report-section"><h2>Device History</h2>'
        h += '<table class="data-table"><thead><tr>'
        h += '<th>Device #</th><th>Platform</th><th>Personal</th><th>Group</th><th>Total</th><th>First Seen</th><th>Last Seen</th><th>Confidence</th>'
        h += '</tr></thead><tbody>'
        for d in devices:
            dev = d[0] or 0
            plat = _P.get(d[1] or "", d[1] or "")
            conf = d[5] or 0
            h += f'<tr><td>{"Primary (0)" if dev == 0 else f"Companion #{dev}"}</td>'
            h += f'<td>{plat}</td><td>{d[6] or 0}</td><td>{d[7] or 0}</td><td>{d[2]}</td>'
            h += f'<td>{self._fmt_date(d[3])}</td><td>{self._fmt_date(d[4])}</td>'
            h += f'<td>{conf*100:.0f}%</td></tr>'
        h += '</tbody></table>'

        # Key ID forensic table
        if keyid_stats:
            h += '<h3>Key ID Pattern Analysis</h3>'
            h += '<table class="data-table mono"><thead><tr><th>Length</th><th>Prefix</th><th>Device</th><th>Platform</th><th>Count</th></tr></thead><tbody>'
            for r in keyid_stats:
                dev = r[2] or 0
                h += f'<tr><td>{r[0]}</td><td>{r[1]}</td><td>{"P" if dev == 0 else f"C#{dev}"}</td><td>{r[3] or ""}</td><td>{r[4]}</td></tr>'
            h += '</tbody></table>'
        h += '</div>'
        return h

    def _render_heatmap(self, grid: list) -> str:
        max_val = max(max(row) for row in grid) if any(any(row) for row in grid) else 1
        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        h = '<div class="report-section"><h2>Activity Heatmap (Day x Hour)</h2>'
        h += '<div class="heatmap-container">'
        h += '<svg viewBox="0 0 520 160" xmlns="http://www.w3.org/2000/svg" style="width:100%;max-width:700px">'
        for di in range(7):
            h += f'<text x="0" y="{20 + di * 19 + 12}" font-size="10" fill="#666">{days[di]}</text>'
            for hr in range(24):
                val = grid[di][hr]
                intensity = val / max_val if max_val else 0
                if val == 0:
                    fill = "#f5f5f5"
                elif intensity < 0.25:
                    fill = "#c8e6c9"
                elif intensity < 0.5:
                    fill = "#81c784"
                elif intensity < 0.75:
                    fill = "#43a047"
                else:
                    fill = "#1b5e20"
                x = 35 + hr * 20
                y = 20 + di * 19
                h += f'<rect x="{x}" y="{y}" width="18" height="17" rx="2" fill="{fill}"><title>{days[di]} {hr:02d}:00 - {val} msgs</title></rect>'
        # Hour labels
        for hr in range(24):
            if hr % 3 == 0:
                h += f'<text x="{35 + hr * 20 + 9}" y="14" font-size="8" text-anchor="middle" fill="#999">{hr}</text>'
        h += '</svg></div></div>'
        return h

    def _render_calendar(self, daily: dict) -> str:
        if not daily:
            return ""
        max_val = max(daily.values()) if daily else 1
        dates = sorted(daily.keys())
        if not dates:
            return ""

        h = '<div class="report-section"><h2>Daily Activity Calendar</h2>'
        h += f'<p>{len(dates)} active days, {sum(daily.values()):,} total messages</p>'
        # Simple bar chart of top 20 days
        top = sorted(daily.items(), key=lambda x: -x[1])[:20]
        h += '<table class="data-table"><thead><tr><th>Date</th><th>Messages</th><th></th></tr></thead><tbody>'
        for d, cnt in top:
            pct = cnt / max_val * 100
            h += f'<tr><td>{d}</td><td>{cnt}</td><td><div class="bar" style="width:{pct:.0f}%"></div></td></tr>'
        h += '</tbody></table></div>'
        return h

    def _render_groups(self, current: list, past: list) -> str:
        if not current and not past:
            return ""
        h = '<div class="report-section"><h2>Groups</h2>'

        if current:
            h += f'<h3>Current Groups ({len(current)})</h3>'
            h += '<table class="data-table"><thead><tr><th></th><th>Group</th><th>Role</th><th>Alias</th><th>Messages</th><th>Media</th><th>First Msg</th><th>Last Msg</th></tr></thead><tbody>'
            for g in current:
                avatar_src = self._avatar_b64(g[2])
                av = f'<img src="{avatar_src}" class="avatar-sm">' if avatar_src else '<div class="avatar-sm-ph">#</div>'
                role = (g[4] or "member").title()
                media = (g[7] or 0) + (g[8] or 0) + (g[9] or 0) + (g[10] or 0)
                alias = g[13] if len(g) > 13 and g[13] else "-"
                h += f'<tr><td>{av}</td><td>{self._esc(g[1] or "?")}<br><small>{g[3] or 0} members</small></td>'
                h += f'<td>{role}</td><td>{self._esc(alias)}</td><td>{g[6]:,}</td><td>{media}</td>'
                h += f'<td>{self._fmt_date(g[11])}</td><td>{self._fmt_date(g[12])}</td></tr>'
            h += '</tbody></table>'

        if past:
            h += f'<h3>Past Groups ({len(past)})</h3>'
            h += '<table class="data-table"><thead><tr><th>Group</th><th>Role</th><th>Messages</th><th>Left Date</th><th>Reason</th></tr></thead><tbody>'
            for g in past:
                h += f'<tr><td>{self._esc(g[1] or "?")}</td><td>{(g[4] or "member").title()}</td>'
                h += f'<td>{g[5]:,}</td><td>{self._fmt_date(g[2])}</td><td>{self._esc(g[3] or "-")}</td></tr>'
            h += '</tbody></table>'
        h += '</div>'
        return h

    def _render_personal(self, data: dict | None) -> str:
        if not data:
            return ""
        h = '<div class="report-section"><h2>Personal Chat</h2>'
        h += '<table class="info-table">'
        h += f'<tr><td class="key">Total Messages</td><td>{data["total"]:,}</td></tr>'
        h += f'<tr><td class="key">Text</td><td>{data["text"]:,}</td></tr>'
        h += f'<tr><td class="key">Images</td><td>{data["images"]:,}</td></tr>'
        h += f'<tr><td class="key">Videos</td><td>{data["videos"]:,}</td></tr>'
        h += f'<tr><td class="key">Audio</td><td>{data["audio"]:,}</td></tr>'
        h += f'<tr><td class="key">Documents</td><td>{data["docs"]:,}</td></tr>'
        h += f'<tr><td class="key">First Message</td><td>{self._fmt_ts(data["first_ts"])}</td></tr>'
        h += f'<tr><td class="key">Last Message</td><td>{self._fmt_ts(data["last_ts"])}</td></tr>'
        h += '</table></div>'
        return h

    def _render_calls(self, data: dict) -> str:
        if not data["total"]:
            return ""
        h = '<div class="report-section"><h2>Call History</h2>'
        h += '<table class="info-table">'
        h += f'<tr><td class="key">Total Calls</td><td>{data["total"]}</td></tr>'
        h += f'<tr><td class="key">Voice</td><td>{data["voice"]}</td></tr>'
        h += f'<tr><td class="key">Video</td><td>{data["video"]}</td></tr>'
        h += f'<tr><td class="key">Made</td><td>{data["made"]}</td></tr>'
        h += f'<tr><td class="key">Received</td><td>{data["received"]}</td></tr>'
        h += f'<tr><td class="key">Total Duration</td><td>{self._fmt_dur(data["total_duration"])}</td></tr>'
        if data.get("voice_chats"):
            h += f'<tr><td class="key">Voice Chats</td><td>{data["voice_chats"]}</td></tr>'
        if data.get("group_calls"):
            h += f'<tr><td class="key">Group Calls</td><td>{data["group_calls"]}</td></tr>'
        if data.get("multi_person"):
            h += f'<tr><td class="key">Multi-person</td><td>{data["multi_person"]}</td></tr>'
        h += '</table>'
        # Call log table (last 30)
        calls = data["calls"][:30]
        if calls:
            h += '<h3>Recent Calls</h3><table class="data-table"><thead><tr>'
            h += '<th>Date</th><th>Direction</th><th>Type</th><th>Category</th>'
            h += '<th>Duration</th><th>Result</th></tr></thead><tbody>'
            cat_labels = {"voice_chat": "Voice Chat", "group_call": "Group Call",
                          "multi_person": "Multi-person", "personal": "Personal"}
            for c in calls:
                cat = cat_labels.get(c[5], c[5]) if len(c) > 5 else "Personal"
                h += f'<tr><td>{self._fmt_ts(c[4])}</td><td>{"Made" if c[0] else "Received"}</td>'
                h += f'<td>{"Video" if c[1] else "Voice"}</td><td>{cat}</td>'
                h += f'<td>{self._fmt_dur(c[2] or 0)}</td>'
                h += f'<td>{c[3] or "-"}</td></tr>'
            h += '</tbody></table>'
        h += '</div>'
        return h

    def _render_locations(self, locations: list) -> str:
        if not locations:
            return ""
        import base64
        h = '<div class="report-section"><h2>Locations Shared</h2>'
        h += f'<p>{len(locations)} location(s) shared by this contact.</p>'
        h += '<table class="data-table"><thead><tr>'
        h += '<th>Date</th><th>Coordinates</th><th>Place</th><th>Type</th>'
        h += '<th>Conversation</th><th>Preview</th>'
        h += '</tr></thead><tbody>'
        for loc in locations[:50]:
            lat, lng = loc[0], loc[1]
            place = self._esc(loc[2] or "")
            addr = self._esc(loc[3] or "")
            is_live = loc[4]
            dur = loc[5]
            map_url = loc[6] or ""
            thumb = loc[7]
            ts = loc[8]
            conv = self._esc(loc[10] or "")

            loc_type = "\U0001F534 Live" if is_live else "Static"
            if is_live and dur:
                loc_type += f" ({self._fmt_dur(dur)})"

            coord_text = f"{lat:.6f}, {lng:.6f}"
            if map_url:
                coord_text = f'<a href="{map_url}" target="_blank">{coord_text}</a>'

            preview_html = ""
            if thumb and len(thumb) > 50:
                b64 = base64.b64encode(bytes(thumb)).decode()
                preview_html = (
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'style="max-width:80px;max-height:60px;border-radius:4px;" />'
                )

            place_cell = place
            if addr:
                place_cell += f'<br><small style="color:#888">{addr}</small>'

            h += f'<tr><td>{self._fmt_ts(ts)}</td>'
            h += f'<td class="mono" style="font-size:11px">{coord_text}</td>'
            h += f'<td>{place_cell}</td>'
            h += f'<td>{loc_type}</td>'
            h += f'<td>{conv}</td>'
            h += f'<td>{preview_html}</td></tr>'
        h += '</tbody></table></div>'
        return h

    def _render_media(self, data: dict) -> str:
        if not data:
            return ""
        h = '<div class="report-section"><h2>Media Breakdown</h2>'
        h += '<table class="data-table"><thead><tr><th>Type</th>'
        for ct in sorted(data.keys()):
            h += f'<th>{ct.title()}</th>'
        h += '<th>Total</th></tr></thead><tbody>'
        for mt in ["images", "videos", "audio", "docs", "stickers", "gifs"]:
            h += f'<tr><td>{mt.title()}</td>'
            row_total = 0
            for ct in sorted(data.keys()):
                v = data[ct].get(mt, 0)
                row_total += v
                h += f'<td>{v:,}</td>'
            h += f'<td><strong>{row_total:,}</strong></td></tr>'
        h += '</tbody></table></div>'
        return h

    def _render_interactions(self, reactions: dict, mentions: dict) -> str:
        h = '<div class="report-section"><h2>Interactions</h2>'
        if reactions["given"]:
            h += '<h3>Reactions Given</h3><div class="emoji-list">'
            for r in reactions["given"]:
                h += f'<span class="emoji-pill">{r[0]} x{r[1]}</span> '
            h += '</div>'
        if reactions["received"]:
            h += '<h3>Reactions Received</h3><div class="emoji-list">'
            for r in reactions["received"]:
                h += f'<span class="emoji-pill">{r[0]} x{r[1]}</span> '
            h += '</div>'
        if mentions["mentioned_by"]:
            h += '<h3>Mentioned By</h3><table class="data-table"><thead><tr><th>Person</th><th>JID</th><th>Times</th></tr></thead><tbody>'
            for m in mentions["mentioned_by"]:
                jid = m[2] if len(m) > 2 else ""
                h += f'<tr><td>{self._esc(m[0])}</td><td class="mono" style="font-size:10px">{self._esc(jid)}</td><td>{m[1]}</td></tr>'
            h += '</tbody></table>'
        if mentions["mentioning"]:
            h += '<h3>Mentioning Others</h3><table class="data-table"><thead><tr><th>Person</th><th>JID</th><th>Times</th></tr></thead><tbody>'
            for m in mentions["mentioning"]:
                jid = m[2] if len(m) > 2 else ""
                h += f'<tr><td>{self._esc(m[0])}</td><td class="mono" style="font-size:10px">{self._esc(jid)}</td><td>{m[1]}</td></tr>'
            h += '</tbody></table>'
        h += '</div>'
        return h

    # ================================================================
    # HTML WRAPPER WITH CSS
    # ================================================================

    def _wrap_html(self, body: str, title: str) -> str:
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>WhatsApp Report - {self._esc(title)}</title>
<style>
@page {{ size: A4; margin: 1.5cm; }}
@media print {{ .no-print {{ display: none; }} }}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; color: #1a1a1a; line-height: 1.5; background: #f8f9fa; }}
.report-section {{ background: white; margin: 16px; padding: 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); page-break-inside: avoid; }}
.case-header {{ background: #075e54; color: white; }}
.case-header h1 {{ font-size: 22px; margin-bottom: 12px; }}
h2 {{ color: #075e54; font-size: 18px; margin-bottom: 12px; border-bottom: 2px solid #075e54; padding-bottom: 6px; }}
h3 {{ color: #128c7e; font-size: 14px; margin: 16px 0 8px; }}
.overview-grid {{ display: flex; gap: 24px; align-items: flex-start; }}
.avatar-large {{ width: 96px; height: 96px; border-radius: 50%; object-fit: cover; }}
.avatar-placeholder {{ width: 96px; height: 96px; border-radius: 50%; background: #075e54; color: white; display: flex; align-items: center; justify-content: center; font-size: 32px; font-weight: 700; }}
.avatar-sm {{ width: 32px; height: 32px; border-radius: 50%; object-fit: cover; }}
.avatar-sm-ph {{ width: 32px; height: 32px; border-radius: 50%; background: #90a4ae; color: white; display: flex; align-items: center; justify-content: center; font-size: 12px; }}
.overview-info {{ flex: 1; }}
.overview-info h3 {{ margin: 0 0 8px; font-size: 20px; color: #1a1a1a; border: none; padding: 0; }}
.info-table {{ border-collapse: collapse; width: 100%; }}
.info-table td {{ padding: 4px 8px; border-bottom: 1px solid #eee; }}
.info-table td.key {{ font-weight: 600; color: #555; width: 160px; white-space: nowrap; }}
.case-header .info-table td {{ border-color: rgba(255,255,255,0.2); color: white; }}
.case-header .info-table td.key {{ color: rgba(255,255,255,0.8); }}
.data-table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
.data-table th {{ background: #f5f5f5; padding: 8px; text-align: left; font-weight: 600; color: #333; border-bottom: 2px solid #ddd; }}
.data-table td {{ padding: 6px 8px; border-bottom: 1px solid #eee; }}
.data-table tbody tr:nth-child(even) {{ background: #fafafa; }}
.data-table.mono td {{ font-family: 'Consolas', 'Courier New', monospace; font-size: 11px; }}
.mono {{ font-family: 'Consolas', 'Courier New', monospace; }}
.verified-badge {{ background: #1da1f2; color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 700; }}
.biz-badge {{ background: #128c7e; color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; }}
.bar {{ height: 14px; background: linear-gradient(90deg, #43a047, #1b5e20); border-radius: 2px; min-width: 2px; }}
.emoji-list {{ margin: 8px 0; }}
.emoji-pill {{ display: inline-block; background: #f5f5f5; padding: 4px 10px; border-radius: 16px; margin: 2px; font-size: 13px; }}
.heatmap-container {{ overflow-x: auto; }}
.footer {{ text-align: center; color: #999; font-size: 11px; padding: 24px; }}
</style>
</head>
<body>
{body}
<div class="footer">
    Generated by WhatsApp Android Forensic Tool | {format_timestamp_with_utc(int(datetime.now().timestamp() * 1000), "full")} | {get_current_timezone_display()}
</div>
</body>
</html>"""
