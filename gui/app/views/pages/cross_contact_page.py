"""Cross-Contact Analysis page — pick N contacts, see what they
share.

The investigator picks 2 (or more) contacts from the case roster.
The page then computes everything they have in common and surfaces
it as a single dashboard:

  * **Shared Groups** — every group conversation that ALL of the
    selected contacts are members of (current or past).  Click any
    group → jump the chat viewer to it.
  * **Calls between them** — voice / video / group calls that
    involved EVERY selected contact (the device owner counts when
    one of the picks is "You").  Includes per-call category, result,
    duration, and a "Go" button.
  * **Files shared in common** — file SHA-256 hashes that AT LEAST
    one of each selected contact has shared.  Surfaces forwarded
    deals / scams / coordinated content.
  * **Cross mentions** — @-mentions where the sender is one
    selected contact and the mentioned is another.
  * **Conversations they all appear in (any role)** — broader than
    "shared groups": includes 1:1 chats too, where applicable.

Why this matters
================
WhatsApp investigators often need to "find the link" between two
suspects — did they ever call each other?  Are they in the same
groups?  Do they share any of the same files (a leaked deck, a scam
PDF)?  The data is all there in msgstore.db; this page makes it
trivial to interrogate.

Owner-aware
===========
The device owner is a first-class pickable contact — appears at the
top of the multi-select list with a "(you)" tag and the DEVICE
OWNER badge.  Owner messages have ``sender_id = NULL`` in WhatsApp,
so every per-contact query has an owner-special path that stamps in
the case_metadata identity.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.services.database import Database
from app.services.theme_manager import ThemeManager


# Sentinel contact-id used to represent the device owner in the
# multi-select list.  No real contact ever has id=-1 in our schema.
OWNER_ID = -1


class CrossContactPage(QWidget):
    """Cross-Contact Analysis — pick N contacts, see what they share."""

    # Click-to-navigate: emitted when the analyst opens any result row
    # via "Go to chat" / "Go to message".  ``msg_id`` may be 0 for
    # "open at bottom of conversation".
    conversation_selected = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._owner_name: str = ""
        self._owner_jid: str = ""
        self._all_contacts: list[dict] = []     # case roster (top N by activity)
        self._picked_ids: set[int] = set()      # currently selected

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(10)

        self._build_header(root)
        self._build_body(root)

        QTimer.singleShot(50, self._initial_load)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_header(self, root: QVBoxLayout) -> None:
        h = QHBoxLayout()
        title = QLabel("Cross-Contact Analysis")
        f = QFont(); f.setPointSize(18); f.setBold(True)
        title.setFont(f)
        h.addWidget(title)
        sub = QLabel(
            "Pick 2 or more contacts on the left → see every group, call, "
            "file, mention they have in common."
        )
        sub.setStyleSheet("color: #78909c; font-size: 12px;")
        h.addWidget(sub, 1)
        root.addLayout(h)

    def _build_body(self, root: QVBoxLayout) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_pane())
        splitter.addWidget(self._build_right_pane())
        splitter.setSizes([320, 1100])
        root.addWidget(splitter, 1)

    def _build_left_pane(self) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: rgba(128,128,128,0.04); "
            "         border-radius: 8px; "
            "         border: 1px solid rgba(128,128,128,0.10); }"
        )
        col = QVBoxLayout(wrap)
        col.setContentsMargins(10, 12, 10, 10)
        col.setSpacing(6)

        cap = QLabel("CONTACTS")
        cap.setStyleSheet(
            "color: #607d8b; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.06em;"
        )
        col.addWidget(cap)

        self._search = QLineEdit()
        self._search.setPlaceholderText("filter by name, phone, JID…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedHeight(28)
        self._search.textChanged.connect(self._refresh_contact_list)
        col.addWidget(self._search)

        self._contact_list = QListWidget()
        self._contact_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; "
            "              outline: none; font-size: 12px; }"
            "QListWidget::item { padding: 6px 8px; border-radius: 3px; }"
            "QListWidget::item:hover { background: rgba(0,137,123,0.08); }"
        )
        self._contact_list.itemChanged.connect(self._on_pick_changed)
        col.addWidget(self._contact_list, 1)

        # Pick summary + clear
        bot = QHBoxLayout()
        bot.setSpacing(6)
        clear_btn = QPushButton("Clear picks")
        clear_btn.clicked.connect(self._clear_picks)
        bot.addWidget(clear_btn)
        self._picks_label = QLabel("0 picked")
        self._picks_label.setStyleSheet(
            "color: #607d8b; font-size: 11px; font-weight: 600;"
        )
        bot.addWidget(self._picks_label)
        bot.addStretch()
        col.addLayout(bot)
        return wrap

    def _build_right_pane(self) -> QWidget:
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # Empty state placeholder
        self._empty = QLabel(
            "← Pick 2 or more contacts on the left to start the analysis."
        )
        self._empty.setAlignment(Qt.AlignCenter)
        self._empty.setStyleSheet(
            "color: #9aa3ac; font-size: 14px; font-style: italic; padding: 80px 0;"
        )
        col.addWidget(self._empty, 1)

        # Results scroll area (lazy-built when picks ≥ 2)
        self._results_scroll = QScrollArea()
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QFrame.NoFrame)
        self._results_scroll.setVisible(False)
        self._results_inner = QWidget()
        self._results_layout = QVBoxLayout(self._results_inner)
        self._results_layout.setContentsMargins(0, 0, 0, 0)
        self._results_layout.setSpacing(14)
        self._results_scroll.setWidget(self._results_inner)
        col.addWidget(self._results_scroll, 1)
        return wrap

    # ------------------------------------------------------------------ #
    # Initial load
    # ------------------------------------------------------------------ #

    def _initial_load(self) -> None:
        try:
            db = Database.get()
        except Exception:
            return
        # Owner identity for the synthetic "You" pick
        try:
            kv = {
                r["key"]: r["value"]
                for r in db.fetchall(
                    "SELECT key, value FROM case_metadata "
                    "WHERE key IN ('device_owner_name','device_owner_phone',"
                    "              'device_owner_jid')"
                )
            }
        except Exception:
            kv = {}
        self._owner_name = kv.get("device_owner_name") or ""
        phone = (kv.get("device_owner_phone") or "").replace("@s.whatsapp.net", "")
        self._owner_jid = kv.get("device_owner_jid") or (
            f"{phone}@s.whatsapp.net" if phone else ""
        )

        # Pull the case roster — top contacts by activity, capped so the
        # left list stays scannable.  Search box can drill down further.
        rows = db.fetchall(
            "SELECT c.id, "
            "       COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
            "                REPLACE(c.phone_jid,'@s.whatsapp.net','')) AS name, "
            "       c.phone_number, c.phone_jid, c.lid_jid, "
            "       c.message_count "
            "FROM contact c "
            "WHERE c.message_count > 0 "
            "ORDER BY c.message_count DESC "
            "LIMIT 2000"
        )
        self._all_contacts = [dict(r) for r in rows]
        self._refresh_contact_list()

    def _refresh_contact_list(self) -> None:
        q = (self._search.text() or "").strip().lower()
        self._contact_list.blockSignals(True)
        self._contact_list.clear()

        # Owner row first
        owner_lbl = (
            f"{self._owner_name} (you)" if self._owner_name else "You (Device Owner)"
        )
        owner_item = QListWidgetItem(f"⭐  {owner_lbl}")
        owner_item.setData(Qt.UserRole, OWNER_ID)
        owner_item.setFlags(owner_item.flags() | Qt.ItemIsUserCheckable)
        owner_item.setCheckState(
            Qt.Checked if OWNER_ID in self._picked_ids else Qt.Unchecked
        )
        owner_item.setForeground(QBrush(QColor("#e65100")))
        if not q or q in owner_lbl.lower():
            self._contact_list.addItem(owner_item)

        for c in self._all_contacts:
            label = (
                f"{c['name']}   ·   {c.get('phone_number') or '—'}   "
                f"({c.get('message_count') or 0:,} msgs)"
            )
            if q and q not in label.lower() \
                    and q not in (c.get('phone_jid') or '').lower():
                continue
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, int(c["id"]))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(
                Qt.Checked if c["id"] in self._picked_ids else Qt.Unchecked
            )
            self._contact_list.addItem(it)

        self._contact_list.blockSignals(False)

    def _on_pick_changed(self, item: QListWidgetItem) -> None:
        cid = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self._picked_ids.add(cid)
        else:
            self._picked_ids.discard(cid)
        self._picks_label.setText(f"{len(self._picked_ids)} picked")
        self._compute_and_render()

    def _clear_picks(self) -> None:
        self._picked_ids.clear()
        self._refresh_contact_list()
        self._picks_label.setText("0 picked")
        self._compute_and_render()

    # ------------------------------------------------------------------ #
    # Compute + render
    # ------------------------------------------------------------------ #

    def _compute_and_render(self) -> None:
        # Clear existing result widgets
        while self._results_layout.count():
            it = self._results_layout.takeAt(0)
            w = it.widget() if it else None
            if w:
                w.deleteLater()

        if len(self._picked_ids) < 2:
            self._empty.setVisible(True)
            self._results_scroll.setVisible(False)
            self._empty.setText(
                f"← Pick {2 - len(self._picked_ids)} more "
                f"contact{'s' if len(self._picked_ids) == 0 else ''} "
                f"to start the analysis."
            )
            return

        self._empty.setVisible(False)
        self._results_scroll.setVisible(True)

        try:
            db = Database.get()
        except Exception:
            return

        # Build the picked-set roster summary header
        self._results_layout.addWidget(self._build_picked_summary(db))

        # Run the analyses in order of usefulness
        self._results_layout.addWidget(self._build_shared_groups(db))
        self._results_layout.addWidget(self._build_calls_between(db))
        self._results_layout.addWidget(self._build_shared_files(db))
        self._results_layout.addWidget(self._build_cross_mentions(db))
        self._results_layout.addWidget(self._build_one_to_one_chats(db))
        self._results_layout.addStretch()

    # ---- Sections --------------------------------------------------- #

    def _picked_for_sql(self) -> tuple[list[int], bool]:
        """Return ``(real_contact_ids, owner_picked)``.  Owner is
        special — its messages have NULL sender_id, so most queries
        need a separate ``OR m.from_me = 1`` branch when it's in the
        picked set.
        """
        owner_picked = OWNER_ID in self._picked_ids
        real = sorted(c for c in self._picked_ids if c != OWNER_ID)
        return real, owner_picked

    def _build_picked_summary(self, db) -> QWidget:
        real, owner_picked = self._picked_for_sql()
        names: list[str] = []
        if owner_picked:
            names.append(
                f"<b style='color:#e65100;'>"
                f"{self._owner_name or 'You'} (you)</b>"
            )
        if real:
            placeholders = ",".join("?" * len(real))
            for r in db.fetchall(
                f"SELECT id, COALESCE(resolved_name, wa_name, phone_number, "
                f"       REPLACE(phone_jid,'@s.whatsapp.net','')) AS n "
                f"FROM contact WHERE id IN ({placeholders})",
                tuple(real)
            ):
                names.append(f"<b>{r['n']}</b>")
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: rgba(0,137,123,0.06); border-radius: 8px; "
            "         border: 1px solid rgba(0,137,123,0.18); padding: 8px; }"
        )
        wl = QVBoxLayout(wrap); wl.setContentsMargins(12, 10, 12, 10)
        wl.addWidget(QLabel(
            f"Comparing <b>{len(self._picked_ids)} contact"
            f"{'s' if len(self._picked_ids) != 1 else ''}</b>:  "
            + "  ·  ".join(names)
        ))
        wl.itemAt(0).widget().setTextFormat(Qt.RichText)
        wl.itemAt(0).widget().setWordWrap(True)
        return wrap

    # === Shared Groups === #

    def _build_shared_groups(self, db) -> QWidget:
        real, owner_picked = self._picked_for_sql()
        # For each contact we need the set of conversation_ids they
        # are/were a participant of (current OR past).  Then intersect.
        per_member: list[set[int]] = []
        for cid in real:
            member_ids = {
                r[0] for r in db.fetchall(
                    "SELECT conversation_id FROM group_member WHERE contact_id = ?",
                    (cid,)
                )
            }
            past_ids = {
                r[0] for r in db.fetchall(
                    "SELECT conversation_id FROM group_past_participant WHERE contact_id = ?",
                    (cid,)
                )
            }
            per_member.append(member_ids | past_ids)

        if owner_picked:
            # The owner is implicit in WhatsApp's group_member table —
            # they don't appear there.  Use chat.participation_status
            # (>0 means owner is a member) via the conversation table.
            owner_convs = {
                r[0] for r in db.fetchall(
                    "SELECT id FROM conversation "
                    "WHERE chat_type IN ('group','community') "
                    "  AND COALESCE(participation_status, 0) > 0"
                )
            }
            per_member.append(owner_convs)

        common = set.intersection(*per_member) if per_member else set()
        # Pull conversation rows for the common set
        rows = []
        if common:
            placeholders = ",".join("?" * len(common))
            rows = db.fetchall(
                f"SELECT id, display_name, chat_type, jid_raw_string, "
                f"       message_count, last_message_ts "
                f"FROM conversation WHERE id IN ({placeholders}) "
                f"ORDER BY last_message_ts DESC",
                tuple(common)
            )

        return self._make_table_section(
            title=f"Shared Groups ({len(rows)})",
            empty_msg="None of the picked contacts share a group / community.",
            cols=["Group", "Type", "JID", "Messages", "Last activity", ""],
            row_widths=[260, 80, 220, 80, 130, 70],
            rows=[(
                r["display_name"] or f"#{r['id']}",
                r["chat_type"] or "group",
                r["jid_raw_string"] or "",
                f"{r['message_count'] or 0:,}",
                _ts_short(r["last_message_ts"] or 0),
                ("Go", int(r["id"]), 0),
            ) for r in rows],
        )

    # === Calls between them === #

    def _build_calls_between(self, db) -> QWidget:
        # Strategy:
        #   * 1:1 calls — only matter when EXACTLY 2 people are picked
        #     (the other being you OR another contact).  Pull
        #     call_record rows where contact_id is the other party
        #     AND from_me matches the owner-pick state.
        #   * Multi-party / group calls — pull any call where the
        #     call_participant table contains EVERY picked contact.
        real, owner_picked = self._picked_for_sql()
        rows: list = []
        if len(self._picked_ids) == 2 and owner_picked and len(real) == 1:
            # Pure 1:1 between owner and one contact
            other = real[0]
            rows = db.fetchall(
                "SELECT cr.id, cr.timestamp, cr.is_video, cr.duration_sec, "
                "       cr.from_me, cr.result_label, cr.call_category, "
                "       cr.conversation_id, cr.group_conversation_id, "
                "       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS who, "
                "       c.phone_jid AS jid "
                "FROM call_record cr "
                "LEFT JOIN contact c ON c.id = cr.contact_id "
                "WHERE cr.contact_id = ? "
                "ORDER BY cr.timestamp DESC LIMIT 200",
                (other,)
            )
        else:
            # Multi-party: every call_participant row must contain
            # all real contacts (owner is implicit in group calls).
            if not real:
                rows = []
            else:
                # Find call_ids where ALL picked real contacts appear
                placeholders = ",".join("?" * len(real))
                count_needed = len(real)
                rows = db.fetchall(
                    f"SELECT cr.id, cr.timestamp, cr.is_video, cr.duration_sec, "
                    f"       cr.from_me, cr.result_label, cr.call_category, "
                    f"       cr.conversation_id, cr.group_conversation_id, "
                    f"       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS who, "
                    f"       c.phone_jid AS jid "
                    f"FROM call_record cr "
                    f"LEFT JOIN contact c ON c.id = cr.contact_id "
                    f"WHERE cr.id IN ("
                    f"  SELECT call_id FROM call_participant "
                    f"  WHERE contact_id IN ({placeholders}) "
                    f"  GROUP BY call_id HAVING COUNT(DISTINCT contact_id) = ?"
                    f") "
                    f"ORDER BY cr.timestamp DESC LIMIT 200",
                    tuple(real) + (count_needed,)
                )

        out_rows = []
        for r in rows:
            ct = "Video" if r["is_video"] else "Voice"
            cat = r["call_category"] or "personal"
            dur = r["duration_sec"] or 0
            dur_str = (f"{dur // 60}m {dur % 60}s" if dur >= 60
                       else (f"{dur}s" if dur else "—"))
            direction = "Outgoing" if r["from_me"] else "Incoming"
            target_conv = r["conversation_id"] or r["group_conversation_id"] or 0
            out_rows.append((
                _ts_short(r["timestamp"] or 0),
                f"{ct} · {cat}",
                direction,
                r["result_label"] or "—",
                dur_str,
                r["who"] or "—",
                ("Go", int(target_conv), 0) if target_conv else ("", 0, 0),
            ))

        return self._make_table_section(
            title=f"Calls between them ({len(out_rows)})",
            empty_msg="No calls recorded between these contacts.",
            cols=["When", "Type", "Direction", "Result", "Duration", "Counterparty", ""],
            row_widths=[140, 130, 90, 90, 80, 180, 70],
            rows=out_rows,
        )

    # === Shared files === #

    def _build_shared_files(self, db) -> QWidget:
        # Find SHA-256 hashes that EVERY picked contact has shared at
        # least once.  Owner-pick maps to from_me=1 messages.
        real, owner_picked = self._picked_for_sql()

        per_member: list[set[str]] = []
        for cid in real:
            hs = {
                r[0] for r in db.fetchall(
                    "SELECT DISTINCT me.file_hash "
                    "FROM media me JOIN message m ON m.id = me.message_id "
                    "WHERE m.sender_id = ? AND me.file_hash IS NOT NULL "
                    "  AND me.file_hash != ''",
                    (cid,)
                )
            }
            per_member.append(hs)
        if owner_picked:
            hs = {
                r[0] for r in db.fetchall(
                    "SELECT DISTINCT me.file_hash "
                    "FROM media me JOIN message m ON m.id = me.message_id "
                    "WHERE m.from_me = 1 AND me.file_hash IS NOT NULL "
                    "  AND me.file_hash != ''"
                )
            }
            per_member.append(hs)

        common_hashes = set.intersection(*per_member) if per_member else set()
        rows = []
        if common_hashes:
            placeholders = ",".join("?" * len(common_hashes))
            rows = db.fetchall(
                f"SELECT me.file_hash, "
                f"       MIN(me.media_name) AS name, "
                f"       MIN(me.mime_type) AS mime, "
                f"       MIN(me.file_size) AS sz, "
                f"       COUNT(*) AS n "
                f"FROM media me "
                f"WHERE me.file_hash IN ({placeholders}) "
                f"GROUP BY me.file_hash ORDER BY n DESC LIMIT 200",
                tuple(common_hashes)
            )

        return self._make_table_section(
            title=f"Files shared in common ({len(rows)})",
            empty_msg="No file (by SHA-256) is shared by all picked contacts.",
            cols=["Filename", "MIME", "Size", "SHA-256", "Total shares", ""],
            row_widths=[260, 130, 90, 240, 110, 0],
            rows=[(
                (r["name"] or "(no filename)")[:80],
                (r["mime"] or "—")[:30],
                _fmt_bytes(r["sz"] or 0),
                (r["file_hash"][:32] + "…" if r["file_hash"] and len(r["file_hash"]) > 32
                    else (r["file_hash"] or "")),
                f"{r['n']:,}",
                ("", 0, 0),
            ) for r in rows],
        )

    # === Cross mentions === #

    def _build_cross_mentions(self, db) -> QWidget:
        real, owner_picked = self._picked_for_sql()
        rows = []
        # Find mentions where (sender ∈ picked) AND (mentioned ∈ picked
        # AND ≠ sender).  Includes owner as either side.
        if len(self._picked_ids) >= 2:
            real_set = set(real)
            picks_for_sender = list(real)
            picks_for_target = list(real)
            sender_filter = ""
            target_filter = ""
            params: list = []
            if real:
                ph = ",".join("?" * len(real))
                sender_filter = f"m.sender_id IN ({ph})"
                params.extend(real)
                target_filter = f"mn.mentioned_id IN ({ph})"
                params.extend(real)
            if owner_picked:
                # When owner is among picks, sender or mentioned can be
                # the owner.  Owner's mentioned_id maps to NULL contact
                # row in mention table — but owner can still be MENTIONED
                # by JID match.  Skip the mentioned-owner case for v1
                # (rare, complex JID mapping); cover sender-owner.
                sender_filter = (
                    f"({sender_filter} OR m.from_me = 1)" if sender_filter
                    else "m.from_me = 1"
                )
            # Need at least one filter
            if sender_filter and target_filter:
                where = f"{sender_filter} AND {target_filter}"
            elif sender_filter:
                where = sender_filter
            elif target_filter:
                where = target_filter
            else:
                where = "1=0"
            rows = db.fetchall(
                f"SELECT m.timestamp, m.id AS msg_id, m.conversation_id, "
                f"       m.from_me, "
                f"       COALESCE(sc.resolved_name, sc.wa_name, sc.phone_number) AS sender_name, "
                f"       COALESCE(mc.resolved_name, mc.wa_name, mc.phone_number) AS mentioned_name, "
                f"       cv.display_name AS conv_name "
                f"FROM mention mn "
                f"JOIN message m ON m.id = mn.message_id "
                f"LEFT JOIN contact sc ON sc.id = m.sender_id "
                f"LEFT JOIN contact mc ON mc.id = mn.mentioned_id "
                f"LEFT JOIN conversation cv ON cv.id = m.conversation_id "
                f"WHERE {where} "
                f"ORDER BY m.timestamp DESC LIMIT 200",
                tuple(params)
            )
        owner_lbl = (self._owner_name or "You") + " (you)"
        out_rows = []
        for r in rows:
            sender = owner_lbl if r["from_me"] else (r["sender_name"] or "—")
            target = r["mentioned_name"] or "—"
            out_rows.append((
                _ts_short(r["timestamp"] or 0),
                sender,
                f"@ {target}",
                r["conv_name"] or "—",
                ("Go", int(r["conversation_id"] or 0), int(r["msg_id"] or 0)),
            ))
        return self._make_table_section(
            title=f"Cross @-mentions ({len(out_rows)})",
            empty_msg="No @-mentions between the picked contacts.",
            cols=["When", "Sender", "Mentioned", "Conversation", ""],
            row_widths=[140, 180, 180, 250, 70],
            rows=out_rows,
        )

    # === 1:1 chats they ALL appear in (broader than groups) === #

    def _build_one_to_one_chats(self, db) -> QWidget:
        # Conversations where every picked contact has sent at least
        # one message (or, for owner, has from_me=1 messages).
        real, owner_picked = self._picked_for_sql()
        per_member: list[set[int]] = []
        for cid in real:
            convs = {
                r[0] for r in db.fetchall(
                    "SELECT DISTINCT conversation_id FROM message "
                    "WHERE sender_id = ? AND message_type != 7",
                    (cid,)
                )
            }
            per_member.append(convs)
        if owner_picked:
            owner_convs = {
                r[0] for r in db.fetchall(
                    "SELECT DISTINCT conversation_id FROM message "
                    "WHERE from_me = 1 AND message_type != 7"
                )
            }
            per_member.append(owner_convs)
        common = set.intersection(*per_member) if per_member else set()
        rows = []
        if common:
            placeholders = ",".join("?" * len(common))
            rows = db.fetchall(
                f"SELECT id, display_name, chat_type, jid_raw_string, "
                f"       message_count, last_message_ts "
                f"FROM conversation WHERE id IN ({placeholders}) "
                f"ORDER BY last_message_ts DESC",
                tuple(common)
            )
        return self._make_table_section(
            title=f"All conversations they appear in ({len(rows)})",
            empty_msg="No conversations have messages from every picked contact.",
            cols=["Conversation", "Type", "Messages", "Last activity", ""],
            row_widths=[300, 100, 100, 140, 70],
            rows=[(
                r["display_name"] or f"#{r['id']}",
                r["chat_type"] or "personal",
                f"{r['message_count'] or 0:,}",
                _ts_short(r["last_message_ts"] or 0),
                ("Go", int(r["id"]), 0),
            ) for r in rows],
        )

    # ------------------------------------------------------------------ #
    # Generic table section builder
    # ------------------------------------------------------------------ #

    def _make_table_section(self, *, title: str, empty_msg: str,
                            cols: list[str], row_widths: list[int],
                            rows: list[tuple]) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: white; border-radius: 8px; "
            "         border: 1px solid rgba(128,128,128,0.16); }"
        )
        wl = QVBoxLayout(wrap); wl.setContentsMargins(14, 10, 14, 10); wl.setSpacing(6)
        h = QLabel(title)
        h.setStyleSheet(
            "color: #00695c; font-size: 14px; font-weight: 700; "
            "padding: 4px 0; border-bottom: 2px solid #00897b;"
        )
        wl.addWidget(h)

        if not rows:
            empty = QLabel(empty_msg)
            empty.setStyleSheet("color: #9aa3ac; font-size: 12px; "
                                "font-style: italic; padding: 8px 0;")
            wl.addWidget(empty)
            return wrap

        tbl = QTableWidget()
        tbl.setColumnCount(len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setRowCount(len(rows))
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.setAlternatingRowColors(True)
        tbl.horizontalHeader().setStretchLastSection(False)
        for i, w in enumerate(row_widths):
            if w > 0:
                tbl.horizontalHeader().setSectionResizeMode(i, QHeaderView.Interactive)
                tbl.setColumnWidth(i, w)
            else:
                tbl.horizontalHeader().setSectionResizeMode(i, QHeaderView.Stretch)

        for r_idx, row in enumerate(rows):
            for c_idx, cell in enumerate(row):
                if isinstance(cell, tuple) and len(cell) == 3 and cell[0] == "Go":
                    btn = QPushButton("Go →")
                    btn.setCursor(Qt.PointingHandCursor)
                    btn.setStyleSheet(
                        "QPushButton { background:#00897b; color:white; border:none; "
                        "             border-radius:4px; font-size:10px; "
                        "             font-weight:600; padding: 4px 10px; }"
                        "QPushButton:hover { background:#00796b; }"
                    )
                    cid, mid = int(cell[1]), int(cell[2])
                    btn.clicked.connect(
                        lambda _=False, c=cid, m=mid:
                            self.conversation_selected.emit(c, m)
                    )
                    tbl.setCellWidget(r_idx, c_idx, btn)
                elif isinstance(cell, tuple) and len(cell) == 3 and cell[0] == "":
                    tbl.setItem(r_idx, c_idx, QTableWidgetItem(""))
                else:
                    tbl.setItem(r_idx, c_idx, QTableWidgetItem(str(cell)))

        # Cap visible row height — virtual scroll keeps memory cheap
        tbl.verticalHeader().setDefaultSectionSize(28)
        cap_rows = min(len(rows), 12)
        tbl.setMinimumHeight(cap_rows * 28 + 36)
        tbl.setMaximumHeight(cap_rows * 28 + 36)
        wl.addWidget(tbl)
        return wrap

    # Compatibility hook for app.reload() pattern used by other pages
    def reload(self) -> None:
        self._initial_load()


# ---------------------------------------------------------------------- #
# Local helpers
# ---------------------------------------------------------------------- #

def _ts_short(ms: int | None) -> str:
    if not ms:
        return "—"
    try:
        from datetime import datetime
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "—"


def _fmt_bytes(n: int | None) -> str:
    if not n:
        return "—"
    n = int(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"
