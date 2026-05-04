"""
Orphaned Media page — browse media files on disk that aren't
linked to any surviving message.

These files exist in the WhatsApp media folder but have no
corresponding record in ``msgstore.db``: cleared chats,
reinstalled WhatsApp, deleted conversations, alternate-app
usage (GBWhatsApp etc.).  Send / receive timestamps are parsed
from WhatsApp's own filename convention.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import Counter
from datetime import datetime

from PySide6.QtCore import QModelIndex, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QTableView,
    QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.services.theme_manager import ThemeManager

logger = logging.getLogger(__name__)

# Roles
THUMB_ROLE = Qt.UserRole + 500
INFO_ROLE = Qt.UserRole + 501


class OrphanedMediaModel(BaseLazyTableModel):
    # Columns: a leading "Match" indicator column makes hash-matched
    # rows visible at a glance — click the "Hash Matched" stats card
    # filters down to just these for the analyst.
    _columns = [
        ("matched_message_id", "Match"),
        ("file_name", "Name"),
        ("matched_conv_name", "Matched Chat"),
        ("folder", "Folder"),
        ("mime_type", "Type"),
        ("file_size", "Size"),
        ("parsed_date", "Date"),
    ]

    _base_sql = """
        SELECT om.file_name, om.folder, om.mime_type, om.file_size,
               om.parsed_date, om.id, om.thumbnail_blob, om.file_path,
               om.width, om.height, om.file_hash,
               om.matched_message_id, om.matched_conversation_id,
               om.matched_conv_name, om.source_type
        FROM orphaned_media om
    """
    _count_sql = "SELECT COUNT(*) FROM orphaned_media om"
    _default_order = "om.parsed_date_ts DESC"

    # In-process thumbnail cache for the gallery view.  Orphaned-media
    # rows don't carry an embedded thumbnail (the ingester comment is
    # "generated on-demand in GUI for speed" — this dict IS that
    # cache), so the first DecorationRole lookup decodes the file from
    # disk and stores a 120-px pixmap; subsequent lookups are O(1).
    # Keyed on row id.
    _thumb_cache: dict[int, "QPixmap"] = {}
    _THUMB_CACHE_MAX = 4096   # ≈40 MB at 120×120 RGBA

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._data):
            return None
        row = self._data[index.row()]
        col = index.column()
        # Row data layout (from _base_sql):
        #   row[0]=file_name, [1]=folder, [2]=mime, [3]=size, [4]=date,
        #   [5]=id, [6]=thumb_blob, [7]=file_path, [8]=w, [9]=h,
        #   [10]=file_hash, [11]=matched_msg_id, [12]=matched_conv_id,
        #   [13]=matched_conv_name, [14]=source_type

        if role == Qt.DisplayRole:
            if col == 0:
                # Match indicator: link emoji when SHA-256 matched a
                # message in the case, blank otherwise
                return "\U0001F517" if row[11] else ""
            if col == 1:
                return row[0] or ""
            if col == 2:
                # Matched chat name (only meaningful if matched)
                return row[13] if row[11] else ""
            if col == 3:
                return row[1] or ""
            if col == 4:
                mime = row[2] or ""
                return mime.split("/")[0] if "/" in mime else mime
            if col == 5:
                sz = row[3] or 0
                if sz > 1048576:
                    return f"{sz / 1048576:.1f} MB"
                if sz > 1024:
                    return f"{sz / 1024:.0f} KB"
                return f"{sz} B"
            if col == 6:
                return row[4] or "Unknown"

        if role == Qt.ToolTipRole:
            if col == 0 and row[11]:
                return (
                    f"Hash-matched to a message in "
                    f"{row[13] or 'chat ID ' + str(row[12])} "
                    f"(msg {row[11]}). Double-click to jump."
                )
            if col == 2 and row[11]:
                return f"Matched chat — msg {row[11]}"

        if role == Qt.ForegroundRole and col == 0 and row[11]:
            # Make the link emoji column subtly highlighted in light
            # purple so matched rows are obvious in long lists.
            return QColor(123, 31, 162)

        if role == THUMB_ROLE:
            return row[6] if len(row) > 6 else None

        # DecorationRole on the Name column (col 1) provides the
        # gallery-view thumbnail.  We try the embedded blob first
        # (rare but free), then fall back to decoding the file on
        # disk, with results cached in OrphanedMediaModel._thumb_cache.
        if role == Qt.DecorationRole and col == 1:
            return self._get_or_make_thumb(row)

        if role == INFO_ROLE:
            return {
                "file_name": row[0], "folder": row[1], "mime_type": row[2],
                "file_size": row[3], "parsed_date": row[4], "id": row[5],
                "file_path": row[7] if len(row) > 7 else "",
                "width": row[8] if len(row) > 8 else 0,
                "height": row[9] if len(row) > 9 else 0,
                "file_hash": row[10] if len(row) > 10 else "",
                "matched_message_id": row[11] if len(row) > 11 else None,
                "matched_conversation_id": row[12] if len(row) > 12 else None,
                "matched_conv_name": row[13] if len(row) > 13 else "",
                "source_type": row[14] if len(row) > 14 else "",
            }

        if role == Qt.UserRole:
            return row[5] if len(row) > 5 else 0

        return None

    def _get_or_make_thumb(self, row) -> "QPixmap | None":
        """Return a cached 120-px thumbnail for the row, or build one.

        For orphaned media we *don't* have an embedded thumbnail in
        99 % of cases — the ingester deliberately skipped that for
        speed.  So we lazy-generate one from the file on disk on
        first request and cache it.  Non-image rows return None so
        the gallery shows a plain tile instead of a junk pixmap.
        """
        rid = row[5] if len(row) > 5 else 0
        cached = self._thumb_cache.get(rid)
        if cached is not None:
            return cached

        # Try the embedded blob first (rare — only set when the
        # ingester or hash-matcher captured one)
        blob = row[6] if len(row) > 6 else None
        if blob and len(blob) > 50:
            try:
                px = QPixmap()
                if px.loadFromData(bytes(blob)):
                    out = px.scaled(120, 120, Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)
                    self._cache_thumb(rid, out)
                    return out
            except Exception:
                pass

        # Fall back to on-disk decode for image files only
        mime = (row[2] or "").lower()
        fp = row[7] if len(row) > 7 else ""
        if not fp or not mime.startswith("image/"):
            return None
        if not os.path.isfile(fp):
            return None
        try:
            px = QPixmap(fp)
            if px.isNull():
                return None
            out = px.scaled(120, 120, Qt.KeepAspectRatio,
                            Qt.SmoothTransformation)
            self._cache_thumb(rid, out)
            return out
        except Exception:
            return None

    def _cache_thumb(self, rid: int, px: "QPixmap") -> None:
        # Simple LRU-ish eviction — when the cache is full, drop the
        # oldest entry.  Keeps memory bounded under heavy gallery
        # scrolling without paying for a real LRU dict.
        if len(self._thumb_cache) >= self._THUMB_CACHE_MAX:
            try:
                self._thumb_cache.pop(next(iter(self._thumb_cache)))
            except StopIteration:
                pass
        self._thumb_cache[rid] = px

    @classmethod
    def clear_thumb_cache(cls) -> None:
        cls._thumb_cache.clear()


class ScanWorker(QThread):
    """Background scan for orphaned media files."""
    progress = Signal(int, int)
    finished = Signal(int)  # count

    def run(self):
        from pathlib import Path
        db = Database.get()
        # Auto-detect media root from resolved paths
        sample = db.fetchone(
            "SELECT resolved_file_path FROM media "
            "WHERE resolved_file_path IS NOT NULL AND file_exists = 1 LIMIT 1"
        )
        if not sample or not sample[0]:
            self.finished.emit(0)
            return

        p = Path(sample[0])
        media_root = None
        for parent in p.parents:
            if parent.name.lower() == "media":
                media_root = parent
                break

        if not media_root or not media_root.exists():
            self.finished.emit(0)
            return

        import importlib.util
        from pathlib import Path as _P
        # Direct file import to avoid app/ package conflict between gui/ and backend/
        _this = _P(__file__).resolve()
        _ingester_path = None
        for _parent in _this.parents:
            _candidate = _parent / "backend" / "app" / "ingestion" / "orphaned_media_ingester.py"
            if _candidate.is_file():
                _ingester_path = _candidate
                break
        if not _ingester_path:
            self.finished.emit(0)
            return
        spec = importlib.util.spec_from_file_location("orphaned_media_ingester", str(_ingester_path))
        _mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_mod)
        ingest_orphaned_media = _mod.ingest_orphaned_media
        # Use a direct write connection
        import sqlite3
        conn = sqlite3.connect(str(db.path))
        conn.execute("PRAGMA journal_mode=WAL")
        # Ensure source_type column exists (migration for older tables)
        try:
            conn.execute("SELECT source_type FROM orphaned_media LIMIT 0")
        except Exception:
            try:
                conn.execute("ALTER TABLE orphaned_media ADD COLUMN source_type TEXT")
                conn.commit()
            except Exception:
                pass

        class _WriteConn:
            def __init__(self, c):
                self._c = c
            def execute(self, sql, params=()):
                self._c.execute(sql, params)
            def fetchall(self, sql, params=()):
                return self._c.execute(sql, params).fetchall()
            def fetchone(self, sql, params=()):
                return self._c.execute(sql, params).fetchone()
            def commit(self):
                self._c.commit()
            def rollback(self):
                self._c.rollback()
            def begin_transaction(self):
                pass
            def get_cursor(self):
                return self._c.cursor()

        wc = _WriteConn(conn)
        count = ingest_orphaned_media(
            wc, media_root,
            progress_callback=lambda c, t: self.progress.emit(c, t),
        )
        conn.close()
        db.reconnect_read()
        self.finished.emit(count)


class HashMatchWorker(QThread):
    """Background SHA-256 hash computation + DB matching.

    Two outputs:
      1. ``orphaned_media.matched_message_id`` populated  (orphan → msg)
      2. ``media`` rows where ``file_exists = 0`` and the hash matches
         an orphan are RESCUED — their file_exists, resolved_file_path,
         and recovery_method get rewritten to point at the orphan's
         on-disk file.  This is a real forensic recovery path: a chat
         message lost its file (cleared chat / reinstall / WhatsApp
         autoclean) but the same bytes still sit in an orphaned slot,
         and we can serve them to the analyst.
    """
    progress = Signal(int, int)
    finished = Signal(int, int)  # match_count, rescued_count

    def __init__(self, date_from: str = None, date_to: str = None, parent=None):
        super().__init__(parent)
        self._date_from = date_from
        self._date_to = date_to

    def run(self):
        db = Database.get()
        # Get orphaned files needing hashing
        where = "WHERE om.file_hash IS NULL"
        params = []
        if self._date_from:
            where += " AND om.parsed_date >= ?"
            params.append(self._date_from)
        if self._date_to:
            where += " AND om.parsed_date <= ?"
            params.append(self._date_to)

        rows = db.fetchall(
            f"SELECT om.id, om.file_path FROM orphaned_media om {where}",
            tuple(params),
        )
        total = len(rows)

        # Compute hashes
        for i, r in enumerate(rows):
            oid, fp = r[0], r[1]
            try:
                h = hashlib.sha256()
                with open(fp, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                import base64
                file_hash = base64.b64encode(h.digest()).decode("ascii")
                db.execute_write(
                    "UPDATE orphaned_media SET file_hash = ? WHERE id = ?",
                    (file_hash, oid),
                )
            except Exception:
                pass
            if (i + 1) % 100 == 0:
                self.progress.emit(i + 1, total)

        # Match orphans BACK to messages — link each orphan to a
        # representative message_id / conversation_id with the same hash.
        match_count = 0
        rescued_count = 0
        try:
            db.execute_write("""
                UPDATE orphaned_media SET
                    matched_message_id = (
                        SELECT me.message_id FROM media me
                        WHERE me.file_hash = orphaned_media.file_hash
                        AND me.file_hash IS NOT NULL LIMIT 1
                    ),
                    matched_conversation_id = (
                        SELECT m.conversation_id FROM media me
                        JOIN message m ON m.id = me.message_id
                        WHERE me.file_hash = orphaned_media.file_hash
                        AND me.file_hash IS NOT NULL LIMIT 1
                    ),
                    matched_conv_name = (
                        SELECT COALESCE(cv.display_name, cv.jid_raw_string)
                        FROM media me
                        JOIN message m ON m.id = me.message_id
                        JOIN conversation cv ON cv.id = m.conversation_id
                        WHERE me.file_hash = orphaned_media.file_hash
                        AND me.file_hash IS NOT NULL LIMIT 1
                    )
                WHERE file_hash IS NOT NULL
                AND file_hash IN (SELECT file_hash FROM media WHERE file_hash IS NOT NULL)
            """)
            match_count = db.scalar(
                "SELECT COUNT(*) FROM orphaned_media WHERE matched_message_id IS NOT NULL"
            ) or 0
        except Exception as e:
            logger.error("Hash match failed: %s", e)

        # ---- MEDIA-TO-MEDIA AUTO-HASH-LINK (REPAIR) ----
        # If ingestion's ``_auto_hash_link_pass`` was skipped (older
        # code version) or failed silently for a case, missing
        # message rows that have a SHA-256-identical sibling
        # remain unlinked.  This pass repairs those cases so the
        # analyst doesn't have to re-ingest from scratch — same
        # SQL the ingester uses, split into the two forensically
        # distinct outcomes.
        try:
            now_ts2 = int(datetime.now().timestamp() * 1000)
            db.execute_write(
                """
                UPDATE media
                   SET file_exists = 1,
                       resolved_file_path = (
                           SELECT donor.resolved_file_path FROM media donor
                            WHERE donor.file_hash = media.file_hash
                              AND donor.file_exists = 1
                              AND donor.resolved_file_path IS NOT NULL
                              AND donor.id != media.id
                            ORDER BY donor.id ASC LIMIT 1
                       ),
                       recovery_method = 'hash_linked_after_delete',
                       recovery_timestamp = ?,
                       media_status = 'on_disk'
                 WHERE file_exists = 0
                   AND was_transferred = 1
                   AND file_hash IS NOT NULL AND file_hash != ''
                   AND (recovery_method IS NULL OR recovery_method = '')
                   AND EXISTS (
                       SELECT 1 FROM media donor
                        WHERE donor.file_hash = media.file_hash
                          AND donor.file_exists = 1
                          AND donor.resolved_file_path IS NOT NULL
                          AND donor.id != media.id
                   )
                """,
                (now_ts2,),
            )
            db.execute_write(
                """
                UPDATE media
                   SET file_exists = 1,
                       resolved_file_path = (
                           SELECT donor.resolved_file_path FROM media donor
                            WHERE donor.file_hash = media.file_hash
                              AND donor.file_exists = 1
                              AND donor.resolved_file_path IS NOT NULL
                              AND donor.id != media.id
                            ORDER BY donor.id ASC LIMIT 1
                       ),
                       recovery_method = 'hash_linked',
                       recovery_timestamp = ?,
                       media_status = 'on_disk'
                 WHERE file_exists = 0
                   AND (was_transferred = 0 OR was_transferred IS NULL)
                   AND file_hash IS NOT NULL AND file_hash != ''
                   AND (recovery_method IS NULL OR recovery_method = '')
                   AND EXISTS (
                       SELECT 1 FROM media donor
                        WHERE donor.file_hash = media.file_hash
                          AND donor.file_exists = 1
                          AND donor.resolved_file_path IS NOT NULL
                          AND donor.id != media.id
                   )
                """,
                (now_ts2,),
            )
            hl_total = db.scalar(
                "SELECT COUNT(*) FROM media WHERE recovery_method "
                "IN ('hash_linked', 'hash_linked_after_delete')"
            ) or 0
            logger.info(
                "Media-to-media auto-hash-link repair: %d total rows "
                "now hash_linked / hash_linked_after_delete",
                hl_total,
            )
        except Exception as e:
            logger.error("Media-to-media hash-link repair failed: %s", e)

        # ---- ORPHAN-RESCUE PASS ----
        # For every media row that's currently "missing" (file_exists=0)
        # but whose SHA-256 matches an orphaned file on disk, point
        # resolved_file_path at the orphan and flip file_exists=1.
        # Tag with recovery_method='orphan_recovered' so the chat
        # viewer / forensic info panel / gallery / report can all
        # surface this as "Recovered from orphaned file" — distinct
        # from hash_linked (sibling message has it) and hash_linked_after_delete
        # (originally received, deleted, sibling has it).
        #
        # was_transferred is preserved if previously set; if it was 1
        # (file was on the phone at extraction per msgstore) we know
        # for sure this is a "received then file gone" scenario and
        # the orphan is the recovered evidence.
        try:
            now_ts = int(datetime.now().timestamp() * 1000)
            db.execute_write(
                """
                UPDATE media SET
                    file_exists = 1,
                    resolved_file_path = (
                        SELECT om.file_path FROM orphaned_media om
                        WHERE om.file_hash = media.file_hash
                          AND om.file_hash IS NOT NULL
                          AND om.file_hash != ''
                        ORDER BY om.id ASC
                        LIMIT 1
                    ),
                    recovery_method = 'orphan_recovered',
                    recovery_timestamp = ?,
                    media_status = 'on_disk'
                WHERE file_exists = 0
                  AND file_hash IS NOT NULL
                  AND file_hash != ''
                  AND (recovery_method IS NULL OR recovery_method = '')
                  AND EXISTS (
                    SELECT 1 FROM orphaned_media om
                    WHERE om.file_hash = media.file_hash
                      AND om.file_hash IS NOT NULL
                      AND om.file_hash != ''
                  )
                """,
                (now_ts,),
            )
            rescued_count = db.scalar(
                "SELECT COUNT(*) FROM media WHERE recovery_method = 'orphan_recovered'"
            ) or 0
            if rescued_count:
                logger.info(
                    "Orphan-rescue pass: %d missing media rows now point at "
                    "orphaned files with the same SHA-256",
                    rescued_count,
                )
        except Exception as e:
            logger.error("Orphan-rescue pass failed: %s", e)

        db.checkpoint_and_reconnect()
        self.progress.emit(total, total)
        self.finished.emit(match_count, rescued_count)


class OrphanedMediaPage(QWidget):
    go_to_chat = Signal(int, int)  # conv_id, msg_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._db = Database.get()
        self._hash_worker: HashMatchWorker | None = None
        self._scan_worker: ScanWorker | None = None
        self._setup_ui()

    def _setup_ui(self):
        _outer = QHBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.setSpacing(0)

        _main = QWidget()
        layout = QVBoxLayout(_main)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        _outer.addWidget(_main, 1)

        tm = ThemeManager.get()
        _lt = tm.is_light
        _accent = "#00897b" if _lt else "#00bcd4"

        # Header
        hdr = QHBoxLayout()
        title = QLabel("Orphaned Media")
        title.setFont(QFont("Segoe UI", 18, QFont.Bold))
        hdr.addWidget(title)
        hdr.addStretch()
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("font-size: 12px; color: #667781;")
        hdr.addWidget(self._stats_label)

        # Scan button (for first-time or re-scan)
        self._scan_btn = QPushButton("\U0001F50D Scan for Orphaned Media")
        self._scan_btn.setFixedHeight(30)
        self._scan_btn.setStyleSheet(f"""
            QPushButton {{ background: {_accent}; color: white; border: none;
                          border-radius: 6px; padding: 4px 14px; font-weight: bold; font-size: 11px; }}
            QPushButton:hover {{ background: #00695c; }}
            QPushButton:disabled {{ background: #999; }}
        """)
        self._scan_btn.clicked.connect(self._start_scan)
        hdr.addWidget(self._scan_btn)
        layout.addLayout(hdr)

        subtitle = QLabel(
            "Media files on disk not linked to any message — "
            "from cleared chats, reinstalled WhatsApp, deleted conversations."
        )
        subtitle.setStyleSheet(f"color: {'#667781' if _lt else '#8696a0'}; font-size: 11px;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Stats cards.  Each card is clickable — clicking jumps
        # the filter combos to the matching subset, so an
        # analyst can drill from "N hash matches" to those rows
        # in a single click.
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self._stat_cards: dict[str, QLabel] = {}
        for key, label, color in [
            ("total", "Total", _accent),
            ("images", "Images", "#66bb6a"),
            ("videos", "Videos", "#42a5f5"),
            ("audio", "Audio", "#ffa726"),
            ("docs", "Documents", "#ab47bc"),
            ("stickers", "Stickers", "#ec407a"),
            ("matched", "Hash Matched", "#ff7043"),
            ("rescued", "Rescued Msgs", "#2e7d32"),
        ]:
            card = QFrame()
            card.setMinimumWidth(110)
            card.setStyleSheet(tm.stat_frame_style() + """
                QFrame { border-radius: 6px; }
                QFrame:hover {
                    background: rgba(0, 188, 212, 0.10);
                    border: 1px solid """ + color + """;
                }
            """)
            card.setCursor(Qt.PointingHandCursor)
            card.setToolTip(f"Click to filter by: {label}")
            cl = QVBoxLayout(card)
            cl.setContentsMargins(8, 4, 8, 4)
            cl.setSpacing(1)
            lbl = QLabel(label)
            lbl.setStyleSheet(tm.stat_label_style())
            cl.addWidget(lbl)
            val = QLabel("...")
            val.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: bold;")
            cl.addWidget(val)
            # Forward clicks to the filter dispatcher
            card.mousePressEvent = lambda _e, k=key: self._on_stat_card_clicked(k)
            stats_row.addWidget(card)
            self._stat_cards[key] = val
        stats_row.addStretch()
        layout.addLayout(stats_row)

        # Filter bar
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)

        self._folder_combo = QComboBox()
        self._folder_combo.setFixedHeight(28)
        self._folder_combo.addItem("All Folders", None)
        self._folder_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(QLabel("Folder:"))
        filter_row.addWidget(self._folder_combo)

        # Source type filter
        self._source_filter = QComboBox()
        self._source_filter.setFixedHeight(28)
        self._source_filter.addItem("All Sources", None)
        for st, label in [("received", "Received"), ("sent", "Sent by Owner"),
                           ("private", "Private/Hidden"), ("status", "Status Saves"),
                           ("gbwhatsapp", "GBWhatsApp"), ("links", "Link Previews")]:
            self._source_filter.addItem(label, st)
        self._source_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._source_filter)

        # Month filter
        self._month_filter = QComboBox()
        self._month_filter.setFixedHeight(28)
        self._month_filter.addItem("All Dates", None)
        self._month_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._month_filter)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search filename...")
        self._search.setFixedHeight(28)
        self._search.setMinimumWidth(150)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self._search, 1)

        self._match_filter = QComboBox()
        self._match_filter.setFixedHeight(28)
        self._match_filter.addItem("All", None)
        self._match_filter.addItem("Hash Matched", "matched")
        self._match_filter.addItem("Unmatched", "unmatched")
        self._match_filter.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self._match_filter)

        # View toggle: Table / Gallery
        self._view_toggle = QPushButton("\U0001F5BC Gallery")
        self._view_toggle.setFixedHeight(28)
        self._view_toggle.setCheckable(True)
        self._view_toggle.setStyleSheet(f"""
            QPushButton {{ background: transparent; border: 1px solid {_accent};
                          border-radius: 6px; padding: 4px 10px; font-size: 10px; color: {_accent}; }}
            QPushButton:checked {{ background: {_accent}; color: white; font-weight: bold; }}
        """)
        self._view_toggle.toggled.connect(self._toggle_view_mode)
        filter_row.addWidget(self._view_toggle)

        # Hash match button
        self._hash_btn = QPushButton("\U0001F517 Run Hash Match")
        self._hash_btn.setFixedHeight(28)
        self._hash_btn.setStyleSheet(f"""
            QPushButton {{ background: {_accent}; color: white; border: none;
                          border-radius: 6px; padding: 4px 12px; font-weight: bold; font-size: 10px; }}
            QPushButton:hover {{ background: #00695c; }}
            QPushButton:disabled {{ background: #999; }}
        """)
        self._hash_btn.clicked.connect(self._start_hash_match)
        filter_row.addWidget(self._hash_btn)

        layout.addLayout(filter_row)

        # Hash progress
        self._hash_progress = QProgressBar()
        self._hash_progress.setFixedHeight(14)
        self._hash_progress.setVisible(False)
        layout.addWidget(self._hash_progress)

        # Model + table
        self._model = OrphanedMediaModel()

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(28)
        hdr_view = self._table.horizontalHeader()
        # Columns: 0=Match (narrow), 1=Name (stretch), 2=Matched Chat,
        #          3=Folder, 4=Type, 5=Size, 6=Date
        hdr_view.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr_view.resizeSection(0, 56)
        hdr_view.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, w in [(2, 160), (3, 140), (4, 80), (5, 80), (6, 100)]:
            hdr_view.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr_view.resizeSection(col, w)
        self._table.clicked.connect(self._on_row_clicked)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self._table, 1)

        # Gallery view (hidden by default)
        self._gallery = QListView()
        self._gallery.setModel(self._model)
        self._gallery.setViewMode(QListView.IconMode)
        self._gallery.setIconSize(QSize(120, 120))
        self._gallery.setGridSize(QSize(140, 150))
        self._gallery.setResizeMode(QListView.Adjust)
        self._gallery.setWrapping(True)
        self._gallery.setFlow(QListView.LeftToRight)
        self._gallery.setMovement(QListView.Static)
        self._gallery.setSelectionMode(QAbstractItemView.SingleSelection)
        self._gallery.setUniformItemSizes(True)
        self._gallery.setSpacing(4)
        self._gallery.clicked.connect(self._on_gallery_clicked)
        self._gallery.setVisible(False)
        layout.addWidget(self._gallery, 1)

        # Detail panel (right side)
        self._detail = _OrphanedDetailPanel()
        self._detail.setVisible(False)
        self._detail.go_to_chat.connect(lambda cid, mid: self.go_to_chat.emit(cid, mid))
        _outer.addWidget(self._detail)

    def showEvent(self, event):
        super().showEvent(event)
        self._load_data()

    def _load_data(self):
        """Load orphaned media data and stats."""
        db = self._db
        try:
            total = db.scalar("SELECT COUNT(*) FROM orphaned_media") or 0
        except Exception:
            total = 0
            self._stats_label.setText("No orphaned media data — run ingestion with media path")
            return

        if total == 0:
            self._stats_label.setText("No orphaned media found")
            return

        self._stats_label.setText(f"{total:,} orphaned files")

        # Stats
        try:
            self._stat_cards["total"].setText(f"{total:,}")
            # Images exclude stickers to avoid double-counting
            img = db.scalar(
                "SELECT COUNT(*) FROM orphaned_media WHERE mime_type LIKE 'image/%' "
                "AND folder NOT LIKE '%Sticker%'"
            ) or 0
            self._stat_cards["images"].setText(f"{img:,}")
            for key, mime_prefix in [("videos", "video"), ("audio", "audio"), ("docs", "application")]:
                cnt = db.scalar(
                    f"SELECT COUNT(*) FROM orphaned_media WHERE mime_type LIKE '{mime_prefix}/%'"
                ) or 0
                self._stat_cards[key].setText(f"{cnt:,}")
            stk = db.scalar(
                "SELECT COUNT(*) FROM orphaned_media WHERE folder LIKE '%Sticker%'"
            ) or 0
            self._stat_cards["stickers"].setText(f"{stk:,}")
            matched = db.scalar(
                "SELECT COUNT(*) FROM orphaned_media WHERE matched_message_id IS NOT NULL"
            ) or 0
            self._stat_cards["matched"].setText(f"{matched:,}")
            # Rescued = messages whose missing file was repaired by
            # pointing at an orphaned file with the same SHA-256.
            try:
                rescued = db.scalar(
                    "SELECT COUNT(*) FROM media WHERE recovery_method = 'orphan_recovered'"
                ) or 0
            except Exception:
                rescued = 0
            self._stat_cards["rescued"].setText(f"{rescued:,}")
        except Exception:
            pass

        # Populate folder filter
        self._folder_combo.blockSignals(True)
        self._folder_combo.clear()
        self._folder_combo.addItem("All Folders", None)
        try:
            folders = db.fetchall(
                "SELECT folder, COUNT(*) FROM orphaned_media GROUP BY folder ORDER BY COUNT(*) DESC"
            )
            for f in folders:
                self._folder_combo.addItem(f"{f[0]} ({f[1]:,})", f[0])
        except Exception:
            pass
        self._folder_combo.blockSignals(False)

        # Populate month filter
        self._month_filter.blockSignals(True)
        self._month_filter.clear()
        self._month_filter.addItem("All Dates", None)
        try:
            months = db.fetchall(
                "SELECT substr(parsed_date, 1, 7) AS month, COUNT(*) "
                "FROM orphaned_media WHERE parsed_date IS NOT NULL "
                "GROUP BY month ORDER BY month DESC"
            )
            for m in months:
                if m[0]:
                    self._month_filter.addItem(f"{m[0]} ({m[1]:,})", m[0])
        except Exception:
            pass
        self._month_filter.blockSignals(False)
        self._folder_combo.blockSignals(False)

        self._apply_filters()

    def _apply_filters(self):
        where_parts = []
        params = []

        folder = self._folder_combo.currentData()
        if folder:
            where_parts.append("om.folder = ?")
            params.append(folder)

        source = self._source_filter.currentData()
        if source:
            where_parts.append("om.source_type = ?")
            params.append(source)

        month = self._month_filter.currentData()
        if month:
            where_parts.append("om.parsed_date LIKE ?")
            params.append(f"{month}%")

        search = self._search.text().strip()
        if search:
            where_parts.append("om.file_name LIKE ?")
            params.append(f"%{search}%")

        match_val = self._match_filter.currentData()
        if match_val == "matched":
            where_parts.append("om.matched_message_id IS NOT NULL")
        elif match_val == "unmatched":
            where_parts.append("om.matched_message_id IS NULL")

        # Type hint set by stats-card clicks (images/videos/audio/docs).
        # Reset by any subsequent change to a non-type filter — i.e.
        # ``_stat_filter_hint`` is single-use; clearing happens after
        # the load so the next user action starts clean.
        type_hint = getattr(self, "_stat_filter_hint", None)
        if type_hint == "images":
            # Images excluding stickers — same logic as the stats card
            # query so the row count matches the displayed result set.
            where_parts.append(
                "om.mime_type LIKE 'image/%' AND om.folder NOT LIKE '%Sticker%'"
            )
        elif type_hint == "videos":
            where_parts.append("om.mime_type LIKE 'video/%'")
        elif type_hint == "audio":
            where_parts.append("om.mime_type LIKE 'audio/%'")
        elif type_hint == "docs":
            where_parts.append("om.mime_type LIKE 'application/%'")
        self._stat_filter_hint = None

        where = " AND ".join(where_parts) if where_parts else ""
        self._model.load(where=where, params=tuple(params))
        # Refresh the count line so the user sees how many rows the
        # filter selected.  Only do this when something is actually
        # filtered — otherwise keep the global "X orphaned files" line.
        if where:
            self._stats_label.setText(
                f"{self._model.total_rows:,} matching "
                f"(of {self._stat_cards['total'].text()} total)"
            )

    def _toggle_view_mode(self, gallery_mode: bool):
        """Switch between table and gallery view."""
        self._table.setVisible(not gallery_mode)
        self._gallery.setVisible(gallery_mode)
        self._view_toggle.setText(
            "\U0001F4CB Table" if gallery_mode else "\U0001F5BC Gallery"
        )
        # Gallery view on a very large dataset is unusable
        # without a filter — the lazy-load batch is bounded but
        # icon-mode lays out everything as it scrolls.  When
        # the user enters gallery without an active filter,
        # suggest one (without forcing — power users can still
        # browse the full set).
        if gallery_mode and self._model.total_rows > 5000 and not self._has_active_filter():
            self._stats_label.setText(
                f"Tip: gallery has {self._model.total_rows:,} tiles — "
                f"click a stats card or pick a filter to narrow down."
            )

    def _has_active_filter(self) -> bool:
        return any([
            self._folder_combo.currentData(),
            self._source_filter.currentData(),
            self._month_filter.currentData(),
            self._search.text().strip(),
            self._match_filter.currentData(),
        ])

    def _on_stat_card_clicked(self, key: str):
        """Apply a one-click filter when a stats card is clicked.

        The stats cards are forensic shortcuts: clicking
        ``Hash Matched`` filters down to the matched files,
        clicking ``Images`` shows just images, clicking
        ``Total`` clears all filters.  More discoverable than
        the equivalent dropdown selection.
        """
        # Block change signals so we only fire _apply_filters() once
        widgets = (self._folder_combo, self._source_filter,
                   self._month_filter, self._match_filter)
        for w in widgets:
            w.blockSignals(True)
        # Reset all filters to "All" first
        for combo in widgets:
            combo.setCurrentIndex(0)
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)

        # Pre-filter on folder name for sticker shortcut (no MIME type
        # for stickers — they live in a Stickers folder).  Other
        # categories filter by source_type or match status.
        if key == "matched":
            # Find "Hash Matched" item in the match filter combo
            for i in range(self._match_filter.count()):
                if self._match_filter.itemData(i) == "matched":
                    self._match_filter.setCurrentIndex(i)
                    break
        elif key == "rescued":
            # The Rescued Msgs card represents `media` rows we wrote
            # ``orphan_recovered`` onto.  Filtering the orphan table
            # to "matched" gets us the corresponding ORPHAN side of
            # those rescues — same set, viewed from the other end.
            # Plus a friendly notice that this card is the rescue
            # path's primary value-add.
            for i in range(self._match_filter.count()):
                if self._match_filter.itemData(i) == "matched":
                    self._match_filter.setCurrentIndex(i)
                    break
            self._stats_label.setText(
                "These orphans rescued previously-missing chat messages — "
                "open the chat to see the file now displayed inline."
            )
        elif key == "stickers":
            # Find a Sticker folder entry in the folder combo
            for i in range(self._folder_combo.count()):
                t = self._folder_combo.itemText(i).lower()
                if "sticker" in t:
                    self._folder_combo.setCurrentIndex(i)
                    break
        # ``total`` falls through with everything reset — shows all rows.
        # ``images`` / ``videos`` / ``audio`` / ``docs`` add a MIME-type
        # WHERE clause via _apply_filters' new "type" hint.
        self._stat_filter_hint = key if key in (
            "images", "videos", "audio", "docs") else None

        for w in widgets:
            w.blockSignals(False)
        self._apply_filters()

    def _on_gallery_clicked(self, index: QModelIndex):
        """Handle gallery tile click — show detail panel."""
        info = self._model.data(index, INFO_ROLE)
        if info:
            self._detail.show_info(info)

    def _on_row_clicked(self, index: QModelIndex):
        info = index.data(INFO_ROLE)
        if info:
            self._detail.show_info(info)

    def _on_row_double_clicked(self, index: QModelIndex):
        info = index.data(INFO_ROLE)
        if info and info.get("matched_conversation_id"):
            self.go_to_chat.emit(
                info["matched_conversation_id"],
                info.get("matched_message_id") or 0,
            )

    def _start_scan(self):
        """Scan for orphaned media files on disk."""
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("Scanning...")
        self._hash_progress.setVisible(True)
        self._hash_progress.setValue(0)

        self._scan_worker = ScanWorker()
        self._scan_worker.progress.connect(self._on_scan_progress)
        self._scan_worker.finished.connect(self._on_scan_finished)
        self._scan_worker.start()

    def _on_scan_progress(self, current: int, total: int):
        if total > 0:
            self._hash_progress.setMaximum(total)
            self._hash_progress.setValue(current)

    def _on_scan_finished(self, count: int):
        self._hash_progress.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText(f"\U0001F50D Scan ({count:,} found)" if count else "\U0001F50D Scan for Orphaned Media")
        self._load_data()

    def _start_hash_match(self):
        if self._hash_worker and self._hash_worker.isRunning():
            return
        self._hash_btn.setEnabled(False)
        self._hash_btn.setText("Hashing...")
        self._hash_progress.setVisible(True)
        self._hash_progress.setValue(0)

        self._hash_worker = HashMatchWorker()
        self._hash_worker.progress.connect(self._on_hash_progress)
        self._hash_worker.finished.connect(self._on_hash_finished)
        self._hash_worker.start()

    def _on_hash_progress(self, current: int, total: int):
        if total > 0:
            self._hash_progress.setMaximum(total)
            self._hash_progress.setValue(current)

    def _on_hash_finished(self, match_count: int, rescued_count: int = 0):
        self._hash_progress.setVisible(False)
        self._hash_btn.setEnabled(True)
        if rescued_count:
            # Surface the rescue count loud and clear — this is one of
            # the most forensically valuable things the page does.
            self._hash_btn.setText(
                f"\U0001F517 Hash Match — {match_count:,} matched, "
                f"{rescued_count:,} missing-message rescued"
            )
            self._stats_label.setText(
                f"\U0001F4BE Rescued <b>{rescued_count:,}</b> previously-missing media "
                f"from orphaned files (chat messages now show their original bytes)."
            )
            self._stats_label.setTextFormat(Qt.RichText)
        else:
            self._hash_btn.setText(f"\U0001F517 Run Hash Match ({match_count} matched)")
        self._load_data()


class _OrphanedDetailPanel(QFrame):
    go_to_chat = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(400)
        # Theme-aware palette so the panel reads correctly in
        # both light and dark themes — hard-coded WhatsApp-dark
        # hex codes would leave the close button nearly
        # invisible against a light-theme parent.
        try:
            tm = ThemeManager.get()
            _is_light = tm.is_light
        except Exception:
            _is_light = False
        if _is_light:
            self._bg = "#ffffff"
            self._hdr_bg = "#f3f4f6"
            self._border = "#d1d5db"
            self._text = "#1f2937"
            self._muted = "#6b7280"
            self._row_border = "rgba(0,0,0,0.06)"
            self._preview_bg = "#f9fafb"
            self._section_color = "#0e7490"
        else:
            self._bg = "#1a2026"
            self._hdr_bg = "#202c33"
            self._border = "#2a3942"
            self._text = "#e9edef"
            self._muted = "#8696a0"
            self._row_border = "rgba(255,255,255,0.05)"
            self._preview_bg = "#111b21"
            self._section_color = "#00bcd4"

        self.setStyleSheet(f"""
            QFrame#OrphanedDetail {{ background: {self._bg};
                border-left: 1px solid {self._border}; }}
            QFrame#OrphanedDetail QWidget {{ background: transparent; }}
            QFrame#OrphanedDetail QLabel {{ background: transparent; }}
        """)
        self.setObjectName("OrphanedDetail")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header \u2014 solid background + clearly-visible close button.
        # The close button uses a button-like style (border + bg) so
        # it's discoverable even at first glance.
        hdr = QFrame()
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(
            f"QFrame {{ background: {self._hdr_bg}; "
            f"border-bottom: 1px solid {self._border}; }}"
        )
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 8, 0)
        _t = QLabel("Orphaned File Details")
        _t.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {self._text};")
        hl.addWidget(_t, 1)
        _cb = QPushButton("\u2715  Close")
        _cb.setFixedHeight(28)
        _cb.setMinimumWidth(72)
        _cb.setCursor(Qt.PointingHandCursor)
        _cb.setToolTip("Close details panel (Esc)")
        _cb.setStyleSheet(
            f"QPushButton {{ background: rgba(127,127,127,0.12); "
            f"border: 1px solid {self._border}; border-radius: 4px; "
            f"color: {self._text}; font-size: 11px; font-weight: 600; "
            f"padding: 0 10px; }}"
            f"QPushButton:hover {{ background: rgba(220,53,69,0.18); "
            f"border-color: #dc3545; color: #dc3545; }}"
        )
        _cb.clicked.connect(lambda: self.setVisible(False))
        hl.addWidget(_cb)
        root.addWidget(hdr)

        # Content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea {{ background: {self._bg}; border: none; }}"
        )
        content = QWidget()
        self._cl = QVBoxLayout(content)
        self._cl.setContentsMargins(12, 8, 12, 8)
        self._cl.setSpacing(4)

        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumHeight(60)
        self._preview.setMaximumHeight(250)
        self._preview.setMaximumWidth(376)
        self._preview.setStyleSheet(
            f"QLabel {{ background: {self._preview_bg}; "
            f"border-radius: 6px; color: {self._muted}; }}"
        )
        self._cl.addWidget(self._preview)

        self._goto_btn = QPushButton("\u2192 Go to Matched Chat")
        self._goto_btn.setFixedHeight(32)
        self._goto_btn.setVisible(False)
        self._goto_btn.setStyleSheet(
            "QPushButton { background: #00897b; border: none; border-radius: 6px;"
            " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #00695c; }"
        )
        self._cl.addWidget(self._goto_btn)

        self._info_container = QWidget()
        self._info_layout = QVBoxLayout(self._info_container)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(0)
        self._cl.addWidget(self._info_container)
        self._cl.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        self._conv_id = None
        self._msg_id = None

    def _add_row(self, label: str, value: str):
        row = QFrame()
        row.setStyleSheet(
            f"QFrame {{ border-bottom: 1px solid {self._row_border}; }}"
        )
        rl = QVBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(1)
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"font-size: 9px; color: {self._muted}; font-weight: bold;"
        )
        rl.addWidget(lbl)
        # Break long paths by inserting zero-width spaces after backslashes
        display_val = value.replace("\\", "\\\u200B").replace("/", "/\u200B")
        val = QLabel(display_val)
        val.setStyleSheet(f"font-size: 11px; color: {self._text};")
        val.setWordWrap(True)
        val.setMaximumWidth(370)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(val)
        self._info_layout.addWidget(row)

    def _add_section(self, title: str):
        lbl = QLabel(title)
        lbl.setStyleSheet(
            f"font-size: 10px; color: {self._section_color}; font-weight: bold;"
            f" padding: 6px 0 2px 0;"
            f" border-bottom: 1px solid {self._section_color}33;"
        )
        self._info_layout.addWidget(lbl)

    def show_info(self, info: dict):
        # Clear old info rows
        while self._info_layout.count():
            item = self._info_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Stop any playing media
        if hasattr(self, '_media_player') and self._media_player:
            self._media_player.stop()

        fp = info.get("file_path", "")
        mime = info.get("mime_type", "")
        max_w = min(self.width() - 24, 356)

        # Preview based on media type
        if fp and os.path.isfile(fp):
            if mime and mime.startswith("image/"):
                # Image preview — load original quality, scaled to fit
                try:
                    from PySide6.QtGui import QImageReader
                    reader = QImageReader(fp)
                    reader.setAutoTransform(True)
                    orig = reader.size()
                    if orig.isValid() and (orig.width() > max_w or orig.height() > 260):
                        reader.setScaledSize(orig.scaled(max_w, 260, Qt.KeepAspectRatio))
                    img = reader.read()
                    if not img.isNull():
                        pxm = QPixmap.fromImage(img)
                        if pxm.width() > max_w:
                            pxm = pxm.scaled(max_w, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self._preview.setPixmap(pxm)
                        self._preview.setFixedHeight(min(260, pxm.height() + 10))
                    else:
                        self._preview.setText("\U0001F4C4 Cannot load image")
                        self._preview.setFixedHeight(60)
                except Exception:
                    self._preview.setText("\U0001F4C4 Cannot load image")
                    self._preview.setFixedHeight(60)

            elif mime and (mime.startswith("video/") or mime.startswith("audio/")):
                # Video/Audio — show play button + open externally
                _icon = "\U0001F3AC" if mime.startswith("video/") else "\U0001F3B5"
                _type = "Video" if mime.startswith("video/") else "Audio"
                self._preview.setText(f"{_icon} {_type}")
                self._preview.setFixedHeight(80)

                # Add play button to open in system player
                play_btn = QPushButton(f"\u25B6 Play {_type} in Default Player")
                play_btn.setFixedHeight(32)
                play_btn.setStyleSheet(
                    "QPushButton { background: #1565c0; border: none; border-radius: 6px;"
                    " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
                    "QPushButton:hover { background: #0d47a1; }"
                )
                play_btn.clicked.connect(
                    lambda checked=False, path=fp: self._open_in_system(path))
                self._info_layout.addWidget(play_btn)

            elif mime and mime.startswith("application/"):
                # Document — show open button
                ext = os.path.splitext(fp)[1].lower()
                _icons = {".pdf": "\U0001F4D5", ".doc": "\U0001F4C4", ".docx": "\U0001F4C4",
                          ".xls": "\U0001F4CA", ".xlsx": "\U0001F4CA", ".zip": "\U0001F4E6",
                          ".apk": "\U0001F4E6"}
                _icon = _icons.get(ext, "\U0001F4C4")
                self._preview.setText(f"{_icon} Document")
                self._preview.setFixedHeight(60)

                open_btn = QPushButton(f"\U0001F4C2 Open Document")
                open_btn.setFixedHeight(32)
                open_btn.setStyleSheet(
                    "QPushButton { background: #7b1fa2; border: none; border-radius: 6px;"
                    " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
                    "QPushButton:hover { background: #6a1b9a; }"
                )
                open_btn.clicked.connect(
                    lambda checked=False, path=fp: self._open_in_system(path))
                self._info_layout.addWidget(open_btn)
            else:
                self._preview.setText("\U0001F4C4 " + (mime or "Unknown"))
                self._preview.setFixedHeight(60)
        else:
            self._preview.setText("\u274C File not found on disk")
            self._preview.setFixedHeight(60)

        # File info
        self._add_section("FILE INFO")
        if info.get("file_name"):
            self._add_row("File Name", info["file_name"])
        if info.get("folder"):
            self._add_row("Folder", info["folder"])
        if info.get("mime_type"):
            self._add_row("Type", info["mime_type"])
        sz = info.get("file_size", 0)
        if sz:
            if sz > 1048576:
                self._add_row("Size", f"{sz / 1048576:.1f} MB ({sz:,} bytes)")
            else:
                self._add_row("Size", f"{sz / 1024:.0f} KB ({sz:,} bytes)")
        w, h = info.get("width", 0), info.get("height", 0)
        if w and h:
            self._add_row("Resolution", f"{w}\u00D7{h}")
        if info.get("parsed_date"):
            self._add_row("Date (from filename)", info["parsed_date"])
        st = info.get("source_type", "")
        if st:
            _st_labels = {
                "received": "\U0001F4E5 Received", "sent": "\U0001F4E4 Sent by Owner",
                "private": "\U0001F512 Private/Hidden", "status": "\U0001F4F1 Status Save",
                "gbwhatsapp": "\U0001F4F1 GBWhatsApp", "links": "\U0001F517 Link Preview",
            }
            self._add_row("Source", _st_labels.get(st, st))
        if info.get("file_path"):
            self._add_row("Disk Path", info["file_path"])
        if info.get("file_hash"):
            self._add_row("SHA-256 Hash", info["file_hash"])

        # Hash match
        if info.get("matched_message_id"):
            self._add_section("HASH MATCH FOUND")
            self._add_row("Matched Chat", info.get("matched_conv_name") or f"Conv #{info.get('matched_conversation_id')}")
            self._add_row("Message ID", str(info["matched_message_id"]))
            self._add_row("Significance",
                          "This file was shared in a chat whose message was deleted/cleared. "
                          "The file on disk matches by SHA-256 hash.")
            self._goto_btn.setVisible(True)
            self._conv_id = info["matched_conversation_id"]
            self._msg_id = info["matched_message_id"]
            try:
                self._goto_btn.clicked.disconnect()
            except Exception:
                pass
            self._goto_btn.clicked.connect(
                lambda: self.go_to_chat.emit(self._conv_id, self._msg_id))
        else:
            self._goto_btn.setVisible(False)

        # "Open File" button for all types
        if fp and os.path.isfile(fp):
            open_file_btn = QPushButton("\U0001F4C2 Open in System Viewer")
            open_file_btn.setFixedHeight(28)
            open_file_btn.setStyleSheet(
                "QPushButton { background: rgba(255,255,255,0.08); border: 1px solid #3b4a54;"
                " border-radius: 6px; color: #e9edef; font-size: 10px; padding: 0 10px; }"
                "QPushButton:hover { background: rgba(255,255,255,0.15); }"
            )
            open_file_btn.clicked.connect(
                lambda checked=False, path=fp: self._open_in_system(path))
            self._info_layout.addWidget(open_file_btn)

        self.setVisible(True)

    def _open_in_system(self, path: str):
        """Open file in the system's default application."""
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
