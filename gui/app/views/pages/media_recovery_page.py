"""Media Recovery Page — standalone sidebar page showing per-conversation
media stats with batch download controls."""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QComboBox,
    QTableView, QHeaderView, QSplitter, QFrame, QProgressBar,
    QAbstractItemView, QMessageBox, QStyledItemDelegate,
)
from PySide6.QtCore import Qt, Signal, QModelIndex, QSize, QRect, QThread
from PySide6.QtGui import QFont, QPainter, QColor, QPixmap, QPainterPath, QBrush, QPen
from PySide6.QtWidgets import QStyle

from app.services.database import Database
from app.models.base_table_model import BaseLazyTableModel

NOW_TS_SQL = "CAST(strftime('%s','now') AS INTEGER)"
HASH_RECOVERED_EXPR = (
    "EXISTS ("
    "SELECT 1 FROM media mx "
    "WHERE mx.file_hash = me.file_hash "
    "AND mx.id != me.id "
    "AND mx.file_hash IS NOT NULL AND TRIM(mx.file_hash) != '' "
    "AND mx.file_exists = 1"
    ")"
)
ON_DISK_EXPR = f"(me.file_exists = 1 OR {HASH_RECOVERED_EXPR})"
DOWNLOADABLE_EXPR = (
    f"(({ON_DISK_EXPR}) = 0 OR ({ON_DISK_EXPR}) IS NULL) "
    "AND me.media_url IS NOT NULL AND TRIM(me.media_url) != '' "
    "AND me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32 "
    f"AND me.cdn_expiry_ts IS NOT NULL AND me.cdn_expiry_ts > {NOW_TS_SQL}"
)
EXPIRED_EXPR = (
    f"(({ON_DISK_EXPR}) = 0 OR ({ON_DISK_EXPR}) IS NULL) "
    "AND me.media_url IS NOT NULL AND TRIM(me.media_url) != '' "
    "AND me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32 "
    f"AND me.cdn_expiry_ts IS NOT NULL AND me.cdn_expiry_ts <= {NOW_TS_SQL}"
)
NO_KEY_EXPR = (
    f"(({ON_DISK_EXPR}) = 0 OR ({ON_DISK_EXPR}) IS NULL) "
    "AND (me.media_key IS NULL OR LENGTH(me.media_key) = 0)"
)

# ── Constants ──
AVATAR_SIZE = 36
AVATAR_COLORS = [
    "#00897b", "#7b1fa2", "#c62828", "#1565c0", "#ef6c00", "#2e7d32",
    "#ad1457", "#4527a0", "#00838f", "#d84315", "#558b2f", "#6a1b9a",
]
TYPE_ICONS = {
    "image": "\U0001F4F7", "video": "\U0001F3AC", "audio": "\U0001F3B5",
    "voice": "\U0001F399", "document": "\U0001F4C4", "sticker": "\U0001F36D",
    "gif": "\U0001F3AC", "animated_gif": "\U0001F3AC", "view_once_image": "\U0001F441",
    "view_once_video": "\U0001F441", "view_once_voice": "\U0001F441",
    "poll": "\U0001F4CA", "location": "\U0001F4CD", "vcard": "\U0001F464",
}


def _type_where_for_filter(type_filter: str) -> str:
    if type_filter == "image":
        return "AND m.type_label IN ('image','view_once_image')"
    if type_filter == "video":
        return "AND m.type_label IN ('video','gif','animated_gif','view_once_video')"
    if type_filter == "audio":
        return "AND m.type_label IN ('audio','voice','view_once_voice')"
    if type_filter == "document":
        return "AND m.type_label = 'document'"
    if type_filter == "sticker":
        return "AND m.type_label = 'sticker'"
    if type_filter == "view_once":
        return "AND m.type_label IN ('view_once_image','view_once_video','view_once_voice')"
    return ""


def _parse_oe(url: str | None) -> int:
    if not url:
        return 0
    m = re.search(r"oe=([0-9A-Fa-f]+)", url)
    return int(m.group(1), 16) if m else 0


# ── Data Model ──

