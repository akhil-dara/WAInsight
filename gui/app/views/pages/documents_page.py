"""
Documents page — dedicated browser for shared files (PDF / ZIP / APK /
Word / Excel / …).

Why a separate page?
====================
Media Gallery mixes photos/videos/stickers with documents; filters there
focus on visual media. Forensics workflows for documents look different:

  * Investigators want to slice by **extension** (legal PDFs vs. APKs vs.
    credential dumps etc.) — and Media Gallery doesn't expose file
    extensions at all.
  * "Shared across N chats" is much more meaningful for documents than
    photos (a propagated APK across 10 chats is a strong signal).
  * File name & size are first-class facets, not visual thumbnails.

Design
======
Left rail  — extension buckets ("pdf / zip / apk / docx / …")
                auto-discovered from the DB with per-bucket
                counts; click to drill down.
Top bar    — free-text search across filename + caption.
             size-range sliders, sender filter, conversation filter,
             date range, **"Shared with ≥ N chats"** filter.
Table view — name | ext | size | sender | chats-shared-in | first-seen.
Click row  — opens detail sidebar with file hash, SHA-256, full path,
             "Open containing chat", "Open in file explorer".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QSize, Signal, QTimer, QPoint
from PySide6.QtGui import QColor, QBrush, QFont, QDesktopServices, QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton, QSpinBox,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget, QScrollArea,
)
from PySide6.QtCore import QUrl

from app.services.database import Database
from app.services.theme_manager import ThemeManager


# Extension → display name + emoji icon. Used for the left-rail buckets
# and for highlighting well-known risky types.
_EXT_CATALOG: dict[str, tuple[str, str]] = {
    "pdf":   ("PDF Documents",   "\U0001F4C4"),  # 📄
    "doc":   ("Word 97-2003",    "\U0001F4DD"),
    "docx":  ("Word",            "\U0001F4DD"),  # 📝
    "xls":   ("Excel 97-2003",   "\U0001F4C8"),
    "xlsx":  ("Excel",           "\U0001F4C8"),  # 📈
    "ppt":   ("PowerPoint 97",   "\U0001F4CA"),
    "pptx":  ("PowerPoint",      "\U0001F4CA"),  # 📊
    "txt":   ("Text",            "\U0001F4C4"),
    "csv":   ("CSV",             "\U0001F4CB"),
    "json":  ("JSON",            "\U0001F9FE"),
    "xml":   ("XML",             "\U0001F9FE"),
    "html":  ("HTML",            "\U0001F310"),
    "zip":   ("ZIP Archive",     "\U0001F5DC"),  # 🗜
    "rar":   ("RAR Archive",     "\U0001F5DC"),
    "7z":    ("7-Zip",           "\U0001F5DC"),
    "tar":   ("Tar Archive",     "\U0001F5DC"),
    "gz":    ("Gzip",            "\U0001F5DC"),
    "apk":   ("Android APK",     "\u26A0\uFE0F"),  # ⚠ flagged
    "exe":   ("Windows EXE",     "\u26A0\uFE0F"),
    "msi":   ("Windows MSI",     "\u26A0\uFE0F"),
    "bat":   ("Batch",           "\u26A0\uFE0F"),
    "ps1":   ("PowerShell",      "\u26A0\uFE0F"),
    "sh":    ("Shell Script",    "\u26A0\uFE0F"),
    "jar":   ("Java JAR",        "\u26A0\uFE0F"),
    "iso":   ("Disk Image",      "\U0001F4BF"),
    "dmg":   ("macOS Image",     "\U0001F4BF"),
}
_RISKY_EXTS = {"apk", "exe", "msi", "bat", "ps1", "sh", "jar", "vbs", "scr", "dll"}


def _fmt_size(n: Optional[int]) -> str:
    if not n:
        return ""
    if n < 1024: return f"{n} B"
    if n < 1024 * 1024: return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024: return f"{n/1048576:.1f} MB"
    return f"{n/1073741824:.2f} GB"


def _ext_of(name: str) -> str:
    if not name:
        return ""
    # Handle compound extensions like tar.gz
    lower = name.lower().rsplit(".", 2)
    if len(lower) == 3 and lower[1] in ("tar",):
        return f"{lower[1]}.{lower[2]}"
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lower()[:10]


class DocumentsPage(QWidget):
    """Dedicated documents browser with extension buckets + shared-count filter."""

    conversation_selected = Signal(int, int)   # conv_id, msg_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._all_rows: list[dict] = []   # cached after first load
        self._ext_counts: dict[str, int] = {}
        self._selected_ext: str | None = None
        self._shared_min: int = 1

        self._build_ui()
        QTimer.singleShot(50, self.reload)

    # ---------------------------------------------------------------- #
    # UI
    # ---------------------------------------------------------------- #

    def _build_ui(self) -> None:
        lt = self._tm.is_light
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setChildrenCollapsible(False)

        # -------- LEFT RAIL: extension buckets ---------
        left = QFrame()
        left.setMinimumWidth(200)
        left.setMaximumWidth(280)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 10, 10, 10)
        ll.setSpacing(4)

        rail_title = QLabel("FILE TYPES")
        rail_title.setStyleSheet(
            f"color: {'#546e7a' if lt else '#8696a0'}; "
            f"font-size: 10px; font-weight: 700; letter-spacing: 0.8px; "
            f"margin-bottom: 4px;"
        )
        ll.addWidget(rail_title)

        self._ext_list = QListWidget()
        self._ext_list.setFrameShape(QFrame.NoFrame)
        self._ext_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; }}
            QListWidget::item {{
                padding: 6px 8px; border-radius: 4px; margin: 1px 0;
                color: {'#1a1a1a' if lt else '#e9edef'};
            }}
            QListWidget::item:selected {{
                background: {'rgba(0,137,123,0.12)' if lt else 'rgba(0,188,212,0.15)'};
                color: {'#00695c' if lt else '#00bcd4'};
                font-weight: 600;
            }}
            QListWidget::item:hover {{
                background: {'rgba(0,0,0,0.04)' if lt else 'rgba(255,255,255,0.05)'};
            }}
        """)
        self._ext_list.itemClicked.connect(self._on_ext_selected)
        ll.addWidget(self._ext_list, 1)

        splitter.addWidget(left)

        # -------- RIGHT: filters + table ---------
        right = QFrame()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 10, 12, 10)
        rl.setSpacing(8)

        # Title
        self._title = QLabel("Documents")
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        self._title.setFont(f)
        rl.addWidget(self._title)

        self._count = QLabel("0 documents")
        self._count.setStyleSheet(f"color: {'#546e7a' if lt else '#8696a0'}; font-size: 11px;")
        rl.addWidget(self._count)

        # Filter row
        flt = QHBoxLayout()
        flt.setSpacing(8)
        self._search = QLineEdit()
        self._search.setPlaceholderText("\u2315 Search by filename or caption…")
        self._search.setFixedHeight(30)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filters)
        flt.addWidget(self._search, 2)

        shared_label = QLabel("Shared with \u2265")
        shared_label.setStyleSheet("font-size: 11px;")
        flt.addWidget(shared_label)
        self._shared_spin = QSpinBox()
        self._shared_spin.setRange(1, 500)
        self._shared_spin.setValue(1)
        self._shared_spin.setFixedWidth(70)
        self._shared_spin.setSuffix(" chats")
        self._shared_spin.valueChanged.connect(self._on_shared_changed)
        flt.addWidget(self._shared_spin)

        # Risky filter
        self._risky_btn = QToolButton()
        self._risky_btn.setText("\u26A0 Risky only")
        self._risky_btn.setCheckable(True)
        self._risky_btn.setToolTip("Show only APK / EXE / script types")
        self._risky_btn.setStyleSheet("""
            QToolButton { background: transparent; border: 1px solid #999;
                          padding: 4px 10px; border-radius: 4px; font-size: 11px; }
            QToolButton:checked { background: #fff3e0; color: #e65100;
                          border-color: #e65100; font-weight: 600; }
        """)
        self._risky_btn.toggled.connect(self._apply_filters)
        flt.addWidget(self._risky_btn)

        flt.addStretch()
        rl.addLayout(flt)

        # Table
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            ["Filename", "Type", "Size", "Sender", "Shared In", "First Seen", ""]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 4, 5, 6):
            self._table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Interactive)
        self._table.setColumnWidth(3, 180)
        self._table.cellDoubleClicked.connect(self._on_row_opened)
        # Right-click context menu — "Where else is this shared?" /
        # "Open conversation" / "Copy hash" / "Open in file explorer"
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_context_menu)
        rl.addWidget(self._table, 1)

        splitter.addWidget(right)
        splitter.setSizes([240, 1000])

        root.addWidget(splitter)

    # ---------------------------------------------------------------- #
    # Data
    # ---------------------------------------------------------------- #

    def reload(self) -> None:
        """Load all document messages from the database."""
        try:
            db = Database.get()
        except RuntimeError:
            return

        # Documents in this schema: type_label = 'document' OR mime like
        # application/* (non-image/video/audio). We pull from the media
        # table joined to message + conversation. Each row carries the
        # first message that shared the file; we compute shared-count
        # by grouping file_hash (fallback file_name when hash missing).
        # Schema note: the `media` table uses `media_name` (not `file_name`)
        # for the original filename. Fall back to the base-name of
        # `file_path` when `media_name` is empty (happens for older
        # ingestions where only the disk path was captured).
        # Owner identity for from_me rows — pulled from case_metadata so
        # owner-sent documents don't surface as "Unknown" (their message
        # row has sender_id NULL, so the LEFT JOIN against contact
        # returns NULL).
        try:
            owner_meta = {
                row["key"]: row["value"]
                for row in db.fetchall(
                    "SELECT key, value FROM case_metadata WHERE key IN ("
                    "'device_owner_name','device_owner_phone',"
                    "'device_owner_jid','device_owner_lid_jid')"
                )
            }
        except Exception:
            owner_meta = {}
        owner_name = owner_meta.get("device_owner_name") or "You (Device Owner)"
        owner_phone = (owner_meta.get("device_owner_phone") or "").replace("@s.whatsapp.net", "")
        owner_jid = owner_meta.get("device_owner_jid") or (
            f"{owner_phone}@s.whatsapp.net" if owner_phone else ""
        )
        self._owner_name = owner_name
        self._owner_jid = owner_jid

        rows = db.fetchall("""
            SELECT me.id AS media_id,
                   COALESCE(NULLIF(me.media_name, ''),
                            CASE
                              WHEN me.file_path IS NOT NULL AND me.file_path != ''
                              THEN REPLACE(
                                REPLACE(me.file_path, '\\', '/'),
                                '\\/', '/')
                            END
                   ) AS file_name,
                   me.file_size, me.mime_type,
                   me.file_hash, me.file_path, me.resolved_file_path, me.file_exists,
                   m.id AS msg_id, m.conversation_id, m.timestamp,
                   m.sender_id, m.from_me,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS sender_raw,
                   c.phone_jid AS sender_jid,
                   c.lid_jid   AS sender_lid,
                   cv.display_name AS conv_name,
                   cv.chat_type    AS conv_type,
                   cv.jid_raw_string AS conv_jid
            FROM media me
            JOIN message m ON m.id = me.message_id
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN conversation cv ON cv.id = m.conversation_id
            WHERE (m.type_label = 'document' OR me.mime_type LIKE 'application/%')
              AND (me.media_name IS NOT NULL AND me.media_name != ''
                   OR me.file_path IS NOT NULL AND me.file_path != '')
            ORDER BY m.timestamp DESC
        """)

        # Compute "shared in N conversations" per file hash/name
        shared_counts: dict[str, int] = {}
        for r in rows:
            key = r["file_hash"] or r["file_name"] or ""
            shared_counts[key] = shared_counts.get(key, 0) + 1

        docs: list[dict] = []
        self._ext_counts.clear()
        for r in rows:
            raw_name = r["file_name"] or ""
            # Strip any directory components and normalise separators so
            # we always show just the filename (file_path fallback may
            # contain "Media/WhatsApp Documents/foo.pdf").
            clean_name = raw_name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            ext = _ext_of(clean_name)
            d = dict(r)
            d["file_name"] = clean_name
            d["ext"] = ext
            # Sender resolution — owner-aware:
            #   from_me=1 → use the device-owner identity from
            #     case_metadata so the column shows the analyst's
            #     name, never "Unknown"
            #   else      → resolved contact (with raw JID-only
            #     fallback when no name was ever recorded)
            if r["from_me"]:
                d["sender"] = f"{owner_name} (you)" if owner_name and "(you)" not in owner_name else owner_name
                d["sender_jid_resolved"] = owner_jid
                d["is_owner"] = True
            else:
                raw_sender = r["sender_raw"]
                if raw_sender:
                    d["sender"] = raw_sender
                elif r["sender_jid"]:
                    # JID-only fallback so the cell never reads
                    # "Unknown" when we at least have the JID.
                    jid = r["sender_jid"]
                    d["sender"] = jid.split("@", 1)[0] or jid
                else:
                    d["sender"] = "Unknown"
                d["sender_jid_resolved"] = r["sender_jid"] or r["sender_lid"] or ""
                d["is_owner"] = False
            key = r["file_hash"] or clean_name or ""
            d["shared_in"] = shared_counts.get(key, 1)
            docs.append(d)
            if ext:
                self._ext_counts[ext] = self._ext_counts.get(ext, 0) + 1

        self._all_rows = docs
        self._populate_extension_rail()
        self._apply_filters()

    def _populate_extension_rail(self) -> None:
        self._ext_list.clear()
        item_all = QListWidgetItem(f"\u2630  All types   ({len(self._all_rows):,})")
        item_all.setData(Qt.UserRole, None)
        self._ext_list.addItem(item_all)
        self._ext_list.setCurrentRow(0)

        # Sort extensions by count desc, risky first
        ordered = sorted(
            self._ext_counts.items(),
            key=lambda kv: (-(1 if kv[0] in _RISKY_EXTS else 0), -kv[1]),
        )
        for ext, cnt in ordered:
            disp, icon = _EXT_CATALOG.get(ext, (ext.upper(), "\U0001F4C4"))
            label = f"{icon}  {disp}  .{ext}  ({cnt:,})"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ext)
            if ext in _RISKY_EXTS:
                item.setForeground(QBrush(QColor("#e65100")))
            self._ext_list.addItem(item)

    def _on_ext_selected(self, item: QListWidgetItem) -> None:
        self._selected_ext = item.data(Qt.UserRole)
        self._apply_filters()

    def _on_shared_changed(self, v: int) -> None:
        self._shared_min = v
        self._apply_filters()

    def _apply_filters(self) -> None:
        q = (self._search.text() or "").strip().lower()
        risky_only = self._risky_btn.isChecked()
        rows: list[dict] = []
        for r in self._all_rows:
            if self._selected_ext and r.get("ext") != self._selected_ext:
                continue
            if risky_only and r.get("ext") not in _RISKY_EXTS:
                continue
            if r.get("shared_in", 1) < self._shared_min:
                continue
            if q:
                blob = (r.get("file_name") or "").lower()
                if q not in blob:
                    continue
            rows.append(r)
        self._render_rows(rows)

    def _render_rows(self, rows: list[dict]) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            fn = r.get("file_name") or ""
            ext = r.get("ext") or ""
            # Name
            name_it = QTableWidgetItem(fn)
            name_it.setData(Qt.UserRole, r)
            if ext in _RISKY_EXTS:
                name_it.setForeground(QBrush(QColor("#e65100")))
            self._table.setItem(i, 0, name_it)
            # Type label with icon
            disp, icon = _EXT_CATALOG.get(ext, (ext.upper() if ext else "—", "\U0001F4C4"))
            type_it = QTableWidgetItem(f"{icon} {disp}")
            self._table.setItem(i, 1, type_it)
            # Size (sortable by raw bytes)
            sz = r.get("file_size") or 0
            size_it = QTableWidgetItem(_fmt_size(sz))
            size_it.setData(Qt.UserRole, int(sz))
            size_it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._table.setItem(i, 2, size_it)
            # Sender
            self._table.setItem(i, 3, QTableWidgetItem(r.get("sender") or ""))
            # Shared-in count
            sh = r.get("shared_in", 1)
            sh_it = QTableWidgetItem(str(sh))
            sh_it.setData(Qt.UserRole, sh)
            sh_it.setTextAlignment(Qt.AlignCenter)
            if sh >= 3:
                sh_it.setForeground(QBrush(QColor("#c62828")))
                font = QFont()
                font.setBold(True)
                sh_it.setFont(font)
            self._table.setItem(i, 4, sh_it)
            # First seen
            ts = r.get("timestamp")
            ts_str = ""
            if ts:
                from datetime import datetime as _dt
                try:
                    ts_str = _dt.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    ts_str = ""
            self._table.setItem(i, 5, QTableWidgetItem(ts_str))
            # Action: open containing chat
            open_btn = QPushButton("Open \u2192")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setFixedHeight(24)
            open_btn.setStyleSheet(
                "QPushButton { background: #00897b; color: white; border: none; "
                "border-radius: 4px; font-size: 10px; font-weight: 600; padding: 0 8px; }"
                "QPushButton:hover { background: #00796b; }"
            )
            row_data = r
            open_btn.clicked.connect(
                lambda _=False, rd=row_data: self._open_chat(rd)
            )
            self._table.setCellWidget(i, 6, open_btn)

        self._table.setSortingEnabled(True)
        self._count.setText(f"{len(rows):,} document{'s' if len(rows) != 1 else ''}"
                            + (f"   |   filtered from {len(self._all_rows):,}"
                               if len(rows) != len(self._all_rows) else ""))

    def _open_chat(self, r: dict) -> None:
        conv_id = r.get("conversation_id")
        msg_id = r.get("msg_id")
        if conv_id and msg_id:
            self.conversation_selected.emit(int(conv_id), int(msg_id))

    # ------------------------------------------------------------------ #
    # Right-click context menu
    # ------------------------------------------------------------------ #

    def _on_row_context_menu(self, pos: QPoint) -> None:
        """Build a right-click menu for the row under the cursor.

        Two key actions analysts ask for repeatedly:
          1. **Find where else this file was shared** — opens a popup
             listing every chat / sender / timestamp where the same
             SHA-256 (or filename) appears, each row click-able to
             jump to that exact message.
          2. **Open this conversation** — same as the row's "Open"
             button but available via right-click on any column.

        Also offered: copy SHA-256, copy filename, reveal on disk.
        """
        idx = self._table.indexAt(pos)
        if not idx.isValid():
            return
        item = self._table.item(idx.row(), 0)
        if not item:
            return
        r = item.data(Qt.UserRole)
        if not r:
            return

        menu = QMenu(self._table)
        menu.setStyleSheet(
            "QMenu { background: white; border: 1px solid #d0d7de; "
            "        padding: 4px; }"
            "QMenu::item { padding: 6px 16px; font-size: 12px; }"
            "QMenu::item:selected { background: #e0f2f1; color: #00695c; }"
            "QMenu::separator { height: 1px; background: #e0e7ed; "
            "                    margin: 4px 8px; }"
        )

        share_count = r.get("shared_in", 1) or 1
        share_act = QAction(
            f"\U0001F50D  Find where this file is shared "
            f"({share_count} chat{'s' if share_count != 1 else ''})",
            menu
        )
        share_act.triggered.connect(lambda: self._show_sharing_popup(r))
        if share_count <= 1:
            share_act.setEnabled(False)
        menu.addAction(share_act)

        open_act = QAction("→  Open this conversation at this message", menu)
        open_act.triggered.connect(lambda: self._open_chat(r))
        menu.addAction(open_act)

        menu.addSeparator()

        copy_name_act = QAction("⎘  Copy filename", menu)
        copy_name_act.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(r.get("file_name") or "")
        )
        menu.addAction(copy_name_act)

        if r.get("file_hash"):
            copy_hash_act = QAction("⎘  Copy SHA-256", menu)
            copy_hash_act.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(r.get("file_hash") or "")
            )
            menu.addAction(copy_hash_act)

        if r.get("sender_jid_resolved"):
            copy_jid_act = QAction("⎘  Copy sender JID", menu)
            copy_jid_act.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(
                    r.get("sender_jid_resolved") or "")
            )
            menu.addAction(copy_jid_act)

        # Reveal on disk if the file actually exists locally
        resolved = r.get("resolved_file_path") or ""
        if resolved and os.path.isfile(resolved):
            menu.addSeparator()
            reveal_act = QAction("\U0001F4C2  Reveal in file explorer", menu)
            reveal_act.triggered.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(os.path.dirname(resolved))
                )
            )
            menu.addAction(reveal_act)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _show_sharing_popup(self, r: dict) -> None:
        """Modal dialog listing every appearance of this file across
        the case.  Each row carries the conversation, sender, timestamp
        and a "Go" button that closes the dialog and opens that exact
        message in the chat viewer.

        Match key: SHA-256 when available (collision-free), else the
        clean filename (works for older ingestions where hashing was
        skipped).
        """
        try:
            db = Database.get()
        except RuntimeError:
            return

        file_hash = r.get("file_hash") or ""
        file_name = r.get("file_name") or ""

        if file_hash:
            shares = db.fetchall("""
                SELECT m.id AS msg_id, m.conversation_id, m.timestamp,
                       m.from_me,
                       cv.display_name AS conv_name,
                       cv.chat_type    AS conv_type,
                       cv.jid_raw_string AS conv_jid,
                       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS sender_name,
                       c.phone_jid AS sender_jid
                FROM media me
                JOIN message m ON m.id = me.message_id
                LEFT JOIN contact c ON c.id = m.sender_id
                LEFT JOIN conversation cv ON cv.id = m.conversation_id
                WHERE me.file_hash = ?
                ORDER BY m.timestamp DESC
                LIMIT 200
            """, (file_hash,))
        elif file_name:
            shares = db.fetchall("""
                SELECT m.id AS msg_id, m.conversation_id, m.timestamp,
                       m.from_me,
                       cv.display_name AS conv_name,
                       cv.chat_type    AS conv_type,
                       cv.jid_raw_string AS conv_jid,
                       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS sender_name,
                       c.phone_jid AS sender_jid
                FROM media me
                JOIN message m ON m.id = me.message_id
                LEFT JOIN contact c ON c.id = m.sender_id
                LEFT JOIN conversation cv ON cv.id = m.conversation_id
                WHERE me.media_name = ?
                ORDER BY m.timestamp DESC
                LIMIT 200
            """, (file_name,))
        else:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Where shared — {file_name[:60]}")
        dlg.setMinimumSize(720, 480)
        dl = QVBoxLayout(dlg)
        dl.setContentsMargins(16, 16, 16, 16)
        dl.setSpacing(10)

        head = QLabel(
            f"<b>{file_name}</b>"
            + (f"<br><code style='background:#f5f5f5;padding:2px 4px;"
               f"border-radius:3px;font-size:10px;'>SHA-256: "
               f"{file_hash[:32]}…{file_hash[-8:]}</code>" if file_hash else "")
            + f"<br><small style='color:#666'>"
              f"Found in {len(shares)} message{'s' if len(shares) != 1 else ''} "
              f"across {len({s['conversation_id'] for s in shares})} "
              f"conversation{'s' if len({s['conversation_id'] for s in shares}) != 1 else ''}"
              f"</small>"
        )
        head.setTextFormat(Qt.RichText)
        head.setWordWrap(True)
        dl.addWidget(head)

        tbl = QTableWidget()
        tbl.setColumnCount(5)
        tbl.setHorizontalHeaderLabels(
            ["Conversation", "Sender", "When", "Direction", ""]
        )
        tbl.setRowCount(len(shares))
        tbl.verticalHeader().setVisible(False)
        tbl.setAlternatingRowColors(True)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        tbl.horizontalHeader().setStretchLastSection(False)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3, 4):
            tbl.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)

        from datetime import datetime as _dt
        for i, s in enumerate(shares):
            conv = s["conv_name"] or "?"
            ctype = s["conv_type"] or "personal"
            tbl.setItem(i, 0, QTableWidgetItem(f"{conv}  [{ctype}]"))

            if s["from_me"]:
                sender = f"{self._owner_name} (you)"
            else:
                sender = (s["sender_name"]
                          or (s["sender_jid"] or "").split("@", 1)[0]
                          or "Unknown")
            tbl.setItem(i, 1, QTableWidgetItem(sender))

            ts = s["timestamp"]
            ts_str = ""
            if ts:
                try:
                    ts_str = _dt.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    ts_str = ""
            tbl.setItem(i, 2, QTableWidgetItem(ts_str))

            tbl.setItem(i, 3, QTableWidgetItem(
                "Outgoing" if s["from_me"] else "Incoming"
            ))

            go_btn = QPushButton("Go →")
            go_btn.setCursor(Qt.PointingHandCursor)
            go_btn.setStyleSheet(
                "QPushButton { background: #00897b; color: white; border: none; "
                "border-radius: 4px; font-size: 10px; font-weight: 600; "
                "padding: 4px 10px; }"
                "QPushButton:hover { background: #00796b; }"
            )
            cid = s["conversation_id"]
            mid = s["msg_id"]

            def _go(_=False, c=cid, m=mid, d=dlg):
                d.accept()
                self.conversation_selected.emit(int(c), int(m))

            go_btn.clicked.connect(_go)
            tbl.setCellWidget(i, 4, go_btn)

        dl.addWidget(tbl, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        dl.addWidget(bb)

        dlg.exec()

    def _on_row_opened(self, row: int, col: int) -> None:
        it = self._table.item(row, 0)
        if not it:
            return
        r = it.data(Qt.UserRole)
        if not r:
            return
        # If file exists on disk, reveal it; otherwise open the chat.
        resolved = r.get("resolved_file_path") or ""
        if resolved and os.path.exists(resolved):
            QDesktopServices.openUrl(QUrl.fromLocalFile(resolved))
        else:
            self._open_chat(r)
