"""
Contact Detail page -- detailed view of a single contact showing avatar,
stats (messages sent/received, media, calls), groups in common,
direct conversation link, and copyable fields.

Opened when a contact row is clicked in the Contacts list or when a
sender name is clicked in the Chat Viewer.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from app.config import format_timestamp
from app.services.database import Database
from app.services.theme_manager import ThemeManager

AVATAR_COLORS = [
    "#00897b", "#6a1b9a", "#c62828", "#1565c0",
    "#ef6c00", "#2e7d32", "#ad1457", "#4527a0",
    "#00838f", "#827717", "#4e342e", "#37474f",
]


class ContactDetailPage(QWidget):
    """Full-page detail view for a single contact."""

    back_requested = Signal()
    conversation_requested = Signal(int, str)  # (conversation_id, display_name)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._contact_id: int | None = None
        self._contact_name: str = ""
        self._direct_conv_id: int | None = None
        self._direct_conv_name: str | None = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ---- Header bar ----
        self._build_header(main_layout)

        # ---- Scrollable content area ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(self._tm.contact_detail_scroll_style())

        content = QWidget()
        content.setStyleSheet(self._tm.contact_detail_content_bg())
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(24, 20, 24, 24)
        self._content_layout.setSpacing(16)

        # Stats bar
        self._build_stats_bar()

        # Contact details section
        self._build_details_section()

        # How this contact entered the case (system events, group joins,
        # call participations, mention references, etc.).  Critical for
        # forensic reports - explains why a contact with 0 DMs / 0 calls
        # / 0 sent msgs still has a phone+LID identity in our DB.
        self._build_provenance_section()

        # Per-contact call history.  Pulls from call_record (this
        # contact as the call's contact_id, i.e. the "other party" for
        # 1-on-1) AND call_participant (this contact joined a group
        # call someone else initiated).  Shows direction, type, result,
        # duration, conv link.
        self._build_calls_section()

        # Device activity section
        self._build_device_section()

        # Direct conversation button
        self._build_direct_conv_button()

        # Status updates section
        self._build_status_section()

        # Groups in common
        self._build_groups_section()

        self._content_layout.addStretch()
        scroll.setWidget(content)
        main_layout.addWidget(scroll, 1)

    # ------------------------------------------------------------------ #
    # Header
    # ------------------------------------------------------------------ #

    def _build_header(self, parent_layout: QVBoxLayout) -> None:
        header = QFrame()
        header.setObjectName("contactDetailHeader")
        header.setFixedHeight(72)
        header.setStyleSheet(self._tm.contact_detail_header_style())
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 12, 0)
        hl.setSpacing(12)

        # Back button
        self._back_btn = QPushButton("\u25C0")  # ◀ solid left triangle
        self._back_btn.setFixedSize(36, 36)
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.setStyleSheet(self._tm.contact_detail_btn_style())
        self._back_btn.clicked.connect(self.back_requested.emit)
        hl.addWidget(self._back_btn)

        # Avatar
        self._avatar_label = QLabel()
        self._avatar_label.setFixedSize(48, 48)
        self._avatar_label.setAlignment(Qt.AlignCenter)
        self._avatar_label.setStyleSheet("""
            QLabel { background: #00897b; border-radius: 24px;
                     color: white; font-size: 18px; font-weight: bold; }
        """)
        hl.addWidget(self._avatar_label)

        # Title column (name + phone + wa_name)
        title_col = QVBoxLayout()
        title_col.setSpacing(1)

        self._name_label = QLabel("Contact")
        name_font = QFont()
        name_font.setPointSize(14)
        name_font.setBold(True)
        self._name_label.setFont(name_font)
        self._name_label.setStyleSheet(self._tm.contact_detail_name_style())
        self._name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_col.addWidget(self._name_label)

        self._phone_label = QLabel("")
        self._phone_label.setStyleSheet(self._tm.contact_detail_phone_style())
        self._phone_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        title_col.addWidget(self._phone_label)
        hl.addLayout(title_col, 1)

        # Report button
        self._report_btn = QPushButton("\U0001F4CB Report")
        self._report_btn.setFixedHeight(32)
        self._report_btn.setCursor(Qt.PointingHandCursor)
        self._report_btn.setToolTip(
            "Generate a comprehensive forensic HTML report for this contact:\n"
            "• Identity, group activity, mentions, media, calls, patterns"
        )
        self._report_btn.setStyleSheet(self._tm.contact_detail_copy_btn_style() + """
            QPushButton { font-size: 11px; font-weight: bold; padding: 4px 12px;
                          min-width: 80px; border-radius: 6px; }
        """)
        self._report_btn.clicked.connect(self._generate_contact_report)
        hl.addWidget(self._report_btn)

        # Copy button
        copy_btn = QPushButton("\u2398")
        copy_btn.setFixedSize(36, 36)
        copy_btn.setToolTip("Copy contact info")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setStyleSheet(self._tm.contact_detail_copy_btn_style())
        copy_btn.clicked.connect(self._copy_contact_info)
        hl.addWidget(copy_btn)

        parent_layout.addWidget(header)

    # ------------------------------------------------------------------ #
    # Stats bar
    # ------------------------------------------------------------------ #

    def _build_stats_bar(self) -> None:
        stats_frame = QFrame()
        stats_frame.setObjectName("contactStatsBar")
        stats_frame.setStyleSheet(self._tm.contact_detail_stats_bar_style())
        stats_layout = QHBoxLayout(stats_frame)
        stats_layout.setContentsMargins(20, 14, 20, 14)
        stats_layout.setSpacing(0)

        self._stat_sent = self._make_stat_widget("Messages Sent", "0")
        self._stat_received = self._make_stat_widget("Messages Received", "0")
        self._stat_media = self._make_stat_widget("Total Media", "0")
        self._stat_calls = self._make_stat_widget("Total Calls", "0")

        for i, widget in enumerate([
            self._stat_sent, self._stat_received,
            self._stat_media, self._stat_calls,
        ]):
            if i > 0:
                sep = QFrame()
                sep.setFixedWidth(1)
                sep.setStyleSheet(self._tm.contact_detail_sep_style())
                sep.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
                stats_layout.addWidget(sep)
            stats_layout.addWidget(widget, 1)

        self._content_layout.addWidget(stats_frame)

    def _make_stat_widget(self, label: str, value: str) -> QWidget:
        w = QWidget()
        vl = QVBoxLayout(w)
        vl.setContentsMargins(0, 0, 0, 0)
        vl.setSpacing(2)
        vl.setAlignment(Qt.AlignCenter)

        val_lbl = QLabel(value)
        val_lbl.setAlignment(Qt.AlignCenter)
        val_font = QFont()
        val_font.setPointSize(18)
        val_font.setBold(True)
        val_lbl.setFont(val_font)
        val_lbl.setStyleSheet(self._tm.contact_detail_stat_value_style())
        val_lbl.setObjectName("statValue")
        vl.addWidget(val_lbl)

        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(self._tm.contact_detail_stat_label_style())
        vl.addWidget(lbl)

        return w

    def _update_stat(self, widget: QWidget, value: str) -> None:
        val_label = widget.findChild(QLabel, "statValue")
        if val_label:
            val_label.setText(value)

    # ------------------------------------------------------------------ #
    # Contact details section
    # ------------------------------------------------------------------ #

    def _build_details_section(self) -> None:
        self._details_frame = QFrame()
        self._details_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        dl = QVBoxLayout(self._details_frame)
        dl.setContentsMargins(16, 12, 16, 12)
        dl.setSpacing(6)

        self._detail_rows: dict[str, QLabel] = {}
        for key, label_text in [
            ("wa_name", "WhatsApp Name"),
            ("display_name", "Display Name"),
            ("phone_number", "Phone Number"),
            ("phone_jid", "Phone JID"),
            ("lid_jid", "LID JID"),
            ("status_text", "Status"),
            ("platform", "Platform"),
            ("linked_devices", "Linked Devices"),
            ("business", "Business"),
            ("business_name", "Business Name"),
            ("business_vertical", "Business Category"),
            ("business_description", "Business Description"),
            ("business_address", "Business Address"),
            ("business_location", "Business Location"),
            ("business_email", "Business Email"),
            ("business_website", "Business Website"),
            ("business_hours", "Business Hours"),
            ("business_member_since", "On WhatsApp Since"),
            ("trust_tier", "Trust Tier"),
            ("fb_linked", "Facebook Page"),
            ("ig_linked", "Instagram"),
        ]:
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(120)
            lbl.setStyleSheet(self._tm.contact_detail_row_label_style())
            row.addWidget(lbl)

            val = QLabel("")
            val.setStyleSheet(self._tm.contact_detail_row_value_style())
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(val, 1)

            self._detail_rows[key] = val
            dl.addLayout(row)

        self._content_layout.addWidget(self._details_frame)

    # ------------------------------------------------------------------ #
    # Provenance section — answers "why is this contact in our DB?"
    # ------------------------------------------------------------------ #

    def _build_provenance_section(self) -> None:
        self._provenance_frame = QFrame()
        self._provenance_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        pl = QVBoxLayout(self._provenance_frame)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(6)
        header = QLabel("How this contact entered the case")
        f = QFont(); f.setPointSize(13); f.setBold(True)
        header.setFont(f)
        header.setStyleSheet(f"color: {self._c_text}; padding-bottom: 4px;"
                              if hasattr(self, '_c_text')
                              else "font-weight:bold; padding-bottom:4px;")
        pl.addWidget(header)
        self._provenance_label = QLabel("")
        self._provenance_label.setWordWrap(True)
        self._provenance_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._provenance_label.setStyleSheet(self._tm.contact_detail_row_value_style())
        pl.addWidget(self._provenance_label)
        self._content_layout.addWidget(self._provenance_frame)

    def _load_provenance(self, contact_id: int) -> None:
        """Compose a forensic-grade explanation of where this contact
        identity came from: msgstore.jid mappings, group joins/leaves,
        system event actor/target appearances, call participations,
        mention references.  Builds a one-paragraph natural-language
        summary so the investigator doesn't need to guess.
        """
        db = Database.get()
        bits: list[str] = []

        # 1. JID identity mappings (msgstore.jid) — the LID-to-phone bridge
        jid_rows = db.fetchall(
            "SELECT jid_raw_string, jid_type FROM jid_to_contact "
            "WHERE contact_id = ? ORDER BY jid_type, jid_row_id",
            (contact_id,),
        )
        phone_jids = sorted({r["jid_raw_string"] for r in jid_rows
                             if r["jid_raw_string"].endswith("@s.whatsapp.net")
                             and "." not in r["jid_raw_string"].split("@")[0]})
        lid_jids = sorted({r["jid_raw_string"] for r in jid_rows
                           if r["jid_raw_string"].endswith("@lid")
                           and "." not in r["jid_raw_string"].split("@")[0]})
        device_pairs = [r for r in jid_rows
                        if "." in r["jid_raw_string"].split("@")[0]]
        if phone_jids and lid_jids:
            bits.append(
                f"<b>How the LID got resolved to a phone:</b><br>"
                f"&nbsp;&nbsp;Both <code>{phone_jids[0]}</code> "
                f"and <code>{lid_jids[0]}</code> exist in WhatsApp's source "
                f"<code>msgstore.jid</code> table for the same underlying device "
                f"set ({len(device_pairs)} per-device variants like "
                f"<code>X.0:N@s.whatsapp.net</code> ↔ "
                f"<code>X.1:N@lid</code> share device-pair indices).<br>"
                f"&nbsp;&nbsp;Our contact_resolver walks that table, joins the phone-JID and "
                f"LID-JID rows that share device-pair structure, and unifies them into ONE "
                f"<code>contact</code> row — this is how the phone gets resolved even when "
                f"the device owner has never DM'd or interacted with this number. "
                f"WhatsApp itself made the linkage; we just lifted it.<br>"
                f"&nbsp;&nbsp;<b>Total JID rows for this contact:</b> {len(jid_rows)} "
                f"(1 base phone + 1 base LID + {len(device_pairs)} device variants)."
            )
        elif phone_jids:
            bits.append(
                f"<b>JID:</b> <code>{phone_jids[0]}</code> (phone-only — no LID "
                f"seen, so this contact predates LID rollout or never used a "
                f"primary device that produced LID signaling)."
            )
        elif lid_jids:
            # Look up the masked phone hint (from msgstore.lid_display_name)
            # so the investigator at least knows the country + last 2 digits.
            row_for_mask = db.fetchone(
                "SELECT lid_masked_phone FROM contact WHERE id = ?",
                (contact_id,),
            )
            masked = (row_for_mask["lid_masked_phone"] or "") if row_for_mask else ""
            mask_bit = (
                f" — WhatsApp's own privacy hint for this LID is "
                f"<code>{masked}</code> (last 2 digits visible)"
                if masked else ""
            )
            bits.append(
                f"<b>JID:</b> <code>{lid_jids[0]}</code><br>"
                f"&nbsp;&nbsp;<b>Why phone is unresolved:</b> the phone-JID counterpart "
                f"<code>{lid_jids[0].replace('@lid', '@s.whatsapp.net')}</code> never "
                f"appeared in WhatsApp's <code>msgstore.jid</code> table on this device. "
                f"This typically happens when:<br>"
                f"&nbsp;&nbsp;&nbsp;&nbsp;• the other party joined via invite link with "
                f"<i>Phone Number Privacy</i> enabled<br>"
                f"&nbsp;&nbsp;&nbsp;&nbsp;• they share a group/community with the device "
                f"owner but never DM'd directly<br>"
                f"&nbsp;&nbsp;&nbsp;&nbsp;• WhatsApp's identity layer only handed over "
                f"the LID; the phone was never revealed to this device.{mask_bit}"
            )

        # 2. System events where this contact is actor or target
        sys_rows = db.fetchall(
            """
            SELECT se.event_label, c.display_name AS conv_name, c.jid_raw_string AS conv_jid,
                   se.timestamp,
                   CASE WHEN se.actor_id = ? THEN 'actor' ELSE 'target' END AS role
            FROM system_event se
            LEFT JOIN conversation c ON c.id = se.conversation_id
            WHERE se.actor_id = ? OR se.target_id = ?
            ORDER BY se.timestamp ASC
            LIMIT 20
            """,
            (contact_id, contact_id, contact_id),
        )
        if sys_rows:
            from datetime import datetime as _dt
            sys_lines = []
            for s in sys_rows[:5]:
                t = _dt.fromtimestamp(s["timestamp"] / 1000).strftime("%Y-%m-%d") \
                    if s["timestamp"] else "?"
                conv = s["conv_name"] or s["conv_jid"] or "(unknown chat)"
                sys_lines.append(
                    f"&nbsp;&nbsp;• <b>{t}</b>: {s['event_label'].replace('_',' ')} "
                    f"({s['role']}) in <i>{conv}</i>"
                )
            extra = f"<br>… and {len(sys_rows) - 5} more" if len(sys_rows) > 5 else ""
            bits.append(
                f"<b>System events ({len(sys_rows)}):</b> "
                f"this contact appears in group/community events:<br>"
                + "<br>".join(sys_lines) + extra
            )

        # 3. Group memberships
        gm_rows = db.fetchall(
            """
            SELECT c.display_name AS conv_name, c.jid_raw_string AS conv_jid,
                   gm.role, gm.label
            FROM group_member gm
            JOIN conversation c ON c.id = gm.conversation_id
            WHERE gm.contact_id = ?
            """,
            (contact_id,),
        )
        if gm_rows:
            grp_names = [g["conv_name"] or g["conv_jid"] or "?" for g in gm_rows]
            bits.append(
                f"<b>Currently a member of {len(gm_rows)} group"
                f"{'s' if len(gm_rows) != 1 else ''}:</b> "
                + ", ".join(grp_names[:5])
                + (f" + {len(grp_names) - 5} more" if len(grp_names) > 5 else "")
            )

        # 4. Past group memberships (from group_past_participant)
        try:
            past_rows = db.fetchall(
                """
                SELECT c.display_name AS conv_name, c.jid_raw_string AS conv_jid
                FROM group_past_participant gp
                JOIN conversation c ON c.id = gp.conversation_id
                WHERE gp.contact_id = ?
                """, (contact_id,),
            )
            if past_rows:
                past_names = [p["conv_name"] or p["conv_jid"] or "?" for p in past_rows]
                bits.append(
                    f"<b>Past member of {len(past_rows)} group"
                    f"{'s' if len(past_rows) != 1 else ''}:</b> "
                    + ", ".join(past_names[:5])
                    + (f" + {len(past_names) - 5} more" if len(past_names) > 5 else "")
                )
        except Exception:
            pass

        # 5. Call records
        try:
            call_rows = db.fetchall(
                "SELECT COUNT(*) AS n FROM call_record WHERE contact_id = ?",
                (contact_id,),
            )
            ncalls = call_rows[0]["n"] if call_rows else 0
            cp_n = db.scalar(
                "SELECT COUNT(*) FROM call_participant WHERE contact_id = ?",
                (contact_id,),
            ) or 0
            if ncalls or cp_n:
                bits.append(
                    f"<b>Calls:</b> {ncalls} call records initiated, "
                    f"{cp_n} call-participant entries (joined / declined someone else's)."
                )
        except Exception:
            pass

        # 6. Mention references — someone tagged them
        try:
            mention_n = db.scalar(
                "SELECT COUNT(*) FROM mention WHERE mentioned_id = ?",
                (contact_id,),
            ) or 0
            if mention_n:
                bits.append(f"<b>Mentioned</b> in {mention_n} message(s) by other people.")
        except Exception:
            pass

        # 7. Reactions made by this contact on others' messages
        try:
            rx_n = db.scalar(
                "SELECT COUNT(*) FROM reaction WHERE reactor_id = ?",
                (contact_id,),
            ) or 0
            if rx_n:
                bits.append(f"<b>Reacted</b> to {rx_n} message(s).")
        except Exception:
            pass

        if not bits:
            self._provenance_label.setText(
                "No upstream references found — this contact was likely "
                "discovered solely through the WhatsApp address book "
                "(<code>wa.db wa_contacts</code>) without ever interacting "
                "with the device owner."
            )
            return
        self._provenance_label.setText("<br><br>".join(bits))

    # ------------------------------------------------------------------ #
    # Per-contact Calls section
    # ------------------------------------------------------------------ #

    def _build_calls_section(self) -> None:
        self._calls_frame = QFrame()
        self._calls_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        cl = QVBoxLayout(self._calls_frame)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(6)
        self._calls_header = QLabel("Calls (0)")
        f = QFont(); f.setPointSize(13); f.setBold(True)
        self._calls_header.setFont(f)
        self._calls_header.setStyleSheet(self._tm.contact_detail_groups_header_style())
        cl.addWidget(self._calls_header)
        # List widget for calls (compact rows)
        from PySide6.QtWidgets import QListWidget
        self._calls_list = QListWidget()
        self._calls_list.setStyleSheet(self._tm.contact_detail_groups_list_style())
        self._calls_list.setMaximumHeight(260)
        cl.addWidget(self._calls_list)
        self._content_layout.addWidget(self._calls_frame)

    def _load_calls(self, contact_id: int) -> None:
        """Load every call this contact participated in:
           (a) call_record.contact_id = us — they're the "other party"
               in a 1-on-1, OR the call's creator in a group.
           (b) call_participant.contact_id = us — they joined someone
               else's group call / voice chat.
        """
        from datetime import datetime as _dt
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QListWidgetItem
        db = Database.get()
        # Calls where this contact is the primary party.
        primary = db.fetchall(
            """
            SELECT cr.id, cr.timestamp, cr.is_video, cr.duration_sec,
                   cr.is_group_call, cr.call_category, cr.from_me,
                   cr.result_label, cr.call_result, cr.conversation_id,
                   c.display_name AS conv_name, c.jid_raw_string AS conv_jid,
                   c.chat_type
            FROM call_record cr
            LEFT JOIN conversation c ON c.id = cr.conversation_id
            WHERE cr.contact_id = ?
            ORDER BY cr.timestamp DESC
            LIMIT 50
            """,
            (contact_id,),
        )
        # Calls where this contact joined someone else's group call.
        joined = db.fetchall(
            """
            SELECT cr.id, cr.timestamp, cr.is_video, cr.duration_sec,
                   cr.is_group_call, cr.call_category,
                   cr.result_label AS cr_result_label,
                   cp.call_result AS my_result,
                   cr.conversation_id,
                   c.display_name AS conv_name, c.jid_raw_string AS conv_jid,
                   c.chat_type
            FROM call_participant cp
            JOIN call_record cr ON cr.id = cp.call_id
            LEFT JOIN conversation c ON c.id = cr.conversation_id
            WHERE cp.contact_id = ? AND cr.contact_id != ?
            ORDER BY cr.timestamp DESC
            LIMIT 50
            """,
            (contact_id, contact_id),
        )
        # Merge + dedupe by call id, sort by ts desc
        seen = set()
        merged: list[dict] = []
        for r in list(primary) + list(joined):
            cid = r["id"]
            if cid in seen: continue
            seen.add(cid)
            merged.append(dict(r))
        merged.sort(key=lambda x: x.get("timestamp") or 0, reverse=True)

        self._calls_list.clear()
        n = len(merged)
        self._calls_header.setText(f"Calls  ({n})")
        if not n:
            self._calls_list.addItem("No calls involving this contact")
            return

        _CALL_RESULT = {0: "missed", 1: "answered", 2: "declined", 3: "ringing", 4: "outgoing"}
        for r in merged:
            ts = (_dt.fromtimestamp(r["timestamp"] / 1000)
                    .strftime("%Y-%m-%d %H:%M") if r.get("timestamp") else "?")
            cat = (r.get("call_category") or "").strip()
            scope = ("📞 Voice Chat"  if cat == "voice_chat"
                     else "👥 Group"   if (cat == "group_call" or r.get("is_group_call"))
                     else "👤 Personal")
            kind = "📹 Video" if r.get("is_video") else "🎤 Voice"
            dur = r.get("duration_sec") or 0
            dur_str = (f"{dur // 60}m {dur % 60}s" if dur >= 60
                       else f"{dur}s" if dur else "—")
            res = (r.get("result_label") or "").strip().lower() \
                  or _CALL_RESULT.get(r.get("call_result") or -1, "")
            res_str = res or "?"
            direction = ("⇡ outgoing" if r.get("from_me")
                         else "⇣ incoming" if "from_me" in r and r.get("from_me") is not None
                         else "↹ joined")
            ctx = r.get("conv_name") or r.get("conv_jid") or ""
            ctx_bit = f" — in {ctx}" if ctx else ""
            label = (f"{ts}   {scope} · {kind} · {direction} · "
                     f"{res_str} · {dur_str}{ctx_bit}")
            it = QListWidgetItem(label)
            # Stash conv id so a future double-click can open the chat.
            if r.get("conversation_id"):
                it.setData(_Qt.UserRole, r["conversation_id"])
            self._calls_list.addItem(it)

    # ------------------------------------------------------------------ #
    # Device Activity section
    # ------------------------------------------------------------------ #

    def _build_device_section(self) -> None:
        """Build the device activity section showing per-device message stats."""
        self._device_frame = QFrame()
        self._device_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        self._device_frame.setVisible(False)  # hidden until data is loaded
        dl = QVBoxLayout(self._device_frame)
        dl.setContentsMargins(16, 12, 16, 12)
        dl.setSpacing(6)

        header = QLabel("\u2261 Device Activity")
        hf = QFont()
        hf.setPointSize(11)
        hf.setBold(True)
        header.setFont(hf)
        header.setStyleSheet(self._tm.contact_detail_row_label_style())
        dl.addWidget(header)

        self._device_list_widget = QLabel("")
        self._device_list_widget.setWordWrap(True)
        self._device_list_widget.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
        )
        self._device_list_widget.setStyleSheet(self._tm.contact_detail_row_value_style())
        dl.addWidget(self._device_list_widget)

        self._content_layout.addWidget(self._device_frame)

    def _update_device_section(self, contact_id: int) -> None:
        """Load and display per-device activity for this contact.

        Separates devices into:
        - Primary Phone (device #0)
        - Recent companion devices (active in last 60 days)
        - Past companion devices (collapsed summary)
        """
        try:
            from datetime import datetime
            import time

            db = Database.get()

            # Get per-device breakdown with platform classification
            # Exclude system (type 7) and newsletter (type 64) messages from device stats
            devices = db.fetchall("""
                SELECT md.device_number,
                       COUNT(*) AS msgs,
                       MIN(m.timestamp) AS first_ts,
                       MAX(m.timestamp) AS last_ts,
                       md.platform_label,
                       ROUND(AVG(md.platform_confidence), 2) AS avg_conf
                FROM message_device md
                JOIN message m ON m.id = md.message_id
                WHERE m.sender_id = ?
                  AND m.message_type NOT IN (7, 64)
                  AND md.platform_label NOT IN ('newsletter', 'channel_bot')
                GROUP BY md.device_number, md.platform_label
                ORDER BY last_ts DESC
            """, (contact_id,))

            if not devices or len(devices) == 0:
                self._device_frame.setVisible(False)
                return

            total_msgs = sum(d[1] for d in devices)
            companion_devs = [d for d in devices if d[0] > 0]
            companion_msgs = sum(d[1] for d in companion_devs)
            pct_comp = (companion_msgs / total_msgs * 100) if total_msgs else 0

            # Classify: phone, recent companion (last 60 days), past companion
            cutoff_ms = int((time.time() - 60 * 86400) * 1000)
            phone = [d for d in devices if d[0] == 0]
            recent = [d for d in companion_devs if d[3] and d[3] >= cutoff_ms]
            past = [d for d in companion_devs if not d[3] or d[3] < cutoff_ms]

            lines = []

            # --- Summary ---
            if companion_msgs == 0:
                lines.append("<b>Phone only</b> \u2014 no companion device messages seen")
                self._device_list_widget.setText("<br>".join(lines))
                self._device_frame.setVisible(True)
                return

            lines.append(
                f"<b>{pct_comp:.0f}%</b> of messages from companion devices "
                f"({companion_msgs:,} of {total_msgs:,})"
            )
            if recent:
                lines.append(
                    f"<b>{len(recent)}</b> recent + <b>{len(past)}</b> past companion"
                    f"{'s' if len(past) != 1 else ''}"
                )
            else:
                lines.append(
                    f"<b>{len(past)}</b> past companion device"
                    f"{'s' if len(past) != 1 else ''} (none recently active)"
                )
            lines.append("")

            def _fmt_ts(ts_ms):
                if not ts_ms:
                    return "?"
                return format_timestamp(ts_ms, "date")

            def _fmt_ts_full(ts_ms):
                if not ts_ms:
                    return "?"
                return format_timestamp(ts_ms, "datetime")

            def _bar(msgs, total):
                pct = (msgs / total * 100) if total else 0
                n = max(1, int(pct / 4))
                return f"<span style='color:#00897b'>{'\u2588' * n}</span>"

            _PLAT_DISP = {
                "android": "Android", "iphone": "iPhone",
                "android_linked": "Android (Linked)", "iphone_linked": "iPhone (Linked)",
                "companion": "Web/Desktop", "newsletter": "Newsletter",
                "channel_bot": "Channel", "phone": "Phone", "unknown": "Unknown",
            }

            def _plat(d):
                return _PLAT_DISP.get(d[4] or "", d[4] or "Phone")

            def _conf(d):
                c = d[5] if len(d) > 5 and d[5] else 0
                return f" ({c*100:.0f}%)" if c else ""

            # --- Primary Phone ---
            if phone:
                p = phone[0]
                pct_p = (p[1] / total_msgs * 100) if total_msgs else 0
                plat = _plat(p)
                conf = _conf(p)
                lines.append(
                    f"\u260E <b>Primary Phone: {plat}</b>{conf} \u2014 "
                    f"{p[1]:,} msgs ({pct_p:.0f}%) "
                    f"{_bar(p[1], total_msgs)}"
                )
                lines.append(
                    f"&nbsp;&nbsp;&nbsp;&nbsp;"
                    f"<span style='color:gray'>{_fmt_ts(p[2])} \u2192 {_fmt_ts_full(p[3])}</span>"
                )
                # Show additional primary platform rows only if significant (> 2% or > 10 msgs)
                primary_total = sum(d[1] for d in phone)
                for extra in phone[1:]:
                    pct_of_primary = (extra[1] / primary_total * 100) if primary_total else 0
                    if extra[1] < 10 and pct_of_primary < 2:
                        continue  # Skip noise (1-2 stray messages from history sync etc.)
                    pct_e = (extra[1] / total_msgs * 100) if total_msgs else 0
                    lines.append(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;\u25B8 also {_plat(extra)}{_conf(extra)}: "
                        f"{extra[1]:,} msgs ({pct_e:.0f}%) "
                        f"<span style='color:gray'>{_fmt_ts(extra[2])} \u2192 {_fmt_ts_full(extra[3])}</span>"
                    )
                lines.append("")

            # --- Recent companion devices (expanded) ---
            if recent:
                lines.append("<b>Recent Companion Devices:</b>")
                for d in recent:
                    pct_d = (d[1] / total_msgs * 100) if total_msgs else 0
                    plat = _plat(d)
                    lines.append(
                        f"&nbsp;&nbsp;\u25B8 <b>#{d[0]}</b> ({plat}): "
                        f"{d[1]:,} msgs ({pct_d:.0f}%) "
                        f"{_bar(d[1], total_msgs)}"
                    )
                    lines.append(
                        f"&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;"
                        f"<span style='color:gray'>{_fmt_ts(d[2])} \u2192 {_fmt_ts_full(d[3])}</span>"
                    )
                lines.append("")

            # --- Past companion devices (collapsed) ---
            if past:
                past_total = sum(d[1] for d in past)
                past_first = min((d[2] for d in past if d[2]), default=None)
                past_last = max((d[3] for d in past if d[3]), default=None)
                nums = ", ".join(f"#{d[0]} ({_plat(d)})" for d in past[:6])
                if len(past) > 6:
                    nums += f" +{len(past) - 6} more"
                lines.append(
                    f"<span style='color:gray'><b>{len(past)} Past Device"
                    f"{'s' if len(past) != 1 else ''}</b>: "
                    f"{past_total:,} msgs total</span>"
                )
                lines.append(
                    f"&nbsp;&nbsp;<span style='color:gray'>"
                    f"{_fmt_ts(past_first)} \u2192 {_fmt_ts_full(past_last)} "
                    f"| {nums}</span>"
                )

            # Add "Full Device History" button
            lines.append("")
            lines.append(
                '<a href="#device_history" style="color:#027eb5;text-decoration:none">'
                '\u25B6 View Full Device History &amp; Timeline</a>'
            )

            self._device_list_widget.setText("<br>".join(lines))
            self._device_frame.setVisible(True)

            # Wire up link click
            self._device_list_widget.setOpenExternalLinks(False)
            try:
                self._device_list_widget.linkActivated.disconnect()
            except RuntimeError:
                pass
            self._device_list_widget.linkActivated.connect(
                lambda _: self._show_device_history(contact_id)
            )

        except Exception:
            self._device_frame.setVisible(False)

    def _show_device_history(self, contact_id: int) -> None:
        """Open full device history dialog for a contact."""
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QTabWidget, QTableWidget,
            QTableWidgetItem, QHeaderView, QPushButton, QFileDialog,
            QTextEdit, QHBoxLayout,
        )
        from PySide6.QtGui import QFont, QColor, QBrush
        from datetime import datetime

        db = Database.get()

        # Get contact name
        name_row = db.fetchone(
            "SELECT resolved_name, phone_jid, lid_jid FROM contact WHERE id = ?",
            (contact_id,),
        )
        contact_name = name_row["resolved_name"] if name_row else f"Contact #{contact_id}"
        phone_jid = (name_row["phone_jid"] or "") if name_row else ""
        lid_jid = (name_row["lid_jid"] or "") if name_row else ""

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Device History - {contact_name}")
        dlg.setMinimumSize(900, 600)
        dlg.resize(1000, 700)
        main_lay = QVBoxLayout(dlg)

        tabs = QTabWidget()
        main_lay.addWidget(tabs)

        # ============================================================
        # TAB 1: Daily Device Timeline
        # ============================================================
        daily_rows = db.fetchall("""
            SELECT date(m.timestamp / 1000, 'unixepoch') AS day,
                   md.device_number,
                   md.platform_label,
                   CASE WHEN cv.chat_type = 'personal' THEN 'personal' ELSE 'group' END AS chat_type,
                   COUNT(*) AS msgs,
                   ROUND(AVG(md.platform_confidence), 2) AS avg_conf
            FROM message_device md
            JOIN message m ON m.id = md.message_id
            JOIN conversation cv ON cv.id = m.conversation_id
            WHERE m.sender_id = ?
            GROUP BY day, md.device_number, md.platform_label, chat_type
            ORDER BY day DESC, msgs DESC
        """, (contact_id,))

        daily_table = QTableWidget()
        daily_table.setColumnCount(7)
        daily_table.setHorizontalHeaderLabels([
            "Date", "Device #", "Platform", "Chat Type",
            "Messages", "Confidence", "Notes"
        ])
        daily_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        daily_table.horizontalHeader().setStretchLastSection(True)
        daily_table.setAlternatingRowColors(True)
        daily_table.setRowCount(len(daily_rows))

        for i, r in enumerate(daily_rows):
            daily_table.setItem(i, 0, QTableWidgetItem(r["day"] or ""))
            dev_num = r["device_number"] or 0
            daily_table.setItem(i, 1, QTableWidgetItem(
                "Primary" if dev_num == 0 else f"#{dev_num}"
            ))
            plat = r["platform_label"] or ""
            _pd = {"android": "Android", "iphone": "iPhone", "companion": "Web/Desktop",
                   "android_linked": "Android (Linked)", "iphone_linked": "iPhone (Linked)",
                   "newsletter": "Newsletter", "channel_bot": "Channel"}.get(plat, plat)
            item = QTableWidgetItem(_pd)
            if "iphone" in plat:
                item.setForeground(QBrush(QColor("#1565c0")))
            elif "android" in plat:
                item.setForeground(QBrush(QColor("#2e7d32")))
            elif plat == "companion":
                item.setForeground(QBrush(QColor("#f57f17")))
            daily_table.setItem(i, 2, item)
            daily_table.setItem(i, 3, QTableWidgetItem(r["chat_type"] or ""))
            daily_table.setItem(i, 4, QTableWidgetItem(str(r["msgs"])))
            conf = r["avg_conf"] or 0
            daily_table.setItem(i, 5, QTableWidgetItem(
                f"{conf*100:.0f}%" if conf else ""
            ))
            # Notes
            notes = ""
            if dev_num == 0 and "iphone" in plat:
                notes = "Primary iPhone"
            elif dev_num == 0 and "android" in plat:
                notes = "Primary Android"
            elif dev_num > 0 and plat == "companion":
                notes = "Web/Desktop session"
            elif dev_num > 0 and "iphone" in plat:
                notes = "Linked iPhone/iPad"
            elif dev_num > 0 and "android" in plat:
                notes = "Linked Android device"
            daily_table.setItem(i, 6, QTableWidgetItem(notes))

        daily_table.setSortingEnabled(True)
        tabs.addTab(daily_table, f"Daily Timeline ({len(daily_rows)} rows)")

        # ============================================================
        # TAB 2: Device Sessions Summary
        # ============================================================
        session_rows = db.fetchall("""
            SELECT md.device_number,
                   md.platform_label,
                   COUNT(*) AS msgs,
                   MIN(m.timestamp) AS first_ts,
                   MAX(m.timestamp) AS last_ts,
                   ROUND(AVG(md.platform_confidence), 2) AS avg_conf,
                   SUM(CASE WHEN cv.chat_type = 'personal' THEN 1 ELSE 0 END) AS personal_msgs,
                   SUM(CASE WHEN cv.chat_type != 'personal' THEN 1 ELSE 0 END) AS group_msgs
            FROM message_device md
            JOIN message m ON m.id = md.message_id
            JOIN conversation cv ON cv.id = m.conversation_id
            WHERE m.sender_id = ?
            GROUP BY md.device_number, md.platform_label
            ORDER BY first_ts
        """, (contact_id,))

        sess_table = QTableWidget()
        sess_table.setColumnCount(8)
        sess_table.setHorizontalHeaderLabels([
            "Device #", "Platform", "Personal Msgs", "Group Msgs",
            "Total", "First Seen", "Last Seen", "Confidence"
        ])
        sess_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        sess_table.horizontalHeader().setStretchLastSection(True)
        sess_table.setAlternatingRowColors(True)
        sess_table.setRowCount(len(session_rows))

        for i, r in enumerate(session_rows):
            dev_num = r["device_number"] or 0
            sess_table.setItem(i, 0, QTableWidgetItem(
                "Primary (0)" if dev_num == 0 else f"Companion #{dev_num}"
            ))
            plat = r["platform_label"] or ""
            _pd = {"android": "Android", "iphone": "iPhone", "companion": "Web/Desktop",
                   "android_linked": "Android (Linked)", "iphone_linked": "iPhone (Linked)"}.get(plat, plat)
            sess_table.setItem(i, 1, QTableWidgetItem(_pd))
            sess_table.setItem(i, 2, QTableWidgetItem(str(r["personal_msgs"] or 0)))
            sess_table.setItem(i, 3, QTableWidgetItem(str(r["group_msgs"] or 0)))
            sess_table.setItem(i, 4, QTableWidgetItem(str(r["msgs"])))
            def _ts(ms):
                if not ms: return ""
                try: return format_timestamp(ms, "datetime")
                except: return ""
            sess_table.setItem(i, 5, QTableWidgetItem(_ts(r["first_ts"])))
            sess_table.setItem(i, 6, QTableWidgetItem(_ts(r["last_ts"])))
            conf = r["avg_conf"] or 0
            sess_table.setItem(i, 7, QTableWidgetItem(
                f"{conf*100:.0f}%" if conf else ""
            ))

        sess_table.setSortingEnabled(True)
        tabs.addTab(sess_table, f"Device Sessions ({len(session_rows)})")

        # ============================================================
        # TAB 3: Forensic Debug (raw key_id patterns, JIDs)
        # ============================================================
        debug_text = QTextEdit()
        debug_text.setReadOnly(True)
        debug_text.setFont(QFont("Consolas", 10))

        lines = []
        lines.append(f"CONTACT: {contact_name}")
        lines.append(f"Phone JID: {phone_jid}")
        lines.append(f"LID JID: {lid_jid}")
        lines.append(f"Contact ID: {contact_id}")
        lines.append("")

        # Key_id length distribution
        keyid_stats = db.fetchall("""
            SELECT length(m.source_key_id) AS key_len,
                   substr(upper(m.source_key_id), 1, 2) AS prefix2,
                   md.device_number,
                   COUNT(*) AS cnt
            FROM message m
            JOIN message_device md ON md.message_id = m.id
            WHERE m.sender_id = ?
            GROUP BY key_len, prefix2, md.device_number
            ORDER BY md.device_number, cnt DESC
        """, (contact_id,))

        lines.append("KEY_ID PATTERN ANALYSIS:")
        lines.append(f"{'Len':>4}  {'Prefix':>6}  {'Dev#':>5} {'Count':>7}  Interpretation")
        lines.append("-" * 60)
        for r in keyid_stats:
            kl = r["key_len"] or 0
            pfx = r["prefix2"] or ""
            dev = r["device_number"] or 0
            cnt = r["cnt"]
            interp = ""
            if kl == 20 and pfx in ("3A", "5E", "4A") and dev == 0:
                interp = "iPhone Primary"
            elif kl == 20 and pfx in ("3F", "3E", "3B"):
                interp = "Companion (Web/Desktop)"
            elif kl == 20 and pfx == "2A":
                interp = "iPhone (likely)" if dev == 0 else "Companion?"
            elif kl == 22 and pfx in ("3E",):
                interp = "Companion (Web/Desktop)"
            elif kl == 32 and dev == 0:
                interp = f"Android Primary{' (current)' if pfx == 'AC' else ' (older)'}"
            elif kl == 32 and dev > 0:
                interp = "Linked Android"
            elif kl == 18:
                interp = "Older format (iPhone/companion)"
            elif kl == 16:
                interp = "Newsletter/Channel"
            lines.append(f"{kl:>4}  {pfx:>6}  {'P' if dev == 0 else f'C#{dev}':>5} {cnt:>7}  {interp}")

        lines.append("")

        # Monthly platform timeline
        monthly = db.fetchall("""
            SELECT strftime('%Y-%m', m.timestamp / 1000, 'unixepoch') AS month,
                   md.platform_label,
                   md.device_number,
                   COUNT(*) AS cnt
            FROM message_device md
            JOIN message m ON m.id = md.message_id
            WHERE m.sender_id = ?
            GROUP BY month, md.platform_label, md.device_number
            ORDER BY month, cnt DESC
        """, (contact_id,))

        lines.append("MONTHLY PLATFORM TIMELINE:")
        lines.append(f"{'Month':>7}  {'Platform':>15}  {'Dev#':>5} {'Count':>7}")
        lines.append("-" * 45)
        for r in monthly:
            dev = r["device_number"] or 0
            lines.append(
                f"{r['month'] or '':>7}  {r['platform_label'] or '':>15}  "
                f"{'P' if dev == 0 else f'C#{dev}':>5} {r['cnt']:>7}"
            )

        debug_text.setPlainText("\n".join(lines))
        tabs.addTab(debug_text, "Forensic Debug")

        # ============================================================
        # Export buttons
        # ============================================================
        btn_lay = QHBoxLayout()
        export_html_btn = QPushButton("Export as HTML")
        export_csv_btn = QPushButton("Export as CSV")

        def _export_html():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Export Device History",
                f"{contact_name}_device_history.html", "HTML (*.html)",
            )
            if not path:
                return
            html = ["<html><head><style>",
                    "body{font-family:Segoe UI,sans-serif;margin:20px}",
                    "table{border-collapse:collapse;width:100%}",
                    "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}",
                    "th{background:#075e54;color:white}",
                    "tr:nth-child(even){background:#f9f9f9}",
                    "h1{color:#075e54}h2{color:#128c7e}",
                    ".mono{font-family:Consolas,monospace;font-size:11px}",
                    "</style></head><body>"]
            html.append(f"<h1>Device History: {contact_name}</h1>")
            html.append(f"<p>Phone JID: <code>{phone_jid}</code><br>")
            html.append(f"LID JID: <code>{lid_jid}</code><br>")
            html.append(f"Contact ID: {contact_id}</p>")

            # Sessions table
            html.append("<h2>Device Sessions</h2><table><tr>")
            for h in ["Device #", "Platform", "Personal", "Group", "Total",
                       "First Seen", "Last Seen", "Confidence"]:
                html.append(f"<th>{h}</th>")
            html.append("</tr>")
            for r in session_rows:
                dev = r["device_number"] or 0
                plat = r["platform_label"] or ""
                _pd = {"android": "Android", "iphone": "iPhone",
                       "companion": "Web/Desktop"}.get(plat, plat)
                def _ts(ms):
                    if not ms: return ""
                    try: return format_timestamp(ms, "datetime")
                    except: return ""
                conf = r["avg_conf"] or 0
                html.append(f"<tr><td>{'Primary (0)' if dev == 0 else f'#{dev}'}</td>"
                           f"<td>{_pd}</td>"
                           f"<td>{r['personal_msgs'] or 0}</td>"
                           f"<td>{r['group_msgs'] or 0}</td>"
                           f"<td>{r['msgs']}</td>"
                           f"<td>{_ts(r['first_ts'])}</td>"
                           f"<td>{_ts(r['last_ts'])}</td>"
                           f"<td>{conf*100:.0f}%</td></tr>")
            html.append("</table>")

            # Key_id debug
            html.append("<h2>Key_ID Pattern Analysis</h2>")
            html.append("<pre class='mono'>")
            html.append("\n".join(lines))
            html.append("</pre>")

            html.append(f"<p><em>Generated by WhatsApp Android Tool</em></p>")
            html.append("</body></html>")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(html))

        def _export_csv():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Export Device History",
                f"{contact_name}_device_history.csv", "CSV (*.csv)",
            )
            if not path:
                return
            import csv
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Date", "Device #", "Platform", "Chat Type",
                           "Messages", "Confidence"])
                for r in daily_rows:
                    w.writerow([
                        r["day"], r["device_number"], r["platform_label"],
                        r["chat_type"], r["msgs"], r["avg_conf"],
                    ])

        export_html_btn.clicked.connect(_export_html)
        export_csv_btn.clicked.connect(_export_csv)
        btn_lay.addWidget(export_html_btn)
        btn_lay.addWidget(export_csv_btn)
        btn_lay.addStretch()
        main_lay.addLayout(btn_lay)

        dlg.exec()

    # ------------------------------------------------------------------ #
    # Direct conversation button
    # ------------------------------------------------------------------ #

    def _build_direct_conv_button(self) -> None:
        self._direct_conv_frame = QFrame()
        self._direct_conv_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        fl = QHBoxLayout(self._direct_conv_frame)
        fl.setContentsMargins(16, 12, 16, 12)

        self._direct_conv_btn = QPushButton("\u2192  Open Direct Conversation")
        self._direct_conv_btn.setCursor(Qt.PointingHandCursor)
        self._direct_conv_btn.setFixedHeight(36)
        self._direct_conv_btn.setStyleSheet(self._tm.contact_detail_direct_btn_style())
        self._direct_conv_btn.clicked.connect(self._on_direct_conv_clicked)
        fl.addWidget(self._direct_conv_btn)
        fl.addStretch()

        self._content_layout.addWidget(self._direct_conv_frame)

        # --- Report Generation Button ---
        report_frame = QFrame()
        report_frame.setStyleSheet(self._tm.contact_detail_section_frame_style())
        rl = QHBoxLayout(report_frame)
        rl.setContentsMargins(16, 12, 16, 12)
        self._report_btn = QPushButton("\U0001F4C4  Generate Full Activity Report (PDF)")
        self._report_btn.setCursor(Qt.PointingHandCursor)
        self._report_btn.setFixedHeight(36)
        self._report_btn.setStyleSheet(
            "QPushButton { background: #075e54; color: white; border: none; "
            "border-radius: 6px; padding: 0 20px; font-weight: 600; font-size: 12px; }"
            "QPushButton:hover { background: #128c7e; }"
        )
        self._report_btn.clicked.connect(self._on_generate_report)
        rl.addWidget(self._report_btn)
        rl.addStretch()
        self._content_layout.addWidget(report_frame)

    def _on_generate_report(self) -> None:
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QFileDialog, QLabel, QMessageBox
        from PySide6.QtWebEngineWidgets import QWebEngineView

        contact_id = getattr(self, "_contact_id", None)
        if not contact_id:
            return

        # Generate HTML
        from app.services.contact_report_generator import ContactReportGenerator
        from app.services.database import Database
        db = Database.get()

        # Get case info
        case_info = {}
        try:
            meta = db.fetchone("SELECT key, value FROM case_metadata WHERE key = 'case_id'")
            if meta:
                case_info["case_id"] = meta[1]
            meta2 = db.fetchone("SELECT key, value FROM case_metadata WHERE key = 'examiner'")
            if meta2:
                case_info["examiner"] = meta2[1]
            meta3 = db.fetchone("SELECT key, value FROM case_metadata WHERE key = 'device_owner_name'")
            if meta3:
                case_info["device_owner"] = meta3[1]
            meta4 = db.fetchone("SELECT key, value FROM case_metadata WHERE key = 'device_owner_jid'")
            if meta4:
                case_info["device_owner_jid"] = meta4[1]
        except Exception:
            pass

        gen = ContactReportGenerator(contact_id, case_info=case_info)
        try:
            html = gen.generate_html()
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to generate report: {e}")
            return

        # Show preview dialog
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Activity Report - {getattr(self, '_contact_name', '')}")
        dlg.setMinimumSize(900, 700)
        dlg.resize(1000, 800)
        lay = QVBoxLayout(dlg)

        preview = QWebEngineView()
        preview.setHtml(html)
        lay.addWidget(preview)

        btn_lay = QHBoxLayout()
        html_btn = QPushButton("Export HTML")
        pdf_btn = QPushButton("Export PDF")
        html_btn.setStyleSheet("QPushButton { padding: 8px 20px; }")
        pdf_btn.setStyleSheet("QPushButton { background: #075e54; color: white; padding: 8px 20px; border: none; border-radius: 4px; font-weight: 600; } QPushButton:hover { background: #128c7e; }")

        def _save_html():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Export HTML Report",
                f"{getattr(self, '_contact_name', 'report')}_report.html",
                "HTML (*.html)",
            )
            if path:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                # Chain of custody: log export
                try:
                    from app.services.chain_of_custody import ChainOfCustody
                    ChainOfCustody.get().log_export(
                        "html_report", path,
                        contact_id=contact_id,
                        contact_name=getattr(self, '_contact_name', ''),
                    )
                except Exception:
                    pass
                QMessageBox.information(dlg, "Saved", f"Report saved to {path}")

        def _save_pdf():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Export PDF Report",
                f"{getattr(self, '_contact_name', 'report')}_report.pdf",
                "PDF (*.pdf)",
            )
            if not path:
                return
            # Use QWebEngineView's built-in printToPdf (no external deps)
            from PySide6.QtCore import QMarginsF
            from PySide6.QtGui import QPageLayout, QPageSize
            page_layout = QPageLayout(
                QPageSize(QPageSize.A4),
                QPageLayout.Portrait,
                QMarginsF(15, 15, 15, 15),
            )
            def _on_pdf_done(ok_path):
                QMessageBox.information(dlg, "Saved", f"PDF saved to {path}")
            preview.page().printToPdf(path, page_layout)
            # printToPdf is async but writes directly to file
            from PySide6.QtCore import QTimer
            def _pdf_done_log():
                try:
                    from app.services.chain_of_custody import ChainOfCustody
                    ChainOfCustody.get().log_export(
                        "pdf_report", path,
                        contact_id=contact_id,
                        contact_name=getattr(self, '_contact_name', ''),
                    )
                except Exception:
                    pass
                QMessageBox.information(dlg, "Saved", f"PDF saved to:\n{path}")
            QTimer.singleShot(2000, _pdf_done_log)

        html_btn.clicked.connect(_save_html)
        pdf_btn.clicked.connect(_save_pdf)
        btn_lay.addWidget(html_btn)
        btn_lay.addWidget(pdf_btn)
        btn_lay.addStretch()
        lay.addLayout(btn_lay)
        dlg.exec()

    def _on_direct_conv_clicked(self) -> None:
        if self._direct_conv_id is not None:
            name = self._direct_conv_name or self._contact_name
            self.conversation_requested.emit(self._direct_conv_id, name)

    # ------------------------------------------------------------------ #
    # Status updates
    # ------------------------------------------------------------------ #

    def _build_status_section(self) -> None:
        self._status_header = QLabel("Status Updates")
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        self._status_header.setFont(f)
        self._status_header.setStyleSheet(self._tm.contact_detail_groups_header_style())
        self._content_layout.addWidget(self._status_header)

        self._status_grid_widget = QWidget()
        self._status_grid_layout = QHBoxLayout(self._status_grid_widget)
        self._status_grid_layout.setContentsMargins(0, 0, 0, 0)
        self._status_grid_layout.setSpacing(8)
        self._status_grid_layout.addStretch()
        self._content_layout.addWidget(self._status_grid_widget)

        # "View all" link (hidden initially)
        self._status_view_all = QPushButton("View All Status Updates \u2192")
        self._status_view_all.setCursor(Qt.PointingHandCursor)
        self._status_view_all.setStyleSheet(
            "QPushButton { color: #00bcd4; border: none; text-align: left; "
            "font-size: 12px; padding: 4px 0; } "
            "QPushButton:hover { text-decoration: underline; }"
        )
        self._status_view_all.setVisible(False)
        self._content_layout.addWidget(self._status_view_all)

    def _load_status_data(self, contact_id: int) -> None:
        """Load and display status post tiles for this contact."""
        db = Database.get()

        # Check if status_post table exists
        try:
            db.scalar("SELECT 1 FROM status_post LIMIT 1")
        except Exception:
            self._status_header.setText("Status Updates  (N/A)")
            return

        posts = db.fetchall("""
            SELECT sp.id, sp.timestamp, sp.type_label, sp.text_content,
                   sp.has_media, sp.media_file_path, sp.view_count,
                   med.thumbnail_blob, med.file_exists
            FROM status_post sp
            LEFT JOIN media med ON med.message_id = sp.message_id
            WHERE sp.contact_id = ?
            ORDER BY sp.timestamp DESC
            LIMIT 8
        """, (contact_id,))

        total = db.scalar(
            "SELECT COUNT(*) FROM status_post WHERE contact_id = ?",
            (contact_id,),
        ) or 0

        self._status_header.setText(f"Status Updates  ({total})")

        # Clear existing tiles
        while self._status_grid_layout.count() > 1:
            item = self._status_grid_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not posts:
            no_status = QLabel("No status updates from this contact")
            no_status.setStyleSheet("color: gray; font-size: 11px; padding: 8px;")
            self._status_grid_layout.insertWidget(0, no_status)
            self._status_view_all.setVisible(False)
            return

        for idx, post in enumerate(posts):
            tile = self._build_status_tile(post)
            self._status_grid_layout.insertWidget(idx, tile)

        self._status_view_all.setVisible(total > 8)

    def _build_status_tile(self, post) -> QWidget:
        """Build a mini status tile for the contact detail page."""
        (sp_id, timestamp, type_label, text_content, has_media,
         file_path, view_count, thumbnail_blob, file_exists) = post

        tile = QFrame()
        tile.setFixedSize(100, 120)

        if file_exists:
            border_color = "#4caf50"
        elif file_path:
            border_color = "#2196f3"
        else:
            border_color = "#ff9800"

        tile.setStyleSheet(
            f"QFrame {{ background: {'#f5f5f5' if self._tm.is_light else '#2a2a3e'}; "
            f"border-radius: 6px; border: 2px solid {border_color}; }}"
        )

        tl = QVBoxLayout(tile)
        tl.setContentsMargins(3, 3, 3, 3)
        tl.setSpacing(2)

        preview = QLabel()
        preview.setFixedHeight(80)
        preview.setAlignment(Qt.AlignCenter)
        preview.setStyleSheet("border: none;")

        if thumbnail_blob:
            pix = QPixmap()
            pix.loadFromData(bytes(thumbnail_blob))
            pix = pix.scaled(94, 76, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            preview.setPixmap(pix)
        elif text_content and type_label == "text":
            preview.setText(text_content[:40])
            preview.setWordWrap(True)
            preview.setStyleSheet(
                "border: none; padding: 4px; font-size: 9px; "
                f"background: {'#e8f5e9' if self._tm.is_light else '#1b5e20'}; "
                "border-radius: 4px;"
            )
        else:
            icons = {"image": "\U0001F5BC", "video": "\U0001F3AC", "voice": "\U0001F3A4", "gif": "GIF"}
            preview.setText(icons.get(type_label, "\U0001F4AC"))
            preview.setStyleSheet(
                f"border: none; font-size: 24px; "
                f"background: {'#eceff1' if self._tm.is_light else '#37374a'}; border-radius: 4px;"
            )
        tl.addWidget(preview)

        # Timestamp
        ts_text = format_timestamp(timestamp, "short") if timestamp else ""
        ts = QLabel(ts_text)
        ts.setStyleSheet("font-size: 8px; color: gray; border: none;")
        ts.setAlignment(Qt.AlignCenter)
        tl.addWidget(ts)

        return tile

    # ------------------------------------------------------------------ #
    # Groups in common
    # ------------------------------------------------------------------ #

    def _build_groups_section(self) -> None:
        self._groups_header = QLabel("Groups in Common")
        f = QFont()
        f.setPointSize(13)
        f.setBold(True)
        self._groups_header.setFont(f)
        self._groups_header.setStyleSheet(self._tm.contact_detail_groups_header_style())
        self._content_layout.addWidget(self._groups_header)

        self._groups_list = QListWidget()
        self._groups_list.setMinimumHeight(120)
        self._groups_list.setStyleSheet(self._tm.contact_detail_groups_list_style())
        self._groups_list.itemDoubleClicked.connect(self._on_group_clicked)
        self._content_layout.addWidget(self._groups_list)

    def _on_group_clicked(self, item: QListWidgetItem) -> None:
        conv_id = item.data(Qt.UserRole)
        conv_name = item.text().split("  (")[0] if "  (" in item.text() else item.text()
        if conv_id is not None:
            self.conversation_requested.emit(conv_id, conv_name)

    # ------------------------------------------------------------------ #
    # Copy contact info
    # ------------------------------------------------------------------ #

    def _copy_contact_info(self) -> None:
        """Copy all contact info to clipboard."""
        lines = [f"Name: {self._name_label.text()}"]
        lines.append(f"Phone: {self._phone_label.text()}")
        for key, label in self._detail_rows.items():
            val = label.text()
            if val and val != "-":
                lines.append(f"{key}: {val}")
        QApplication.clipboard().setText("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Generate Contact Report
    # ------------------------------------------------------------------ #

    def _generate_contact_report(self) -> None:
        """Generate and open a comprehensive forensic report for this contact.

        Flow:
            1. Open the section-picker dialog so the investigator chooses
               sections + output format (HTML / PDF) + save location.
               Selection persists in QSettings.
            2. Run the backend report builder, always producing HTML
               first (every section is fault-isolated inside the
               builder).
            3. If the user picked PDF, render the HTML to PDF via
               QWebEngineView.printToPdf and write to the chosen path.
            4. Open the result in the default browser / PDF viewer.
        """
        if not self._contact_id:
            return

        import webbrowser
        import traceback
        import tempfile
        from pathlib import Path
        from PySide6.QtWidgets import QDialog, QMessageBox

        # 1. Section-picker dialog (now also has format + save-path)
        from app.views.dialogs.report_sections_dialog import ReportSectionsDialog
        db = Database.get()
        default_dir = (db.path.parent / "reports") if db and db.path else Path.home()
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            default_dir = Path.home()
        dlg = ReportSectionsDialog(
            contact_name=(self._contact_name
                          or f"Contact #{self._contact_id}"),
            parent=self,
            default_dir=default_dir,
            default_filename_stem="contact_report",
        )
        if dlg.exec() != QDialog.Accepted:
            return  # user cancelled
        sections = dlg.get_selection() or {}
        out_format = dlg.output_format          # "html" or "pdf"
        chosen_path = dlg.output_path           # Path

        # 3. Backend builder always writes HTML first.  When PDF is
        # requested we redirect the HTML to a temp file, then render
        # it to the user's PDF target.
        from app.views.pages._report_loader import load_contact_report
        generate_contact_report = load_contact_report()

        try:
            self._report_btn.setText("Generating...")
            self._report_btn.setEnabled(False)
            QApplication.processEvents()

            if out_format == ReportSectionsDialog.FORMAT_PDF:
                tmp_html = Path(tempfile.gettempdir()) / (
                    f"_wai_contact_{self._contact_id}_"
                    f"{datetime.now().strftime('%H%M%S%f')}.html"
                )
                generate_contact_report(
                    analysis_db_path=str(db.path),
                    contact_id=self._contact_id,
                    output_path=str(tmp_html),
                    sections=sections,
                )
                self._render_html_to_pdf(tmp_html, chosen_path)
                try:
                    tmp_html.unlink()
                except Exception:
                    pass
                result_path = chosen_path
            else:
                result_path = generate_contact_report(
                    analysis_db_path=str(db.path),
                    contact_id=self._contact_id,
                    output_path=str(chosen_path),
                    sections=sections,
                )
                if not isinstance(result_path, Path):
                    result_path = Path(str(result_path))

            self._report_btn.setText("\U0001F4CB Report")
            self._report_btn.setEnabled(True)
            try:
                webbrowser.open(Path(result_path).as_uri())
            except Exception:
                pass

        except Exception as e:
            self._report_btn.setText("\U0001F4CB Report")
            self._report_btn.setEnabled(True)
            # Show the full traceback in the details pane so
            # the user has actionable diagnostic output instead
            # of a one-line "operation failed" message.
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Report Generation Failed")
            box.setText("Failed to generate contact report.")
            box.setInformativeText(str(e))
            box.setDetailedText(traceback.format_exc())
            box.exec()

    def _render_html_to_pdf(self, html_path: Path, pdf_path: Path) -> None:
        """Render an HTML file to PDF via QWebEngineView's printToPdf
        (mirrors the Group Report's PDF pipeline: landscape A4, tight
        margins, 1400×1800 off-screen viewport so wide tables compute
        proper column widths before printing).
        """
        from pathlib import Path as _P
        from PySide6.QtCore import QEventLoop, QMarginsF, QUrl, QTimer
        from PySide6.QtGui import QPageLayout, QPageSize
        from PySide6.QtWebEngineWidgets import QWebEngineView

        html_path = _P(html_path)
        pdf_path = _P(pdf_path)

        view = QWebEngineView()
        view.resize(1400, 1800)
        loop = QEventLoop()

        def _on_load(ok: bool) -> None:
            if not ok:
                loop.quit()
                return
            QTimer.singleShot(400, _do_print)

        def _do_print() -> None:
            layout = QPageLayout(
                QPageSize(QPageSize.A4),
                QPageLayout.Landscape,
                QMarginsF(8, 10, 8, 10),
            )
            view.page().printToPdf(str(pdf_path), layout)

        def _on_pdf_done(path: str, ok: bool) -> None:
            loop.quit()

        view.loadFinished.connect(_on_load)
        view.page().pdfPrintingFinished.connect(_on_pdf_done)
        view.load(QUrl.fromLocalFile(str(html_path.resolve())))
        # Fail-safe so the loop can't hang forever if the page never
        # finishes loading (broken local resources etc.)
        QTimer.singleShot(20_000, loop.quit)
        loop.exec()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def load_contact(self, contact_id: int) -> None:
        """Load and display all data for the given contact."""
        self._contact_id = contact_id
        db = Database.get()

        # ----------------------------------------------------------
        # 1. Basic contact info
        # ----------------------------------------------------------
        contact = db.fetchone(
            """
            SELECT id, resolved_name, phone_number, phone_jid, lid_jid,
                   wa_name, display_name, status_text,
                   is_business, platform_estimate, avatar_blob,
                   business_name, business_category, lid_display_name,
                   COALESCE(linked_device_count, 0) AS linked_device_count,
                   lid_masked_phone,
                   business_vertical, business_description, business_address,
                   business_city, business_postal_code,
                   business_latitude, business_longitude, business_location_name,
                   business_email, business_website,
                   business_hours_note, business_time_zone,
                   business_member_since, business_cover_url,
                   trust_tier, is_meta_verified,
                   fb_linked_name, fb_linked_likes,
                   ig_linked_name, ig_linked_followers
            FROM contact
            WHERE id = ?
            """,
            (contact_id,),
        )
        if not contact:
            self._name_label.setText("Contact not found")
            self._phone_label.setText("")
            return

        name = (
            contact["resolved_name"]
            or contact["display_name"]
            or contact["wa_name"]
            or contact["phone_number"]
            or contact["phone_jid"]
            or "Unknown"
        )
        self._contact_name = name

        # Build phone display
        phone_parts = []
        phone_num = contact["phone_number"]
        phone_jid = contact["phone_jid"]
        wa_name = contact["wa_name"]

        if phone_num:
            phone_display = f"+{phone_num}" if not phone_num.startswith("+") else phone_num
            phone_parts.append(phone_display)
        elif phone_jid and "@" in phone_jid:
            raw = phone_jid.split("@")[0]
            phone_parts.append(f"+{raw}" if raw.isdigit() else raw)

        if wa_name and wa_name != name:
            phone_parts.append(f"~{wa_name}")

        # Update header
        self._name_label.setText(name)
        self._phone_label.setText("  |  ".join(phone_parts) if phone_parts else "")
        self._set_avatar(contact_id, name, contact["avatar_blob"])

        # ----------------------------------------------------------
        # 2. Detail rows
        # ----------------------------------------------------------
        self._detail_rows["wa_name"].setText(contact["wa_name"] or "-")
        self._detail_rows["display_name"].setText(contact["display_name"] or "-")

        # Show clean phone number.  IMPORTANT: a real phone always wins -
        # we never replace a resolved number with the LID masked hint.
        # The masked phone is only used as a fallback when the contact's
        # phone genuinely couldn't be resolved (i.e. msgstore.jid had no
        # phone-JID counterpart for the LID).
        try:
            masked = contact["lid_masked_phone"] or ""
        except (IndexError, KeyError):
            masked = ""
        if phone_num:
            phone_display = f"+{phone_num}" if not phone_num.startswith("+") else phone_num
            self._detail_rows["phone_number"].setText(phone_display)
        elif phone_jid and "@" in phone_jid:
            raw = phone_jid.split("@")[0]
            self._detail_rows["phone_number"].setText(f"+{raw}" if raw.isdigit() else raw)
        elif masked:
            # Unresolved LID - surface WhatsApp's own privacy hint so the
            # investigator can at least see "+91∙∙∙∙∙∙89" instead of the
            # raw 15-digit LID.  Tag it so it's clear this isn't the
            # real number we have access to.
            self._detail_rows["phone_number"].setText(
                f"{masked}  (LID-only — phone never seen)"
            )
        else:
            self._detail_rows["phone_number"].setText("-")

        self._detail_rows["phone_jid"].setText(contact["phone_jid"] or "-")
        self._detail_rows["lid_jid"].setText(contact["lid_jid"] or "-")
        self._detail_rows["status_text"].setText(contact["status_text"] or "-")
        platform = contact["platform_estimate"] or ""
        try:
            plat_conf = contact["platform_confidence"] or 0
        except (IndexError, KeyError):
            plat_conf = 0
        _plat_labels = {
            "android": "Android", "iphone": "iPhone",
            "multi_device": "Multi-Device", "phone": "Phone",
        }
        plat_text = _plat_labels.get(platform, platform.title() if platform else "-")
        if plat_conf and plat_conf > 0:
            plat_text += f" ({plat_conf*100:.0f}% confidence)"
        self._detail_rows["platform"].setText(plat_text)
        # Linked / companion devices -- show current + historical counts
        current_linked = contact["linked_device_count"] or 0
        if "linked_devices" in self._detail_rows:
            # Count historical unique companion device numbers from messages
            try:
                hist_count = db.scalar("""
                    SELECT COUNT(DISTINCT md.device_number)
                    FROM message_device md
                    JOIN message m ON m.id = md.message_id
                    WHERE m.sender_id = ? AND md.device_number > 0
                """, (contact_id,))
                hist_count = hist_count or 0
            except Exception:
                hist_count = 0

            if hist_count > 0:
                parts = []
                if current_linked > 0:
                    parts.append(f"{current_linked} currently linked")
                parts.append(f"{hist_count} seen historically")
                self._detail_rows["linked_devices"].setText(" | ".join(parts))
            elif current_linked > 0:
                self._detail_rows["linked_devices"].setText(
                    f"{current_linked} currently linked"
                )
            else:
                self._detail_rows["linked_devices"].setText("Phone only")
        is_business = contact["is_business"]
        self._detail_rows["business"].setText(
            "\u2705 Yes" if is_business else "No"
        )
        business_name = contact["business_name"] or ""
        business_cat = contact["business_category"] or ""
        if business_name:
            biz_text = business_name
            if business_cat:
                biz_text += f" ({business_cat})"
            self._detail_rows["business_name"].setText(biz_text)
        else:
            self._detail_rows["business_name"].setText("-")

        # ----------------------------------------------------------
        # 3. Stats
        # ----------------------------------------------------------
        sent_row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM message WHERE sender_id = ? AND message_type != 7",
            (contact_id,),
        )
        messages_sent = (sent_row["cnt"] if sent_row else 0) or 0

        direct_conv = db.fetchone(
            """
            SELECT c.id, c.display_name
            FROM conversation c
            WHERE c.jid_raw_string IN (
                SELECT jtc.jid_raw_string FROM jid_to_contact jtc
                WHERE jtc.contact_id = ?
            ) AND c.chat_type = 'personal'
            LIMIT 1
            """,
            (contact_id,),
        )

        messages_received = 0
        if direct_conv:
            self._direct_conv_id = direct_conv["id"]
            self._direct_conv_name = direct_conv["display_name"]
            recv_row = db.fetchone(
                """
                SELECT COUNT(*) as cnt FROM message
                WHERE conversation_id = ? AND (sender_id IS NULL OR sender_id != ?)
                  AND message_type != 7
                """,
                (self._direct_conv_id, contact_id),
            )
            messages_received = (recv_row["cnt"] if recv_row else 0) or 0
            self._direct_conv_btn.setEnabled(True)
            self._direct_conv_btn.setText(
                f"\u2192  Open Chat  ({direct_conv['display_name'] or name})"
            )
        else:
            self._direct_conv_id = None
            self._direct_conv_name = None
            self._direct_conv_btn.setEnabled(False)
            self._direct_conv_btn.setText("No Direct Conversation Found")

        # Media count
        media_row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM media WHERE message_id IN "
            "(SELECT id FROM message WHERE sender_id = ?)",
            (contact_id,),
        )
        total_media = (media_row["cnt"] if media_row else 0) or 0

        # Call count
        calls_row = db.fetchone(
            "SELECT COUNT(*) as cnt FROM call_record WHERE contact_id = ?",
            (contact_id,),
        )
        total_calls = (calls_row["cnt"] if calls_row else 0) or 0

        self._update_stat(self._stat_sent, f"{messages_sent:,}")
        self._update_stat(self._stat_received, f"{messages_received:,}")
        self._update_stat(self._stat_media, f"{total_media:,}")
        self._update_stat(self._stat_calls, f"{total_calls:,}")

        # ----------------------------------------------------------
        # 3b. Device activity
        # ----------------------------------------------------------
        self._update_device_section(contact_id)

        # ----------------------------------------------------------
        # 3b.5. Provenance — why is this contact in our DB?
        # ----------------------------------------------------------
        try:
            self._load_provenance(contact_id)
        except Exception as e:
            try:
                self._provenance_label.setText(f"(provenance load error: {e})")
            except Exception:
                pass

        # ----------------------------------------------------------
        # 3b.6. Per-contact call history
        # ----------------------------------------------------------
        try:
            self._load_calls(contact_id)
        except Exception as e:
            try:
                self._calls_list.clear()
                self._calls_list.addItem(f"(calls load error: {e})")
            except Exception:
                pass

        # ----------------------------------------------------------
        # 3c. Status updates
        # ----------------------------------------------------------
        self._load_status_data(contact_id)

        # ----------------------------------------------------------
        # 4. Groups in common
        # ----------------------------------------------------------
        # The DEVICE OWNER is never in `group_member` (WhatsApp's own
        # group_participants table excludes the owner because the owner is
        # implicit).  So for the device-owner contact we MUST drive the
        # current/past split off `conversation.participation_status` —
        # without this fix, every group the owner ever messaged shows up
        # as "past member" even when the owner is still a current member.
        #
        # WhatsApp participation_status enum:
        #   1 = invited / pending
        #   2 = active (current member)        ← treat as CURRENT
        #   3 = past (voluntarily left)        ← treat as PAST
        #   4 = past (removed / kicked)        ← treat as PAST
        owner_jid = db.scalar(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_jid'"
        )
        is_device_owner = bool(
            owner_jid and (
                contact["phone_jid"] == owner_jid
                or contact["lid_jid"] == owner_jid
            )
        )

        if is_device_owner:
            # Drive both buckets off participation_status — group_member
            # is empty for the owner so the original join would never
            # match.
            current_groups = db.fetchall(
                """
                SELECT c.id, c.display_name, c.participant_count, c.avatar_blob,
                       'current' AS membership, NULL AS role
                FROM conversation c
                WHERE c.chat_type = 'group'
                  AND c.participation_status IN (1, 2)
                ORDER BY COALESCE(c.message_count, 0) DESC, c.display_name
                """
            )
            past_groups = db.fetchall(
                """
                SELECT c.id, c.display_name, c.participant_count, c.avatar_blob,
                       'past' AS membership
                FROM conversation c
                WHERE c.chat_type = 'group'
                  AND c.participation_status IN (3, 4)
                  AND COALESCE(c.message_count, 0) > 0
                ORDER BY COALESCE(c.message_count, 0) DESC, c.display_name
                """
            )
        else:
            # Other contacts: drive off group_member (canonical) and
            # message-presence for "past" inference.
            current_groups = db.fetchall(
                """
                SELECT c.id, c.display_name, c.participant_count, c.avatar_blob,
                       'current' AS membership, gm.role
                FROM conversation c
                JOIN group_member gm ON gm.conversation_id = c.id
                WHERE gm.contact_id = ? AND c.chat_type = 'group'
                ORDER BY c.display_name
                """,
                (contact_id,),
            )
            past_groups = db.fetchall(
                """
                SELECT DISTINCT c.id, c.display_name, c.participant_count, c.avatar_blob,
                       'past' AS membership
                FROM conversation c
                JOIN message m ON m.conversation_id = c.id
                WHERE m.sender_id = ? AND c.chat_type = 'group'
                  AND c.id NOT IN (SELECT gm.conversation_id FROM group_member gm WHERE gm.contact_id = ?)
                ORDER BY c.display_name
                """,
                (contact_id, contact_id),
            )

        current_ids = {g["id"] for g in current_groups}

        all_groups = list(current_groups) + list(past_groups)

        self._groups_list.clear()
        if all_groups:
            current_count = len(current_groups)
            past_count = len(past_groups)
            header_parts = []
            if current_count:
                header_parts.append(f"{current_count} current")
            if past_count:
                header_parts.append(f"{past_count} past")
            self._groups_header.setText(
                f"Groups in Common  ({len(all_groups)}: {', '.join(header_parts)})"
            )
            for group in all_groups:
                gname = group["display_name"] or f"Group #{group['id']}"
                pcount = group["participant_count"] or 0
                membership = group["membership"]
                try:
                    role = group["role"] or "member"
                except (IndexError, KeyError):
                    role = "member"
                role_badge = ""
                if role == "superadmin":
                    role_badge = " [Super Admin]"
                elif role == "admin":
                    role_badge = " [Admin]"
                if membership == "past":
                    label = f"\u23F3 {gname}  ({pcount} members) — past member"
                else:
                    label = f"\u2705 {gname}  ({pcount} members){role_badge}"
                item = QListWidgetItem(label)
                item.setData(Qt.UserRole, group["id"])
                # Show group avatar if available
                avatar_blob = group["avatar_blob"]
                if avatar_blob and len(avatar_blob) > 100:
                    pxm = QPixmap()
                    pxm.loadFromData(avatar_blob)
                    if not pxm.isNull():
                        from PySide6.QtGui import QIcon
                        item.setIcon(QIcon(pxm.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
                self._groups_list.addItem(item)
        else:
            self._groups_header.setText("Groups in Common  (0)")
            placeholder = QListWidgetItem("No groups in common")
            placeholder.setFlags(Qt.NoItemFlags)
            placeholder.setForeground(QColor("#90a4ae"))
            self._groups_list.addItem(placeholder)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _set_avatar(self, contact_id: int, name: str, avatar_blob) -> None:
        self._avatar_blob = avatar_blob  # Store for viewer
        self._avatar_name = name
        self._avatar_label.setCursor(Qt.PointingHandCursor)
        self._avatar_label.mousePressEvent = self._on_avatar_click
        if avatar_blob and len(avatar_blob) > 100:
            pxm = QPixmap()
            pxm.loadFromData(avatar_blob)
            if not pxm.isNull():
                size = 48
                result = QPixmap(size, size)
                result.fill(QColor(0, 0, 0, 0))

                painter = QPainter(result)
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setRenderHint(QPainter.SmoothPixmapTransform)

                clip = QPainterPath()
                clip.addEllipse(0.0, 0.0, float(size), float(size))
                painter.setClipPath(clip)

                scaled = pxm.scaled(
                    size, size,
                    Qt.KeepAspectRatioByExpanding,
                    Qt.SmoothTransformation,
                )
                dx = (scaled.width() - size) // 2
                dy = (scaled.height() - size) // 2
                painter.drawPixmap(-dx, -dy, scaled)
                painter.end()

                self._avatar_label.setPixmap(result)
                self._avatar_label.setText("")
                self._avatar_label.setStyleSheet(
                    "QLabel { background: transparent; border-radius: 24px; }"
                )
                return

        initials = "".join(
            w[0] for w in name.split()[:2] if w and w[0].isalpha()
        )[:2].upper()
        if not initials:
            initials = "?"
        avatar_bg = AVATAR_COLORS[contact_id % len(AVATAR_COLORS)]
        self._avatar_label.setPixmap(QPixmap())
        self._avatar_label.setText(initials)
        self._avatar_label.setStyleSheet(
            f"QLabel {{ background: {avatar_bg}; border-radius: 24px; "
            f"color: white; font-size: 18px; font-weight: bold; }}"
        )

    def _on_avatar_click(self, event) -> None:
        blob = getattr(self, "_avatar_blob", None)
        if not blob or len(blob) < 100:
            return
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QPushButton, QFileDialog
        from PySide6.QtGui import QPixmap
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Profile Picture - {getattr(self, '_avatar_name', '')}")
        dlg.setMinimumSize(320, 320)
        lay = QVBoxLayout(dlg)
        pxm = QPixmap()
        pxm.loadFromData(blob)
        if pxm.isNull():
            return
        img_label = QLabel()
        img_label.setPixmap(pxm.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        img_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(img_label)
        save_btn = QPushButton("Save as...")
        def _save():
            path, _ = QFileDialog.getSaveFileName(
                dlg, "Save Profile Picture",
                f"{getattr(self, '_avatar_name', 'avatar')}.jpg",
                "JPEG (*.jpg);;PNG (*.png)",
            )
            if path:
                pxm.save(path)
        save_btn.clicked.connect(_save)
        lay.addWidget(save_btn)
        dlg.exec()
