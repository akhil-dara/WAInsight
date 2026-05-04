"""
Tagged Messages page -- investigator-flagged messages across all conversations.
Master-detail layout: table on top, message detail preview below.
Supports search, navigation to source chat, note editing, and CSV/HTML export.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableView,
    QTextEdit, QVBoxLayout, QWidget,
)

from app.config import format_timestamp
from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

_JOINS = """
    FROM message_tag mt
    INNER JOIN message m ON m.id = mt.message_id
    LEFT JOIN media md ON md.message_id = m.id
    LEFT JOIN contact c ON c.id = m.sender_id
    LEFT JOIN conversation conv ON conv.id = m.conversation_id
"""


class TaggedMessagesModel(BaseLazyTableModel):
    _columns = [
        ("tag_label", "Tag"),
        ("text_content", "Message"),
        ("sender_name", "Sender"),
        ("conv_name", "Conversation"),
        ("timestamp", "Timestamp"),
        ("note", "Note"),
    ]

    # Sender resolution is owner-aware: when ``from_me = 1`` the
    # WhatsApp message row carries a NULL sender_id (the owner is
    # implicit), so the LEFT JOIN against contact returns NULLs and
    # without intervention the column reads "Unknown".  We pull the
    # owner identity from ``case_metadata`` and surface it here via
    # CASE so the analyst sees their real name + phone instead.
    _base_sql = f"""
        SELECT mt.tag_label,
               COALESCE(md.media_caption, m.text_content, '[' || m.type_label || ']') AS text_content,
               CASE
                   WHEN m.from_me = 1 THEN
                       COALESCE(
                           (SELECT value FROM case_metadata
                              WHERE key = 'device_owner_name'),
                           'You (Device Owner)') || ' (you)'
                   ELSE
                       COALESCE(c.resolved_name, c.wa_name, c.phone_number,
                                REPLACE(c.phone_jid, '@s.whatsapp.net', ''),
                                'Unknown')
               END AS sender_name,
               COALESCE(conv.display_name, conv.jid_raw_string) AS conv_name,
               m.timestamp, mt.note,
               mt.id, mt.message_id, m.conversation_id,
               COALESCE(conv.display_name, conv.jid_raw_string, '#' || m.conversation_id) AS nav_conv_name,
               mt.tagged_at, mt.tagged_by,
               m.from_me, m.type_label, m.is_revoked,
               CASE
                   WHEN m.from_me = 1 THEN
                       COALESCE(
                           (SELECT value FROM case_metadata
                              WHERE key = 'device_owner_phone'),
                           '')
                   ELSE
                       COALESCE(c.phone_number,
                                REPLACE(c.phone_jid, '@s.whatsapp.net', ''),
                                '')
               END AS phone_number
    {_JOINS}"""

    _count_sql = f"SELECT COUNT(*) {_JOINS}"
    _default_order = "mt.tagged_at DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 0:
                return f"\u2691 {raw}" if raw else "\u2691 flagged"
            if col == 4 and raw:
                return format_timestamp(raw, "full")
            if col == 1 and raw:
                s = str(raw).replace("\n", " ")
                return s[:120] + "\u2026" if len(s) > 120 else s
            if col == 5 and raw:
                s = str(raw)
                return s[:80] + "\u2026" if len(s) > 80 else s
            return str(raw) if raw is not None else ""

        if role == Qt.ForegroundRole:
            tm = ThemeManager.get()
            if col == 0:
                return QColor("#ef5350")
            if col == 5 and row_data[5]:
                return QColor("#43a047") if tm.is_light else QColor("#66bb6a")
            if col == 2:
                return QColor("#00695c") if tm.is_light else QColor("#4dd0e1")

        if role == Qt.TextAlignmentRole:
            if col == 4:
                return Qt.AlignRight | Qt.AlignVCenter

        if role == Qt.ToolTipRole:
            if col == 1:
                return str(row_data[col]) if row_data[col] else ""
            if col == 5:
                return str(row_data[col]) if row_data[col] else ""

        return None

    @property
    def total_rows(self) -> int:
        return self._total_rows


class TaggedMessagesPage(QWidget):
    """View and manage investigator-tagged messages across all conversations."""

    conversation_selected = Signal(int, str)
    # Carries ``(conv_id, msg_id)`` so the chat viewer can jump
    # directly to the tagged message instead of opening the chat
    # at the bottom and then re-centring.
    go_to_message = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._db = Database.get()
        is_light = self._tm.is_light

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        # ── Header ──
        header = QHBoxLayout()
        title = QLabel("\u2691  Tagged Messages")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet(self._tm.header_label_style())
        header.addWidget(self._count_label)
        header.addStretch()

        _btn_h = 30
        _btn_css = self._tm.filter_btn_style()
        for label, slot in [
            ("\u21E9 CSV", self._export_csv),
            ("\u21E9 HTML", self._export_html),
            ("\U0001F4E6 Bundle\u2026", self._export_bundle),
            ("\u2718 Clear All", self._clear_all_tags),
        ]:
            btn = QPushButton(label)
            btn.setFixedHeight(_btn_h)
            btn.setStyleSheet(_btn_css)
            btn.clicked.connect(slot)
            header.addWidget(btn)
        layout.addLayout(header)

        # ── Search toolbar ──
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("\u2315  Search tagged messages...")
        self._search.setFixedHeight(36)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)
        refresh_btn = QPushButton("\u21BB Refresh")
        refresh_btn.setFixedHeight(36)
        refresh_btn.setStyleSheet(_btn_css)
        refresh_btn.clicked.connect(self._apply)
        toolbar.addWidget(refresh_btn)
        layout.addLayout(toolbar)

        # ── Ensure table exists ──
        self._ensure_tag_table()

        # ── Splitter: table + detail panel ──
        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        # ── Table ──
        table_frame = QWidget()
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(4)

        hint = QLabel(
            "Click row to preview  \u2502  "
            "Double-click to open chat  \u2502  "
            "Right-click for options"
        )
        hint.setStyleSheet(self._tm.hint_label_style())
        table_layout.addWidget(hint)

        self._model = TaggedMessagesModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(28)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_context_menu)
        self._table.doubleClicked.connect(self._on_double_click)
        self._table.clicked.connect(self._on_row_clicked)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, w in [(0, 80), (2, 140), (3, 170), (4, 185), (5, 180)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        for hcol in range(6, 16):
            self._table.setColumnHidden(hcol, True)

        table_layout.addWidget(self._table, 1)
        splitter.addWidget(table_frame)

        # ── Detail panel ──
        self._detail_frame = QFrame()
        if is_light:
            self._detail_frame.setStyleSheet("""
                QFrame { background: #ffffff; border: 1px solid #e0e0e0;
                         border-radius: 8px; }
            """)
        else:
            self._detail_frame.setStyleSheet("""
                QFrame { background: rgba(128,128,128,0.06);
                         border: 1px solid rgba(128,128,128,0.12);
                         border-radius: 8px; }
            """)
        detail_layout = QVBoxLayout(self._detail_frame)
        detail_layout.setContentsMargins(16, 12, 16, 12)
        detail_layout.setSpacing(8)

        # Detail: header row with tag + go-to-chat button
        detail_header = QHBoxLayout()
        self._detail_tag = QLabel("\u2691")
        self._detail_tag.setStyleSheet(
            "color: #ef5350; font-size: 14px; font-weight: bold;"
        )
        detail_header.addWidget(self._detail_tag)

        self._detail_conv = QLabel("")
        self._detail_conv.setStyleSheet(
            f"color: {'#00695c' if is_light else '#4dd0e1'}; font-size: 13px; "
            f"font-weight: bold;"
        )
        detail_header.addWidget(self._detail_conv)
        detail_header.addStretch()

        self._goto_btn = QPushButton("\u2192  Go to Chat")
        self._goto_btn.setFixedHeight(30)
        self._goto_btn.setCursor(Qt.PointingHandCursor)
        self._goto_btn.setStyleSheet(
            "QPushButton { background: #00695c; color: white; border: none; "
            "border-radius: 4px; padding: 0 16px; font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background: #00897b; }"
        )
        self._goto_btn.clicked.connect(self._goto_chat_from_detail)
        self._goto_btn.setVisible(False)
        detail_header.addWidget(self._goto_btn)

        self._edit_note_btn = QPushButton("\u270E Note")
        self._edit_note_btn.setFixedHeight(30)
        self._edit_note_btn.setCursor(Qt.PointingHandCursor)
        self._edit_note_btn.setStyleSheet(_btn_css)
        self._edit_note_btn.clicked.connect(self._edit_note_from_detail)
        self._edit_note_btn.setVisible(False)
        detail_header.addWidget(self._edit_note_btn)

        self._remove_tag_btn = QPushButton("\u2718 Remove")
        self._remove_tag_btn.setFixedHeight(30)
        self._remove_tag_btn.setCursor(Qt.PointingHandCursor)
        self._remove_tag_btn.setStyleSheet(_btn_css)
        self._remove_tag_btn.clicked.connect(self._remove_tag_from_detail)
        self._remove_tag_btn.setVisible(False)
        detail_header.addWidget(self._remove_tag_btn)

        detail_layout.addLayout(detail_header)

        # Detail: metadata row
        self._detail_meta = QLabel("")
        self._detail_meta.setWordWrap(True)
        self._detail_meta.setStyleSheet(
            f"color: {'#667781' if is_light else '#90a4ae'}; font-size: 11px;"
        )
        self._detail_meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        detail_layout.addWidget(self._detail_meta)

        # Detail: separator
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            f"background: {'#e8eaed' if is_light else 'rgba(128,128,128,0.15)'};"
        )
        detail_layout.addWidget(sep)

        # Detail: media thumbnail (image/video) — click to open fullscreen
        self._detail_thumb_wrap = QHBoxLayout()
        self._detail_thumb_wrap.setContentsMargins(0, 0, 0, 0)
        self._detail_thumb_wrap.setSpacing(12)
        self._detail_thumb = QLabel()
        self._detail_thumb.setFixedSize(180, 130)
        self._detail_thumb.setAlignment(Qt.AlignCenter)
        self._detail_thumb.setCursor(Qt.PointingHandCursor)
        self._detail_thumb.setStyleSheet(
            f"background: {'#f4f5f5' if is_light else '#111b21'};"
            f" border: 1px solid {'#e0e3e7' if is_light else 'rgba(128,128,128,0.15)'};"
            f" border-radius: 6px; color: {'#90a4ae' if is_light else '#607d8b'};"
            f" font-size: 10px;"
        )
        self._detail_thumb.setText("")
        self._detail_thumb.setVisible(False)
        self._detail_thumb.mousePressEvent = lambda _=None: self._open_media_lightbox()
        self._detail_thumb_wrap.addWidget(self._detail_thumb)

        # Detail: message text (scrollable, selectable)
        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setMinimumHeight(60)
        self._detail_text.setStyleSheet(
            f"QTextEdit {{ background: transparent; border: none; "
            f"font-size: 12px; color: {'#1b1b1b' if is_light else '#e0e0e0'}; }}"
        )
        self._detail_thumb_wrap.addWidget(self._detail_text, 1)
        detail_layout.addLayout(self._detail_thumb_wrap, 1)

        # Detail: note section
        note_row = QHBoxLayout()
        note_icon = QLabel("\u270E")
        note_icon.setStyleSheet(
            f"color: {'#43a047' if is_light else '#66bb6a'}; font-size: 12px;"
        )
        note_row.addWidget(note_icon)
        self._detail_note = QLabel("")
        self._detail_note.setWordWrap(True)
        self._detail_note.setStyleSheet(
            f"color: {'#43a047' if is_light else '#66bb6a'}; font-size: 11px; "
            f"font-style: italic;"
        )
        self._detail_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        note_row.addWidget(self._detail_note, 1)
        detail_layout.addLayout(note_row)

        # Initial empty state
        self._show_empty_detail()

        splitter.addWidget(self._detail_frame)
        splitter.setStretchFactor(0, 3)  # table gets 3/4
        splitter.setStretchFactor(1, 1)  # detail gets 1/4

        layout.addWidget(splitter, 1)

        # ── State ──
        self._selected_row_data = None
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._search.returnPressed.connect(self._apply)
        QTimer.singleShot(50, self._apply)

    # ── Table management ──

    def _ensure_tag_table(self):
        """Create message_tag table if it doesn't exist."""
        # Check if table already exists on the read connection first
        try:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_tag'"
            ).fetchone()
            if exists:
                return  # Table already there, nothing to do
        except Exception:
            pass

        # Create via write connection, then checkpoint WAL and reconnect read
        try:
            self._db.execute_write("""
                CREATE TABLE IF NOT EXISTS message_tag (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id  INTEGER NOT NULL UNIQUE REFERENCES message(id),
                    tag_label   TEXT DEFAULT 'flagged',
                    note        TEXT DEFAULT '',
                    tagged_at   TEXT DEFAULT (datetime('now')),
                    tagged_by   TEXT DEFAULT 'investigator'
                )
            """)
            self._db.execute_write("""
                CREATE INDEX IF NOT EXISTS idx_message_tag_mid
                ON message_tag(message_id)
            """)
            # Checkpoint WAL so immutable read connection sees the changes
            self._db.checkpoint_and_reconnect()
        except Exception as e:
            print(f"[TaggedMessages] Table creation error: {e}")

    def _apply(self):
        """Reload the table with current search filter."""
        try:
            self._db.scalar("SELECT COUNT(*) FROM message_tag")
        except Exception:
            self._count_label.setText("0 tagged messages")
            return

        parts, params = [], []
        text = self._search.text().strip()
        if text:
            parts.append(
                "(m.text_content LIKE ? OR md.media_caption LIKE ? "
                "OR c.resolved_name LIKE ? OR c.wa_name LIKE ? "
                "OR conv.display_name LIKE ? OR mt.note LIKE ?)"
            )
            params.extend([f"%{text}%"] * 6)

        self._model.load(where=" AND ".join(parts), params=tuple(params))
        n = self._model.total_rows
        self._count_label.setText(f"{n:,} tagged message{'s' if n != 1 else ''}")
        self._show_empty_detail()

    def _get_row_data(self, index: QModelIndex) -> tuple | None:
        if not index.isValid():
            return None
        row = index.row()
        if 0 <= row < len(self._model._data):
            return self._model._data[row]
        return None

    # ── Detail panel ──

    def _show_empty_detail(self):
        """Show placeholder when no row is selected."""
        self._selected_row_data = None
        self._detail_tag.setText("\u2691  Select a tagged message above")
        self._detail_conv.setText("")
        self._detail_meta.setText("")
        self._detail_text.setPlainText("")
        self._detail_note.setText("No message selected")
        if hasattr(self, "_detail_thumb"):
            self._detail_thumb.setVisible(False)
            self._detail_thumb.clear()
        self._current_preview_pixmap = None
        self._current_preview_path = None
        self._goto_btn.setVisible(False)
        self._edit_note_btn.setVisible(False)
        self._remove_tag_btn.setVisible(False)

    def _on_row_clicked(self, index: QModelIndex):
        """Show detail for clicked row."""
        row_data = self._get_row_data(index)
        if not row_data:
            self._show_empty_detail()
            return
        self._selected_row_data = row_data
        is_light = self._tm.is_light

        tag = row_data[0] or "flagged"
        text = row_data[1] or ""
        sender = row_data[2] or "Unknown"
        conv_name = row_data[3] or ""
        ts = row_data[4]
        note = row_data[5] or ""
        tagged_at = row_data[10] or ""
        tagged_by = row_data[11] or "investigator"
        from_me = row_data[12]
        type_label = row_data[13] or ""
        is_revoked = row_data[14]
        phone = row_data[15] or ""

        # Direction arrow
        direction = "\u2192 Sent" if from_me else "\u2190 Received"
        direction_color = "#00695c" if is_light else "#4dd0e1"

        self._detail_tag.setText(f"\u2691 {tag}")
        self._detail_conv.setText(f"\u2502  {conv_name}")

        # Metadata line
        ts_str = format_timestamp(ts, "full") if ts else "N/A"
        phone_str = f"+{phone}" if phone and phone[0].isdigit() else phone
        meta_parts = [
            f"<b>{direction}</b>",
            f"Sender: <b>{sender}</b>",
        ]
        if phone_str:
            meta_parts.append(f"Phone: {phone_str}")
        meta_parts.append(f"Type: {type_label}")
        meta_parts.append(f"Time: <code>{ts_str}</code>")
        meta_parts.append(f"Tagged: {tagged_at} by {tagged_by}")
        if is_revoked:
            meta_parts.append("<span style='color:#ef5350;'>\u2718 Revoked</span>")
        self._detail_meta.setText(
            f"<span style='color:{direction_color};'>"
            + "  \u2502  ".join(meta_parts) + "</span>"
        )

        # Message text
        self._detail_text.setPlainText(str(text) if text else "(no text)")

        # Media thumbnail — load on demand for image / video / sticker / gif types
        self._populate_media_thumb(row_data)

        # Note
        self._detail_note.setText(note if note else "(no note -- right-click or click Edit Note)")

        # Show action buttons
        self._goto_btn.setVisible(True)
        self._edit_note_btn.setVisible(True)
        self._remove_tag_btn.setVisible(True)

    def _populate_media_thumb(self, row_data: tuple):
        """Show a 180x130 preview for image-bearing tagged messages."""
        from PySide6.QtGui import QPixmap
        msg_id = row_data[7]
        type_label = (row_data[13] or "").lower()
        media_types = {"image", "video", "sticker", "gif", "animated_gif",
                        "view_once_image", "view_once_video"}
        if type_label not in media_types:
            self._detail_thumb.setVisible(False)
            self._current_preview_path = None
            return
        try:
            row = self._db.fetchone(
                "SELECT me.resolved_file_path, me.thumbnail_blob "
                "FROM media me WHERE me.message_id = ?",
                (msg_id,),
            )
        except Exception:
            row = None
        if not row:
            self._detail_thumb.setVisible(False)
            self._current_preview_path = None
            return
        fpath, blob = row[0], row[1]
        pm = None
        # Prefer full-resolution disk file for clarity
        if fpath and os.path.isfile(fpath):
            pm = QPixmap(fpath)
        elif blob:
            pm = QPixmap()
            try:
                if isinstance(blob, (bytes, bytearray)):
                    pm.loadFromData(bytes(blob))
                elif isinstance(blob, str):
                    import base64 as _b64
                    pm.loadFromData(_b64.b64decode(blob))
            except Exception:
                pm = None
        if not pm or pm.isNull():
            self._detail_thumb.setText("No preview")
            self._detail_thumb.setVisible(True)
            self._current_preview_path = None
            return
        scaled = pm.scaled(180, 130, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._detail_thumb.setPixmap(scaled)
        self._detail_thumb.setToolTip(f"Click to open full size\n{fpath or '(blob)'}")
        self._detail_thumb.setVisible(True)
        self._current_preview_path = fpath if (fpath and os.path.isfile(fpath)) else None
        # Stash the pixmap for the lightbox click
        self._current_preview_pixmap = pm

    def _open_media_lightbox(self):
        """Click the detail thumb → open the fullscreen image viewer."""
        pm = getattr(self, "_current_preview_pixmap", None)
        if not pm or pm.isNull():
            return
        rd = self._selected_row_data
        info = {}
        if rd:
            info = {
                "file_path": getattr(self, "_current_preview_path", "") or "",
                "conv_name": rd[3] or "",
                "sender_name": rd[2] or "",
                "timestamp": rd[4] or 0,
            }
        try:
            from app.views.widgets.image_lightbox import show_lightbox
            show_lightbox(self, pm, info)
        except Exception as e:
            print(f"[Tagged] lightbox failed: {e}")

    def _on_double_click(self, index: QModelIndex):
        row_data = self._get_row_data(index)
        if not row_data:
            return
        msg_id = row_data[7]
        conv_id = row_data[8]
        conv_name = row_data[9] or f"#{conv_id}"
        if conv_id and msg_id:
            self.go_to_message.emit(int(conv_id), int(msg_id))
        elif conv_id:
            self.conversation_selected.emit(int(conv_id), str(conv_name))

    def _goto_chat_from_detail(self):
        if not self._selected_row_data:
            return
        msg_id = self._selected_row_data[7]
        conv_id = self._selected_row_data[8]
        conv_name = self._selected_row_data[9] or f"#{conv_id}"
        if conv_id and msg_id:
            self.go_to_message.emit(int(conv_id), int(msg_id))
        elif conv_id:
            self.conversation_selected.emit(int(conv_id), str(conv_name))

    def _edit_note_from_detail(self):
        if self._selected_row_data:
            msg_id = self._selected_row_data[7]
            current = self._selected_row_data[5] or ""
            self._edit_note(msg_id, current)

    def _remove_tag_from_detail(self):
        if self._selected_row_data:
            self._remove_tag(self._selected_row_data[7])

    # ── Context menu ──

    def _show_context_menu(self, pos):
        index = self._table.indexAt(pos)
        row_data = self._get_row_data(index)
        if not row_data:
            return

        menu = QMenu(self)
        menu.setStyleSheet(self._tm.context_menu_style())

        conv_id = row_data[8]
        conv_name = row_data[9] or f"#{conv_id}"
        msg_id = row_data[7]

        if conv_id:
            go = menu.addAction("\u2192  Go to Chat (and scroll to this message)")
            if msg_id:
                go.triggered.connect(
                    lambda _=False, c=int(conv_id), m=int(msg_id):
                        self.go_to_message.emit(c, m)
                )
            else:
                go.triggered.connect(
                    lambda _=False, c=int(conv_id), n=str(conv_name):
                        self.conversation_selected.emit(c, n)
                )
            menu.addSeparator()

        edit = menu.addAction("\u270E  Edit Note")
        edit.triggered.connect(lambda: self._edit_note(msg_id, row_data[5] or ""))

        remove = menu.addAction("\u2718  Remove Tag")
        remove.triggered.connect(lambda: self._remove_tag(msg_id))
        menu.addSeparator()

        msg_text = row_data[1] or ""
        if msg_text:
            copy_msg = menu.addAction("Copy Message Text")
            copy_msg.triggered.connect(
                lambda: QApplication.clipboard().setText(str(msg_text))
            )

        phone = row_data[15] or ""
        if phone:
            pstr = f"+{phone}" if phone[0].isdigit() else phone
            copy_ph = menu.addAction(f"Copy Phone ({pstr})")
            copy_ph.triggered.connect(
                lambda: QApplication.clipboard().setText(pstr)
            )

        copy_all = menu.addAction("Copy All Details")
        copy_all.triggered.connect(lambda: self._copy_row_details(row_data))

        menu.exec(QCursor.pos())

    # ── Actions ──

    def _edit_note(self, msg_id: int, current_note: str):
        note, ok = QInputDialog.getText(
            self, "Edit Note",
            "Investigator note for this tagged message:",
            text=current_note,
        )
        if ok:
            try:
                self._db.execute_write(
                    "UPDATE message_tag SET note = ? WHERE message_id = ?",
                    (note, msg_id),
                )
                self._apply()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _remove_tag(self, msg_id: int):
        try:
            self._db.execute_write(
                "DELETE FROM message_tag WHERE message_id = ?", (msg_id,),
            )
            self._apply()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _clear_all_tags(self):
        n = self._model.total_rows
        if n == 0:
            return
        reply = QMessageBox.question(
            self, "Clear All Tags",
            f"Remove all {n} tag{'s' if n != 1 else ''}?\n"
            "This action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            try:
                self._db.execute_write("DELETE FROM message_tag")
                self._apply()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _copy_row_details(self, row_data):
        lines = [
            f"Tag: {row_data[0]}",
            f"Message: {row_data[1]}",
            f"Sender: {row_data[2]}",
            f"Conversation: {row_data[3]}",
            f"Timestamp: {format_timestamp(row_data[4], 'full')}",
            f"Note: {row_data[5]}",
            f"Tagged At: {row_data[10]}",
            f"Tagged By: {row_data[11]}",
            f"Direction: {'Sent' if row_data[12] else 'Received'}",
            f"Type: {row_data[13]}",
            f"Revoked: {row_data[14]}",
            f"Phone: {row_data[15]}",
        ]
        QApplication.clipboard().setText("\n".join(lines))

    # ── Export ──

    @staticmethod
    def _load_bundle_exporter_cls():
        """Load ``ViewerBundleExporter`` from ``backend/`` via importlib
        so the gui/ side doesn't have to import it through a
        ``backend.app.export`` path that collides with the gui's own
        ``app`` package.  Same loader pattern as
        ``export_html_dialog._load_bundle_exporter_cls``.
        """
        import importlib.util
        from pathlib import Path as _P
        here = _P(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "backend" / "app" / "export" / "viewer_bundle_exporter.py"
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location(
                    "wainsight_viewer_bundle_exporter", str(candidate))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.ViewerBundleExporter
        raise FileNotFoundError(
            "Could not locate backend/app/export/viewer_bundle_exporter.py"
        )

    def _export_bundle(self) -> None:
        """Open the Tagged Export dialog and run the bundle exporter
        using its mode + buffer + media settings.

        Three modes are supported (see TaggedExportDialog docstring):
          * full conversations
          * tagged messages only (+ media)
          * tagged messages with ±N day buffer (compaction markers
            inserted between non-adjacent kept messages)
        """
        if self._model.total_rows == 0:
            QMessageBox.information(
                self, "Export", "No tagged messages to export."
            )
            return
        from app.views.dialogs.tagged_export_dialog import TaggedExportDialog

        # Build (conv_id, msg_id, ts) for every tagged message so
        # the dialog can show counts and the runner can build the
        # per-conv whitelist + ±N day window.
        try:
            rows = self._db.fetchall(
                "SELECT m.id AS msg_id, m.conversation_id AS conv_id,"
                "       m.timestamp AS ts "
                "FROM message_tag mt "
                "JOIN message m ON m.id = mt.message_id "
                "ORDER BY m.timestamp"
            )
        except Exception as e:
            QMessageBox.warning(
                self, "Export failed",
                f"Could not enumerate tagged messages:\n{e}",
            )
            return

        tagged_by_conv: dict[int, list[tuple[int, int]]] = {}
        for r in rows:
            tagged_by_conv.setdefault(r["conv_id"], []).append(
                (int(r["msg_id"]), int(r["ts"] or 0))
            )
        conv_ids = list(tagged_by_conv.keys())
        if not conv_ids:
            QMessageBox.information(
                self, "Export", "No tagged messages to export."
            )
            return

        # Default save folder: <case>/exports/
        try:
            case_dir = self._db.path.parent
        except Exception:
            from pathlib import Path
            case_dir = Path.home()
        default_dir = case_dir / "exports"
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            default_dir = case_dir

        dlg = TaggedExportDialog(
            self,
            default_dir=default_dir,
            tagged_count=self._model.total_rows,
            conv_count=len(conv_ids),
        )
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.is_ok:
            return

        # ---- Build the per-conversation message-id whitelist ----
        # Mode A (full): no whitelist; the bundle exporter emits the
        # full conversation as before.
        # Mode B (tagged only): whitelist = exact tagged ids.
        # Mode C (buffer): whitelist = every message in any window
        # ``[tagged_ts - days, tagged_ts + days]`` for that conv.
        msg_id_filter: dict[int, set[int]] | None = None
        if dlg.mode == dlg.MODE_TAGGED_ONLY:
            msg_id_filter = {
                cid: {mid for mid, _ in pairs}
                for cid, pairs in tagged_by_conv.items()
            }
        elif dlg.mode == dlg.MODE_BUFFER:
            buf_ms = int(dlg.buffer_days) * 86_400_000
            msg_id_filter = {}
            for cid, pairs in tagged_by_conv.items():
                # Union of all windows; merge overlapping windows then
                # query the message ids inside each.
                windows: list[tuple[int, int]] = sorted(
                    [(ts - buf_ms, ts + buf_ms) for _, ts in pairs if ts]
                )
                merged: list[list[int]] = []
                for lo, hi in windows:
                    if merged and lo <= merged[-1][1] + 1:
                        if hi > merged[-1][1]:
                            merged[-1][1] = hi
                    else:
                        merged.append([lo, hi])
                ids: set[int] = set()
                for lo, hi in merged:
                    try:
                        sub = self._db.fetchall(
                            "SELECT id FROM message "
                            "WHERE conversation_id = ? "
                            "  AND timestamp BETWEEN ? AND ?",
                            (cid, lo, hi),
                        )
                        ids.update(int(r["id"]) for r in sub)
                    except Exception as e:
                        print(f"[TaggedExport] window query failed cid={cid}: {e}")
                # Always include the tagged ids themselves even if a
                # ts was 0 / NULL (no window could be computed).
                for mid, _ in pairs:
                    ids.add(mid)
                msg_id_filter[cid] = ids

        # ---- Run the exporter on a worker thread ----
        # Load ViewerBundleExporter from backend/ via importlib so we
        # don't have to fight the gui/ vs backend/ ``app`` package
        # name collision.  Same pattern as export_html_dialog.
        ViewerBundleExporter = self._load_bundle_exporter_cls()

        title = (
            "Tagged messages — full conversations"
            if dlg.mode == dlg.MODE_FULL else
            "Tagged messages only"
            if dlg.mode == dlg.MODE_TAGGED_ONLY else
            f"Tagged messages — ±{dlg.buffer_days} day buffer"
        )

        # Resolve case_info for the bundle header (best effort).
        case_info: dict | None = None
        try:
            from app.services.case_manager import CaseManager
            cm = CaseManager.get()
            if cm and cm.current_case:
                meta = cm.current_case.metadata or {}
                case_info = {
                    "case_id":  meta.get("case_id"),
                    "examiner": meta.get("examiner"),
                    "notes":    meta.get("notes"),
                    "created":  meta.get("created"),
                }
        except Exception:
            pass

        self._bundle_exporter = ViewerBundleExporter(
            conversation_ids=conv_ids,
            db_path=str(self._db.path),
            output_dir=str(dlg.output_dir),
            include_media=dlg.include_media,
            make_zip=dlg.make_zip,
            title=title,
            case_info=case_info,
            message_id_filter=msg_id_filter,
        )

        # Lightweight progress dialog
        from PySide6.QtWidgets import QProgressDialog
        prog = QProgressDialog(
            "Building tagged-message bundle…", "Cancel", 0, len(conv_ids), self
        )
        prog.setWindowTitle("Export tagged messages")
        prog.setMinimumDuration(200)
        prog.setAutoClose(False)
        prog.setAutoReset(False)

        def _on_progress(cur: int, total: int, label: str) -> None:
            prog.setMaximum(max(total, 1))
            prog.setValue(cur)
            prog.setLabelText(label or "Building bundle…")

        def _on_finished(out: str, err: str) -> None:
            prog.close()
            if err:
                QMessageBox.warning(
                    self, "Export failed",
                    f"Bundle export failed:\n\n{err}",
                )
                return
            QMessageBox.information(
                self, "Export complete",
                f"Tagged-message bundle written to:\n{out}",
            )
            try:
                import webbrowser
                from pathlib import Path
                p = Path(out)
                if p.exists():
                    webbrowser.open(p.as_uri())
            except Exception:
                pass

        self._bundle_exporter.progress.connect(_on_progress)
        self._bundle_exporter.finished.connect(_on_finished)
        prog.canceled.connect(self._bundle_exporter.requestInterruption)
        self._bundle_exporter.start()

    def _export_csv(self):
        if self._model.total_rows == 0:
            QMessageBox.information(self, "Export", "No tagged messages to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Tagged Messages",
            "tagged_messages.csv", "CSV Files (*.csv)",
        )
        if not path:
            return
        try:
            rows = self._db.fetchall(self._export_sql())
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([
                    "Tag", "Note", "Tagged At", "Tagged By",
                    "Message ID", "From Me", "Timestamp", "Type", "Revoked",
                    "Message Text", "Sender", "Phone", "Conversation",
                    "Conversation ID",
                ])
                for row in rows:
                    out = list(row)
                    if out[6]:
                        out[6] = format_timestamp(out[6], "full")
                    w.writerow(out)
            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(rows)} tagged messages to:\n{path}",
            )
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _export_html(self):
        if self._model.total_rows == 0:
            QMessageBox.information(self, "Export", "No tagged messages to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Tagged Messages (HTML)",
            "tagged_messages.html", "HTML Files (*.html)",
        )
        if not path:
            return
        try:
            rows = self._db.fetchall(self._export_sql())
            parts = [
                "<!DOCTYPE html>",
                "<html><head><meta charset='utf-8'>",
                "<title>Tagged Messages - WAInsight</title>",
                "<style>",
                "body{font-family:'Segoe UI',sans-serif;margin:24px;background:#f5f5f5}",
                "h1{color:#00695c;margin-bottom:4px}",
                ".sub{color:#666;margin-bottom:20px;font-size:13px}",
                "table{border-collapse:collapse;width:100%;background:#fff;"
                "box-shadow:0 1px 3px rgba(0,0,0,.1);border-radius:6px;overflow:hidden}",
                "th{background:#00695c;color:#fff;padding:10px 12px;text-align:left;font-size:12px}",
                "td{padding:8px 12px;border-bottom:1px solid #e8eaed;font-size:12px;vertical-align:top}",
                "tr:nth-child(even){background:#fafafa}",
                "tr:hover{background:#e8f5e9}",
                ".tag{color:#ef5350;font-weight:bold}",
                ".note{color:#43a047;font-style:italic}",
                ".revoked{color:#999;text-decoration:line-through}",
                ".sent td:first-child{border-left:3px solid #00695c}",
                ".ts{font-family:monospace;font-size:11px;white-space:nowrap}",
                "</style></head><body>",
                "<h1>\u2691 Tagged Messages Report</h1>",
                f"<p class='sub'>Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                f"&middot; {len(rows)} tagged messages</p>",
                "<table><tr><th>#</th><th>Tag</th><th>Message</th><th>Sender</th>"
                "<th>Phone</th><th>Conversation</th><th>Timestamp</th>"
                "<th>Note</th><th>Tagged At</th></tr>",
            ]
            for i, r in enumerate(rows, 1):
                tag, note, tagged_at = r[0], r[1], r[2]
                from_me, ts, is_rev = r[5], r[6], r[8]
                text, sender, phone, conv = r[9], r[10], r[11], r[12]
                ts_s = format_timestamp(ts, "full") if ts else ""
                te = _esc(text)
                if is_rev:
                    te = f"<span class='revoked'>{te}</span>"
                ne = _esc(note)
                rc = " class='sent'" if from_me else ""
                parts.append(
                    f"<tr{rc}><td>{i}</td>"
                    f"<td class='tag'>\u2691 {tag or 'flagged'}</td>"
                    f"<td>{te}</td><td>{_esc(sender)}</td>"
                    f"<td>{_esc(phone)}</td><td>{_esc(conv)}</td>"
                    f"<td class='ts'>{ts_s}</td>"
                    f"<td class='note'>{ne}</td>"
                    f"<td class='ts'>{tagged_at or ''}</td></tr>"
                )
            parts.append("</table>")
            parts.append(
                "<p style='margin-top:20px;color:#999;font-size:11px'>"
                "Generated by WAInsight</p></body></html>"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))
            QMessageBox.information(
                self, "Export Complete",
                f"Exported {len(rows)} tagged messages to:\n{path}",
            )
        except Exception as e:
            QMessageBox.warning(self, "Export Error", str(e))

    @staticmethod
    def _export_sql() -> str:
        # Owner-aware sender + phone — owner-sent rows would otherwise
        # export as "Unknown" with no phone (sender_id NULL on from_me).
        return """
            SELECT mt.tag_label, mt.note, mt.tagged_at, mt.tagged_by,
                   m.id, m.from_me, m.timestamp, m.type_label, m.is_revoked,
                   COALESCE(md.media_caption, m.text_content,
                            '[' || m.type_label || ']') AS text_content,
                   CASE
                       WHEN m.from_me = 1 THEN
                           COALESCE(
                               (SELECT value FROM case_metadata
                                  WHERE key = 'device_owner_name'),
                               'You (Device Owner)') || ' (you)'
                       ELSE
                           COALESCE(c.resolved_name, c.wa_name, c.phone_number,
                                    REPLACE(c.phone_jid, '@s.whatsapp.net', ''),
                                    'Unknown')
                   END AS sender,
                   CASE
                       WHEN m.from_me = 1 THEN
                           COALESCE(
                               (SELECT value FROM case_metadata
                                  WHERE key = 'device_owner_phone'),
                               '')
                       ELSE
                           COALESCE(c.phone_number,
                                    REPLACE(c.phone_jid, '@s.whatsapp.net', ''),
                                    '')
                   END AS phone,
                   COALESCE(conv.display_name, conv.jid_raw_string) AS conversation,
                   m.conversation_id
            FROM message_tag mt
            INNER JOIN message m ON m.id = mt.message_id
            LEFT JOIN media md ON md.message_id = m.id
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN conversation conv ON conv.id = m.conversation_id
            ORDER BY mt.tagged_at DESC
        """


def _esc(v) -> str:
    """HTML-escape a value."""
    s = str(v) if v else ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