class _RecoveryModel(BaseLazyTableModel):
    """Per-conversation media recovery stats."""

    _columns = [
        ("avatar_blob", ""), ("display_name", "Conversation"), ("chat_type", "Type"),
        ("total_media", "Total"), ("on_disk", "On Disk"), ("has_key_url", "Downloadable"),
        ("expired", "Expired"), ("no_key", "Missing"), ("conv_id", "ID"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._type_filter = ""  # "" = all, or "image", "video", etc.
        self._chat_filter = ""
        self._chat_scope = ""

    def set_type_filter(self, type_label: str):
        self._type_filter = type_label
        self.reload()

    def set_chat_filter(self, text: str):
        self._chat_filter = (text or "").strip().lower()
        self.reload()

    def set_chat_scope(self, scope: str):
        self._chat_scope = (scope or "").strip().lower()
        self.reload()

    def columnCount(self, parent=QModelIndex()) -> int:
        # Render this model as a single custom-painted list column.
        return 1

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._data):
            return None
        if role == Qt.DisplayRole:
            return self._data[index.row()][1] or ""
        return None

    def reload(self):
        try:
            db = Database.get()
        except RuntimeError:
            # Database not initialized yet (page created before case opened)
            self._data = []
            self._total_rows = 0
            return
        tf = self._type_filter
        chat_filter = self._chat_filter
        chat_scope = self._chat_scope

        type_where = _type_where_for_filter(tf)

        chat_where = ""
        params: list[object] = []
        if chat_scope:
            chat_where += " AND LOWER(COALESCE(c.chat_type, '')) = ?"
            params.append(chat_scope)
        if chat_filter:
            chat_where += (
                " AND (LOWER(COALESCE(c.display_name, '')) LIKE ? "
                "OR LOWER(COALESCE(c.chat_type, '')) LIKE ? "
                "OR LOWER(COALESCE(c.jid_raw_string, '')) LIKE ?)"
            )
            like = f"%{chat_filter}%"
            params.extend([like, like, like])

        sql = f"""
            SELECT
                COALESCE(c.avatar_blob, ct.avatar_blob) AS avatar_blob,
                c.display_name,
                c.chat_type,
                COUNT(me.id) AS total_media,
                SUM(CASE WHEN {ON_DISK_EXPR} THEN 1 ELSE 0 END) AS on_disk,
                SUM(CASE WHEN {DOWNLOADABLE_EXPR} THEN 1 ELSE 0 END) AS has_key_url,
                SUM(CASE WHEN {EXPIRED_EXPR} THEN 1 ELSE 0 END) AS expired,
                SUM(CASE WHEN {NO_KEY_EXPR} THEN 1 ELSE 0 END) AS no_key,
                c.id AS conv_id
            FROM conversation c
            LEFT JOIN contact ct ON ct.phone_jid = c.jid_raw_string
            JOIN message m ON m.conversation_id = c.id
            JOIN media me ON me.message_id = m.id
            WHERE 1=1 {type_where} {chat_where}
            GROUP BY c.id
            HAVING total_media > 0
            ORDER BY has_key_url DESC, total_media DESC
        """

        self.beginResetModel()
        try:
            rows = db.fetchall(sql, tuple(params))
            self._data = [tuple(r) for r in rows]
            self._total_rows = len(self._data)
            print(f"[MediaRecovery] Loaded {len(self._data)} conversations with media")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[MediaRecovery] Query error: {e}")
            self._data = []
            self._total_rows = 0
        self.endResetModel()


# ── Conversation Row Delegate ──

class _ConvDelegate(QStyledItemDelegate):
    """Renders conversation rows with avatar, name, and colored stat bars."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._avatar_cache: dict[int, QPixmap] = {}

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 64)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        rect = option.rect

        # Selection highlight
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, QColor("#e8f5e9"))
        elif index.row() % 2:
            painter.fillRect(rect, QColor("#fafafa"))

        row_data = index.model()._data[index.row()] if index.row() < len(index.model()._data) else None
        if not row_data:
            painter.restore()
            return

        avatar_blob, name, chat_type, total, on_disk, downloadable, expired, no_key, conv_id = row_data
        if not name:
            if chat_type == "broadcast":
                name = "Status Updates"
            else:
                name = f"Chat #{conv_id}"

        # Avatar
        x = rect.x() + 8
        y = rect.y() + (rect.height() - AVATAR_SIZE) // 2
        self._draw_avatar(painter, x, y, conv_id, name, avatar_blob)

        # Name + type badge
        text_x = x + AVATAR_SIZE + 10
        painter.setFont(QFont("Segoe UI", 11, QFont.Bold))
        painter.setPen(QColor("#111b21"))
        name_rect = QRect(text_x, rect.y() + 6, rect.width() - text_x - 200, 20)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         painter.fontMetrics().elidedText(name, Qt.ElideRight, name_rect.width()))

        # Chat type badge
        if chat_type:
            painter.setFont(QFont("Segoe UI", 8))
            painter.setPen(QColor("#667781"))
            painter.drawText(QRect(text_x, rect.y() + 28, 200, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, chat_type)

        # Stats: colored bar
        bar_x = rect.right() - 220
        bar_y = rect.y() + 12
        bar_w = 190
        bar_h = 10

        if total > 0:
            # Proportional segments
            on_disk_w = int(on_disk / total * bar_w)
            dl_w = int(downloadable / total * bar_w)
            expired_w = int(expired / total * bar_w)
            nokey_w = bar_w - on_disk_w - dl_w - expired_w

            painter.setPen(Qt.NoPen)
            # Green = on disk
            if on_disk_w > 0:
                painter.setBrush(QColor("#4caf50"))
                painter.drawRoundedRect(bar_x, bar_y, on_disk_w, bar_h, 2, 2)
            # Blue = downloadable
            if dl_w > 0:
                painter.setBrush(QColor("#1565c0"))
                painter.drawRoundedRect(bar_x + on_disk_w, bar_y, dl_w, bar_h, 2, 2)
            # Orange = expired
            if expired_w > 0:
                painter.setBrush(QColor("#ff9800"))
                painter.drawRoundedRect(bar_x + on_disk_w + dl_w, bar_y, expired_w, bar_h, 2, 2)
            # Red/orange = missing/no key
            if nokey_w > 0:
                painter.setBrush(QColor("#e65100"))
                painter.drawRoundedRect(bar_x + on_disk_w + dl_w + expired_w, bar_y, nokey_w, bar_h, 2, 2)

        # Count labels
        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor("#2e7d32"))
        painter.drawText(QRect(bar_x, bar_y + 14, 40, 16), Qt.AlignLeft, f"{on_disk}")
        painter.setPen(QColor("#1565c0"))
        painter.drawText(QRect(bar_x + 45, bar_y + 14, 40, 16), Qt.AlignLeft, f"{downloadable}")
        painter.setPen(QColor("#ff9800"))
        painter.drawText(QRect(bar_x + 90, bar_y + 14, 40, 16), Qt.AlignLeft, f"{expired}")
        painter.setPen(QColor("#e65100"))
        painter.drawText(QRect(bar_x + 135, bar_y + 14, 50, 16), Qt.AlignLeft, f"{no_key}")

        painter.restore()

    def _draw_avatar(self, painter: QPainter, x, y, conv_id, name, blob):
        if blob and len(blob) > 100 and conv_id not in self._avatar_cache:
            pm = QPixmap()
            pm.loadFromData(blob)
            if not pm.isNull():
                self._avatar_cache[conv_id] = pm

        path = QPainterPath()
        path.addEllipse(x, y, AVATAR_SIZE, AVATAR_SIZE)
        painter.setClipPath(path)

        if conv_id in self._avatar_cache:
            pm = self._avatar_cache[conv_id]
            scaled = pm.scaled(AVATAR_SIZE, AVATAR_SIZE, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            painter.drawPixmap(x, y, scaled)
        else:
            color = QColor(AVATAR_COLORS[conv_id % len(AVATAR_COLORS)])
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(x, y, AVATAR_SIZE, AVATAR_SIZE)
            # Initials
            initials = "".join(w[0] for w in (name or "#").split()[:2] if w and w[0].isalpha()) or "#"
            painter.setPen(QColor("white"))
            painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
            painter.drawText(QRect(x, y, AVATAR_SIZE, AVATAR_SIZE), Qt.AlignCenter, initials[:2].upper())

        painter.setClipping(False)


class _BulkFilteredDownloadWorker(QThread):
    """Downloads all currently filtered downloadable media across many chats."""

    progress = Signal(int, int, str)
    finished = Signal(int, int, int)

    def __init__(self, conv_ids: list[int], db_path: str, type_filter: str = ""):
        super().__init__()
        self._conv_ids = [int(c) for c in conv_ids if c is not None]
        self._db_path = db_path
        self._type_filter = type_filter or ""
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        import os
        import sqlite3 as _sqlite3
        import time as _time_mod

        if not self._conv_ids:
            self.finished.emit(0, 0, 0)
            return

        conn = _sqlite3.connect(f"file:{self._db_path}?immutable=1", uri=True, check_same_thread=False)
        conn.row_factory = _sqlite3.Row
        write_conn = _sqlite3.connect(self._db_path, check_same_thread=False)

        type_where = ""
        if self._type_filter == "image":
            type_where = "AND m.type_label IN ('image','view_once_image') "
        elif self._type_filter == "video":
            type_where = "AND m.type_label IN ('video','gif','animated_gif','view_once_video') "
        elif self._type_filter == "audio":
            type_where = "AND m.type_label IN ('audio','voice','view_once_voice') "
        elif self._type_filter == "document":
            type_where = "AND m.type_label = 'document' "
        elif self._type_filter == "sticker":
            type_where = "AND m.type_label = 'sticker' "
        elif self._type_filter == "view_once":
            type_where = "AND m.type_label IN ('view_once_image','view_once_video','view_once_voice') "

        placeholders = ",".join("?" for _ in self._conv_ids)
        rows = conn.execute(
            "SELECT me.id, me.media_url, me.media_key, me.mime_type, "
            "me.file_hash, m.id as message_id, COALESCE(m.type_label, '') as type_label, "
            "COALESCE(m.source_key_id, '') as source_key_id "
            "FROM media me "
            "JOIN message m ON m.id = me.message_id "
            f"WHERE m.conversation_id IN ({placeholders}) "
            f"{type_where}"
            f"AND {DOWNLOADABLE_EXPR} "
            "ORDER BY m.conversation_id, m.timestamp DESC",
            tuple(self._conv_ids),
        ).fetchall()

        total = len(rows)
        if total == 0:
            self.finished.emit(0, 0, 0)
            conn.close()
            write_conn.close()
            return

        from app.services.media_crypto import (
            download_and_decrypt, get_media_type, get_extension_for_mime,
        )

        from app.services.case_manager import CaseManager
        _cm = CaseManager.get()
        if _cm.is_open and _cm.recovered_media_dir:
            save_dir = str(_cm.recovered_media_dir)
        else:
            save_dir = os.path.join(os.path.dirname(str(Database.get().path)), "recovered_media"
        )
        os.makedirs(save_dir, exist_ok=True)

        downloaded = 0
        failed = 0
        skipped = 0

        # Evidence DB audit logging
        try:
            from app.db.media_evidence_db import MediaEvidenceDB
        except ImportError:
            import importlib.util
            from pathlib import Path as _P
            for _p in _P(__file__).resolve().parents:
                _c = _p / "backend" / "app" / "db" / "media_evidence_db.py"
                if _c.is_file():
                    _s = importlib.util.spec_from_file_location("media_evidence_db", str(_c))
                    _m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)
                    MediaEvidenceDB = _m.MediaEvidenceDB; break
            else:
                class MediaEvidenceDB:
                    _instance = None
                    @classmethod
                    def get(cls): return cls()
                    is_open = False
                    def log_download(self, *a, **kw): pass
                    def update_registry_status(self, *a, **kw): pass
        evidence_db = MediaEvidenceDB.get()

        for i, row in enumerate(rows):
            if self._cancelled:
                break

            media_id = row[0]
            url = row[1]
            key = row[2]
            mime_type = row[3] or ""
            file_hash = row[4]
            message_id = row[5]
            type_label = row[6]

            ext = get_extension_for_mime(mime_type) if mime_type else ".bin"
            save_path = os.path.join(save_dir, f"Recovered_msg_media_{message_id}_{int(_time_mod.time())}{ext}")
            source_key_id = row[7] if len(row) > 7 else ""

            media_type = get_media_type(type_label, mime_type)
            self.progress.emit(i + 1, total, f"Downloading: msg_{message_id}{ext}")

            # Audit: download_start
            if evidence_db.is_open:
                evidence_db.log_download(
                    source_key_id, "download_start", success=True,
                    media_url=url, expected_hash=file_hash,
                    notes=f"bulk_recovery mime={mime_type} media_id={media_id}")

            try:
                recovery_ts = int(_time_mod.time() * 1000)
                download_and_decrypt(
                    url=url, media_key=key, media_type=media_type,
                    file_hash=file_hash, save_path=save_path, timeout=20,
                )

                # Audit: download_success
                _sz = os.path.getsize(save_path) if os.path.exists(save_path) else 0
                if evidence_db.is_open:
                    evidence_db.log_download(
                        source_key_id, "download_success", success=True,
                        media_url=url, expected_hash=file_hash,
                        saved_path=save_path, saved_size=_sz)
                    evidence_db.update_registry_status(
                        source_key_id, recovery_method="downloaded",
                        recovery_timestamp=recovery_ts,
                        recovered_file_path=save_path, is_on_disk=1)

                write_conn.execute(
                    "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                    "recovery_method = 'downloaded', recovery_timestamp = ? WHERE id = ?",
                    (save_path, recovery_ts, media_id),
                )
                if file_hash:
                    write_conn.execute(
                        "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                        "recovery_method = 'hash_linked', recovery_timestamp = ? "
                        "WHERE file_hash = ? AND id != ? AND (file_exists = 0 OR file_exists IS NULL)",
                        (save_path, recovery_ts, file_hash, media_id),
                    )
                write_conn.commit()
                downloaded += 1
            except Exception as e:
                failed += 1
                err_msg = f"{type(e).__name__}: {e}"
                self.progress.emit(i + 1, total, f"Failed: {err_msg}")
                # Audit: download_fail
                if evidence_db.is_open:
                    evidence_db.log_download(
                        source_key_id, "download_fail", success=False,
                        error_message=err_msg, media_url=url,
                        expected_hash=file_hash)
                # Mark as download_failed so it doesn't keep showing as downloadable
                try:
                    write_conn.execute(
                        "UPDATE media SET media_status = 'download_failed' WHERE id = ?",
                        (media_id,),
                    )
                    write_conn.commit()
                except Exception:
                    pass

        try:
            write_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        conn.close()
        write_conn.close()
        try:
            Database.get().reconnect_read()
        except Exception:
            pass
        self.finished.emit(downloaded, failed, skipped)


# ── Main Page ──

class MediaRecoveryPage(QWidget):
    """Standalone sidebar page: per-conversation media recovery stats + batch download."""

    conversation_selected = Signal(int, str)  # conv_id, display_name

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_conv_id: int | None = None
        self._current_conv_name: str = ""
        self._dl_worker: QThread | None = None
        self._global_dl_worker: QThread | None = None
        self._scope_counts_label: QLabel | None = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 8)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("Media Recovery")
        title.setFont(QFont("Segoe UI", 18, QFont.Bold))
        hdr.addWidget(title)
        hdr.addStretch()

        self._global_stats_label = QLabel("")
        self._global_stats_label.setStyleSheet("font-size: 12px; color: #667781;")
        hdr.addWidget(self._global_stats_label)

        # Global download button (at the top, not per-chat)
        self._global_dl_btn = QPushButton("Download All Matching")
        self._global_dl_btn.setEnabled(False)
        self._global_dl_btn.setFixedHeight(32)
        self._global_dl_btn.setStyleSheet("""
            QPushButton { background: #1565c0; color: white; border: none;
                          padding: 4px 16px; border-radius: 6px; font-weight: 600; font-size: 11px; }
            QPushButton:hover { background: #0d47a1; }
            QPushButton:disabled { background: #999; }
        """)
        self._global_dl_btn.clicked.connect(self._on_download_all_matching)
        hdr.addWidget(self._global_dl_btn)
        layout.addLayout(hdr)

        self._scope_counts_label = QLabel("")
        self._scope_counts_label.setStyleSheet("font-size: 11px; color: #667781; margin-bottom: 4px;")
        self._scope_counts_label.setTextFormat(Qt.RichText)
        layout.addWidget(self._scope_counts_label)

        # Legend bar
        legend = QHBoxLayout()
        legend.setSpacing(16)
        for color, label in [("#4caf50", "On Disk"), ("#1565c0", "Downloadable"),
                              ("#ff9800", "Expired"), ("#e65100", "Missing/No Key")]:
            dot = QLabel(f"<span style='color:{color};font-size:14px'>\u25CF</span> {label}")
            dot.setTextFormat(Qt.RichText)
            dot.setStyleSheet("font-size: 11px;")
            legend.addWidget(dot)
        legend.addStretch()
        layout.addLayout(legend)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)

        self._chat_filter_edit = QLineEdit()
        self._chat_filter_edit.setPlaceholderText("Filter chats like Excel: name, type, or JID")
        self._chat_filter_edit.textChanged.connect(self._on_chat_filter_changed)
        search_row.addWidget(self._chat_filter_edit, 2)

        self._chat_scope = QComboBox()
        self._chat_scope.addItems([
            "All Chats", "Personal", "Group", "Community", "Broadcast", "Newsletter", "Status",
        ])
        self._chat_scope.currentIndexChanged.connect(self._on_chat_scope_changed)
        search_row.addWidget(self._chat_scope, 1)

        self._sender_scope = QComboBox()
        self._sender_scope.addItems(["All Senders", "My Media", "Others"])
        self._sender_scope.currentIndexChanged.connect(self._on_sender_filter_changed)
        search_row.addWidget(self._sender_scope, 1)

        self._sender_filter_edit = QLineEdit()
        self._sender_filter_edit.setPlaceholderText("Filter selected chat by sender name")
        self._sender_filter_edit.textChanged.connect(self._on_sender_filter_changed)
        search_row.addWidget(self._sender_filter_edit, 2)

        layout.addLayout(search_row)

        # Type filter buttons
        filter_row = QHBoxLayout()
        filter_row.setSpacing(4)
        self._filter_btns: list[QPushButton] = []
        for label, tf in [("All", ""), ("Images", "image"), ("Videos", "video"),
                           ("Audio", "audio"), ("Documents", "document"),
                           ("Stickers", "sticker"), ("View-Once", "view_once")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(tf == "")
            btn.setStyleSheet("""
                QPushButton { background: #e0e0e0; border: none; padding: 5px 12px;
                              border-radius: 12px; font-size: 11px; font-weight: 500; }
                QPushButton:checked { background: #008069; color: white; }
                QPushButton:hover { background: #c0c0c0; }
                QPushButton:checked:hover { background: #006b5a; }
            """)
            btn.clicked.connect(lambda checked, t=tf, b=btn: self._on_type_filter(t, b))
            filter_row.addWidget(btn)
            self._filter_btns.append(btn)
        filter_row.addStretch()
        layout.addLayout(filter_row)

        # Splitter: left (conversation list) + right (detail)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)

        # LEFT: Conversation table
        self._model = _RecoveryModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setItemDelegate(_ConvDelegate())
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(64)
        self._table.setAlternatingRowColors(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setStyleSheet("""
            QTableView { border: 1px solid #e0e0e0; border-radius: 8px; background: white; }
            QTableView::item:selected { background: #e8f5e9; }
        """)
        self._table.clicked.connect(self._on_row_clicked)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        splitter.addWidget(self._table)

        # RIGHT: Detail panel
        self._detail = QFrame()
        self._detail.setStyleSheet("QFrame { background: white; border: 1px solid #e0e0e0; border-radius: 8px; }")
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(16, 12, 16, 12)

        self._detail_title = QLabel("Select a conversation")
        self._detail_title.setFont(QFont("Segoe UI", 14, QFont.Bold))
        detail_layout.addWidget(self._detail_title)

        self._detail_stats = QLabel("")
        self._detail_stats.setWordWrap(True)
        self._detail_stats.setTextFormat(Qt.RichText)
        detail_layout.addWidget(self._detail_stats)

        self._detail_hint = QLabel("Use the sender filters above to narrow this chat like a spreadsheet.")
        self._detail_hint.setStyleSheet("font-size: 11px; color: #667781;")
        detail_layout.addWidget(self._detail_hint)

        # Download button (per-chat)
        self._dl_btn = QPushButton("Download All for This Chat")
        self._dl_btn.setEnabled(False)
        self._dl_btn.setStyleSheet("""
            QPushButton { background: #008069; color: white; border: none;
                          padding: 10px 20px; border-radius: 6px; font-weight: 600; font-size: 13px; }
            QPushButton:hover { background: #006b5a; }
            QPushButton:disabled { background: #999; }
        """)
        self._dl_btn.clicked.connect(self._on_download_click)
        detail_layout.addWidget(self._dl_btn)

        # Progress
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setFixedHeight(16)
        self._progress.setStyleSheet("""
            QProgressBar { border: 1px solid #ccc; border-radius: 4px; text-align: center; font-size: 10px; }
            QProgressBar::chunk { background: #008069; border-radius: 3px; }
        """)
        detail_layout.addWidget(self._progress)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("font-size: 10px; color: #667781;")
        self._progress_label.setVisible(False)
        detail_layout.addWidget(self._progress_label)

        detail_layout.addStretch()
        splitter.addWidget(self._detail)

        splitter.setSizes([550, 400])
        layout.addWidget(splitter, 1)

    def showEvent(self, event):
        super().showEvent(event)
        try:
            self._load_data()
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[MediaRecovery] showEvent error: {e}")

    def _load_data(self):
        self._model.reload()
        self._update_global_stats()

    def _update_scope_counts(self):
        if not self._scope_counts_label:
            return
        try:
            db = Database.get()
        except RuntimeError:
            self._scope_counts_label.setText("")
            return

        type_where = _type_where_for_filter(self._model._type_filter)
        base_from = (
            "FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "JOIN conversation c ON c.id = m.conversation_id "
            f"WHERE 1=1 {type_where} "
        )

        overall = db.scalar(f"SELECT COUNT(*) {base_from} AND {DOWNLOADABLE_EXPR}") or 0
        groups = db.scalar(
            f"SELECT COUNT(*) {base_from} AND LOWER(COALESCE(c.chat_type, '')) = 'group' "
            f"AND {DOWNLOADABLE_EXPR}"
        ) or 0
        personal = db.scalar(
            f"SELECT COUNT(*) {base_from} AND LOWER(COALESCE(c.chat_type, '')) = 'personal' "
            f"AND {DOWNLOADABLE_EXPR}"
        ) or 0

        self._scope_counts_label.setText(
            f"<span style='color:#1565c0'>{groups:,} downloadable by groups</span> | "
            f"<span style='color:#1565c0'>{personal:,} downloadable by personal</span> | "
            f"<span style='color:#1565c0'>{overall:,} downloadable overall</span>"
        )

    def _update_global_stats(self):
        total = sum(r[3] for r in self._model._data) if self._model._data else 0
        on_disk = sum(r[4] for r in self._model._data) if self._model._data else 0
        downloadable = sum(r[5] for r in self._model._data) if self._model._data else 0
        expired = sum(r[6] for r in self._model._data) if self._model._data else 0
        no_key = sum(r[7] for r in self._model._data) if self._model._data else 0
        convs = len(self._model._data)
        self._global_stats_label.setText(
            f"{convs:,} chats | {total:,} media | "
            f"<span style='color:#4caf50'>{on_disk:,} on disk</span> | "
            f"<span style='color:#1565c0'>{downloadable:,} downloadable</span> | "
            f"<span style='color:#ff9800'>{expired:,} expired</span> | "
            f"<span style='color:#e65100'>{no_key:,} missing</span>"
        )
        self._global_stats_label.setTextFormat(Qt.RichText)
        self._global_dl_btn.setEnabled(downloadable > 0)
        self._global_dl_btn.setText(
            f"Download All Matching ({downloadable:,})" if downloadable > 0
            else "No Downloadable Media in Current Filter"
        )
        self._update_scope_counts()

    def _on_type_filter(self, type_label: str, btn: QPushButton):
        for b in self._filter_btns:
            b.setChecked(b is btn)
        self._model.set_type_filter(type_label)
        self._update_global_stats()
        self._refresh_current_detail()

    def _on_chat_filter_changed(self, text: str):
        self._model.set_chat_filter(text)
        self._update_global_stats()
        self._clear_selection_if_filtered_out()

    def _on_chat_scope_changed(self, *_args):
        scope_map = {
            "All Chats": "",
            "Personal": "personal",
            "Group": "group",
            "Community": "community",
            "Broadcast": "broadcast",
            "Newsletter": "newsletter",
            "Status": "status",
        }
        self._model.set_chat_scope(scope_map.get(self._chat_scope.currentText(), ""))
        self._update_global_stats()
        self._clear_selection_if_filtered_out()

    def _on_sender_filter_changed(self, *_args):
        self._refresh_current_detail()

    def _on_row_clicked(self, index: QModelIndex):
        row = index.row()
        if row < 0 or row >= len(self._model._data):
            return
        row_data = self._model._data[row]
        conv_id = row_data[8]
        chat_type = row_data[2] or ""
        name = row_data[1] or ("Status Updates" if chat_type == "broadcast" else f"Chat #{conv_id}")
        self._current_conv_id = conv_id
        self._current_conv_name = name
        self._load_detail(conv_id, name)

    def _on_row_double_clicked(self, index: QModelIndex):
        row = index.row()
        if row < 0 or row >= len(self._model._data):
            return
        row_data = self._model._data[row]
        conv_id = row_data[8]
        chat_type = row_data[2] or ""
        name = row_data[1] or ("Status Updates" if chat_type == "broadcast" else f"Chat #{conv_id}")
        self.conversation_selected.emit(conv_id, name)

    def _sender_where_clause(self) -> tuple[str, list[object]]:
        scope = self._sender_scope.currentText()
        sender_filter = (self._sender_filter_edit.text() or "").strip().lower()
        parts: list[str] = []
        params: list[object] = []

        if scope == "My Media":
            parts.append("m.from_me = 1")
        elif scope == "Others":
            parts.append("m.from_me = 0")

        if sender_filter:
            parts.append(
                "LOWER(COALESCE("
                "CASE WHEN m.from_me = 1 THEN 'You' END, "
                "snd.resolved_name, snd.wa_name, snd.phone_number, m.rendered_sender, ''"
                ")) LIKE ?"
            )
            params.append(f"%{sender_filter}%")

        return (" AND " + " AND ".join(parts), params) if parts else ("", [])

    def _refresh_current_detail(self):
        if self._current_conv_id:
            self._load_detail(self._current_conv_id, self._current_conv_name or self._detail_title.text())

    def _clear_selection_if_filtered_out(self):
        if not self._current_conv_id:
            return

        still_visible = any(
            len(row) > 8 and row[8] == self._current_conv_id
            for row in self._model._data
        )
        if still_visible:
            self._refresh_current_detail()
            return

        self._current_conv_id = None
        self._current_conv_name = ""
        self._table.clearSelection()
        self._detail_title.setText("Select a conversation")
        self._detail_stats.setText("")
        self._dl_btn.setEnabled(False)
        self._dl_btn.setText("Download All for This Chat")

    def _load_detail(self, conv_id: int, name: str):
        self._detail_title.setText(name)
        self._current_conv_name = name
        db = Database.get()
        sender_where, sender_params = self._sender_where_clause()

        # Type breakdown
        rows = db.fetchall(
            "SELECT m.type_label, COUNT(*) as cnt, "
            f"  SUM(CASE WHEN {ON_DISK_EXPR} THEN 1 ELSE 0 END) as on_disk, "
            f"  SUM(CASE WHEN {DOWNLOADABLE_EXPR} THEN 1 ELSE 0 END) as downloadable, "
            f"  SUM(CASE WHEN {EXPIRED_EXPR} THEN 1 ELSE 0 END) as expired, "
            f"  SUM(CASE WHEN {NO_KEY_EXPR} THEN 1 ELSE 0 END) as no_key "
            "FROM media me JOIN message m ON m.id = me.message_id "
            "LEFT JOIN contact snd ON snd.id = m.sender_id "
            "WHERE m.conversation_id = ? "
            f"{sender_where} "
            "GROUP BY m.type_label ORDER BY cnt DESC",
            (conv_id, *sender_params),
        )

        html = "<table cellspacing='4' style='font-size: 12px; margin-top: 8px;'>"
        html += ("<tr style='font-weight:700;border-bottom:1px solid #ccc'>"
                 "<td>Type</td><td style='text-align:right'>Total</td>"
                 "<td style='text-align:right;color:#4caf50'>Disk</td>"
                 "<td style='text-align:right;color:#1565c0'>DL</td>"
                 "<td style='text-align:right;color:#ff9800'>Expired</td>"
                 "<td style='text-align:right;color:#e65100'>Missing</td></tr>")

        total_dl = 0
        total_expired = 0
        for r in rows:
            tl = r[0] or "unknown"
            icon = TYPE_ICONS.get(tl, "\U0001F4CE")
            html += (f"<tr><td>{icon} {tl}</td>"
                     f"<td style='text-align:right;font-weight:600'>{r[1]:,}</td>"
                     f"<td style='text-align:right;color:#4caf50'>{r[2]:,}</td>"
                     f"<td style='text-align:right;color:#1565c0'>{r[3]:,}</td>"
                     f"<td style='text-align:right;color:#ff9800'>{r[4]:,}</td>"
                     f"<td style='text-align:right;color:#e65100'>{r[5]:,}</td></tr>")
            total_dl += r[3]
            total_expired += r[4]

        html += "</table>"
        if total_expired > 0:
            html += (
                f"<div style='margin-top:8px;color:#667781;font-size:11px'>"
                f"Filtered view includes <span style='color:#ff9800;font-weight:600'>{total_expired:,}</span> "
                f"expired items that cannot be downloaded.</div>"
            )
        self._detail_stats.setText(html)

        self._dl_btn.setEnabled(total_dl > 0)
        self._dl_btn.setText(
            f"Download {total_dl:,} Files" if total_dl > 0
            else "No downloadable media"
        )

    def _on_download_click(self):
        if not self._current_conv_id:
            return

        from app.views.pages.chat_viewer_page import BulkMediaDownloadWorker

        db = Database.get()
        sender_where, sender_params = self._sender_where_clause()
        missing = db.scalar(
            "SELECT COUNT(*) FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "LEFT JOIN contact snd ON snd.id = m.sender_id "
            "WHERE m.conversation_id = ? "
            f"{sender_where} "
            f"AND {DOWNLOADABLE_EXPR}",
            (self._current_conv_id, *sender_params),
        ) or 0

        if missing == 0:
            QMessageBox.information(self, "Nothing to Download", "No downloadable media for this conversation.")
            return

        reply = QMessageBox.question(
            self, "Download Media",
            f"Download and decrypt {missing:,} media files?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self._dl_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setMaximum(missing)
        self._progress.setValue(0)
        self._progress_label.setVisible(True)

        sender_mode = {
            "All Senders": "all",
            "My Media": "mine",
            "Others": "others",
        }.get(self._sender_scope.currentText(), "all")
        self._dl_worker = BulkMediaDownloadWorker(
            self._current_conv_id,
            str(db.path),
            sender_mode=sender_mode,
            sender_filter=self._sender_filter_edit.text(),
        )
        self._dl_worker.progress.connect(self._on_dl_progress)
        self._dl_worker.finished.connect(self._on_dl_finished)
        self._dl_worker.start()

    def _on_download_all_matching(self):
        conv_ids = [row[8] for row in self._model._data if len(row) > 8]
        total_dl = sum(row[5] for row in self._model._data) if self._model._data else 0
        if not conv_ids or total_dl <= 0:
            QMessageBox.information(
                self,
                "Nothing to Download",
                "There are no downloadable media files in the current filter.",
            )
            return

        scope_label = self._chat_scope.currentText()
        type_label = next((b.text() for b in self._filter_btns if b.isChecked()), "All")
        reply = QMessageBox.question(
            self,
            "Download All Matching",
            f"Download and decrypt {total_dl:,} media files for:\n"
            f"Scope: {scope_label}\nType: {type_label}\n"
            f"Matching chats: {len(conv_ids):,}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        db = Database.get()
        self._global_dl_btn.setEnabled(False)
        self._dl_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setMaximum(total_dl)
        self._progress.setValue(0)
        self._progress_label.setVisible(True)
        self._progress_label.setText("Preparing bulk download...")

        self._global_dl_worker = _BulkFilteredDownloadWorker(
            conv_ids,
            str(db.path),
            type_filter=self._model._type_filter,
        )
        self._global_dl_worker.progress.connect(self._on_dl_progress)
        self._global_dl_worker.finished.connect(self._on_dl_finished)
        self._global_dl_worker.start()

    def _on_dl_progress(self, current: int, total: int, status: str):
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._progress_label.setText(f"[{current}/{total}] {status}")

    def _on_dl_finished(self, downloaded: int, failed: int, skipped: int):
        self._progress.setVisible(False)
        self._progress_label.setText(f"Done: {downloaded} downloaded, {failed} failed, {skipped} skipped")
        self._dl_btn.setEnabled(True)

        # Checkpoint WAL + reconnect
        try:
            db = Database.get()
            db.checkpoint_and_reconnect()
        except Exception:
            pass

        # Refresh data
        self._load_data()
        if self._current_conv_id:
            name = self._detail_title.text()
            self._load_detail(self._current_conv_id, name)

        QMessageBox.information(self, "Download Complete",
                                f"Downloaded: {downloaded:,}\nFailed: {failed:,}\nSkipped: {skipped:,}")
