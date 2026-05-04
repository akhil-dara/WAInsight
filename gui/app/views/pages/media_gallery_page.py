"""
Media Gallery page — browse media files with type filters,
thumbnails, file status (on disk / downloadable / expired), and
conversation context.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from app.config import format_timestamp as _fmt_ts, timestamp_to_local_datetime as _ts_to_dt

from PySide6.QtCore import QDate, QModelIndex, QObject, QPoint, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDateEdit, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListView, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QStyledItemDelegate, QTableView, QToolTip, QVBoxLayout,
    QWidget,
)

from datetime import date as _date_type

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database
from app.views.widgets.calendar_heatmap import CalendarHeatmapWidget


# Role for thumbnail blob
THUMB_ROLE = Qt.UserRole + 400
MEDIA_INFO_ROLE = Qt.UserRole + 401


def _format_file_size(size_bytes) -> str:
    if not size_bytes or size_bytes <= 0:
        return ""
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f} GB"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_duration(ms) -> str:
    if not ms or ms <= 0:
        return ""
    total_secs = int(ms) // 1000
    mins, secs = divmod(total_secs, 60)
    if mins >= 60:
        hrs, mins = divmod(mins, 60)
        return f"{hrs}h {mins}m"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


class MediaGalleryModel(BaseLazyTableModel):
    _batch_size = 160
    _columns = [
        ("media_name", "Name"),
        ("mime_type", "Type"),
        ("file_size", "Size"),
        ("resolution", "Resolution"),
        ("duration_ms", "Duration"),
        ("media_caption", "Caption"),
    ]

    _base_sql = """
        SELECT COALESCE(me.media_name, '') AS media_name,
               me.mime_type, me.file_size, me.width, me.height,
               me.duration_ms, COALESCE(me.media_caption, '') AS media_caption,
               me.id, me.thumbnail_blob,
               me.file_exists, me.media_url, me.resolved_file_path,
               m.type_label, m.from_me, me.message_id, m.conversation_id,
               COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
               CASE
                   WHEN me.file_hash IS NOT NULL AND TRIM(me.file_hash) != '' THEN
                       COALESCE((
                           SELECT COUNT(DISTINCT mxm.conversation_id)
                           FROM media mx
                           LEFT JOIN message mxm ON mxm.id = mx.message_id
                           WHERE mx.file_hash = me.file_hash
                       ), 1)
                   ELSE 1
               END AS cross_chat_count,
               COALESCE(sc.display_name, sc.wa_name, sc.phone_number, '') AS sender_name,
               m.timestamp AS msg_timestamp,
               me.file_hash,
               COALESCE(me.media_status, '') AS media_status,
               COALESCE(me.recovery_method, '') AS recovery_method,
               me.recovery_timestamp,
               -- New: full JIDs for both the conversation and the
               -- sender contact, so the detail panel and the dropdown
               -- can show the authoritative WhatsApp identity alongside
               -- the display name.
               COALESCE(conv.jid_raw_string, '')   AS conv_jid_full,
               COALESCE(sc.phone_jid, '')          AS sender_phone_jid,
               COALESCE(sc.lid_jid, '')            AS sender_lid_jid,
               COALESCE(sc.phone_number, '')       AS sender_phone
        FROM media me
        LEFT JOIN message m ON m.id = me.message_id
        LEFT JOIN conversation conv ON conv.id = m.conversation_id
        LEFT JOIN contact sc ON sc.id = m.sender_id
    """
    _count_sql = (
        "SELECT COUNT(*) FROM media me "
        "LEFT JOIN message m ON m.id = me.message_id "
        "LEFT JOIN conversation conv ON conv.id = m.conversation_id "
        "LEFT JOIN contact sc ON sc.id = m.sender_id"
    )
    _default_order = "me.id DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._data):
            return None
        row_data = self._data[index.row()]
        col = index.column()

        # Row: name(0), mime(1), size(2), w(3), h(4), dur(5), caption(6),
        # id(7), thumb(8), file_exists(9), media_url(10), resolved(11),
        # type_label(12), from_me(13), message_id(14), conv_id(15), conv_name(16),
        # cross_chat_count(17), sender_name(18), msg_timestamp(19), file_hash(20),
        # media_status(21), recovery_method(22), recovery_timestamp(23),
        # conv_jid_full(24), sender_phone_jid(25), sender_lid_jid(26),
        # sender_phone(27)

        if role == Qt.DisplayRole:
            if col == 0:
                name = row_data[0]
                if not name:
                    tl = row_data[12] or ""
                    return tl.replace("_", " ").title() if tl else "Media"
                return str(name)
            if col == 1:
                return str(row_data[1]) if row_data[1] else ""
            if col == 2:
                return _format_file_size(row_data[2])
            if col == 3:
                w, h = row_data[3], row_data[4]
                if w and h and w > 0 and h > 0:
                    return f"{w}x{h}"
                return ""
            if col == 4:
                return _format_duration(row_data[5])
            if col == 5:
                return str(row_data[6]) if row_data[6] else ""
            return ""

        if role == Qt.TextAlignmentRole and col in (2, 3, 4):
            return Qt.AlignRight | Qt.AlignVCenter

        if role == Qt.ForegroundRole:
            if col == 1:
                mime = row_data[1] or ""
                if mime.startswith("image/"):
                    return QColor("#66bb6a")
                if mime.startswith("video/"):
                    return QColor("#42a5f5")
                if mime.startswith("audio/"):
                    return QColor("#ffa726")
                if mime.startswith("application/"):
                    return QColor("#ab47bc")

        if role == THUMB_ROLE:
            return row_data[8] if len(row_data) > 8 else None

        if role == MEDIA_INFO_ROLE:
            mime = row_data[1] or ""
            name = row_data[0] or ""
            dur = row_data[5]
            dur_str = _format_duration(dur) if dur else ""
            size_str = _format_file_size(row_data[2])
            file_exists_db = bool(row_data[9]) if len(row_data) > 9 else False
            media_url = row_data[10] if len(row_data) > 10 else None
            resolved = row_data[11] if len(row_data) > 11 else None
            file_exists = file_exists_db and bool(resolved) and os.path.isfile(resolved)
            type_label = row_data[12] if len(row_data) > 12 else ""

            # Use DB columns for proper status
            db_status = row_data[21] if len(row_data) > 21 else ""
            recovery_method = row_data[22] if len(row_data) > 22 else ""
            recovery_ts = row_data[23] if len(row_data) > 23 else None

            if recovery_method == "downloaded":
                status = "downloaded"
            elif recovery_method == "hash_linked":
                status = "hash_linked"
            elif recovery_method == "hash_linked_after_delete":
                status = "hash_linked_after_delete"
            elif recovery_method == "orphan_recovered":
                status = "orphan_recovered"
            elif db_status:
                status = db_status  # on_disk, downloadable, expired, no_key, thumb_only, missing
            elif file_exists:
                status = "on_disk"
            elif media_url and str(media_url).strip():
                status = "downloadable"
            else:
                status = "missing"

            from_me = bool(row_data[13]) if len(row_data) > 13 else False
            message_id = row_data[14] if len(row_data) > 14 else None
            conv_id = row_data[15] if len(row_data) > 15 else None
            conv_name = row_data[16] if len(row_data) > 16 else ""
            cross_chat_count = row_data[17] if len(row_data) > 17 else 0
            sender_name = row_data[18] if len(row_data) > 18 else ""

            msg_ts = row_data[19] if len(row_data) > 19 else None
            file_hash = row_data[20] if len(row_data) > 20 else ""
            conv_jid_full   = row_data[24] if len(row_data) > 24 else ""
            sender_phone_jid = row_data[25] if len(row_data) > 25 else ""
            sender_lid_jid   = row_data[26] if len(row_data) > 26 else ""
            sender_phone     = row_data[27] if len(row_data) > 27 else ""
            w = row_data[3] or 0
            h = row_data[4] or 0
            resolution = f"{w}\u00D7{h}" if w > 0 and h > 0 else ""

            return {
                "name": name, "mime": mime, "duration": dur_str,
                "size": size_str, "file_exists": file_exists,
                "size_bytes": row_data[2] or 0,
                "status": status, "type_label": type_label,
                "resolved": resolved, "from_me": from_me,
                "message_id": message_id, "conversation_id": conv_id,
                "conversation_name": conv_name or "",
                "conversation_jid": conv_jid_full or "",
                "cross_chat_count": cross_chat_count or 0,
                "sender_name": sender_name or "",
                "sender_phone": sender_phone or "",
                "sender_phone_jid": sender_phone_jid or "",
                "sender_lid_jid": sender_lid_jid or "",
                "timestamp": msg_ts,
                "file_hash": file_hash or "",
                "resolution": resolution,
                "caption": row_data[6] or "",
                "media_id": row_data[7],
                "thumb_blob": row_data[8],
                "recovery_method": recovery_method,
                "recovery_timestamp": recovery_ts,
                "media_status": db_status,
            }

        if role == Qt.UserRole:
            return row_data[7] if len(row_data) > 7 else 0

        return None


class _PersistentThumbCache:
    """SQLite-backed L2 persistent thumbnail cache (one .db file per case).

    A single ``_gallery_thumbcache.db`` replaces a per-file JPEG
    tree.  Reasons:

      * SQLite is faster than the filesystem for many small
        blobs and uses less disk because it skips per-file inode
        and cluster overhead
        (https://www.sqlite.org/fasterthanfs.html).
      * One file to ship / back up / wipe instead of a directory
        tree of tens of thousands of tiny JPEGs.
      * No ``mkdir`` syscalls per tile.
      * WAL mode lets multiple worker threads write concurrently
        while the GUI thread reads, with no lock contention.
      * Atomic per-row writes — a crash mid-write doesn't leave
        a half-written JPEG.

    Primary key is ``(media_id, kind)``.  Each row stores the
    source-file mtime alongside the JPEG so the cache invalidates
    automatically when the underlying file changes (re-recovered
    media, re-downloaded CDN file).  ``accessed`` epoch supports
    later LRU eviction once cases grow past a few GB of
    thumbnails.
    """
    DB_FILENAME = "_gallery_thumbcache.db"

    def __init__(self, case_dir: str):
        import sqlite3
        self._sqlite3 = sqlite3
        self._db_path = os.path.join(case_dir, self.DB_FILENAME)
        # GUI-thread connection (read-mostly).  Worker threads open
        # their OWN connections via _open_worker_conn().
        self._gui_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._gui_conn.execute("PRAGMA journal_mode = WAL")
        self._gui_conn.execute("PRAGMA synchronous = NORMAL")
        self._gui_conn.execute("PRAGMA temp_store = MEMORY")
        self._gui_conn.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap
        self._gui_conn.execute("""
            CREATE TABLE IF NOT EXISTS thumbs (
                media_id  INTEGER NOT NULL,
                kind      TEXT NOT NULL,
                src_mtime REAL NOT NULL,
                jpeg_blob BLOB NOT NULL,
                accessed  REAL NOT NULL,
                PRIMARY KEY (media_id, kind)
            ) WITHOUT ROWID
        """)
        self._gui_conn.commit()
        # Per-thread worker connections (sqlite3 module forbids sharing
        # a connection across threads unless ``check_same_thread=False``
        # AND we serialise access ourselves; per-thread is cleaner).
        import threading
        self._thread_local = threading.local()

    def _open_worker_conn(self):
        """Return a thread-local sqlite3 connection.  Lazily opens one
        per thread on first call."""
        conn = getattr(self._thread_local, "conn", None)
        if conn is None:
            conn = self._sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            self._thread_local.conn = conn
        return conn

    def get_jpeg_bytes(self, media_id: int, kind: str,
                       expected_mtime: float | None = None) -> bytes | None:
        """Read JPEG bytes for (media_id, kind).  If expected_mtime is
        given and doesn't match what's stored, treat as a miss (the
        underlying source file has changed since the thumb was made -
        return None and let the caller re-extract)."""
        try:
            row = self._gui_conn.execute(
                "SELECT jpeg_blob, src_mtime FROM thumbs WHERE media_id=? AND kind=?",
                (media_id, kind),
            ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        jpeg_blob, stored_mtime = row[0], row[1]
        if expected_mtime is not None and abs(stored_mtime - expected_mtime) > 1.0:
            # Source changed since we cached; treat as miss
            return None
        return bytes(jpeg_blob)

    def put_jpeg_bytes(self, media_id: int, kind: str,
                       jpeg_blob: bytes, src_mtime: float) -> None:
        """Worker-thread write: insert-or-replace.  Uses thread-local
        connection so concurrent writes from the 4 image workers don't
        contend on a single connection.  WAL mode handles the SQLite
        side of concurrency."""
        conn = self._open_worker_conn()
        try:
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO thumbs(media_id, kind, src_mtime, "
                "jpeg_blob, accessed) VALUES (?, ?, ?, ?, ?)",
                (media_id, kind, src_mtime, jpeg_blob, now),
            )
            conn.commit()
        except Exception as e:
            print(f"[ThumbCache] put failed for {media_id}/{kind}: {e}")

    def evict_lru(self, max_mb: int = 800) -> int:
        """Evict oldest-accessed rows until total cache size < max_mb.
        Returns number of rows deleted.  Call on app start or after a
        large bulk-extract pass."""
        try:
            row = self._gui_conn.execute(
                "SELECT COALESCE(SUM(LENGTH(jpeg_blob)), 0) FROM thumbs"
            ).fetchone()
            total = row[0] if row else 0
            if total < max_mb * 1024 * 1024:
                return 0
            # Delete oldest 10% to amortise eviction cost
            cur = self._gui_conn.execute(
                "DELETE FROM thumbs WHERE rowid IN ("
                "  SELECT rowid FROM thumbs ORDER BY accessed ASC "
                "  LIMIT (SELECT COUNT(*) FROM thumbs) / 10"
                ")"
            )
            self._gui_conn.commit()
            return cur.rowcount or 0
        except Exception:
            return 0

    def stats(self) -> tuple[int, int]:
        """Return (row_count, total_bytes).  For diagnostic logging."""
        try:
            row = self._gui_conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(jpeg_blob)), 0) FROM thumbs"
            ).fetchone()
            return (row[0] or 0, row[1] or 0)
        except Exception:
            return (0, 0)


class _PoolWorker(QObject):
    """Tiny QObject that lives on a worker QThread and pumps the parent
    extractor's queue.  Each pool-worker is bound to a single ``kind``
    ('img' or 'vid') so we can keep image and video processing on
    separate threads with appropriate concurrency limits."""
    def __init__(self, parent_extractor: "_GalleryThumbnailWorker", kind: str):
        super().__init__()
        self._parent_ext = parent_extractor
        self._kind = kind

    def run_loop(self):
        if self._kind == "vid":
            self._parent_ext._run_video_loop()
        else:
            self._parent_ext._run_image_loop()


class _GalleryThumbnailWorker(QObject):
    """Multi-thread background generator for image thumbnails and
    video first-frame thumbnails for the Media Gallery.

    A pool of N image workers + 1 video worker because:

    * Image decode is CPU-bound (``QImageReader`` scaled-decode),
      independent per file, and dominated by a few-ms hot path.
      A 4-way parallel pool gives near-linear speed-up for the
      viewport burst and keeps the queue draining faster than
      the scroll wheel can move.
    * Video extraction needs its own ``QThread`` because
      ``QMediaPlayer`` + ``QVideoSink`` require an event loop on
      the worker thread AND can interfere with each other if
      multiple instances run concurrently on the same audio
      device — single video worker.

    Result signal is ``Qt.QueuedConnection`` so the delegate's
    repaint slot runs safely on the GUI thread.  Frames are
    persisted to the per-case L2 SQLite cache, so subsequent
    case opens are instant — no re-decode of multi-MB originals.
    """
    frame_ready = Signal(int, str)   # (media_id, kind: 'img' | 'vid')

    # Parallel image-decoder threads.  4 saturates a typical
    # 4-core machine without starving the GUI thread.  Videos
    # always run on exactly 1 thread (see class docstring).
    NUM_IMAGE_WORKERS = 4

    def __init__(self, persistent_cache: "_PersistentThumbCache"):
        super().__init__()
        from queue import Queue
        self._cache_db = persistent_cache
        self._img_queue = Queue()
        self._vid_queue = Queue()
        # Track which media_ids are currently in the queue so we don't
        # enqueue the same job twice from successive paint() calls.
        self._inflight: set[int] = set()
        # Track media_ids whose extraction permanently failed (codec
        # missing, file unreadable, etc) so paint() doesn't keep
        # re-queueing them every frame.  Cleared only on case switch.
        self._failed: set[int] = set()
        # Image worker pool — N independent threads.  Canonical Qt
        # pattern: each thread holds a small _PoolWorker QObject moved
        # onto it; the thread's started() signal kicks off the worker's
        # loop.  No QThread.run monkey-patching (which is fragile across
        # PySide6 versions because QThread.run is invoked from C++ and
        # may bypass Python instance-attribute overrides).
        self._img_threads: list[QThread] = []
        self._img_objs: list[_PoolWorker] = []
        for i in range(self.NUM_IMAGE_WORKERS):
            t = QThread()
            t.setObjectName(f"GalleryImgWorker-{i}")
            obj = _PoolWorker(self, kind="img")
            obj.moveToThread(t)
            t.started.connect(obj.run_loop)
            self._img_threads.append(t)
            self._img_objs.append(obj)
        # Video worker — single thread (QMediaPlayer concurrency issues
        # on Windows audio backend).
        self._vid_thread = QThread()
        self._vid_thread.setObjectName("GalleryVidWorker")
        self._vid_obj = _PoolWorker(self, kind="vid")
        self._vid_obj.moveToThread(self._vid_thread)
        self._vid_thread.started.connect(self._vid_obj.run_loop)

    def start(self):
        for t in self._img_threads:
            t.start()
        self._vid_thread.start()

    def enqueue(self, media_id: int, src_path: str, kind: str,
                delegate: "MediaThumbnailDelegate") -> None:
        """Queue an extraction job.  ``kind`` is 'img' or 'vid'.  Image
        jobs go to the parallel pool; video jobs go to the single video
        worker.  Idempotent: same media_id never queues twice."""
        try:
            if media_id in self._inflight or media_id in self._failed:
                return
            self._inflight.add(media_id)
            if kind == "vid":
                self._vid_queue.put((media_id, src_path))
            else:
                self._img_queue.put((media_id, src_path))
            # Hook the result signal up to the delegate (idempotent)
            try:
                self.frame_ready.connect(
                    delegate._on_thumb_extracted,
                    Qt.UniqueConnection | Qt.QueuedConnection,
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[MediaGallery] enqueue failed: {e}")

    def _run_image_loop(self):
        """One of NUM_IMAGE_WORKERS threads pulling image jobs in
        parallel.  Decodes with Pillow (libjpeg-turbo, ~2-3x faster than
        Qt's JPEG path), writes the JPEG bytes to the SQLite L2 cache,
        emits frame_ready (queued back to GUI thread)."""
        from queue import Empty
        while True:
            try:
                job = self._img_queue.get(timeout=0.5)
            except Empty:
                continue
            if job is None:
                break
            media_id, src = job
            ok = False
            try:
                ok = self._extract_image_thumb(media_id, src)
            except Exception as e:
                print(f"[MediaGallery] img worker {media_id} failed: {e}")
            self._inflight.discard(media_id)
            if ok:
                self.frame_ready.emit(media_id, "img")
            else:
                self._failed.add(media_id)

    def _run_video_loop(self):
        """Single video worker - QMediaPlayer + QVideoSink with an
        inner QEventLoop on THIS thread.  Concurrency limit of 1
        because multiple parallel QMediaPlayer instances on Windows
        often fight for the same audio device handle and can deadlock
        the FFmpeg backend."""
        from queue import Empty
        while True:
            try:
                job = self._vid_queue.get(timeout=0.5)
            except Empty:
                continue
            if job is None:
                break
            media_id, src = job
            ok = False
            try:
                ok = self._extract_video_frame(media_id, src)
            except Exception as e:
                print(f"[MediaGallery] vid worker {media_id} failed: {e}")
            self._inflight.discard(media_id)
            if ok:
                self.frame_ready.emit(media_id, "vid")
            else:
                self._failed.add(media_id)

    def _extract_image_thumb(self, media_id: int, src: str) -> bool:
        """Image thumb extract via Pillow (libjpeg-turbo) → JPEG bytes
        → SQLite.  Pillow's ``thumbnail()`` is ~2-3× faster than Qt's
        QImageReader for JPEG, especially with the bundled
        libjpeg-turbo backend.

        Stores the source-file mtime alongside the blob for automatic
        invalidation on file change.
        """
        if not src or not os.path.isfile(src):
            return False
        try:
            from PIL import Image
            import io
            mtime = os.path.getmtime(src)
            with Image.open(src) as im:
                # Apply EXIF rotation BEFORE thumbnailing so the saved
                # JPEG is correctly oriented and the renderer doesn't
                # have to re-rotate at paint time.
                try:
                    from PIL import ImageOps
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                im.thumbnail((640, 640), Image.LANCZOS)
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85, optimize=False,
                        progressive=False)
                jpeg_bytes = buf.getvalue()
            self._cache_db.put_jpeg_bytes(media_id, "img", jpeg_bytes, mtime)
            return True
        except Exception as e:
            print(f"[MediaGallery] Pillow img decode failed {media_id}: {e}")
            return False

    def _extract_video_frame(self, media_id: int, src: str) -> bool:
        """Video frame: QMediaPlayer + QVideoSink + an inner event loop
        on THE WORKER THREAD (no painter active here, so the inner
        QEventLoop is safe).  Captured QImage is converted to JPEG
        bytes in-memory and stored in the SQLite L2 cache."""
        if not src or not os.path.isfile(src):
            return False
        try:
            from PySide6.QtCore import QUrl, QEventLoop, QTimer
            from PySide6.QtCore import QBuffer, QByteArray, QIODevice
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
        except Exception:
            return False
        result = {"img": None}
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        sink = QVideoSink()
        player = QMediaPlayer()
        player.setVideoSink(sink)
        try:
            ao = player.audioOutput()
            if ao: ao.setMuted(True)
        except Exception:
            pass
        def on_frame(frame):
            if not frame.isValid():
                return
            try:
                img = frame.toImage()
                if img.isNull():
                    return
                target = 640
                if img.width() > target or img.height() > target:
                    img = img.scaled(target, target, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation)
                result["img"] = img
            finally:
                player.stop()
                loop.quit()
        sink.videoFrameChanged.connect(on_frame)
        timer.timeout.connect(loop.quit)
        player.setSource(QUrl.fromLocalFile(src))
        player.play()
        timer.start(2500)
        loop.exec()
        try: player.stop()
        except Exception: pass
        img = result["img"]
        if img is None or img.isNull():
            return False
        # QImage → JPEG bytes via in-memory QBuffer (no temp file)
        try:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            ok = img.save(buf, "JPG", 85)
            buf.close()
            if not ok or ba.size() < 100:
                return False
            mtime = os.path.getmtime(src)
            self._cache_db.put_jpeg_bytes(media_id, "vid", bytes(ba), mtime)
            return True
        except Exception:
            return False


def _ensure_qpixmap_cache_size():
    """Bump Qt's global QPixmapCache to 256 MB once per process.  At
    640×640 JPEG decode → ~50-150 KB per QPixmap, this comfortably
    holds 1500-5000 thumbnails hot in RAM (which is more than any
    viewport will ever show)."""
    from PySide6.QtGui import QPixmapCache
    if not getattr(_ensure_qpixmap_cache_size, "_done", False):
        QPixmapCache.setCacheLimit(256 * 1024)   # KB
        _ensure_qpixmap_cache_size._done = True


class MediaThumbnailDelegate(QStyledItemDelegate):
    """Delegate to render media thumbnails in a gallery grid view.

    Full-bleed thumbnails with overlaid info badges (WhatsApp/Google Photos style).
    Thumbnail priority:
        1. Actual file on disk (image/sticker types only)
        2. thumbnail_blob from DB
        3. Colored icon fallback with type label
    """

    TILE_SIZE = 160
    # Class-shared per-case L2 cache (single SQLite file).  Reset on
    # case switch via ``reset_persistent_cache``.
    _persistent_cache: _PersistentThumbCache | None = None

    def __init__(self, parent=None):
        super().__init__(parent)
        # Tiny in-process map of "we already know media_id has no
        # source/thumb" so paint() doesn't keep retrying impossible
        # cases.  Distinct from QPixmapCache (which holds the actual
        # decoded pixmaps) and from the L2 SQLite store.
        self._negative_cache: set[int] = set()
        # Lookup-result cache so paint() can skip the SQLite + decode
        # round-trip on identical successive paint cycles.  Keyed by
        # (media_id, kind), value is True if the L2 lookup succeeded
        # and the pixmap is in QPixmapCache, False if it isn't.  This
        # is purely a hint - the authoritative path always re-checks
        # QPixmapCache.find().
        self._l1_hint: dict[tuple[int, str], bool] = {}
        # Cache for scaled "fill" / "fit" operations so we don't
        # re-scale the same source pixmap every paint.
        self._scaled_cache: dict[tuple, QPixmap] = {}
        self.skip_uncached = False  # PF-03: skip expensive loads during fast scroll
        # Debounce timer for viewport repaints triggered by the worker
        # thread - see _on_thumb_extracted.
        self._update_timer: QTimer | None = None

        _ensure_qpixmap_cache_size()

        # Resolve & open the SQLite L2 cache.  Class-shared so workers
        # in other delegate instances (e.g. table view) reuse it.
        try:
            if MediaThumbnailDelegate._persistent_cache is None:
                from app.services.database import Database
                _inst = Database.get()
                db_path = getattr(_inst, "_db_path", None)
                if db_path is None:
                    db_path = getattr(_inst, "path", None)
                if db_path:
                    base = os.path.dirname(str(db_path))
                    cache = _PersistentThumbCache(base)
                    rows, total = cache.stats()
                    print(f"[MediaGallery] L2 SQLite cache: {cache._db_path} "
                          f"({rows} thumbs, {total/1024/1024:.1f} MB)")
                    MediaThumbnailDelegate._persistent_cache = cache
                else:
                    print("[MediaGallery] WARNING: could not resolve case dir; "
                          "thumbnail caching disabled")
        except Exception as e:
            print(f"[MediaGallery] L2 cache setup failed: {e}")
            import traceback
            traceback.print_exc()
        # Detect light theme once
        try:
            from app.services.theme_manager import ThemeManager
            self._lt = ThemeManager.get().is_light
        except Exception:
            self._lt = False
        self._bg_col = QColor(235, 238, 242) if self._lt else QColor(24, 28, 32)
        self._icon_bg = {
            "image": QColor(76, 175, 80, 35) if self._lt else QColor(76, 175, 80, 25),
            "video": QColor(33, 150, 243, 35) if self._lt else QColor(33, 150, 243, 25),
            "audio": QColor(255, 152, 0, 35) if self._lt else QColor(255, 152, 0, 25),
            "voice": QColor(255, 152, 0, 35) if self._lt else QColor(255, 152, 0, 25),
            "document": QColor(156, 39, 176, 35) if self._lt else QColor(156, 39, 176, 25),
            "sticker": QColor(0, 188, 212, 35) if self._lt else QColor(0, 188, 212, 25),
        }
        self._icon_fg = {
            "image": QColor(76, 175, 80),
            "video": QColor(33, 150, 243),
            "audio": QColor(255, 152, 0),
            "voice": QColor(255, 152, 0),
            "document": QColor(156, 39, 176),
            "sticker": QColor(0, 188, 212),
        }

    def sizeHint(self, option, index):
        return QSize(self.TILE_SIZE, self.TILE_SIZE)

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        rect = option.rect
        inner = rect.adjusted(1, 1, -1, -1)
        media_id = index.data(Qt.UserRole) or 0
        thumb_blob = index.data(THUMB_ROLE)
        info = index.data(MEDIA_INFO_ROLE) or {}
        mime = info.get("mime", "")
        status = info.get("status", "missing")
        type_label = info.get("type_label", "")
        resolved = info.get("resolved")
        file_exists = info.get("file_exists", False)

        # Clip to rounded rect for the entire tile
        tile_clip = QPainterPath()
        tile_clip.addRoundedRect(float(inner.x()), float(inner.y()),
                                  float(inner.width()), float(inner.height()), 6, 6)
        painter.setClipPath(tile_clip)

        # Background
        painter.fillRect(inner, self._bg_col)

        # Full-bleed thumbnail rendering.  Priority order, image-first:
        #   1. The actual file on disk — full original quality,
        #      scaled down to TILE_SIZE in _get_file_thumb so we
        #      hold an HQ pixmap without paying the multi-MB cost
        #      of the raw decode.  msgstore's embedded
        #      ``thumbnail_blob`` is only a few hundred bytes and
        #      looks pixelated at tile size, so when the file is
        #      on disk for an image-ish type the disk file always
        #      wins.
        #   2. msgstore.media.thumbnail_blob — tiny embedded
        #      thumb, acceptable for off-disk content and for
        #      video / audio.
        #   3. Icon fallback — colored tile with type label.
        # During fast scroll only already-cached thumbnails are
        # rendered; the scroll-idle pass repaints uncached cells
        # once movement settles.
        drawn = False
        img_w = inner.width()
        img_h = inner.height()
        _skip = self.skip_uncached  # fast-scroll debounce flag

        is_image_mime = bool(mime) and mime.startswith("image/")
        is_video_mime = bool(mime) and mime.startswith("video/")
        # Image-ish: has a disk image we can decode at HQ
        _is_image_ish = (
            is_image_mime
            or type_label in ("image", "sticker", "gif", "animated_gif")
        )
        # Video-ish: has a disk video we can extract a frame from
        _is_video_ish = is_video_mime or type_label in ("video",)

        # 1. Disk file - PREFERRED whenever we have one for an image-ish type.
        # PAINT-SAFE: only reads from the per-case disk JPEG cache.  If
        # the cache is empty for this media_id we queue a worker job and
        # fall through to the embedded thumbnail blob; the worker will
        # write the JPEG asynchronously and trigger a repaint.
        #
        # During fast scroll we skip the disk read but STILL queue
        # extraction - this is the difference between "scroll-then-flash"
        # and "scroll-and-it's-already-ready".  The queue check is a
        # set lookup + os.path.isfile, microseconds, so calling it for
        # every visible cell on every paint is fine.  The 4-worker pool
        # (image side) keeps draining while the user is mid-drag.
        # Two-tier cache lookup is built into _get_*_cached:
        #   L1: QPixmapCache.find() - microseconds, hash lookup
        #   L2: SQLite get_jpeg_bytes() - 1-3 ms, primary-key lookup
        #   Miss: queue worker, return None, paint falls through to blob
        # During fast scroll we skip the L2 read but still queue so the
        # workers run ahead of the user; by the time scroll stops, the
        # workers have already populated the cache for visible cells.
        if file_exists and resolved and _is_image_ish:
            from PySide6.QtGui import QPixmapCache
            pxm = QPixmapCache.find(self._l1_key(media_id, "img"))
            if (pxm is None or pxm.isNull()) and not _skip:
                pxm = self._get_image_thumb_cached(media_id)
            if pxm is None or pxm.isNull():
                self._queue_image_extract(media_id, resolved)
            if pxm and not pxm.isNull():
                drawn = self._draw_thumb_fill(painter, pxm, inner)

        # 1b. Disk video - same two-tier pattern.
        if not drawn and file_exists and resolved and _is_video_ish:
            from PySide6.QtGui import QPixmapCache
            pxm = QPixmapCache.find(self._l1_key(media_id, "vid"))
            if (pxm is None or pxm.isNull()) and not _skip:
                pxm = self._get_video_frame_thumb_cached(media_id)
            if pxm is None or pxm.isNull():
                self._queue_video_extract(media_id, resolved)
            if pxm and not pxm.isNull():
                drawn = self._draw_thumb_fill(painter, pxm, inner)

        # 2. Thumbnail blob - fallback when no disk file (off-disk media,
        # or video without an extractable frame).  Cached in QPixmapCache
        # under a distinct "blob" kind so it doesn't collide with disk thumbs.
        if not drawn and thumb_blob and len(thumb_blob) > 50:
            from PySide6.QtGui import QPixmapCache
            blob_key = self._l1_key(media_id, "blob")
            pxm = QPixmapCache.find(blob_key)
            if pxm is None or pxm.isNull():
                if not _skip:
                    pxm = self._get_thumb(media_id, thumb_blob)
                else:
                    pxm = None
            if pxm and not pxm.isNull():
                if type_label in ("image", "video", "gif", "animated_gif", "sticker", ""):
                    drawn = self._draw_thumb_fill(painter, pxm, inner)
                else:
                    drawn = self._draw_thumb_fit(painter, pxm, inner)

        # 3. Icon fallback — clean colored tile with type label
        if not drawn:
            self._draw_icon_fallback(painter, inner, type_label, mime)

        # --- Overlay badges (on top of the thumbnail) ---

        # Selection border
        from PySide6.QtWidgets import QStyle
        painter.setClipPath(tile_clip)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(QColor(0, 188, 212))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(inner.adjusted(1, 1, -1, -1), 5, 5)

        # Status dot — top-left corner (color reflects provenance)
        dot_x = inner.x() + 5
        dot_y = inner.y() + 5
        _dot_colors = {
            "on_disk": QColor(80, 200, 80),       # green — original
            "downloaded": QColor(0, 180, 220),     # cyan — CDN downloaded
            "hash_linked": QColor(160, 100, 220),  # purple — hash recovered
            "hash_linked_after_delete": QColor(230, 130, 0),  # amber — received-then-deleted, hash-linked
            "orphan_recovered": QColor(46, 125, 50),  # dark green — rescued from orphaned file on disk
            "downloadable": QColor(80, 180, 255),  # blue — available
            "download_failed": QColor(220, 80, 60),  # red — failed download
            "expired": QColor(220, 120, 50),       # orange — expired
            "no_key": QColor(200, 160, 50),        # yellow — no key
            "thumb_only": QColor(150, 150, 150),   # gray — thumbnail only
        }
        painter.setBrush(_dot_colors.get(status, QColor(180, 60, 60)))
        painter.setPen(QColor(0, 0, 0, 80))
        painter.drawEllipse(dot_x, dot_y, 8, 8)

        # Date pill — top-left, below status dot
        msg_ts = info.get("timestamp")
        if msg_ts:
            try:
                _d = _ts_to_dt(msg_ts)
                date_str = _d.strftime("%b %d '%y")
                painter.setFont(QFont("Segoe UI", 6, QFont.Bold))
                _fm = QFontMetrics(painter.font())
                _tw = _fm.horizontalAdvance(date_str) + 6
                _dr = QRect(inner.x() + 3, inner.y() + 16, _tw, 13)
                _dp = QPainterPath()
                _dp.addRoundedRect(float(_dr.x()), float(_dr.y()),
                                   float(_dr.width()), float(_dr.height()), 3, 3)
                painter.fillPath(_dp, QColor(0, 0, 0, 170))
                painter.setPen(QColor(255, 255, 255, 230))
                painter.drawText(_dr, Qt.AlignCenter, date_str)
            except Exception:
                pass

        # Duration badge — top-right (for video/audio)
        dur = info.get("duration", "")
        if dur:
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            fm = QFontMetrics(painter.font())
            tw = fm.horizontalAdvance(dur) + 8
            badge_rect = QRect(inner.right() - tw - 3, inner.y() + 3, tw, 16)
            bp = QPainterPath()
            bp.addRoundedRect(float(badge_rect.x()), float(badge_rect.y()),
                              float(badge_rect.width()), float(badge_rect.height()), 3, 3)
            painter.fillPath(bp, QColor(0, 0, 0, 160))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(badge_rect, Qt.AlignCenter, dur)

        # Size badge — bottom-right
        size_str = info.get("size", "")
        if size_str:
            painter.setFont(QFont("Segoe UI", 7))
            fm = QFontMetrics(painter.font())
            tw = fm.horizontalAdvance(size_str) + 8
            sr = QRect(inner.right() - tw - 3, inner.bottom() - 17, tw, 14)
            sp = QPainterPath()
            sp.addRoundedRect(float(sr.x()), float(sr.y()),
                              float(sr.width()), float(sr.height()), 3, 3)
            painter.fillPath(sp, QColor(0, 0, 0, 140))
            painter.setPen(QColor(220, 225, 230))
            painter.drawText(sr, Qt.AlignCenter, size_str)

        # Video play icon — center
        if drawn and type_label in ("video", "gif", "animated_gif"):
            cx = inner.x() + inner.width() // 2 - 16
            cy = inner.y() + inner.height() // 2 - 16
            play_bg = QPainterPath()
            play_bg.addEllipse(float(cx), float(cy), 32.0, 32.0)
            painter.fillPath(play_bg, QColor(0, 0, 0, 130))
            painter.setFont(QFont("Segoe UI", 14, QFont.Bold))
            painter.setPen(QColor(255, 255, 255, 220))
            painter.drawText(QRect(cx, cy, 32, 32), Qt.AlignCenter, "\u25B6")

        # Download overlay for downloadable
        if status == "downloadable" and drawn:
            painter.fillRect(inner, QColor(0, 0, 0, 50))
            cx = inner.x() + inner.width() // 2 - 14
            cy = inner.y() + inner.height() // 2 - 14
            dl_bg = QPainterPath()
            dl_bg.addEllipse(float(cx), float(cy), 28.0, 28.0)
            painter.fillPath(dl_bg, QColor(0, 0, 0, 140))
            painter.setFont(QFont("Segoe UI", 12, QFont.Bold))
            painter.setPen(QColor(80, 200, 255))
            painter.drawText(QRect(cx, cy, 28, 28), Qt.AlignCenter, "\u2B07")

        # Cross-chat count badge — top-right (below duration if present)
        cross_chat_count = info.get("cross_chat_count", 0)
        if cross_chat_count and cross_chat_count > 1:
            badge_text = f"{cross_chat_count} chats"
            painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
            fm = QFontMetrics(painter.font())
            tw = fm.horizontalAdvance(badge_text) + 8
            badge_y = inner.y() + 3 + (19 if dur else 0)
            cc_rect = QRect(inner.right() - tw - 3, badge_y, tw, 16)
            cc_path = QPainterPath()
            cc_path.addRoundedRect(float(cc_rect.x()), float(cc_rect.y()),
                                   float(cc_rect.width()), float(cc_rect.height()), 3, 3)
            painter.fillPath(cc_path, QColor(128, 0, 200, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(cc_rect, Qt.AlignCenter, badge_text)

        # Conversation name overlay — semi-transparent bar at bottom
        conv_name = info.get("conversation_name", "")
        if conv_name:
            bar_h = 18
            bar_rect = QRect(inner.x(), inner.bottom() - bar_h + 1, inner.width(), bar_h)
            painter.fillRect(bar_rect, QColor(0, 0, 0, 166))
            painter.setFont(QFont("Segoe UI", 7))
            fm = QFontMetrics(painter.font())
            elided = fm.elidedText(conv_name, Qt.ElideRight, bar_rect.width() - 6)
            text_rect = bar_rect.adjusted(3, 0, -3, 0)
            painter.setPen(QColor(255, 255, 255, 220))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

        painter.setClipping(False)
        painter.restore()

    def helpEvent(self, event, view, option, index):
        """Hover tooltip — full forensic identity for Chat + Sender."""
        if event.type() == event.Type.ToolTip and index.isValid():
            info = index.data(MEDIA_INFO_ROLE) or {}
            name = info.get("name", "") or ""
            type_label = info.get("type_label", "") or ""
            conv_name = info.get("conversation_name", "")
            conv_jid  = info.get("conversation_jid", "")
            from_me = info.get("from_me", False)
            sender_name = info.get("sender_name", "") or ""
            sender_phone = info.get("sender_phone", "") or ""
            sender_pjid  = info.get("sender_phone_jid", "") or ""
            sender_ljid  = info.get("sender_lid_jid", "") or ""
            size_str = info.get("size", "")
            status = info.get("status", "")
            dur = info.get("duration", "")
            res = info.get("resolution", "")
            ts  = info.get("timestamp")

            # Build tooltip lines — use filename if available, else type + chat
            if name:
                lines = [f"<b>{name}</b>"]
            elif type_label and conv_name:
                lines = [f"<b>{type_label.title()} in {conv_name}</b>"]
            elif type_label:
                lines = [f"<b>{type_label.title()}</b>"]
            else:
                lines = ["<b>Media file</b>"]

            # Chat: name + full JID
            if conv_name or conv_jid:
                chat_line = f"<b>Chat:</b> {conv_name or 'Unknown'}"
                if conv_jid:
                    chat_line += f" &nbsp;<span style='color:#777'>[{conv_jid}]</span>"
                lines.append(chat_line)

            # Sender: device-owner expansion for from_me=1, else
            # name + phone + JID + LID
            if from_me:
                # Lazy-resolve owner once via the delegate's view's parent
                # (cached on the delegate instance).
                owner = self._owner_tuple()
                primary = owner[0] or "Device owner"
                pieces = []
                if owner[1]: pieces.append(f"+{owner[1]}")
                if owner[2]: pieces.append(f"[{owner[2]}]")
                snd_line = f"<b>Sender:</b> {primary}"
                if pieces:
                    snd_line += " &nbsp;<span style='color:#777'>" + " ".join(pieces) + "</span>"
                snd_line += " <span style='color:#999'>(Me)</span>"
            else:
                primary = sender_name or "Unknown"
                pieces = []
                if sender_phone: pieces.append(f"+{sender_phone}")
                if sender_pjid:  pieces.append(f"[{sender_pjid}]")
                if sender_ljid:  pieces.append(f"LID:{sender_ljid}")
                snd_line = f"<b>Sender:</b> {primary}"
                if pieces:
                    snd_line += " &nbsp;<span style='color:#777'>" + " ".join(pieces) + "</span>"
            lines.append(snd_line)

            # When sent
            if ts:
                try:
                    lines.append(f"<b>Date:</b> {_fmt_ts(ts, 'full')}")
                except Exception:
                    pass

            if dur:    lines.append(f"<b>Duration:</b> {dur}")
            if res:    lines.append(f"<b>Resolution:</b> {res}")
            if size_str: lines.append(f"<b>Size:</b> {size_str}")

            status_map = {"on_disk": "Original (on disk)", "downloaded": "Downloaded",
                          "hash_linked": "Found via hash match", "hash_linked_after_delete": "Originally received here — file deleted, hash-linked",
                          "orphan_recovered": "Rescued from orphaned file on disk",
                          "downloadable": "Downloadable",
                          "download_failed": "Download failed", "expired": "URL expired",
                          "no_key": "No key", "thumb_only": "Thumbnail only", "missing": "Missing"}
            lines.append(f"<b>Status:</b> {status_map.get(status, status)}")

            QToolTip.showText(event.globalPos(), "<br>".join(lines), view)
            return True
        return super().helpEvent(event, view, option, index)

    def _owner_tuple(self) -> tuple[str, str, str]:
        """Per-delegate cached (name, phone, jid) for the device
        owner.  Used by the tile tooltip to expand a from_me=1
        entry into the owner's actual identity (the case-level
        owner name + phone + JID)."""
        cached = getattr(self, "_owner_cache_t", None)
        if cached is not None:
            return cached
        name, phone, jid = "", "", ""
        try:
            from app.services.database import Database
            db = Database.get()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_name'")
            if r and r[0]: name = str(r[0]).strip()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_phone'")
            if r and r[0]: phone = str(r[0]).strip()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_jid'")
            if r and r[0]: jid = str(r[0]).strip()
        except Exception:
            pass
        self._owner_cache_t = (name, phone, jid)
        return self._owner_cache_t

    def _draw_thumb_fill(self, painter: QPainter, pxm: QPixmap,
                         rect: QRect) -> bool:
        """Crop-fill: scale to cover the entire tile, center-crop excess.
        Cached in ``_scaled_cache`` keyed by (cacheKey, target size)."""
        pw, ph = pxm.width(), pxm.height()
        rw, rh = rect.width(), rect.height()
        if pw <= 0 or ph <= 0:
            return False
        fill_key = ("fill", pxm.cacheKey(), rw, rh)
        cached = self._scaled_cache.get(fill_key)
        if cached is None:
            scale = max(rw / pw, rh / ph)
            sw = int(pw * scale)
            sh = int(ph * scale)
            scaled = pxm.scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            sx = (sw - rw) // 2
            sy = (sh - rh) // 2
            cached = scaled.copy(sx, sy, rw, rh)
            self._scaled_cache[fill_key] = cached
            self._evict_scaled_cache()
        painter.drawPixmap(rect.x(), rect.y(), cached)
        return True

    def _draw_thumb_fit(self, painter: QPainter, pxm: QPixmap,
                        rect: QRect) -> bool:
        """Fit thumbnail inside tile with letterboxing.  Cached scaled result."""
        rw, rh = rect.width() - 8, rect.height() - 8
        fit_key = ("fit", pxm.cacheKey(), rw, rh)
        cached = self._scaled_cache.get(fit_key)
        if cached is None:
            cached = pxm.scaled(rw, rh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._scaled_cache[fit_key] = cached
            self._evict_scaled_cache()
        px = rect.x() + (rect.width() - cached.width()) // 2
        py = rect.y() + (rect.height() - cached.height()) // 2
        painter.drawPixmap(px, py, cached)
        return True

    def _draw_icon_fallback(self, painter: QPainter, rect: QRect,
                            type_label: str, mime: str):
        """Draw a clean colored icon tile with type label."""
        # Determine category for color
        cat = type_label
        if cat in ("animated_gif",):
            cat = "video"
        if cat not in self._icon_bg:
            if "image" in mime:
                cat = "image"
            elif "video" in mime:
                cat = "video"
            elif "audio" in mime:
                cat = "audio"
            else:
                cat = "document"

        painter.fillRect(rect, self._icon_bg.get(cat, self._bg_col))

        icons = {
            "image": "\U0001F5BC", "video": "\U0001F4F9", "audio": "\U0001F3B5",
            "voice": "\U0001F3A4", "document": "\U0001F4C4", "sticker": "\U0001F3AD",
            "gif": "\U0001F3AC", "animated_gif": "\U0001F3AC",
        }
        icon = icons.get(type_label, icons.get(cat, "\U0001F4C4"))
        fg = self._icon_fg.get(cat, QColor(140, 150, 160))

        # Icon
        painter.setFont(QFont("Segoe UI", 28))
        painter.setPen(fg)
        icon_rect = QRect(rect.x(), rect.y() - 8, rect.width(), rect.height())
        painter.drawText(icon_rect, Qt.AlignCenter, icon)

        # Type label below icon
        label = type_label.replace("_", " ").upper() if type_label else cat.upper()
        painter.setFont(QFont("Segoe UI", 7, QFont.Bold))
        painter.setPen(QColor(fg.red(), fg.green(), fg.blue(), 180))
        label_rect = QRect(rect.x(), rect.y() + rect.height() // 2 + 14,
                           rect.width(), 16)
        painter.drawText(label_rect, Qt.AlignCenter, label)

    def _get_thumb(self, media_id: int, blob: bytes) -> QPixmap | None:
        """Decode the small msgstore embedded thumbnail blob into a
        QPixmap.  Cached in QPixmapCache under the 'blob' kind so it
        doesn't collide with disk-cache hits."""
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtCore import QByteArray
        key = self._l1_key(media_id, "blob")
        pm = QPixmapCache.find(key)
        if pm is not None and not pm.isNull():
            return pm
        pm = QPixmap()
        pm.loadFromData(QByteArray(blob))
        if pm.isNull():
            return None
        QPixmapCache.insert(key, pm)
        return pm

    @staticmethod
    def _l1_key(media_id: int, kind: str) -> str:
        """QPixmapCache key.  Distinct prefix per kind so img/vid don't
        collide in L1 (they could have different aspect ratios)."""
        return f"mg:{kind}:{media_id}"

    def _get_image_thumb_cached(self, media_id: int) -> QPixmap | None:
        """L1 (QPixmapCache) → L2 (SQLite) lookup.  PAINT-SAFE.

        L1 hit: returned in microseconds (Qt internal hash + ref-count).
        L2 hit: ~1-3 ms to load JPEG bytes from SQLite + decode to
        QPixmap, then promote to L1 so the next paint is L1-fast.
        Total miss: returns None, caller queues background extraction.
        """
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtCore import QByteArray
        key = self._l1_key(media_id, "img")
        # L1 lookup
        pm = QPixmapCache.find(key)
        if pm is not None and not pm.isNull():
            return pm
        # L2 lookup
        if MediaThumbnailDelegate._persistent_cache is None:
            return None
        try:
            jpeg_bytes = MediaThumbnailDelegate._persistent_cache.get_jpeg_bytes(media_id, "img")
        except Exception:
            jpeg_bytes = None
        if not jpeg_bytes:
            return None
        try:
            pm = QPixmap()
            ba = QByteArray(jpeg_bytes)
            pm.loadFromData(ba, "JPEG")
            if pm.isNull():
                return None
            dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
            pm.setDevicePixelRatio(dpr)
            QPixmapCache.insert(key, pm)
            return pm
        except Exception:
            return None

    def _queue_image_extract(self, media_id: int, path: str) -> None:
        """Schedule a background image thumbnail generation.  Idempotent."""
        if not path or not os.path.isfile(path):
            return
        # If L2 already has it, nothing to do
        cache_db = MediaThumbnailDelegate._persistent_cache
        if cache_db is None:
            return
        try:
            if cache_db.get_jpeg_bytes(media_id, "img") is not None:
                return
        except Exception:
            pass
        worker = MediaThumbnailDelegate._get_worker()
        if worker is None:
            return
        worker.enqueue(media_id, path, "img", self)

    def _get_video_frame_thumb_cached(self, media_id: int) -> QPixmap | None:
        """L1 (QPixmapCache) → L2 (SQLite) for video first-frame thumbs."""
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtCore import QByteArray
        key = self._l1_key(media_id, "vid")
        pm = QPixmapCache.find(key)
        if pm is not None and not pm.isNull():
            return pm
        if MediaThumbnailDelegate._persistent_cache is None:
            return None
        try:
            jpeg_bytes = MediaThumbnailDelegate._persistent_cache.get_jpeg_bytes(media_id, "vid")
        except Exception:
            jpeg_bytes = None
        if not jpeg_bytes:
            return None
        try:
            pm = QPixmap()
            ba = QByteArray(jpeg_bytes)
            pm.loadFromData(ba, "JPEG")
            if pm.isNull():
                return None
            dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
            pm.setDevicePixelRatio(dpr)
            QPixmapCache.insert(key, pm)
            return pm
        except Exception:
            return None

    def _queue_video_extract(self, media_id: int, path: str) -> None:
        """Schedule a background video-frame extraction.  Idempotent."""
        if not path or not os.path.isfile(path):
            return
        cache_db = MediaThumbnailDelegate._persistent_cache
        if cache_db is None:
            return
        try:
            if cache_db.get_jpeg_bytes(media_id, "vid") is not None:
                return
        except Exception:
            pass
        worker = MediaThumbnailDelegate._get_worker()
        if worker is None:
            return
        worker.enqueue(media_id, path, "vid", self)

    @classmethod
    def _get_worker(cls):
        """Lazy-init the singleton background thumbnail worker."""
        worker = getattr(cls, "_worker", None)
        if worker is not None:
            return worker
        if cls._persistent_cache is None:
            return None
        try:
            worker = _GalleryThumbnailWorker(cls._persistent_cache)
            worker.start()
            cls._worker = worker
        except Exception as e:
            print(f"[MediaGallery] thumbnail worker unavailable: {e}")
            cls._worker = None
            worker = None
        return worker

    def _on_thumb_extracted(self, media_id: int, kind: str) -> None:
        """Slot fired (queued, GUI thread) when the worker has written
        a thumbnail to SQLite.  Promotes JPEG bytes from L2 → L1 right
        away (so the next paint is microseconds), then schedules a
        debounced viewport.update().
        """
        from PySide6.QtGui import QPixmapCache
        from PySide6.QtCore import QByteArray
        try:
            cache_db = MediaThumbnailDelegate._persistent_cache
            if cache_db is not None:
                jpeg_bytes = cache_db.get_jpeg_bytes(media_id, kind)
                if jpeg_bytes:
                    pm = QPixmap()
                    pm.loadFromData(QByteArray(jpeg_bytes), "JPEG")
                    if not pm.isNull():
                        dpr = (QApplication.instance().devicePixelRatio()
                               if QApplication.instance() else 1.0)
                        pm.setDevicePixelRatio(dpr)
                        QPixmapCache.insert(self._l1_key(media_id, kind), pm)
        except Exception:
            pass
        # Coalesce repaints (many worker completions per second → one repaint per 150ms)
        if self._update_timer is None:
            self._update_timer = QTimer()
            self._update_timer.setSingleShot(True)
            self._update_timer.setInterval(150)
            self._update_timer.timeout.connect(self._fire_viewport_update)
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _fire_viewport_update(self) -> None:
        try:
            view = self.parent()
            if view is not None and hasattr(view, "viewport"):
                view.viewport().update()
        except Exception:
            pass
        # Best-effort viewport refresh - the gallery view will repaint
        # the visible cells, which now find the JPEG in _disk_thumb_dir.
        try:
            view = self.parent()
            if view is not None and hasattr(view, "viewport"):
                view.viewport().update()
        except Exception:
            pass

    def _evict_scaled_cache(self):
        """Cap the scaled-pixmap helper cache.  Bounded so we don't
        hold rescaled copies forever; QPixmapCache is the source of
        truth for L1 hits."""
        while len(self._scaled_cache) > 800:
            self._scaled_cache.pop(next(iter(self._scaled_cache)))


class _PopupCheckList(QFrame):
    """Floating popup with search + checkable item list."""

    item_toggled = Signal()  # emitted on any check change

    def __init__(self, is_light: bool = True, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_WindowPropagation)
        self.setFixedWidth(440)
        self.setMinimumHeight(100)
        self.setMaximumHeight(420)
        _lt = is_light

        # Frame styling — solid background with shadow border
        self.setStyleSheet(
            f"_PopupCheckList {{ background: {'#ffffff' if _lt else '#1e2a33'};"
            f" border: 1px solid {'#d0d7de' if _lt else '#3a4a54'};"
            f" border-radius: 8px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(5)

        # Search
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name, number, or LID...")
        self._search.setFixedHeight(30)
        self._search.setClearButtonEnabled(True)
        self._search.setStyleSheet(
            f"QLineEdit {{ border: 1px solid {'#d0d7de' if _lt else 'rgba(255,255,255,0.15)'};"
            f" border-radius: 6px; padding: 4px 10px; font-size: 11px;"
            f" background: {'#f6f8fa' if _lt else 'rgba(255,255,255,0.06)'};"
            f" color: {'#333' if _lt else '#e0e0e0'}; }}"
            f" QLineEdit:focus {{ border-color: {'#00897b' if _lt else '#00bcd4'}; }}"
        )
        self._search.textChanged.connect(self._filter_items)
        layout.addWidget(self._search)

        # Select All / Clear row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        _btn_ss = (
            f"QPushButton {{ padding: 2px 10px; border-radius: 4px;"
            f" border: 1px solid {'#d0d7de' if _lt else 'rgba(255,255,255,0.15)'};"
            f" font-size: 9px; font-weight: 600;"
            f" color: {'#555' if _lt else '#aaa'};"
            f" background: {'#f6f8fa' if _lt else 'rgba(255,255,255,0.05)'}; }}"
            f" QPushButton:hover {{ background: {'rgba(0,137,123,0.1)' if _lt else 'rgba(0,188,212,0.15)'};"
            f" border-color: {'#00897b' if _lt else '#00bcd4'};"
            f" color: {'#00695c' if _lt else '#00bcd4'}; }}"
        )
        sa = QPushButton("Select All")
        sa.setFixedHeight(22)
        sa.setStyleSheet(_btn_ss)
        sa.clicked.connect(self._select_all)
        btn_row.addWidget(sa)
        cl = QPushButton("Clear All")
        cl.setFixedHeight(22)
        cl.setStyleSheet(_btn_ss)
        cl.clicked.connect(self._clear_all)
        btn_row.addWidget(cl)
        self._match_lbl = QLabel("")
        self._match_lbl.setStyleSheet(
            f"font-size: 9px; color: {'#78909c' if _lt else '#667781'};"
        )
        btn_row.addWidget(self._match_lbl)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Scrollable checkbox list
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
        )
        self._item_widget = QWidget()
        self._item_layout = QVBoxLayout(self._item_widget)
        self._item_layout.setContentsMargins(0, 0, 0, 0)
        self._item_layout.setSpacing(0)
        self._scroll.setWidget(self._item_widget)
        layout.addWidget(self._scroll, 1)

        self._rows: list[tuple] = []  # (row_widget, checkbox, data, search_text)
        self._checked: set = set()
        self._refreshing = False
        self._is_light = _lt

    def populate(self, items: list[tuple], checked: set):
        """Rebuild checkboxes. items = [(display_label, data, count, search_text), ...]."""
        self._refreshing = True

        # Clear old
        for row_w, cb, _, _ in self._rows:
            row_w.setParent(None)
            row_w.deleteLater()
        self._rows.clear()

        # Remove old stretch
        while self._item_layout.count():
            item = self._item_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()

        self._checked = set(checked)
        _lt = self._is_light

        from PySide6.QtWidgets import QCheckBox
        _cb_style = (
            f"QCheckBox {{ padding: 3px 4px; font-size: 10px; spacing: 6px;"
            f" color: {'#333' if _lt else '#d0d7de'}; }}"
            f" QCheckBox::indicator {{ width: 14px; height: 14px; }}"
        )
        _row_hover = (
            f"QWidget:hover {{ background: {'rgba(0,137,123,0.06)' if _lt else 'rgba(0,188,212,0.06)'};"
            f" border-radius: 4px; }}"
        )

        for display, data, count, search_text in items:
            row_w = QWidget()
            row_w.setStyleSheet(_row_hover)
            row_l = QHBoxLayout(row_w)
            row_l.setContentsMargins(4, 1, 8, 1)
            row_l.setSpacing(6)

            cb = QCheckBox(display)
            cb.setStyleSheet(_cb_style)
            cb.setChecked(data in self._checked)
            cb.setProperty("item_data", data)
            cb.toggled.connect(self._on_toggled)
            row_l.addWidget(cb, 1)

            count_lbl = QLabel(f"{count:,}")
            count_lbl.setStyleSheet(
                f"font-size: 10px; font-weight: bold;"
                f" color: {'#00796b' if _lt else '#4dd0c8'};"
                f" min-width: 44px; padding-right: 2px;"
            )
            count_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            row_l.addWidget(count_lbl)

            self._item_layout.addWidget(row_w)
            self._rows.append((row_w, cb, data, search_text.lower()))

        self._item_layout.addStretch()
        self._match_lbl.setText(f"{len(items)} contacts")
        self._refreshing = False

    def _on_toggled(self, checked: bool):
        if self._refreshing:
            return
        cb = self.sender()
        if not cb:
            return
        data = cb.property("item_data")
        if checked:
            self._checked.add(data)
        else:
            self._checked.discard(data)
        self.item_toggled.emit()

    def _filter_items(self, text: str):
        t = text.lower().strip()
        visible = 0
        for row_w, cb, data, search_text in self._rows:
            vis = (t in search_text) if t else True
            row_w.setVisible(vis)
            if vis:
                visible += 1
        self._match_lbl.setText(f"{visible} / {len(self._rows)} shown")

    def _select_all(self):
        self._refreshing = True
        for row_w, cb, data, _ in self._rows:
            if row_w.isVisible():
                cb.setChecked(True)
                self._checked.add(data)
        self._refreshing = False
        self.item_toggled.emit()

    def _clear_all(self):
        self._refreshing = True
        self._checked.clear()
        for row_w, cb, data, _ in self._rows:
            cb.setChecked(False)
        self._refreshing = False
        self.item_toggled.emit()

    def get_checked(self) -> set:
        return set(self._checked)

    def showEvent(self, event):
        super().showEvent(event)
        self._search.setFocus()
        self._search.clear()


class _CheckableSearchCombo(QWidget):
    """Multi-select combo button that opens a floating popup with search + checkboxes.

    Click the button → floating popup with search field + checkable items + counts.
    Emits `selection_changed` when any checkbox toggles.
    """

    selection_changed = Signal()

    def __init__(self, placeholder: str = "All", parent=None):
        super().__init__(parent)
        self._placeholder = placeholder
        self._items: list[tuple] = []  # (display, data, count, search_text)
        self._checked: set = set()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn = QPushButton(f"▾  {placeholder}")
        self._btn.setFixedHeight(28)
        self._btn.setCursor(Qt.PointingHandCursor)
        self._btn.clicked.connect(self._toggle_popup)
        layout.addWidget(self._btn)

        self._popup: _PopupCheckList | None = None
        self._btn_style = ""
        self._dropdown_style = ""

    def set_style(self, btn_style: str, dropdown_style: str):
        self._btn_style = btn_style
        self._btn.setStyleSheet(btn_style)
        self._dropdown_style = dropdown_style

    def set_items(self, items: list[tuple], preserve_checked: bool = True):
        """Set items. Each item is (label: str, data, count: int).

        label is the display text, data is the filter value, count is media count.
        """
        old_checked = self._checked.copy() if preserve_checked else set()
        valid_data = {d for _, d, _ in items}
        self._checked = old_checked & valid_data if preserve_checked else set()

        # Build enriched items: (display, data, count, search_text)
        self._items = []
        for label, data, count in items:
            self._items.append((label, data, count, label))

        # Update popup if currently open
        if self._popup and self._popup.isVisible():
            self._popup.populate(self._items, self._checked)

        self._update_btn_text()

    def checked_values(self) -> list:
        return list(self._checked)

    def clear_selection(self):
        self._checked.clear()
        self._update_btn_text()

    def _toggle_popup(self):
        if self._popup and self._popup.isVisible():
            self._popup.close()
            return

        try:
            from app.services.theme_manager import ThemeManager
            _lt = ThemeManager.get().is_light
        except Exception:
            _lt = True
        self._popup = _PopupCheckList(is_light=_lt)
        self._popup.populate(self._items, self._checked)
        self._popup.item_toggled.connect(self._on_popup_changed)

        # Position below the button
        pos = self._btn.mapToGlobal(self._btn.rect().bottomLeft())
        self._popup.move(pos.x(), pos.y() + 2)
        self._popup.show()

    def _on_popup_changed(self):
        if self._popup:
            self._checked = self._popup.get_checked()
        self._update_btn_text()
        self.selection_changed.emit()

    def _update_btn_text(self):
        n = len(self._checked)
        if n == 0:
            self._btn.setText(f"▾  {self._placeholder}")
        elif n == 1:
            for display, data, count, _ in self._items:
                if data in self._checked:
                    short = display[:28] + "…" if len(display) > 28 else display
                    self._btn.setText(f"▾  {short}")
                    break
        else:
            self._btn.setText(f"▾  {n} selected")


class _MediaDetailPanel(QFrame):
    """Right-side detail panel for selected media item.

    Collapsible: an arrow toggle on the left edge lets users collapse
    the panel to a thin 28px strip with a '◀' arrow, and expand back
    to full 380px with '▶'. This gives more gallery space when needed.
    """
    go_to_chat = Signal(int, int)
    preview_requested = Signal(dict)

    EXPANDED_W = 380
    COLLAPSED_W = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self._panel_expanded = True
        self.setFixedWidth(self.EXPANDED_W)
        self.setStyleSheet("""
            QFrame#MediaDetail { background: #1a2026; border-left: 1px solid #2a3942; }
            QFrame#MediaDetail QWidget { background: transparent; }
            QFrame#MediaDetail QLabel { background: transparent; }
            QFrame#MediaDetail QScrollArea { background: #1a2026; }
        """)
        self.setObjectName("MediaDetail")
        self._conv_id = None
        self._msg_id = None
        self._resolved_path = None
        self._info: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header with collapse toggle
        hdr = QFrame()
        hdr.setFixedHeight(40)
        hdr.setStyleSheet("QFrame { background: #202c33; border-bottom: 1px solid #2a3942; }")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(4, 0, 8, 0)

        # Collapse/expand arrow button
        self._collapse_btn = QPushButton("\u25B6")  # ▶ = collapse to right
        self._collapse_btn.setFixedSize(24, 28)
        self._collapse_btn.setCursor(Qt.PointingHandCursor)
        self._collapse_btn.setToolTip("Collapse panel")
        self._collapse_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; font-size: 13px;"
            " color: #8696a0; }"
            "QPushButton:hover { color: #e9edef; background: rgba(255,255,255,0.08);"
            " border-radius: 4px; }"
        )
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        hl.addWidget(self._collapse_btn)

        self._hdr_title = QLabel("Media Details")
        self._hdr_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #e9edef;")
        hl.addWidget(self._hdr_title, 1)
        _cb = QPushButton("\u2715 CLOSE")
        _cb.setFixedHeight(26)
        _cb.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #3b4a54; border-radius: 4px;"
            " color: #8696a0; font-size: 10px; padding: 2px 8px; }"
            "QPushButton:hover { color: #e9edef; border-color: #8696a0; }"
        )
        _cb.clicked.connect(lambda: self.setVisible(False))
        self._close_btn = _cb
        hl.addWidget(_cb)
        root.addWidget(hdr)

        # Scrollable content (hidden when collapsed)
        self._scroll = QScrollArea()
        scroll = self._scroll
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        content = QWidget()
        self._cl = QVBoxLayout(content)
        self._cl.setContentsMargins(12, 8, 12, 8)
        self._cl.setSpacing(6)

        # Preview image
        self._preview = QLabel()
        self._preview.setAlignment(Qt.AlignCenter)
        self._preview.setMinimumHeight(80)
        self._preview.setMaximumHeight(300)
        self._preview.setStyleSheet("QLabel { background: #111b21; border-radius: 6px; }")
        self._cl.addWidget(self._preview)

        # Action buttons (always visible)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self._goto_btn = QPushButton("\u2192 GO TO CHAT")
        self._goto_btn.setFixedHeight(32)
        self._goto_btn.setStyleSheet(
            "QPushButton { background: #00897b; border: none; border-radius: 6px;"
            " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #00695c; }"
        )
        self._goto_btn.clicked.connect(self._on_goto)
        btn_row.addWidget(self._goto_btn)
        self._preview_btn = QPushButton("\u25B6 PREVIEW")
        self._preview_btn.setFixedHeight(32)
        self._preview_btn.setStyleSheet(
            "QPushButton { background: #00897b; border: none; border-radius: 6px;"
            " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #00695c; }"
            "QPushButton:disabled { background: #37474f; color: #607d8b; }"
        )
        self._preview_btn.clicked.connect(self._on_preview)
        btn_row.addWidget(self._preview_btn)
        self._open_btn = QPushButton("\u2197 OPEN FILE")
        self._open_btn.setFixedHeight(32)
        self._open_btn.setStyleSheet(
            "QPushButton { background: #1565c0; border: none; border-radius: 6px;"
            " color: white; font-size: 11px; font-weight: bold; padding: 0 12px; }"
            "QPushButton:hover { background: #0d47a1; }"
            "QPushButton:disabled { background: #37474f; color: #607d8b; }"
        )
        self._open_btn.clicked.connect(self._on_open)
        btn_row.addWidget(self._open_btn)
        self._cl.addLayout(btn_row)

        # Dynamic info container (rebuilt on each show_media call)
        self._info_container = QWidget()
        self._info_layout = QVBoxLayout(self._info_container)
        self._info_layout.setContentsMargins(0, 0, 0, 0)
        self._info_layout.setSpacing(0)
        self._cl.addWidget(self._info_container)
        self._cl.addStretch()

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

    def _add_row(self, label: str, value: str):
        """Add a single label+value row to the info container."""
        row = QFrame()
        row.setStyleSheet("QFrame { border-bottom: 1px solid rgba(255,255,255,0.05); }")
        rl = QVBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(1)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-size: 9px; color: #8696a0; font-weight: bold;")
        rl.addWidget(lbl)
        val = QLabel(value)
        val.setStyleSheet("font-size: 11px; color: #e9edef;")
        val.setWordWrap(True)
        val.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rl.addWidget(val)
        self._info_layout.addWidget(row)

    def _add_chat_button_row(self, title: str, subtitle: str, conv_id: int, msg_id: int):
        row = QFrame()
        row.setStyleSheet("QFrame { border-bottom: 1px solid rgba(255,255,255,0.05); }")
        rl = QVBoxLayout(row)
        rl.setContentsMargins(0, 4, 0, 4)
        rl.setSpacing(3)
        lbl = QLabel(title)
        lbl.setStyleSheet("font-size: 10px; color: #e9edef; font-weight: bold;")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        rl.addWidget(lbl)
        sub = QLabel(subtitle)
        sub.setStyleSheet("font-size: 9px; color: #8696a0;")
        sub.setWordWrap(True)
        sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        rl.addWidget(sub)
        btn = QPushButton("Open this message")
        btn.setFixedHeight(24)
        btn.setStyleSheet(
            "QPushButton { background: transparent; border: 1px solid #3b4a54;"
            " border-radius: 4px; color: #00bcd4; font-size: 10px; }"
            "QPushButton:hover { background: rgba(0,188,212,0.12); }"
        )
        btn.clicked.connect(lambda _=False, c=conv_id, m=msg_id: self.go_to_chat.emit(c, m))
        rl.addWidget(btn)
        self._info_layout.addWidget(row)

    def _add_section(self, title: str):
        """Add a section header."""
        lbl = QLabel(title)
        lbl.setStyleSheet(
            "font-size: 10px; color: #00bcd4; font-weight: bold;"
            " padding: 6px 0 2px 0; border-bottom: 1px solid rgba(0,188,212,0.2);")
        self._info_layout.addWidget(lbl)

    def _clear_info(self):
        """Remove all dynamic info rows."""
        while self._info_layout.count():
            item = self._info_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _resolve_device_owner(self) -> tuple[str, str, str]:
        """Look up device owner name + phone + JID from case_metadata,
        cached per panel instance.  Returns (name, phone, jid).  Used to
        expand "Me" / from_me=1 rows into a real, identifiable entry in
        the Sender field of the detail panel.
        """
        cached = getattr(self, "_owner_cache", None)
        if cached is not None:
            return cached
        name = ""
        phone = ""
        jid = ""
        try:
            from app.services.database import Database
            db = Database.get()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_name'")
            if r and r[0]: name = str(r[0]).strip()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_phone'")
            if r and r[0]: phone = str(r[0]).strip()
            r = db.fetchone("SELECT value FROM case_metadata WHERE key='device_owner_jid'")
            if r and r[0]: jid = str(r[0]).strip()
        except Exception as e:
            print(f"[MediaGallery] device-owner lookup failed: {e}")
        self._owner_cache = (name, phone, jid)
        return self._owner_cache

    def _read_exif(self, path: str) -> dict[str, str]:
        """Read EXIF metadata from an image file. Returns {label: value} pairs."""
        exif = {}
        try:
            from PySide6.QtGui import QImageReader
            reader = QImageReader(path)
            # PySide6 doesn't expose EXIF directly, use Python's PIL if available
            try:
                from PIL import Image
                from PIL.ExifTags import TAGS
                img = Image.open(path)
                exif_data = img._getexif()
                if exif_data:
                    for tag_id, val in exif_data.items():
                        tag = TAGS.get(tag_id, str(tag_id))
                        if tag in ("Make", "Model", "DateTime", "DateTimeOriginal",
                                   "ExposureTime", "FNumber", "ISOSpeedRatings",
                                   "FocalLength", "Software", "GPSInfo",
                                   "ImageWidth", "ImageLength", "Orientation"):
                            if isinstance(val, bytes):
                                try:
                                    val = val.decode("utf-8", errors="ignore")
                                except Exception:
                                    val = str(val)
                            exif[tag] = str(val)
                img.close()
            except ImportError:
                pass  # PIL not installed — skip EXIF
            except Exception:
                pass
        except Exception:
            pass
        return exif

    def show_media(self, info: dict, thumb_pxm: QPixmap | None = None):
        """Populate panel with all available media info — dynamic fields.

        Preview area is sized for clarity: panel width minus padding,
        capped at 480px tall.  At a 380px panel that's a 360px-wide
        preview - matches Lightroom / Apple Photos sidebar density.
        Aspect ratio preserved (KeepAspectRatio).
        """
        self._info = dict(info or {})
        # Preview
        if thumb_pxm and not thumb_pxm.isNull():
            pw = max(280, min(self.width() - 24, 380))
            ph = 480   # was 280 - made tall portrait images look squashed
            scaled = thumb_pxm.scaled(
                pw, ph, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._preview.setPixmap(scaled)
            self._preview.setFixedHeight(min(scaled.height() + 10, ph + 10))
        else:
            self._preview.setText("\U0001F4C4 No preview")
            self._preview.setFixedHeight(60)

        # Clear previous info rows
        self._clear_info()

        # Build dynamic fields — only show non-empty ones
        self._add_section("FILE INFO")
        fields = []
        if info.get("name"):
            fields.append(("File Name", info["name"]))
        mime = info.get("mime") or ""
        type_label = info.get("type_label") or ""
        if mime or type_label:
            fields.append(("Type", f"{type_label} ({mime})" if mime else type_label))
        if info.get("size"):
            fields.append(("Size", info["size"]))
        if info.get("resolution"):
            fields.append(("Resolution", info["resolution"]))
        if info.get("duration"):
            fields.append(("Duration", info["duration"]))
        if info.get("caption"):
            fields.append(("Caption", info["caption"]))

        # Status + Recovery info
        status = info.get("status", "missing")
        status_map = {
            "on_disk": "\U0001F7E2 Original (on disk)",
            "downloaded": "\u2B07\uFE0F Downloaded from CDN",
            "hash_linked": "\U0001F517 Found via hash match",
            "hash_linked_after_delete": "\u26A0\uFE0F Originally received here \u2014 file deleted, hash-linked from another message",
            "orphan_recovered": "\U0001F4BE Rescued from orphaned file on disk",
            "downloadable": "\U0001F535 Downloadable (not yet downloaded)",
            "download_failed": "\u274C Download failed (CDN error or expired)",
            "expired": "\U0001F7E0 CDN link expired",
            "no_key": "\U0001F512 No decryption key",
            "thumb_only": "\U0001F5BC Thumbnail only",
            "missing": "\U0001F534 Missing",
        }
        fields.append(("Status", status_map.get(status, status)))

        recovery = info.get("recovery_method")
        if recovery:
            rm_map = {
                "downloaded": (
                    "Recovered by tool — file was downloaded from WhatsApp "
                    "CDN AFTER the phone was extracted.  Was not on the "
                    "device at extraction time."
                ),
                "hash_linked": (
                    "Hash-linked — this message was NOT received on the "
                    "phone.  The displayed file came from a DIFFERENT "
                    "message with the same SHA-256 (content-equivalent, "
                    "but not proof of receipt for this message)."
                ),
                "hash_linked_after_delete": (
                    "Originally received in this chat — msgstore "
                    "transferred=1 confirms the file WAS on the phone at "
                    "some point, but the local copy was later deleted.  "
                    "The same SHA-256 still exists in another message; "
                    "that file is shown for context.  Receipt of this "
                    "specific message IS established (transferred=1); the "
                    "displayed bytes come from a sibling row."
                ),
                "orphan_recovered": (
                    "Rescued from an orphaned file on disk — the chat "
                    "record had no file (cleared chat / reinstall / "
                    "WhatsApp autoclean), but a file with the same "
                    "SHA-256 was still in the WhatsApp media folder. "
                    "The displayed file IS the original bytes — same "
                    "hash as what the message originally carried — even "
                    "though the chat-side link to it was lost."
                ),
            }
            fields.append(("Recovery Method", rm_map.get(recovery, recovery)))
        rts = info.get("recovery_timestamp")
        if rts:
            try:
                fields.append(("Recovered At", _fmt_ts(rts, "full")))
            except Exception:
                pass

        # SHA-256 in hex (canonical form).  msgstore.message_media.file_hash
        # is stored base64-encoded; we decode to hex which is what
        # VirusTotal / hashlookup / timeline tools expect.
        if info.get("file_hash"):
            try:
                import base64 as _b64m
                hex_d = _b64m.b64decode(info["file_hash"]).hex()
                if hex_d:
                    fields.append(("SHA-256", hex_d))
            except Exception:
                fields.append(("SHA-256 (base64)", info["file_hash"]))

        # Disk Path: re-label depending on provenance so the analyst
        # never reads it as the original on-device path for THIS message
        # when actually it came from a hash-linked sibling.
        if info.get("resolved"):
            disk_label = "Disk Path"
            if recovery == "hash_linked":
                disk_label = "Disk Path (hash-linked from another message)"
            elif recovery == "hash_linked_after_delete":
                disk_label = "Disk Path (hash-linked — original was deleted)"
            elif recovery == "orphan_recovered":
                disk_label = "Disk Path (rescued from orphaned file)"
            elif recovery == "downloaded":
                disk_label = "Disk Path (recovered by tool)"
            fields.append((disk_label, info["resolved"]))

        for label, value in fields:
            self._add_row(label, str(value))

        # Message context
        self._add_section("MESSAGE CONTEXT")
        # Build a forensic-grade Sender label.
        # For from_me=1 messages WhatsApp leaves sender_id NULL on the
        # row (see contact_resolver), so "Me" becomes the device owner's
        # name + phone resolved from case_metadata.  For incoming
        # messages we append the contact's phone JID and (when present)
        # LID JID so the analyst sees the authoritative WhatsApp ID.
        from_me = bool(info.get("from_me"))
        sender_lines = []
        if from_me:
            owner_name, owner_phone, owner_jid = self._resolve_device_owner()
            primary = owner_name or "Device owner"
            extras = []
            if owner_phone:
                extras.append(f"+{owner_phone}")
            if owner_jid:
                extras.append(f"[{owner_jid}]")
            sender_label = primary + ("  " + "  ".join(extras) if extras else "") + "  (Me / from_me=1)"
        else:
            primary = info.get("sender_name") or "Unknown"
            extras = []
            sphone = (info.get("sender_phone") or "").strip()
            spjid = (info.get("sender_phone_jid") or "").strip()
            sljid = (info.get("sender_lid_jid") or "").strip()
            if sphone:
                extras.append(f"+{sphone}")
            if spjid:
                extras.append(f"[{spjid}]")
            if sljid:
                extras.append(f"LID:{sljid}")
            sender_label = primary + ("  " + "  ".join(extras) if extras else "")
        if sender_label.strip():
            self._add_row("Sender", sender_label)

        # Chat label: name + full JID so groups and 1:1s are unambiguous
        cname = (info.get("conversation_name") or "").strip()
        cjid  = (info.get("conversation_jid") or "").strip()
        if cname or cjid:
            chat_label = cname or "Unknown"
            if cjid:
                chat_label += f"  [{cjid}]"
            self._add_row("Chat", chat_label)

        ts = info.get("timestamp")
        if ts:
            try:
                self._add_row("Message Date", _fmt_ts(ts, "full"))
            except Exception:
                pass

        if info.get("message_id"):
            self._add_row("Message ID", str(info["message_id"]))

        self._add_shared_instances(info)

        # EXIF data (if file on disk and is an image)
        resolved = info.get("resolved")
        if resolved and os.path.isfile(resolved):
            mime_lower = (info.get("mime") or "").lower()
            if "image" in mime_lower or info.get("type_label") in ("image", "sticker"):
                exif = self._read_exif(resolved)
                if exif:
                    self._add_section("EXIF DATA")
                    _exif_labels = {
                        "Make": "Camera Make", "Model": "Camera Model",
                        "DateTime": "Date/Time", "DateTimeOriginal": "Original Date",
                        "ExposureTime": "Exposure", "FNumber": "Aperture (f/)",
                        "ISOSpeedRatings": "ISO", "FocalLength": "Focal Length",
                        "Software": "Software", "Orientation": "Orientation",
                    }
                    for tag, val in exif.items():
                        label = _exif_labels.get(tag, tag)
                        self._add_row(label, val)

        self._conv_id = info.get("conversation_id")
        self._msg_id = info.get("message_id")
        self._resolved_path = resolved
        self._preview_btn.setEnabled(bool(info.get("file_exists")))
        self._open_btn.setEnabled(bool(info.get("file_exists")))
        self.setVisible(True)

    def _add_shared_instances(self, info: dict):
        file_hash = info.get("file_hash") or ""
        if not file_hash:
            return
        try:
            from app.services.database import Database
            rows = Database.get().fetchall(
                """SELECT me.id, me.message_id, m.conversation_id, m.from_me,
                          COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
                          COALESCE(conv.jid_raw_string, '') AS conv_jid,
                          CASE WHEN m.from_me = 1 THEN 'You'
                               ELSE COALESCE(sc.resolved_name, sc.display_name, sc.wa_name, sc.phone_number, 'Unknown')
                          END AS sender_name,
                          COALESCE(sc.phone_number, '') AS sender_phone,
                          COALESCE(sc.phone_jid, '')    AS sender_phone_jid,
                          COALESCE(sc.lid_jid, '')      AS sender_lid_jid,
                          CASE WHEN m.from_me = 1 THEN 'Sent' ELSE 'Received' END AS direction,
                          m.timestamp
                   FROM media me
                   LEFT JOIN message m ON m.id = me.message_id
                   LEFT JOIN conversation conv ON conv.id = m.conversation_id
                   LEFT JOIN contact sc ON sc.id = m.sender_id
                   WHERE me.file_hash = ?
                   ORDER BY m.timestamp ASC, me.id ASC
                   LIMIT 25""",
                (file_hash,),
            )
        except Exception:
            return
        if len(rows) <= 1:
            return

        # Resolve owner once for the from_me=1 entries
        owner_name, owner_phone, owner_jid = self._resolve_device_owner()

        self._add_section(f"SHARED INSTANCES ({len(rows)} found)")
        for idx, row in enumerate(rows[:10]):
            when = ""
            if row["timestamp"]:
                try:
                    when = _fmt_ts(row["timestamp"], "full")
                except Exception:
                    pass
            title_prefix = "First found" if idx == 0 else f"Also shared #{idx + 1}"
            # Chat: name + JID
            conv = row["conv_name"] or "Unknown chat"
            cjid = (row["conv_jid"] or "").strip()
            chat_label = conv + (f"  [{cjid}]" if cjid else "")
            # Sender: name + phone + JID; expand "Me" to the owner
            if row["from_me"]:
                primary = owner_name or "Device owner"
                pieces = []
                if owner_phone: pieces.append(f"+{owner_phone}")
                if owner_jid:   pieces.append(f"[{owner_jid}]")
                sender_lbl = primary + ("  " + "  ".join(pieces) if pieces else "") + "  (Me)"
            else:
                primary = row["sender_name"] or "Unknown"
                pieces = []
                sp = (row["sender_phone"] or "").strip()
                pj = (row["sender_phone_jid"] or "").strip()
                lj = (row["sender_lid_jid"] or "").strip()
                if sp: pieces.append(f"+{sp}")
                if pj: pieces.append(f"[{pj}]")
                if lj: pieces.append(f"LID:{lj}")
                sender_lbl = primary + ("  " + "  ".join(pieces) if pieces else "")
            subtitle = " | ".join(part for part in [row["direction"], sender_lbl, when] if part)
            if row["conversation_id"] and row["message_id"]:
                self._add_chat_button_row(
                    f"{title_prefix}: {chat_label}", subtitle,
                    row["conversation_id"], row["message_id"],
                )
            else:
                self._add_row(f"{title_prefix}: {chat_label}", subtitle)
        remaining = len(rows) - 10
        if remaining > 0:
            self._add_row("More copies", f"{remaining} more not shown here. Use Find Copies from the tile menu for the full list.")

    def _toggle_collapse(self):
        """Toggle between expanded (380px) and collapsed (28px thin strip)."""
        self._panel_expanded = not self._panel_expanded
        if self._panel_expanded:
            self.setFixedWidth(self.EXPANDED_W)
            self._collapse_btn.setText("\u25B6")  # ▶ collapse
            self._collapse_btn.setToolTip("Collapse panel")
            self._hdr_title.show()
            self._close_btn.show()
            self._scroll.show()
        else:
            self.setFixedWidth(self.COLLAPSED_W)
            self._collapse_btn.setText("\u25C0")  # ◀ expand
            self._collapse_btn.setToolTip("Expand panel")
            self._hdr_title.hide()
            self._close_btn.hide()
            self._scroll.hide()

    def _on_goto(self):
        if self._conv_id and self._msg_id:
            self.go_to_chat.emit(self._conv_id, self._msg_id)

    def _on_preview(self):
        if self._resolved_path and os.path.isfile(self._resolved_path):
            self.preview_requested.emit(self._info)

    def _on_open(self):
        if self._resolved_path and os.path.isfile(self._resolved_path):
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._resolved_path))


class MediaGalleryPage(QWidget):
    # Signal: navigate to conversation and message
    go_to_chat = Signal(int, int)  # (conversation_id, message_id)
    # Signal: navigate to Image Similarity page pre-loaded with this msg_id
    find_similar_requested = Signal(int)  # message_id

    def __init__(self, parent=None):
        super().__init__(parent)
        # Top-level: horizontal split — main content (left) + detail panel (right)
        _outer = QHBoxLayout(self)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.setSpacing(0)
        _main = QWidget()
        layout = QVBoxLayout(_main)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        _outer.addWidget(_main, 1)

        # Header
        header = QHBoxLayout()
        title = QLabel("Media Gallery")
        f = QFont(); f.setPointSize(16); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #78909c; font-size: 11px;")
        header.addWidget(self._count_label)
        header.addStretch()
        # Whole-case media-forensics report — opens the customisation
        # dialog so the analyst picks scope / sections / layout etc.
        self._media_report_btn = QPushButton("\U0001F4CA Media Dashboard…")
        self._media_report_btn.setFixedHeight(28)
        self._media_report_btn.setStyleSheet(
            "QPushButton { padding: 3px 14px; border-radius: 12px;"
            " border: 1px solid #00897b; color: #00897b; font-size: 11px;"
            " font-weight: 600; background: transparent; }"
            " QPushButton:hover { background: rgba(0,137,123,0.10); }"
        )
        self._media_report_btn.setToolTip(
            "Build the offline Media Forensics Dashboard — folder-shaped "
            "artifact with cascading filters (conversation, sender, status, "
            "MIME, extension, date), per-day histogram, sharded thumbnails "
            "and in-browser CSV/XLSX/HTML export.  Scales to ~200k rows."
        )
        self._media_report_btn.clicked.connect(self._open_media_report_dialog)
        header.addWidget(self._media_report_btn)
        layout.addLayout(header)

        # Stats bar (collapsible — hidden by default to save space)
        self._stats_container = QWidget()
        self._stats_container.setVisible(False)
        _stats_outer = QVBoxLayout(self._stats_container)
        _stats_outer.setContentsMargins(0, 0, 0, 0)
        _stats_outer.setSpacing(4)
        self._stats_bar = QHBoxLayout()
        self._stats_bar.setSpacing(10)
        self._stat_labels: dict[str, QLabel] = {}
        for key, label_text, color in [
            ("total", "Total", "#00bcd4"),
            ("on_disk", "On Disk", "#66bb6a"),
            ("downloadable", "Downloadable", "#42a5f5"),
            ("expired", "Expired", "#ff7043"),
            ("total_size", "Total Size", "#ab47bc"),
        ]:
            frame = QFrame()
            from app.services.theme_manager import ThemeManager
            _tm = ThemeManager.get()
            frame.setStyleSheet(_tm.stat_frame_style())
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(8, 4, 8, 4)
            fl.setSpacing(1)
            lbl = QLabel(label_text)
            lbl.setStyleSheet(_tm.stat_label_style())
            val = QLabel("...")
            val.setStyleSheet(f"color: {color}; font-size: 13px; font-weight: bold;")
            fl.addWidget(lbl)
            fl.addWidget(val)
            self._stats_bar.addWidget(frame)
            self._stat_labels[key] = val
        # Resolve by Hash button
        self._resolve_hash_btn = QPushButton("Resolve by Hash")
        self._resolve_hash_btn.setFixedHeight(28)
        self._resolve_hash_btn.setStyleSheet(
            "QPushButton { padding: 3px 10px; border-radius: 12px;"
            " border: 1px solid rgba(128,128,128,0.18); font-size: 10px; }"
            " QPushButton:hover { background: rgba(0,188,212,0.15);"
            " border-color: #00bcd4; color: #00bcd4; }"
        )
        self._resolve_hash_btn.setToolTip(
            "Find missing files where another copy with the same hash exists on disk"
        )
        self._resolve_hash_btn.clicked.connect(self._resolve_by_hash)
        self._stats_bar.addWidget(self._resolve_hash_btn)

        self._stats_bar.addStretch()
        _stats_outer.addLayout(self._stats_bar)

        # Legend
        legend = QHBoxLayout()
        legend.setSpacing(12)
        _legend_tips = {
            "Original": "File found on device storage in WhatsApp Media folder",
            "Downloaded": "Recovered by downloading from WhatsApp CDN + decrypting",
            "Hash recovered": "Found identical file (same SHA-256) in another chat",
            "Downloadable": "CDN URL + key available — can be downloaded via Media Recovery",
            "Expired": "CDN link expired (>30 days) — file cannot be downloaded anymore",
            "Missing": "File not found, no CDN URL or key available",
        }
        for color, label in [
            ("#50c850", "Original"), ("#00b4dc", "Downloaded"),
            ("#a064dc", "Hash recovered"), ("#50b4ff", "Downloadable"),
            ("#c86432", "Expired"), ("#963c3c", "Missing"),
        ]:
            dot = QLabel(f'<span style="color:{color};">\u25CF</span> {label}')
            dot.setStyleSheet("font-size: 9px; color: #aaa;")
            if label in _legend_tips:
                dot.setToolTip(_legend_tips[label])
                dot.setCursor(Qt.WhatsThisCursor)
            legend.addWidget(dot)
        legend.addStretch()
        _stats_outer.addLayout(legend)
        layout.addWidget(self._stats_container)

        # Toggle button for stats
        self._stats_toggle = QPushButton("\u25B6 Stats")
        self._stats_toggle.setFixedHeight(20)
        self._stats_toggle.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " font-size: 9px; color: #78909c; text-align: left; }"
            "QPushButton:hover { color: #00bcd4; }"
        )
        self._stats_toggle.clicked.connect(self._toggle_stats)
        layout.addWidget(self._stats_toggle)

        # Search + Filter toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by name or caption...")
        self._search.setFixedHeight(32)
        self._search.setClearButtonEnabled(True)
        toolbar.addWidget(self._search, 1)

        self._filter_btns: dict[str, QPushButton] = {}
        _filter_tooltips = {
            "all": "Show all media files regardless of status or type",
            "images": "Photos, screenshots, and image files (JPEG, PNG, WebP)",
            "videos": "Video files including GIFs and animated content (MP4, 3GP)",
            "audio": "Audio messages, voice notes, and music files (OGG, MP3, AAC)",
            "docs": "Documents, PDFs, spreadsheets, and other file attachments",
            "stickers": "WhatsApp sticker images (WebP format)",
            "on_disk": "Original files found on the device's storage — "
                       "these are unmodified files from the WhatsApp Media folder",
            "downloaded": "Files recovered by downloading from WhatsApp's CDN servers "
                          "using the media URL + decryption key stored in msgstore.db. "
                          "These are bit-for-bit identical to the original.",
            "recovered": "Files found via SHA-256 hash matching — the original file "
                         "was missing but an identical copy was found in another chat "
                         "or conversation on the same device.",
            "downloadable": "Files that CAN be downloaded from WhatsApp CDN — the URL "
                            "and decryption key are available in msgstore.db but the file "
                            "hasn't been downloaded yet. Use Media Recovery to download.",
        }
        for fid, label in [
            ("all", "All"), ("images", "Images"), ("videos", "Videos"),
            ("audio", "Audio"), ("docs", "Documents"), ("stickers", "Stickers"),
            ("on_disk", "On Disk"), ("downloaded", "Downloaded"),
            ("recovered", "Recovered"), ("downloadable", "Downloadable"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet("""
                QPushButton { padding: 3px 10px; border-radius: 12px;
                              border: 1px solid rgba(128,128,128,0.18); font-size: 10px; }
                QPushButton:checked { background: rgba(0,188,212,0.2);
                                      border-color: #00bcd4; color: #00bcd4; }
                QPushButton:hover:!checked { background: rgba(128,128,128,0.08); }
            """)
            btn.clicked.connect(self._on_filter)
            if fid in _filter_tooltips:
                btn.setToolTip(_filter_tooltips[fid])
            if fid == "all":
                btn.setChecked(True)
            toolbar.addWidget(btn)
            self._filter_btns[fid] = btn
        layout.addLayout(toolbar)

        # --- Filter toolbar row 2: Sender | Conversation | Stickers Off | Sort ---
        from app.services.theme_manager import ThemeManager
        _tm = ThemeManager.get()

        _combo_style = (
            "QComboBox { padding: 3px 8px; border-radius: 6px;"
            " border: 1px solid #d0d7de; font-size: 10px; color: #333;"
            " background: #fff; min-width: 140px; }"
            " QComboBox:hover { border-color: #00897b; }"
            " QComboBox::drop-down { border: none; width: 18px; }"
            " QComboBox::down-arrow { image: none; border-left: 4px solid transparent;"
            " border-right: 4px solid transparent; border-top: 5px solid #667781; }"
            " QComboBox QAbstractItemView { background: #fff; border: 1px solid #d0d7de;"
            " selection-background-color: rgba(0,137,123,0.12); color: #333;"
            " font-size: 10px; }"
        ) if _tm.is_light else (
            "QComboBox { padding: 3px 8px; border-radius: 6px;"
            " border: 1px solid rgba(255,255,255,0.12); font-size: 10px;"
            " color: rgba(255,255,255,0.8); background: rgba(255,255,255,0.06);"
            " min-width: 140px; }"
            " QComboBox:hover { border-color: #00bcd4; }"
            " QComboBox::drop-down { border: none; width: 18px; }"
            " QComboBox::down-arrow { image: none; border-left: 4px solid transparent;"
            " border-right: 4px solid transparent; border-top: 5px solid #aebac1; }"
            " QComboBox QAbstractItemView { background: #1a2730;"
            " border: 1px solid #2a3a44; selection-background-color: rgba(0,188,212,0.2);"
            " color: #e0e0e0; font-size: 10px; }"
        )

        _toggle_on_style = (
            "QPushButton { padding: 3px 10px; border-radius: 12px;"
            " border: 1px solid #d0d7de; font-size: 10px; color: #555; background: #fff; }"
            " QPushButton:checked { background: rgba(0,137,123,0.12);"
            " border-color: #00897b; color: #00695c; font-weight: bold; }"
            " QPushButton:hover:!checked { background: #f6f8fa; }"
        ) if _tm.is_light else (
            "QPushButton { padding: 3px 10px; border-radius: 12px;"
            " border: 1px solid rgba(255,255,255,0.12); font-size: 10px;"
            " color: rgba(255,255,255,0.7); background: transparent; }"
            " QPushButton:checked { background: rgba(0,188,212,0.2);"
            " border-color: #00bcd4; color: #00bcd4; font-weight: bold; }"
            " QPushButton:hover:!checked { background: rgba(255,255,255,0.05); }"
        )

        filter_row2 = QHBoxLayout()
        filter_row2.setSpacing(8)

        # Sender multi-select with search
        _combo_btn_style = (
            "QPushButton { padding: 3px 8px; border-radius: 6px;"
            " border: 1px solid #d0d7de; font-size: 10px; color: #333;"
            " background: #fff; min-width: 160px; text-align: left; }"
            " QPushButton:hover { border-color: #00897b; }"
            " QPushButton:checked { border-color: #00897b; background: rgba(0,137,123,0.06); }"
        ) if _tm.is_light else (
            "QPushButton { padding: 3px 8px; border-radius: 6px;"
            " border: 1px solid rgba(255,255,255,0.12); font-size: 10px;"
            " color: rgba(255,255,255,0.8); background: rgba(255,255,255,0.06);"
            " min-width: 160px; text-align: left; }"
            " QPushButton:hover { border-color: #00bcd4; }"
            " QPushButton:checked { border-color: #00bcd4; background: rgba(0,188,212,0.1); }"
        )
        _dropdown_style = (
            "QFrame { background: #fff; border: 1px solid #d0d7de;"
            " border-radius: 6px; }"
        ) if _tm.is_light else (
            "QFrame { background: #1a2730; border: 1px solid #2a3a44;"
            " border-radius: 6px; }"
        )

        sender_lbl = QLabel("Sender:")
        sender_lbl.setStyleSheet("font-size: 10px; color: #667781;")
        filter_row2.addWidget(sender_lbl)
        self._sender_combo = _CheckableSearchCombo("All Senders")
        self._sender_combo.set_style(_combo_btn_style, _dropdown_style)
        self._sender_combo.setFixedWidth(220)
        self._sender_combo.selection_changed.connect(self._on_sender_filter_changed)
        filter_row2.addWidget(self._sender_combo)

        # Conversation multi-select with search
        conv_lbl = QLabel("Chat:")
        conv_lbl.setStyleSheet("font-size: 10px; color: #667781;")
        filter_row2.addWidget(conv_lbl)
        self._conv_combo = _CheckableSearchCombo("All Conversations")
        self._conv_combo.set_style(_combo_btn_style, _dropdown_style)
        self._conv_combo.setFixedWidth(260)
        self._conv_combo.selection_changed.connect(self._on_conversation_filter_changed)
        filter_row2.addWidget(self._conv_combo)

        # Stickers Off toggle
        self._stickers_off_btn = QPushButton("Stickers Off")
        self._stickers_off_btn.setCheckable(True)
        self._stickers_off_btn.setChecked(True)  # ON by default
        self._stickers_off_btn.setFixedHeight(28)
        self._stickers_off_btn.setStyleSheet(_toggle_on_style)
        self._stickers_off_btn.clicked.connect(lambda: self._apply())
        filter_row2.addWidget(self._stickers_off_btn)

        # Owner's media quick filter
        self._owner_btn = QPushButton("My Media")
        self._owner_btn.setCheckable(True)
        self._owner_btn.setFixedHeight(28)
        self._owner_btn.setStyleSheet(_toggle_on_style)
        self._owner_btn.clicked.connect(lambda: self._apply())
        filter_row2.addWidget(self._owner_btn)

        # Sort combo
        sort_lbl = QLabel("Sort:")
        sort_lbl.setStyleSheet("font-size: 10px; color: #667781;")
        filter_row2.addWidget(sort_lbl)
        self._sort_combo = QComboBox()
        self._sort_combo.setStyleSheet(_combo_style)
        self._sort_combo.setFixedHeight(28)
        self._sort_combo.addItem("Newest First", "m.timestamp DESC")
        self._sort_combo.addItem("Oldest First", "m.timestamp ASC")
        self._sort_combo.addItem("Largest First", "me.file_size DESC")
        self._sort_combo.addItem("Smallest First", "me.file_size ASC")
        self._sort_combo.addItem("By Type", "me.mime_type ASC, me.id DESC")
        # Most-shared sort: groups by file_hash and counts how many
        # message rows share the same hash, then orders DESC.  A photo
        # forwarded across 12 chats sorts above one shared in 1 chat.
        # NULL/empty hashes (recovered-only thumbnails with no SHA) sort
        # last so they don't pollute the top.
        self._sort_combo.addItem(
            "Most Shared First",
            "CASE WHEN me.file_hash IS NULL OR me.file_hash = '' THEN 0 "
            "  ELSE (SELECT COUNT(*) FROM media mx WHERE mx.file_hash = me.file_hash) "
            "END DESC, m.timestamp DESC",
        )
        self._sort_combo.addItem("Tagged First", "_tagged_sort")
        self._sort_combo.currentIndexChanged.connect(lambda: self._apply())
        filter_row2.addWidget(self._sort_combo)

        # --- Size filter: Min KB | Max KB | "Tagged only" toggle ---
        # Two numeric inputs accept KB.  Either may be left
        # blank.  Useful for surfacing large-file outliers
        # (full-quality photos, long videos) during evidence
        # triage.
        size_lbl = QLabel("Size:")
        size_lbl.setStyleSheet("color: #888; font-size: 11px; padding: 0 4px;")
        filter_row2.addWidget(size_lbl)
        self._size_min_input = QLineEdit()
        self._size_min_input.setPlaceholderText("min KB")
        self._size_min_input.setFixedWidth(80)
        self._size_min_input.setFixedHeight(28)
        self._size_min_input.setToolTip(
            "Minimum file size in KB.  Only media at or above this size "
            "shows in the gallery.  Useful for filtering out tiny "
            "thumbnails / icons during forensic review."
        )
        self._size_min_input.editingFinished.connect(lambda: self._apply())
        filter_row2.addWidget(self._size_min_input)
        _size_dash = QLabel("–")
        _size_dash.setStyleSheet("color: #888; font-size: 11px;")
        filter_row2.addWidget(_size_dash)
        self._size_max_input = QLineEdit()
        self._size_max_input.setPlaceholderText("max KB")
        self._size_max_input.setFixedWidth(80)
        self._size_max_input.setFixedHeight(28)
        self._size_max_input.setToolTip(
            "Maximum file size in KB.  Leave blank for no upper limit."
        )
        self._size_max_input.editingFinished.connect(lambda: self._apply())
        filter_row2.addWidget(self._size_max_input)

        # "Tagged only" toggle - quick filter to show only investigator-
        # tagged media without picking a tag name.
        self._tagged_only_btn = QPushButton("\U0001F516 Tagged only")
        self._tagged_only_btn.setCheckable(True)
        self._tagged_only_btn.setFixedHeight(28)
        self._tagged_only_btn.setToolTip(
            "Show only media items you've tagged.  Toggle off to see all."
        )
        self._tagged_only_btn.setStyleSheet(
            "QPushButton { border: 1px solid rgba(120,120,120,0.4); "
            " border-radius: 14px; padding: 2px 12px; font-size: 11px; "
            " background: transparent; } "
            "QPushButton:checked { background: #00897b; color: #fff; "
            " border-color: #00695c; font-weight: 600; }"
        )
        self._tagged_only_btn.toggled.connect(lambda: self._apply())
        filter_row2.addWidget(self._tagged_only_btn)

        filter_row2.addStretch()
        layout.addLayout(filter_row2)

        # --- Filter toolbar row 3: Date range — Quick picks + Calendar heatmap ---
        _qp_style = (
            "QPushButton { padding: 2px 8px; border-radius: 10px;"
            " border: 1px solid #d0d7de; font-size: 9px; color: #555; background: #fff; }"
            " QPushButton:checked { background: rgba(0,137,123,0.12);"
            " border-color: #00897b; color: #00695c; font-weight: bold; }"
            " QPushButton:hover:!checked { background: #f6f8fa; }"
        ) if _tm.is_light else (
            "QPushButton { padding: 2px 8px; border-radius: 10px;"
            " border: 1px solid rgba(255,255,255,0.12); font-size: 9px;"
            " color: rgba(255,255,255,0.7); }"
            " QPushButton:checked { background: rgba(0,188,212,0.2);"
            " border-color: #00bcd4; color: #00bcd4; font-weight: bold; }"
            " QPushButton:hover:!checked { background: rgba(255,255,255,0.05); }"
        )

        date_row = QHBoxLayout()
        date_row.setSpacing(6)
        date_lbl = QLabel("Date:")
        date_lbl.setStyleSheet("font-size: 10px; color: #667781;")
        date_row.addWidget(date_lbl)

        self._date_quick_btns: dict[str, QPushButton] = {}
        for qid, qlabel in [
            ("all_time", "All Time"), ("7d", "7 Days"), ("30d", "30 Days"),
            ("3m", "3 Months"), ("1y", "1 Year"), ("calendar", "\U0001F4C5 Calendar"),
        ]:
            qbtn = QPushButton(qlabel)
            qbtn.setCheckable(True)
            qbtn.setFixedHeight(28)
            qbtn.setStyleSheet(_qp_style)
            qbtn.setProperty("date_id", qid)
            qbtn.clicked.connect(self._on_date_quick_pick)
            if qid == "all_time":
                qbtn.setChecked(True)
            date_row.addWidget(qbtn)
            self._date_quick_btns[qid] = qbtn

        date_row.addStretch()
        layout.addLayout(date_row)

        # Calendar heatmap widget (hidden by default, shown when "Calendar" clicked)
        self._calendar_heatmap = CalendarHeatmapWidget()
        self._calendar_heatmap.setVisible(False)
        self._calendar_heatmap.range_selected.connect(self._on_calendar_range)
        self._calendar_heatmap.date_selected.connect(self._on_calendar_day)
        self._calendar_heatmap.range_cleared.connect(self._on_calendar_cleared)
        # Apply/Close action-bar signals - hide the calendar so the user
        # isn't trapped inside it after picking a date.  Selection is
        # already emitted via date_selected/range_selected on click.
        self._calendar_heatmap.apply_requested.connect(self._on_calendar_apply)
        self._calendar_heatmap.close_requested.connect(self._on_calendar_close)
        layout.addWidget(self._calendar_heatmap)

        # Track active date filter
        self._date_mode = "all_time"
        self._calendar_start: _date_type | None = None
        self._calendar_end: _date_type | None = None

        # View toggle
        toggle_row = QHBoxLayout()
        self._view_grid_btn = QPushButton("Grid")
        self._view_grid_btn.setCheckable(True)
        self._view_grid_btn.setChecked(True)
        self._view_grid_btn.setFixedSize(60, 24)
        _ts = """QPushButton { padding: 2px 8px; border-radius: 4px;
                     border: 1px solid rgba(128,128,128,0.15); font-size: 10px; }
                 QPushButton:checked { background: rgba(0,188,212,0.15);
                     border-color: #00bcd4; color: #00bcd4; }"""
        self._view_grid_btn.setStyleSheet(_ts)
        self._view_grid_btn.clicked.connect(lambda: self._set_view("grid"))
        toggle_row.addWidget(self._view_grid_btn)

        self._view_table_btn = QPushButton("Table")
        self._view_table_btn.setCheckable(True)
        self._view_table_btn.setFixedSize(60, 24)
        self._view_table_btn.setStyleSheet(_ts)
        self._view_table_btn.clicked.connect(lambda: self._set_view("table"))
        toggle_row.addWidget(self._view_table_btn)

        # Thumbnail size slider
        toggle_row.addSpacing(16)
        size_icon_lbl = QLabel("Tile:")
        size_icon_lbl.setStyleSheet("font-size: 10px; color: #667781;")
        toggle_row.addWidget(size_icon_lbl)
        self._size_slider = QSlider(Qt.Horizontal)
        self._size_slider.setRange(80, 400)
        self._size_slider.setSingleStep(20)
        self._size_slider.setPageStep(40)
        self._size_slider.setValue(160)
        self._size_slider.setFixedWidth(140)
        self._size_slider.setFixedHeight(20)
        self._size_slider.valueChanged.connect(self._on_tile_size_changed)
        toggle_row.addWidget(self._size_slider)
        self._size_label = QLabel("160px")
        self._size_label.setStyleSheet("font-size: 10px; color: #667781; min-width: 36px;")
        toggle_row.addWidget(self._size_label)

        toggle_row.addStretch()
        layout.addLayout(toggle_row)

        # Model
        self._model = MediaGalleryModel()

        # Grid view
        self._grid = QListView()
        self._grid.setModel(self._model)
        self._grid_delegate = MediaThumbnailDelegate()
        self._grid.setItemDelegate(self._grid_delegate)
        self._grid.setViewMode(QListView.IconMode)
        self._grid.setResizeMode(QListView.Adjust)
        self._grid.setGridSize(QSize(164, 164))
        self._grid.setWrapping(True)
        self._grid.setSpacing(3)
        self._grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._grid.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._grid.setUniformItemSizes(True)
        self._grid.setStyleSheet("""
            QListView { background-color: transparent; border: none; }
            QListView::item { border: 1px solid rgba(128,128,128,0.08); border-radius: 4px; }
            QListView::item:selected { border: 2px solid #00bcd4; }
        """)
        self._grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self._grid.customContextMenuRequested.connect(self._grid_context_menu)
        self._grid.doubleClicked.connect(self._on_grid_double_click)
        self._grid.clicked.connect(self._on_grid_click)

        # Table view
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(28)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 120), (2, 80), (3, 90), (4, 80), (5, 180)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._table_context_menu)
        self._table.doubleClicked.connect(self._on_table_double_click)
        self._table.clicked.connect(self._on_table_click)
        self._table.setVisible(False)

        # Content area: grid/table + detail panel side by side
        content_area = QHBoxLayout()
        content_area.setContentsMargins(0, 0, 0, 0)
        content_area.setSpacing(0)
        _view_container = QWidget()
        _vcl = QVBoxLayout(_view_container)
        _vcl.setContentsMargins(0, 0, 0, 0)
        _vcl.setSpacing(0)
        _vcl.addWidget(self._grid, 1)
        _vcl.addWidget(self._table, 1)
        content_area.addWidget(_view_container, 1)

        _content_w = QWidget()
        _content_w.setLayout(content_area)
        layout.addWidget(_content_w, 1)

        # Detail panel (right side, full height, hidden by default)
        self._detail_panel = _MediaDetailPanel(self)
        self._detail_panel.setVisible(False)
        self._detail_panel.go_to_chat.connect(lambda cid, mid: self.go_to_chat.emit(cid, mid))
        self._detail_panel.preview_requested.connect(self._open_preview_info)
        _outer.addWidget(self._detail_panel)
        self._current_view = "grid"

        # --- MG-03: Floating batch action bar (shown when multiple items selected) ---
        self._batch_bar = QFrame(self)
        self._batch_bar.setStyleSheet(
            "QFrame { background: rgba(0,30,40,0.92); border-radius: 10px;"
            " border: 1px solid #00bcd4; }"
        )
        batch_layout = QHBoxLayout(self._batch_bar)
        batch_layout.setContentsMargins(14, 6, 14, 6)
        batch_layout.setSpacing(10)
        self._batch_count_label = QLabel("0 selected")
        self._batch_count_label.setStyleSheet(
            "color: #e0f7fa; font-size: 12px; font-weight: bold;"
        )
        batch_layout.addWidget(self._batch_count_label)
        self._batch_export_btn = QPushButton("Export Selected")
        self._batch_export_btn.setStyleSheet(
            "QPushButton { background: #00897b; color: white; border: none;"
            " border-radius: 6px; padding: 4px 14px; font-size: 11px; }"
            " QPushButton:hover { background: #00bcd4; }"
        )
        self._batch_export_btn.clicked.connect(self._batch_export)
        batch_layout.addWidget(self._batch_export_btn)
        self._batch_copy_btn = QPushButton("Copy Paths")
        self._batch_copy_btn.setStyleSheet(
            "QPushButton { background: #455a64; color: white; border: none;"
            " border-radius: 6px; padding: 4px 14px; font-size: 11px; }"
            " QPushButton:hover { background: #607d8b; }"
        )
        self._batch_copy_btn.clicked.connect(self._batch_copy_paths)
        batch_layout.addWidget(self._batch_copy_btn)
        self._batch_bar.setFixedHeight(40)
        self._batch_bar.setVisible(False)

        self._grid.selectionModel().selectionChanged.connect(self._on_selection_changed)

        # --- MG-05: Smooth scrollbar feel ---
        self._grid.verticalScrollBar().setSingleStep(20)

        # Scroll position indicator overlay
        self._scroll_indicator = QLabel(self)
        self._scroll_indicator.setStyleSheet(
            "background: rgba(0,30,40,0.80); color: #e0f7fa; font-size: 11px;"
            " font-weight: bold; padding: 4px 10px; border-radius: 8px;"
        )
        self._scroll_indicator.setAlignment(Qt.AlignCenter)
        self._scroll_indicator.setVisible(False)

        self._scroll_indicator_timer = QTimer()
        self._scroll_indicator_timer.setSingleShot(True)
        self._scroll_indicator_timer.setInterval(1000)
        self._scroll_indicator_timer.timeout.connect(
            lambda: self._scroll_indicator.setVisible(False)
        )
        self._grid.verticalScrollBar().valueChanged.connect(self._on_scroll_position)

        # --- MG-06: Keyboard shortcuts ---
        QShortcut(QKeySequence(Qt.Key_Space), self, self._shortcut_preview)
        QShortcut(QKeySequence(Qt.Key_Return), self, self._shortcut_go_to_chat)
        QShortcut(QKeySequence.SelectAll, self, self._shortcut_select_all)
        QShortcut(QKeySequence("Ctrl+E"), self, self._batch_export)

        # Ctrl+Mouse Wheel zoom — override grid wheel event
        self._orig_grid_wheel_event = self._grid.wheelEvent
        self._grid.wheelEvent = self._grid_wheel_event

        # --- PF-03: Scroll debounce for thumbnail loading ---
        self._scroll_debounce_timer = QTimer()
        self._scroll_debounce_timer.setSingleShot(True)
        self._scroll_debounce_timer.setInterval(100)
        self._scroll_debounce_timer.timeout.connect(self._load_visible_thumbnails)
        self._is_fast_scrolling = False
        self._grid.verticalScrollBar().valueChanged.connect(self._on_scroll_debounce)

        # Debounce search
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._apply)
        self._search.textChanged.connect(lambda: self._search_timer.start())

        self._current_filter = "all"
        self._combos_ready = False
        self._combo_refreshing = False
        self._needs_reload = False
        self._loaded_once = False
        QTimer.singleShot(50, self._apply)
        QTimer.singleShot(100, self._load_stats)
        QTimer.singleShot(1200, self._load_filter_options)

    def resizeEvent(self, event):
        """Reposition floating batch bar and scroll indicator on resize."""
        super().resizeEvent(event)
        self._position_batch_bar()
        self._position_scroll_indicator()

    def hideEvent(self, event):
        """Release heavy gallery caches when navigating away.

        After the SQLite-backed L2 / QPixmapCache L1 refactor, the
        delegate no longer owns a dict cache.  We just clear the small
        scaled-pixmap helper cache and reset the fast-scroll flag.
        QPixmapCache is process-global and managed by Qt - no manual
        clearing needed; Qt evicts on its own LRU schedule.
        """
        super().hideEvent(event)
        if hasattr(self, "_grid_delegate"):
            try:
                self._grid_delegate._scaled_cache.clear()
            except Exception:
                pass
            self._grid_delegate.skip_uncached = True
        if hasattr(self, "_model") and self._model.rowCount() > 0:
            self._model.clear()
            self._needs_reload = True
        if hasattr(self, "_detail_panel"):
            self._detail_panel.setVisible(False)

    def showEvent(self, event):
        """Reload a small first batch after caches were released."""
        super().showEvent(event)
        if getattr(self, "_needs_reload", False) or not getattr(self, "_loaded_once", False):
            self._needs_reload = False
            if hasattr(self, "_grid_delegate"):
                self._grid_delegate.skip_uncached = False
            QTimer.singleShot(0, self._apply)
            QTimer.singleShot(100, self._load_stats)
            if not getattr(self, "_combos_ready", False):
                QTimer.singleShot(600, self._load_filter_options)

    def _position_batch_bar(self):
        """Center the batch bar at the bottom of the widget."""
        if self._batch_bar.isVisible():
            bw = min(380, self.width() - 40)
            self._batch_bar.setFixedWidth(bw)
            x = (self.width() - bw) // 2
            y = self.height() - 56
            self._batch_bar.move(x, y)

    def _position_scroll_indicator(self):
        """Position the scroll indicator at the top-right of the grid area."""
        x = self.width() - self._scroll_indicator.sizeHint().width() - 24
        y = self._grid.y() + 8
        self._scroll_indicator.move(max(0, x), y)

    # --- MG-03: Batch action methods ---

    def _on_selection_changed(self):
        """Update batch bar visibility and count when selection changes."""
        sel = self._grid.selectionModel().selectedIndexes()
        count = len(sel)
        if count > 1:
            self._batch_count_label.setText(f"{count} selected")
            self._batch_bar.setVisible(True)
            self._position_batch_bar()
            self._batch_bar.raise_()
        else:
            self._batch_bar.setVisible(False)

    def _get_selected_infos(self) -> list[dict]:
        """Return MEDIA_INFO_ROLE dicts for all selected grid items."""
        infos = []
        for idx in self._grid.selectionModel().selectedIndexes():
            info = idx.data(MEDIA_INFO_ROLE)
            if info:
                infos.append(info)
        return infos

    def _batch_export(self):
        """Export selected media files to a chosen directory."""
        infos = self._get_selected_infos()
        paths = [i["resolved"] for i in infos
                 if i.get("resolved") and os.path.isfile(i["resolved"])]
        if not paths:
            self._count_label.setText("No on-disk files in selection to export")
            return
        from PySide6.QtWidgets import QFileDialog
        dest = QFileDialog.getExistingDirectory(self, "Export Selected Media To")
        if not dest:
            return
        import shutil
        copied = 0
        for p in paths:
            try:
                shutil.copy2(p, dest)
                copied += 1
            except Exception:
                pass
        self._count_label.setText(f"Exported {copied} / {len(paths)} files to {dest}")

    def _batch_copy_paths(self):
        """Copy resolved file paths of selected items to clipboard."""
        infos = self._get_selected_infos()
        paths = [i["resolved"] for i in infos
                 if i.get("resolved") and os.path.isfile(i["resolved"])]
        if paths:
            QApplication.clipboard().setText("\n".join(paths))
            self._count_label.setText(f"Copied {len(paths)} path(s) to clipboard")
        else:
            self._count_label.setText("No on-disk file paths to copy")

    # --- MG-05: Scroll position indicator ---

    def _on_scroll_position(self, value: int):
        """Show scroll position indicator during scroll."""
        total = self._model.total_rows if hasattr(self._model, 'total_rows') else 0
        if total <= 0:
            return
        top_idx = self._nearest_visible_index(False)
        bottom_idx = self._nearest_visible_index(True)
        if top_idx.isValid() and bottom_idx.isValid():
            first = top_idx.row() + 1
            last = bottom_idx.row() + 1
            if last < first:
                first, last = last, first
            text = f"{first:,}-{last:,} / {total:,}"
        else:
            loaded = self._model.rowCount()
            text = f"{min(loaded, total):,} loaded / {total:,}"
        self._scroll_indicator.setText(text)
        self._scroll_indicator.adjustSize()
        self._position_scroll_indicator()
        self._scroll_indicator.setVisible(True)
        self._scroll_indicator.raise_()
        self._scroll_indicator_timer.start()

    def _nearest_visible_index(self, from_bottom: bool) -> QModelIndex:
        viewport = self._grid.viewport()
        step_x = max(24, self._grid.gridSize().width() // 2)
        if from_bottom:
            y_values = range(max(0, viewport.height() - 1), -1, -24)
        else:
            y_values = range(0, max(1, viewport.height()), 24)
        for y in y_values:
            for x in range(0, max(1, viewport.width()), step_x):
                idx = self._grid.indexAt(QPoint(x, y))
                if idx.isValid():
                    return idx
        return QModelIndex()

    # --- MG-06: Keyboard shortcut handlers ---

    def _shortcut_preview(self):
        """Space: preview the currently selected item."""
        idx = self._grid.currentIndex()
        if idx.isValid():
            info = idx.data(MEDIA_INFO_ROLE) or {}
            resolved = info.get("resolved")
            if resolved and os.path.isfile(resolved):
                self._open_preview(idx)

    def _shortcut_go_to_chat(self):
        """Enter: navigate to the selected item's conversation."""
        idx = self._grid.currentIndex()
        if idx.isValid():
            info = idx.data(MEDIA_INFO_ROLE) or {}
            conv_id = info.get("conversation_id")
            msg_id = info.get("message_id")
            if conv_id and msg_id:
                self.go_to_chat.emit(conv_id, msg_id)

    def _shortcut_select_all(self):
        """Ctrl+A: select all visible items in the grid."""
        model = self._grid.model()
        if model and model.rowCount() > 0:
            self._grid.selectAll()

    def _grid_wheel_event(self, event):
        """Handle Ctrl+Wheel for zoom, normal wheel for scroll."""
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                new_val = min(self._size_slider.value() + 20, self._size_slider.maximum())
            else:
                new_val = max(self._size_slider.value() - 20, self._size_slider.minimum())
            self._size_slider.setValue(new_val)
            event.accept()
        else:
            self._orig_grid_wheel_event(event)

    # --- PF-03: Scroll debounce for thumbnail loading ---

    def _on_scroll_debounce(self, _value: int):
        """During fast scroll, defer thumbnail loading by 100ms."""
        self._is_fast_scrolling = True
        self._grid_delegate.skip_uncached = True
        self._scroll_debounce_timer.start()

    def _load_visible_thumbnails(self):
        """Called after scroll settles.  Repaint visible area + warm
        the prefetch zone (±20/40 rows around viewport) so the next
        scroll movement lands on already-cached tiles.

        Prefetch sizes are asymmetric: more ahead than behind, because
        users mostly scroll forward.  20 rows behind covers a fast
        scroll-back, 40 ahead covers a fling forward.
        """
        self._is_fast_scrolling = False
        self._grid_delegate.skip_uncached = False
        self._grid.viewport().update()
        # Kick off prefetch in a single-shot timer so the repaint above
        # gets first dibs on the GUI thread.
        QTimer.singleShot(0, self._prefetch_around_viewport)

    def _prefetch_around_viewport(self):
        """Walk ±20/40 rows around the visible viewport and queue
        thumbnail extraction for any cells that aren't already cached.
        Cheap to call (just queue.put + os.path.isfile per cell)."""
        try:
            model = self._grid.model()
            if model is None or model.rowCount() == 0:
                return
            # Visible row range
            vp = self._grid.viewport().rect()
            first, last = -1, -1
            for r in range(model.rowCount()):
                idx = model.index(r, 0)
                vr = self._grid.visualRect(idx)
                if not vp.intersects(vr):
                    continue
                if first < 0:
                    first = r
                last = r
            if first < 0:
                return
            # ±20 behind, +40 ahead
            lo = max(0, first - 20)
            hi = min(model.rowCount() - 1, last + 40)
            for r in range(lo, hi + 1):
                idx = model.index(r, 0)
                info = model.data(idx, MEDIA_INFO_ROLE) or {}
                resolved = info.get("resolved")
                file_exists = info.get("file_exists")
                if not (resolved and file_exists):
                    continue
                media_id = info.get("media_id")
                if not media_id:
                    continue
                type_label = info.get("type_label") or ""
                mime = info.get("mime") or ""
                if (type_label in ("image", "sticker", "gif", "animated_gif")
                        or mime.startswith("image/")):
                    self._grid_delegate._queue_image_extract(media_id, resolved)
                elif type_label == "video" or mime.startswith("video/"):
                    self._grid_delegate._queue_video_extract(media_id, resolved)
        except Exception as e:
            print(f"[MediaGallery] prefetch error: {e}")

    def _set_view(self, mode: str):
        self._current_view = mode
        self._view_grid_btn.setChecked(mode == "grid")
        self._view_table_btn.setChecked(mode == "table")
        self._grid.setVisible(mode == "grid")
        self._table.setVisible(mode == "table")

    def _on_tile_size_changed(self, value: int):
        """Update thumbnail tile size from slider."""
        self._grid_delegate.TILE_SIZE = value
        self._grid.setGridSize(QSize(value + 4, value + 4))
        self._grid.doItemsLayout()
        self._size_label.setText(f"{value}px")

    def _toggle_stats(self):
        vis = not self._stats_container.isVisible()
        self._stats_container.setVisible(vis)
        self._stats_toggle.setText("\u25BC Stats" if vis else "\u25B6 Stats")

    def _on_filter(self):
        fid = self.sender().property("filter_id")
        _type_ids = {"all", "images", "videos", "audio", "docs", "stickers"}
        _status_ids = {"on_disk", "downloaded", "recovered", "downloadable"}
        if fid in _type_ids:
            # Type buttons: single-select within type group
            for k, b in self._filter_btns.items():
                if k in _type_ids:
                    b.setChecked(k == fid)
            self._current_filter = fid
        elif fid in _status_ids:
            # Status buttons: toggle independently (can combine with type)
            pass
        self._apply()
        # Cascade: refresh calendar counts + combos when type/status changes
        if self._calendar_heatmap.isVisible():
            self._load_calendar_data()
        if self._combos_ready:
            self._combo_refreshing = True
            self._populate_sender_combo()
            self._populate_conversation_combo()
            self._combo_refreshing = False

    def _load_filter_options(self):
        """Populate sender and conversation combos from DB (called once)."""
        if not self.isVisible():
            return
        self._combo_refreshing = True
        self._populate_sender_combo()
        self._populate_conversation_combo()
        self._combo_refreshing = False
        self._combos_ready = True

    def _selected_sender_filter(self) -> list:
        """Return list of checked sender values. Empty = all."""
        if not hasattr(self, "_sender_combo"):
            return []
        return self._sender_combo.checked_values()

    def _selected_conversation_filter(self) -> list:
        """Return list of checked conversation values. Empty = all."""
        if not hasattr(self, "_conv_combo"):
            return []
        return self._conv_combo.checked_values()

    def _populate_sender_combo(self, **_kwargs):
        db = Database.get()
        where_parts, params_list = self._build_filter_parts(exclude={"sender"})
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params = tuple(params_list)
        try:
            sender_rows = db.fetchall(
                f"""SELECT c.id,
                    COALESCE(c.resolved_name, '') AS resolved_name,
                    COALESCE(c.display_name, '') AS display_name,
                    COALESCE(c.wa_name, '') AS wa_name,
                    COALESCE(c.phone_number, '') AS phone_number,
                    COALESCE(c.phone_jid, '') AS phone_jid,
                    COALESCE(c.lid_jid, '') AS lid_jid,
                    COUNT(*) AS media_count,
                    CASE WHEN m.sender_id IS NULL THEN '__from_me__' ELSE c.id END AS sender_key,
                    m.sender_id
                FROM media me
                LEFT JOIN message m ON m.id = me.message_id
                LEFT JOIN contact c ON c.id = m.sender_id
                {where}
                GROUP BY sender_key
                ORDER BY media_count DESC
                """,
                params,
            )
            items = []
            for sr in sender_rows:
                sid = sr["sender_key"]
                if sr["sender_id"] is None:
                    label = "Me (sent)"
                else:
                    # Build rich label: Name + phone + JID/LID for searchability
                    parts = []
                    name = sr["resolved_name"] or sr["display_name"]
                    if name:
                        parts.append(name)
                    elif sr["wa_name"]:
                        parts.append(f"~{sr['wa_name']}")
                    phone = sr["phone_number"]
                    if phone:
                        parts.append(f"+{phone}")
                    jid = sr["phone_jid"]
                    if jid:
                        jid_short = jid.replace("@s.whatsapp.net", "").replace("@lid", "")
                        if jid_short and jid_short not in (phone or ""):
                            parts.append(f"[{jid_short}]")
                    lid_jid = sr["lid_jid"]
                    if lid_jid:
                        lid_short = lid_jid.replace("@lid", "")
                        parts.append(f"LID:{lid_short}")
                    if not parts:
                        parts.append(f"Contact #{sr['id'] or '?'}")
                    label = "  ".join(parts)
                count = sr["media_count"] or 0
                items.append((label, sid, count))
            self._sender_combo.set_items(items, preserve_checked=True)
        except Exception as e:
            print(f"[MediaGallery] Sender combo error: {e}")

    def _populate_conversation_combo(self, **_kwargs):
        db = Database.get()
        where_parts, params_list = self._build_filter_parts(exclude={"conversation"})
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params = tuple(params_list)
        try:
            conv_rows = db.fetchall(
                f"""SELECT m.conversation_id,
                    COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
                    conv.jid_raw_string AS conv_jid,
                    conv.chat_type AS chat_type,
                    COUNT(*) AS media_count
                FROM media me
                LEFT JOIN message m ON m.id = me.message_id
                LEFT JOIN conversation conv ON conv.id = m.conversation_id
                {where}
                GROUP BY m.conversation_id
                HAVING media_count > 0
                ORDER BY media_count DESC
                """,
                params,
            )
            items = []
            for cr in conv_rows:
                cid = cr["conversation_id"]
                cname = cr["conv_name"] or "Unknown"
                cjid = (cr["conv_jid"] or "").strip()
                ctype = (cr["chat_type"] or "").strip()
                count = cr["media_count"] or 0
                # Build rich label so the user can identify groups
                # vs personal chats and search by JID directly:
                #   "Group Name  [120363407...@g.us]"
                #   "Jane Doe  +1 555 555 5555  [15555555555@s.whatsapp.net]"
                # The JID is the authoritative ID — useful when
                # several contacts share a display name, or when
                # investigators need to map a chat to a specific
                # WhatsApp identity.
                label_parts = [cname]
                if cjid:
                    # Show short JID inline; full one is in the search
                    # haystack via the same string.
                    short_jid = cjid
                    if "@s.whatsapp.net" in cjid:
                        short_jid = cjid.replace("@s.whatsapp.net", "")
                        label_parts.append(f"+{short_jid}")
                        label_parts.append(f"[{cjid}]")
                    elif "@lid" in cjid:
                        label_parts.append(f"[{cjid}]")
                    elif "@g.us" in cjid:
                        label_parts.append(f"[{cjid}]")
                    elif "@broadcast" in cjid or "@newsletter" in cjid:
                        label_parts.append(f"[{cjid}]")
                    else:
                        label_parts.append(f"[{cjid}]")
                if ctype and ctype not in ("personal", "individual", ""):
                    label_parts.append(f"({ctype})")
                label = "  ".join(p for p in label_parts if p)
                items.append((label, cid, count))
            self._conv_combo.set_items(items, preserve_checked=True)
        except Exception as e:
            print(f"[MediaGallery] Conversation combo error: {e}")

    def _on_sender_filter_changed(self):
        if self._combo_refreshing:
            return
        self._combo_refreshing = True
        self._populate_conversation_combo()
        self._combo_refreshing = False
        self._apply()
        # Cascade: refresh calendar counts to reflect sender filter
        if self._calendar_heatmap.isVisible():
            self._load_calendar_data()

    def _on_conversation_filter_changed(self):
        if self._combo_refreshing:
            return
        self._combo_refreshing = True
        self._populate_sender_combo()
        self._combo_refreshing = False
        self._apply()
        # Cascade: refresh calendar counts to reflect conversation filter
        if self._calendar_heatmap.isVisible():
            self._load_calendar_data()

    def _on_date_quick_pick(self):
        """Handle date quick-pick button clicks."""
        qid = self.sender().property("date_id")

        # Toggle behaviour for the Calendar button: clicking it
        # while the calendar is already open hides it and reverts
        # to "ALL TIME".  Without this the only way to dismiss
        # the calendar is to pick another quick-pick.
        if qid == "calendar" and self._calendar_heatmap.isVisible():
            self._calendar_heatmap.setVisible(False)
            self._calendar_heatmap._clear_range()
            self._calendar_start = None
            self._calendar_end = None
            for k, b in self._date_quick_btns.items():
                b.setChecked(k == "all")
            self._date_mode = "all"
            self._apply()
            return

        for k, b in self._date_quick_btns.items():
            b.setChecked(k == qid)
        self._date_mode = qid

        show_calendar = (qid == "calendar")
        self._calendar_heatmap.setVisible(show_calendar)

        if show_calendar:
            # Load media counts into the calendar heatmap
            self._load_calendar_data()
            # Don't apply yet — user picks a date/range on the calendar
            if not self._calendar_start:
                return  # no range selected yet, just show calendar
        else:
            # Clear calendar selection when switching to quick-pick
            self._calendar_start = None
            self._calendar_end = None
            self._calendar_heatmap._clear_range()

        self._apply()

    def _on_calendar_range(self, start, end):
        """Calendar heatmap: user selected a date range."""
        self._calendar_start = start
        self._calendar_end = end
        self._date_mode = "calendar"
        for k, b in self._date_quick_btns.items():
            b.setChecked(k == "calendar")
        self._apply()
        # Cascade: refresh sender/conversation combos with new date range
        self._cascade_from_date()

    def _on_calendar_day(self, d):
        """Calendar heatmap: single day click."""
        self._calendar_start = d
        self._calendar_end = d
        self._date_mode = "calendar"
        for k, b in self._date_quick_btns.items():
            b.setChecked(k == "calendar")
        self._apply()
        self._cascade_from_date()

    def _on_calendar_cleared(self):
        """Calendar heatmap: selection cleared."""
        self._calendar_start = None
        self._calendar_end = None
        # Revert to all_time
        self._date_mode = "all_time"
        for k, b in self._date_quick_btns.items():
            b.setChecked(k == "all_time")
        self._apply()
        self._cascade_from_date()

    def _on_calendar_apply(self):
        """Calendar Apply button: selection is already emitted via
        date_selected/range_selected on click, so the filter is already
        in effect.  Just dismiss the calendar widget."""
        self._calendar_heatmap.setVisible(False)
        # Revert the "Calendar" quick-pick button to its un-pressed state
        for k, b in self._date_quick_btns.items():
            b.setChecked(k == "calendar" and (
                self._calendar_start is not None or self._calendar_end is not None
            ))

    def _on_calendar_close(self):
        """Calendar Close button: dismiss without changing filter."""
        self._calendar_heatmap.setVisible(False)
        # If no selection was active, also un-press the Calendar quick-pick
        if not self._calendar_start and not self._calendar_end:
            for k, b in self._date_quick_btns.items():
                b.setChecked(k == self._date_mode)

    def _cascade_from_date(self):
        """After date change, refresh sender/conversation combos with cascading."""
        if not self._combos_ready:
            return
        self._combo_refreshing = True
        self._populate_sender_combo()
        self._populate_conversation_combo()
        self._combo_refreshing = False

    def _load_calendar_data(self):
        """Load media-per-day counts into the calendar heatmap, respecting active filters (excl. date)."""
        try:
            db = Database.get()
        except RuntimeError:
            return
        where_parts, params_list = self._build_filter_parts(exclude={"date"})
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        params = tuple(params_list)
        try:
            rows = db.fetchall(
                f"SELECT DATE(m.timestamp / 1000, 'unixepoch', 'localtime') AS d, COUNT(*) AS cnt "
                f"FROM media me "
                f"LEFT JOIN message m ON m.id = me.message_id "
                f"LEFT JOIN conversation conv ON conv.id = m.conversation_id "
                f"LEFT JOIN contact sc ON sc.id = m.sender_id "
                f"{where} "
                f"GROUP BY d ORDER BY d",
                params,
            )
            counts: dict[_date_type, int] = {}
            for r in rows:
                try:
                    d_str = str(r[0]) if r[0] else None
                    if d_str:
                        counts[_date_type.fromisoformat(d_str)] = int(r[1])
                except (ValueError, TypeError):
                    pass
            self._calendar_heatmap.load_counts(counts, label="media files")
        except Exception as e:
            print(f"[MediaGallery] Calendar data load error: {e}")

    def _get_date_range_ms(self):
        """Return (from_ms, to_ms) or None based on current date mode."""
        today = QDate.currentDate()

        if self._date_mode == "all_time":
            return None
        elif self._date_mode == "7d":
            d = today.addDays(-7)
        elif self._date_mode == "30d":
            d = today.addDays(-30)
        elif self._date_mode == "3m":
            d = today.addMonths(-3)
        elif self._date_mode == "1y":
            d = today.addYears(-1)
        elif self._date_mode == "calendar":
            if self._calendar_start and self._calendar_end:
                from_ms = int(datetime(
                    self._calendar_start.year, self._calendar_start.month,
                    self._calendar_start.day
                ).timestamp() * 1000)
                to_ms = int(datetime(
                    self._calendar_end.year, self._calendar_end.month,
                    self._calendar_end.day, 23, 59, 59
                ).timestamp() * 1000)
                return (from_ms, to_ms)
            return None
        else:
            return None

        from_ms = int(datetime(d.year(), d.month(), d.day()).timestamp() * 1000)
        to_ms = int(datetime(today.year(), today.month(), today.day(), 23, 59, 59).timestamp() * 1000)
        return (from_ms, to_ms)

    def _build_filter_parts(self, exclude: set[str] | None = None) -> tuple[list[str], list]:
        """Build WHERE clauses from all active filters, excluding categories in `exclude`.

        Exclude keys: "type", "status", "stickers", "sender", "conversation",
                      "date", "search", "owner"

        Returns (where_parts, params_list) — both mutable lists.
        """
        exclude = exclude or set()
        parts: list[str] = []
        params: list = []

        # Type filter
        if "type" not in exclude:
            cf = self._current_filter
            if cf == "images":
                parts.append("me.mime_type LIKE 'image/%'")
                parts.append("COALESCE(m.type_label, '') != 'sticker'")
            elif cf == "videos":
                parts.append("me.mime_type LIKE 'video/%'")
            elif cf == "audio":
                parts.append("me.mime_type LIKE 'audio/%'")
            elif cf == "docs":
                parts.append("me.mime_type LIKE 'application/%'")
            elif cf == "stickers":
                parts.append(
                    "(me.is_animated_sticker = 1 OR COALESCE(m.type_label,'') = 'sticker')"
                )

        # Status filters
        if "status" not in exclude:
            status_clauses = []
            _btn = self._filter_btns
            if _btn.get("on_disk") and _btn["on_disk"].isChecked():
                status_clauses.append(
                    "(me.file_exists = 1 AND (me.recovery_method IS NULL OR me.recovery_method = ''))")
            if _btn.get("downloaded") and _btn["downloaded"].isChecked():
                status_clauses.append("me.recovery_method = 'downloaded'")
            if _btn.get("recovered") and _btn["recovered"].isChecked():
                # Hash-linked + orphan-recovered all count as "recovered" —
                # the visible artifact in each case is a file matched by
                # SHA-256 (either from a sibling message OR an orphaned
                # file in the WhatsApp media folder).  The forensic
                # distinction is preserved in recovery_method itself
                # and surfaced separately in the detail panel.
                status_clauses.append(
                    "me.recovery_method IN ('hash_linked', "
                    "'hash_linked_after_delete', 'orphan_recovered')")
            if _btn.get("downloadable") and _btn["downloadable"].isChecked():
                status_clauses.append(
                    "(me.file_exists = 0 AND me.media_key IS NOT NULL "
                    "AND me.media_url IS NOT NULL AND me.media_url != '')")
            if status_clauses:
                parts.append("(" + " OR ".join(status_clauses) + ")")

        # Stickers Off toggle
        if "stickers" not in exclude and "type" not in exclude:
            if (self._current_filter != "stickers"
                    and hasattr(self, '_stickers_off_btn')
                    and self._stickers_off_btn.isChecked()):
                parts.append("COALESCE(m.type_label, '') != 'sticker'")

        # Owner / Sender filter
        if "owner" not in exclude and "sender" not in exclude:
            if hasattr(self, '_owner_btn') and self._owner_btn.isChecked():
                parts.append("m.from_me = 1")
            elif hasattr(self, '_sender_combo') and self._combos_ready:
                checked = self._sender_combo.checked_values()
                if checked:
                    from_me = "__from_me__" in checked
                    ids = [v for v in checked if v != "__from_me__"]
                    clauses = []
                    if from_me:
                        clauses.append("m.from_me = 1")
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        clauses.append(f"m.sender_id IN ({placeholders})")
                        params.extend(ids)
                    if clauses:
                        parts.append("(" + " OR ".join(clauses) + ")")
        elif "sender" not in exclude and "owner" in exclude:
            if hasattr(self, '_sender_combo') and self._combos_ready:
                checked = self._sender_combo.checked_values()
                if checked:
                    from_me = "__from_me__" in checked
                    ids = [v for v in checked if v != "__from_me__"]
                    clauses = []
                    if from_me:
                        clauses.append("m.from_me = 1")
                    if ids:
                        placeholders = ",".join("?" for _ in ids)
                        clauses.append(f"m.sender_id IN ({placeholders})")
                        params.extend(ids)
                    if clauses:
                        parts.append("(" + " OR ".join(clauses) + ")")

        # Conversation filter
        if "conversation" not in exclude:
            if hasattr(self, '_conv_combo') and self._combos_ready:
                checked = self._conv_combo.checked_values()
                if checked:
                    placeholders = ",".join("?" for _ in checked)
                    parts.append(f"m.conversation_id IN ({placeholders})")
                    params.extend(checked)

        # Date range filter
        if "date" not in exclude:
            if hasattr(self, '_date_mode'):
                dr = self._get_date_range_ms()
                if dr:
                    parts.append("m.timestamp BETWEEN ? AND ?")
                    params.extend([dr[0], dr[1]])

        # Search text
        if "search" not in exclude:
            text = self._search.text().strip()
            if text:
                parts.append("(me.media_name LIKE ? OR me.media_caption LIKE ?)")
                params.extend([f"%{text}%", f"%{text}%"])

        # Size filter (KB).  Either bound can be blank.  Stored as bytes
        # in me.file_size so we multiply the user's KB by 1024.  Garbage
        # input is silently ignored (parses as None).
        if "size" not in exclude and hasattr(self, '_size_min_input'):
            def _parse_kb(txt: str) -> int | None:
                t = (txt or "").strip().replace(",", "")
                if not t:
                    return None
                try:
                    return max(0, int(float(t) * 1024))
                except (TypeError, ValueError):
                    return None
            min_b = _parse_kb(self._size_min_input.text())
            max_b = _parse_kb(self._size_max_input.text())
            if min_b is not None:
                parts.append("me.file_size >= ?")
                params.append(min_b)
            if max_b is not None:
                parts.append("me.file_size <= ?")
                params.append(max_b)

        # Tagged-only toggle: any row in message_tag for this message
        if "tagged" not in exclude and hasattr(self, '_tagged_only_btn'):
            if self._tagged_only_btn.isChecked():
                parts.append(
                    "EXISTS (SELECT 1 FROM message_tag mt "
                    "  WHERE mt.message_id = m.id)"
                )

        return parts, params

    def refresh_for_timezone_change(self) -> None:
        """Called by main_window when user changes the timezone setting.
        Repaints the gallery so date pills and detail panel timestamps
        reflect the new timezone."""
        # Repaint grid tiles (date pills use _ts_to_dt now)
        if hasattr(self, '_grid'):
            self._grid.viewport().update()
        # Refresh detail panel if visible
        if hasattr(self, '_detail_panel') and self._detail_panel.isVisible():
            info = getattr(self._detail_panel, '_info', None)
            if info:
                self._detail_panel.show_media(info)

    def _apply(self):
        if not self.isVisible() and not getattr(self, "_needs_reload", False):
            return
        where_parts: list[str] = []
        params: list = []

        # --- Type filter ---
        if self._current_filter == "all":
            pass  # stickers handled by toggle below
        elif self._current_filter == "images":
            where_parts.append("me.mime_type LIKE 'image/%'")
            where_parts.append("COALESCE(m.type_label, '') != 'sticker'")
        elif self._current_filter == "videos":
            where_parts.append("me.mime_type LIKE 'video/%'")
        elif self._current_filter == "audio":
            where_parts.append("me.mime_type LIKE 'audio/%'")
        elif self._current_filter == "docs":
            where_parts.append("me.mime_type LIKE 'application/%'")
        elif self._current_filter == "stickers":
            where_parts.append(
                "(me.is_animated_sticker = 1 OR COALESCE(m.type_label,'') = 'sticker')"
            )

        # --- Status filters (independent toggles, combine with type) ---
        status_clauses = []
        _btn = self._filter_btns
        if _btn.get("on_disk") and _btn["on_disk"].isChecked():
            status_clauses.append(
                "(me.file_exists = 1 AND (me.recovery_method IS NULL OR me.recovery_method = ''))")
        if _btn.get("downloaded") and _btn["downloaded"].isChecked():
            status_clauses.append("me.recovery_method = 'downloaded'")
        if _btn.get("recovered") and _btn["recovered"].isChecked():
            # Hash-linked + orphan-recovered all count as "recovered" —
            # any file recovered via SHA-256 match (sibling message or
            # orphaned-file rescue) lives under this filter.
            status_clauses.append(
                "me.recovery_method IN ('hash_linked', "
                "'hash_linked_after_delete', 'orphan_recovered')")
        if _btn.get("downloadable") and _btn["downloadable"].isChecked():
            status_clauses.append(
                "(me.file_exists = 0 AND me.media_key IS NOT NULL "
                "AND me.media_url IS NOT NULL AND me.media_url != '')")
        if status_clauses:
            where_parts.append("(" + " OR ".join(status_clauses) + ")")

        # --- Stickers Off toggle ---
        if (self._current_filter != "stickers"
                and hasattr(self, '_stickers_off_btn')
                and self._stickers_off_btn.isChecked()):
            where_parts.append("COALESCE(m.type_label, '') != 'sticker'")

        # --- Owner / Sender filter ---
        if hasattr(self, '_owner_btn') and self._owner_btn.isChecked():
            where_parts.append("m.from_me = 1")
        elif hasattr(self, '_sender_combo') and self._combos_ready:
            checked = self._sender_combo.checked_values()
            if checked:
                from_me = "__from_me__" in checked
                ids = [v for v in checked if v != "__from_me__"]
                clauses = []
                if from_me:
                    clauses.append("m.from_me = 1")
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    clauses.append(f"m.sender_id IN ({placeholders})")
                    params.extend(ids)
                if clauses:
                    where_parts.append("(" + " OR ".join(clauses) + ")")

        # --- Conversation filter ---
        if hasattr(self, '_conv_combo') and self._combos_ready:
            checked = self._conv_combo.checked_values()
            if checked:
                placeholders = ",".join("?" for _ in checked)
                where_parts.append(f"m.conversation_id IN ({placeholders})")
                params.extend(checked)

        # --- Date range filter ---
        if hasattr(self, '_date_mode'):
            dr = self._get_date_range_ms()
            if dr:
                where_parts.append("m.timestamp BETWEEN ? AND ?")
                params.extend([dr[0], dr[1]])

        # --- Search text (existing) ---
        text = self._search.text().strip()
        if text:
            where_parts.append("(me.media_name LIKE ? OR me.media_caption LIKE ?)")
            params.extend([f"%{text}%", f"%{text}%"])

        # --- Sort order ---
        order = ""
        if hasattr(self, '_sort_combo'):
            order = self._sort_combo.currentData() or ""
        # Special "Tagged First" pseudo-token: tagged media (any row in
        # message_tag for this message) comes first, then by recency.
        # Done via correlated EXISTS so no extra JOIN is needed.
        if order == "_tagged_sort":
            order = (
                "CASE WHEN EXISTS (SELECT 1 FROM message_tag mt "
                "  WHERE mt.message_id = m.id) THEN 0 ELSE 1 END ASC, "
                "m.timestamp DESC"
            )
        if not order:
            order = "me.id DESC"

        self._model.load(
            where=" AND ".join(where_parts),
            params=tuple(params),
            order=order,
        )
        self._loaded_once = True
        # Show active filter info in count label for clarity
        _active_filters = []
        if (hasattr(self, '_stickers_off_btn')
                and self._stickers_off_btn.isChecked()):
            _active_filters.append("stickers hidden")
        # Check if any status filter is active
        for fid in ("on_disk", "downloaded", "recovered", "downloadable"):
            btn = self._filter_btns.get(fid)
            if btn and btn.isChecked():
                _active_filters.append(btn.text().lower())
                break
        _suffix = f"  ({', '.join(_active_filters)})" if _active_filters else ""
        self._count_label.setText(f"{self._model.total_rows:,} media files{_suffix}")

    def _load_stats(self):
        if not self.isVisible():
            return
        db = Database.get()
        total = db.scalar("SELECT COUNT(*) FROM media") or 0
        on_disk = db.scalar("SELECT COUNT(*) FROM media WHERE file_exists = 1") or 0
        downloadable = db.scalar(
            "SELECT COUNT(*) FROM media WHERE file_exists = 0 "
            "AND media_key IS NOT NULL AND media_url IS NOT NULL AND media_url != '' "
            "AND COALESCE(media_status, '') != 'download_failed'"
        ) or 0
        total_size = db.scalar("SELECT SUM(file_size) FROM media") or 0

        self._stat_labels["total"].setText(f"{total:,}")
        self._stat_labels["on_disk"].setText(f"{on_disk:,}")
        self._stat_labels["downloadable"].setText(f"{downloadable:,}")
        self._stat_labels["expired"].setText(f"{total - on_disk - downloadable:,}")
        self._stat_labels["total_size"].setText(_format_file_size(total_size))

    def _grid_context_menu(self, pos):
        idx = self._grid.indexAt(pos)
        if not idx.isValid():
            return
        info = idx.data(MEDIA_INFO_ROLE) or {}
        resolved = info.get("resolved")

        menu = QMenu(self)

        # Show conversation context
        conv_name = info.get("conversation_name", "")
        direction = "Sent" if info.get("from_me") else "Received"
        if conv_name:
            header = menu.addAction(f"\U0001F4AC {direction} in: {conv_name}")
            header.setEnabled(False)
            menu.addSeparator()

        if resolved and os.path.isfile(resolved):
            open_act = menu.addAction("\U0001F4C2 Open File")
            open_act.triggered.connect(lambda: self._open_file(resolved))
            copy_act = menu.addAction("\U0001F4C1 Copy Path")
            copy_act.triggered.connect(lambda: QApplication.clipboard().setText(resolved))
        elif info.get("status") == "downloadable":
            # Media has URL + key but no file on disk
            media_id = idx.data(Qt.UserRole)
            dl_act = menu.addAction("\u21E9 Download & Decrypt Original")
            dl_act.triggered.connect(
                lambda _=False, mid=media_id: self._download_single(mid)
            )
        elif info.get("status") == "expired":
            expired_act = menu.addAction("\u26A0 URL Expired - Cannot Download")
            expired_act.setEnabled(False)

        # Go to chat
        conv_id = info.get("conversation_id")
        msg_id = info.get("message_id")
        if conv_id and msg_id:
            go_act = menu.addAction("\u2192 Go to Message in Chat")
            go_act.triggered.connect(
                lambda _=False, c=conv_id, m=msg_id: self.go_to_chat.emit(c, m)
            )

        # Find Shared / Similar copies — routes to the Image Similarity page.
        # That page's exact SHA-256 mode shows every chat + sender + timestamp
        # where this file was shared, and flipping to Visual mode surfaces
        # near-duplicates. Replaces the old in-line "Find Copies" popup.
        media_id = idx.data(Qt.UserRole)
        if media_id and msg_id:
            copies_act = menu.addAction("\U0001F50D Find Shared / Similar copies")
            copies_act.setToolTip(
                "Open the Image Similarity page: default exact SHA-256 match "
                "lists every chat and sender this exact file was shared in."
            )
            copies_act.triggered.connect(
                lambda _=False, mid=msg_id: self.find_similar_requested.emit(mid)
            )

        # ---------------- Tagging ----------------
        # Two distinct actions:
        #   "Tag this media" - adds tag to JUST this message_id.
        #   "Tag this and all where it's shared" - finds every message
        #       that carries a media row with the same SHA-256 (i.e.
        #       every chat / sender that shared this exact file) and
        #       tags ALL of them in one go.  Uses the existing
        #       message_tag schema.
        # The action shows the existing tag count in the label so the
        # analyst sees at a glance "this is already in 'evidence_set_2'."
        if msg_id:
            self._add_tag_actions_to_menu(menu, info, msg_id)

        if menu.actions():
            menu.exec(self._grid.mapToGlobal(pos))

    def _add_tag_actions_to_menu(self, menu, info: dict, msg_id: int) -> None:
        """Inject the two tag actions plus an existing-tags submenu into
        the given QMenu.  Shared by both grid and table context menus."""
        try:
            from app.services.message_tag_service import MessageTagService
            svc = MessageTagService.instance()
            existing_tags = svc.list_tags_for(int(msg_id))
        except Exception:
            existing_tags = []

        menu.addSeparator()
        # Header showing current tag state - non-clickable.
        if existing_tags:
            tag_summary = ", ".join(existing_tags[:4])
            if len(existing_tags) > 4:
                tag_summary += f", +{len(existing_tags) - 4} more"
            hdr = menu.addAction(f"\U0001F516 Tagged: {tag_summary}")
            hdr.setEnabled(False)
        else:
            hdr = menu.addAction("\U0001F516 Not tagged")
            hdr.setEnabled(False)

        # 1. Tag this single message
        tag_one = menu.addAction("\U0001F516 Tag this media…")
        tag_one.setToolTip(
            "Adds a tag to ONLY this message - the same image shared "
            "in another chat is not affected."
        )
        tag_one.triggered.connect(
            lambda _=False, mid=int(msg_id): self._tag_messages([mid], scope="this")
        )

        # 2. Tag this AND every other instance of this file.
        # Only meaningful when the media row has a hash (so we can
        # find peers).  We run a small probe query to count peers and
        # disable the action if there's only this one instance.
        file_hash = (info.get("file_hash") or "").strip()
        if file_hash:
            peer_count = self._count_shared_instances(file_hash)
            if peer_count > 1:
                tag_all = menu.addAction(
                    f"\U0001F516 Tag this AND {peer_count - 1} other "
                    f"shared instance{'s' if peer_count - 1 != 1 else ''}…"
                )
                tag_all.setToolTip(
                    "Tags every message in every chat that has this exact "
                    "file (matched by SHA-256).  Useful when the same "
                    "evidence image was forwarded across multiple chats."
                )
                tag_all.triggered.connect(
                    lambda _=False, h=file_hash:
                    self._tag_messages_by_hash(h, scope="all_shared")
                )
            else:
                solo = menu.addAction(
                    "\U0001F516 (only one instance of this file in case)"
                )
                solo.setEnabled(False)

        # Untag - shown only when there's at least one tag.
        if existing_tags:
            sub = menu.addMenu("✕ Remove tag")
            for lbl in existing_tags:
                act = sub.addAction(lbl)
                act.triggered.connect(
                    lambda _=False, mid=int(msg_id), tag=lbl:
                    self._untag_message(mid, tag)
                )

    @staticmethod
    def _count_shared_instances(file_hash: str) -> int:
        """How many media rows share the same SHA-256?  Used to decide
        whether the 'Tag all shared instances' action is meaningful."""
        try:
            row = Database.get().fetchone(
                "SELECT COUNT(*) FROM media WHERE file_hash = ?",
                (file_hash,),
            )
            return int(row[0]) if row else 0
        except Exception:
            return 0

    def _tag_messages(self, message_ids: list[int], scope: str = "this") -> None:
        """Prompt for a tag label and add the tag to the supplied message
        ids.  Refreshes the gallery so the 'Tagged only' / 'Tagged First'
        filters update immediately."""
        if not message_ids:
            return
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        default = f"flagged_{datetime.now().strftime('%Y%m%d')}"
        title = "Tag this media" if scope == "this" else "Tag all shared instances"
        prompt = (
            "Tag name (will be created if new):" if scope == "this"
            else f"Tag name (will be applied to {len(message_ids)} messages):"
        )
        name, ok = QInputDialog.getText(self, title, prompt, text=default)
        if not ok or not name.strip():
            return
        try:
            from app.services.message_tag_service import MessageTagService
            svc = MessageTagService.instance()
            tag = svc.ensure_tag(name.strip())
            added = svc.bulk_tag(tag, message_ids)
        except Exception as e:
            QMessageBox.warning(self, "Tagging failed", str(e))
            return
        QMessageBox.information(
            self, "Tagged",
            f"Tag <b>{tag}</b> applied to <b>{added}</b> of {len(message_ids)} "
            f"message{'s' if len(message_ids) != 1 else ''}.<br><br>"
            f"View the full set on the <b>Tagged Messages</b> page or use "
            f"<b>Tagged only</b> in this gallery's filter bar.",
        )
        # Refresh so the Tagged-only filter / Tagged-First sort updates
        try:
            self._apply()
        except Exception:
            pass

    def _tag_messages_by_hash(self, file_hash: str, scope: str = "all_shared") -> None:
        """Look up all message_ids that share the given file_hash, then
        delegate to _tag_messages.  This is the 'tag everywhere this
        file was shared' path."""
        if not file_hash:
            return
        try:
            rows = Database.get().fetchall(
                "SELECT DISTINCT m.id FROM media me "
                "LEFT JOIN message m ON m.id = me.message_id "
                "WHERE me.file_hash = ? AND m.id IS NOT NULL",
                (file_hash,),
            )
            ids = [int(r[0]) for r in rows if r and r[0]]
        except Exception as e:
            print(f"[MediaGallery] hash lookup failed: {e}")
            return
        if not ids:
            return
        self._tag_messages(ids, scope=scope)

    def _untag_message(self, msg_id: int, tag_label: str) -> None:
        try:
            from app.services.message_tag_service import MessageTagService
            svc = MessageTagService.instance()
            svc.untag(tag_label, [int(msg_id)])
        except Exception as e:
            print(f"[MediaGallery] untag failed: {e}")
            return
        try:
            self._apply()
        except Exception:
            pass

    def _find_copies(self, media_id: int):
        """Find other media with the same file hash (forwarded copies)."""
        db = Database.get()
        row = db.fetchone(
            "SELECT file_hash FROM media WHERE id = ?", (media_id,)
        )
        if not row or not row["file_hash"]:
            return
        copies = db.fetchall(
            """SELECT me.id, me.media_name, m.conversation_id,
                      COALESCE(conv.display_name, conv.jid_raw_string, '') AS conv_name,
                      m.from_me, m.timestamp
               FROM media me
               LEFT JOIN message m ON m.id = me.message_id
               LEFT JOIN conversation conv ON conv.id = m.conversation_id
               WHERE me.file_hash = ? AND me.id != ?
               ORDER BY m.timestamp DESC LIMIT 20""",
            (row["file_hash"], media_id),
        )
        if not copies:
            return
        # Show in a simple menu
        menu = QMenu("Copies found", self)
        for c in copies:
            conv = c["conv_name"] or "Unknown"
            direction = "Sent" if c["from_me"] else "Recv"
            ts = ""
            if c["timestamp"]:
                try:
                    ts = _ts_to_dt(c["timestamp"]).strftime(" %Y-%m-%d")
                except (ValueError, OSError):
                    pass
            label = f"{direction} in {conv}{ts}"
            act = menu.addAction(label)
            _cid = c["conversation_id"]
            if _cid:
                act.triggered.connect(
                    lambda _=False, cid=_cid: self.go_to_chat.emit(cid, 0)
                )
        menu.exec(self.cursor().pos())

    def _download_single(self, media_id: int):
        """Download and decrypt a single media file."""
        db = Database.get()
        row = db.fetchone(
            """SELECT me.media_url, me.media_key, me.mime_type, me.file_hash,
                      me.media_name, m.type_label, me.message_id
               FROM media me
               LEFT JOIN message m ON m.id = me.message_id
               WHERE me.id = ?""",
            (media_id,),
        )
        if not row:
            return

        url = row["media_url"]
        key = row["media_key"]
        if not url or not key:
            self._count_label.setText("No URL or key for this media")
            return

        try:
            from app.services.media_crypto import (
                download_and_decrypt, get_media_type, get_extension_for_mime,
            )
            media_type = get_media_type(row["type_label"] or "", row["mime_type"] or "")
            ext = get_extension_for_mime(row["mime_type"] or "")
            save_dir = os.path.join(str(db.path.parent), "recovered_media")
            os.makedirs(save_dir, exist_ok=True)
            save_path = os.path.join(save_dir, f"media_{media_id}{ext}")

            self._count_label.setText(f"Downloading media {media_id}...")
            QApplication.processEvents()

            download_and_decrypt(
                url=url, media_key=key, media_type=media_type,
                file_hash=row["file_hash"], save_path=save_path, timeout=20,
            )

            # Update DB to mark as downloaded.  FORENSIC CORRECTNESS:
            # the message that triggered the download gets
            # recovery_method='downloaded'; every OTHER message with the
            # same SHA-256 (i.e. the same file forwarded to other chats)
            # gets recovery_method='hash_linked' so the renderer never
            # mis-labels them as "Original (transferred to device)".
            import time as _time_mod
            recovery_ts = int(_time_mod.time() * 1000)
            db.execute_write(
                "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                "recovery_method = 'downloaded', recovery_timestamp = ? "
                "WHERE id = ?",
                (save_path, recovery_ts, media_id),
            )
            file_hash = row["file_hash"]
            if file_hash:
                db.execute_write(
                    "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                    "recovery_method = 'hash_linked', recovery_timestamp = ? "
                    "WHERE file_hash = ? AND id != ? "
                    "  AND (file_exists = 0 OR file_exists IS NULL)",
                    (save_path, recovery_ts, file_hash, media_id),
                )
            db.reconnect_read()

            self._count_label.setText(f"Downloaded: {os.path.basename(save_path)}")
            self._open_file(save_path)

        except Exception as e:
            self._count_label.setText(f"Download failed: {e}")

    def _on_grid_click(self, index: QModelIndex):
        """Single-click on grid tile -> show detail panel."""
        if not index.isValid():
            return
        self._show_detail_for_index(index)

    def _on_grid_double_click(self, index: QModelIndex):
        """Double-click on grid tile -> open file if on disk, else go to chat."""
        if not index.isValid():
            return
        info = index.data(MEDIA_INFO_ROLE) or {}
        resolved = info.get("resolved")
        if resolved and os.path.isfile(resolved):
            self._open_preview(index)
        else:
            conv_id = info.get("conversation_id")
            msg_id = info.get("message_id")
            if conv_id and msg_id:
                self.go_to_chat.emit(conv_id, msg_id)

    def _on_table_click(self, index: QModelIndex):
        """Single-click on table row -> show detail panel."""
        if not index.isValid():
            return
        self._show_detail_for_index(index)

    def _on_table_double_click(self, index: QModelIndex):
        """Double-click on table row -> go to message in chat."""
        if not index.isValid():
            return
        row = index.row()
        if 0 <= row < len(self._model._data):
            row_data = self._model._data[row]
            conv_id = row_data[15] if len(row_data) > 15 else None
            msg_id = row_data[14] if len(row_data) > 14 else None
            if conv_id and msg_id:
                self.go_to_chat.emit(conv_id, msg_id)

    def _show_detail_for_index(self, index: QModelIndex):
        """Populate and show the detail panel for the given model index.

        Preview-image priority (high to low):
          1. Original disk file — decoded fresh at the 800px
             target via ``QImageReader`` (Qt's built-in fast
             scaled-decode path, no full-resolution decode).
             Sharpest quality, suitable for HiDPI displays.
          2. L2 SQLite cached thumbnail (640px JPEG already
             decoded by the gallery worker).  Same quality as
             the gallery tile uses.
          3. msgstore embedded thumbnail blob (low quality,
             ~96px).  Last-resort.
        """
        info = index.data(MEDIA_INFO_ROLE) or {}
        thumb_pxm = None
        resolved = info.get("resolved")
        media_id = info.get("media_id")
        type_label = info.get("type_label", "")
        mime = (info.get("mime") or "").lower()
        is_image = ("image" in mime or type_label in ("image", "sticker", "gif", "animated_gif"))
        is_video = ("video" in mime or type_label == "video")

        # 1. Original file on disk for images - sharpest preview.
        if is_image and resolved and os.path.isfile(resolved):
            try:
                from PySide6.QtGui import QImageReader
                reader = QImageReader(resolved)
                reader.setAutoTransform(True)
                orig = reader.size()
                if orig.isValid():
                    target = 800   # was 400 - too soft on HiDPI
                    if orig.width() > target or orig.height() > target:
                        scaled = orig.scaled(target, target, Qt.KeepAspectRatio)
                        reader.setScaledSize(scaled)
                    img = reader.read()
                    if not img.isNull():
                        thumb_pxm = QPixmap.fromImage(img)
            except Exception:
                pass

        # 2. L2 SQLite cache (covers videos with extracted frame, plus
        # images where the original isn't on disk).  Reads the same
        # 640px JPEG the gallery tile renders, so the panel preview
        # matches what the user clicked.
        if (not thumb_pxm or thumb_pxm.isNull()) and media_id:
            try:
                cache = MediaThumbnailDelegate._persistent_cache
                if cache is not None:
                    kind = "vid" if is_video else "img"
                    jpeg_bytes = cache.get_jpeg_bytes(int(media_id), kind)
                    if jpeg_bytes:
                        from PySide6.QtCore import QByteArray
                        thumb_pxm = QPixmap()
                        thumb_pxm.loadFromData(QByteArray(jpeg_bytes), "JPEG")
                        if thumb_pxm.isNull():
                            thumb_pxm = None
            except Exception:
                pass

        # 3. Last-resort: msgstore embedded thumbnail blob.
        if not thumb_pxm or thumb_pxm.isNull():
            thumb_blob = info.get("thumb_blob")
            if thumb_blob and len(thumb_blob) > 50:
                thumb_pxm = QPixmap()
                thumb_pxm.loadFromData(thumb_blob)
                if thumb_pxm.isNull():
                    thumb_pxm = None
        self._detail_panel.show_media(info, thumb_pxm)

    def _table_context_menu(self, pos):
        """Context menu for table view - same as grid."""
        idx = self._table.indexAt(pos)
        if not idx.isValid():
            return
        row = idx.row()
        if row < 0 or row >= len(self._model._data):
            return
        row_data = self._model._data[row]
        info = {
            "name": row_data[0] or "",
            "mime": row_data[1] or "",
            "resolved": row_data[11] if len(row_data) > 11 else None,
            "file_exists": bool(row_data[9]) if len(row_data) > 9 else False,
            "status": "on_disk" if (len(row_data) > 9 and row_data[9]) else "missing",
            "from_me": bool(row_data[13]) if len(row_data) > 13 else False,
            "message_id": row_data[14] if len(row_data) > 14 else None,
            "conversation_id": row_data[15] if len(row_data) > 15 else None,
            "conversation_name": row_data[16] if len(row_data) > 16 else "",
        }
        if not info["file_exists"] and (len(row_data) > 10 and row_data[10]):
            from app.services.media_crypto import is_url_likely_valid
            info["status"] = "downloadable" if is_url_likely_valid(row_data[10]) else "expired"

        # Reuse grid context menu logic
        media_id = row_data[7] if len(row_data) > 7 else 0
        menu = QMenu(self)
        resolved = info.get("resolved")
        conv_name = info.get("conversation_name", "")
        direction = "Sent" if info.get("from_me") else "Received"
        if conv_name:
            header = menu.addAction(f"\U0001F4AC {direction} in: {conv_name}")
            header.setEnabled(False)
            menu.addSeparator()
        if resolved and os.path.isfile(resolved):
            open_act = menu.addAction("\U0001F4C2 Open File")
            open_act.triggered.connect(lambda: self._open_file(resolved))
        elif info.get("status") == "downloadable":
            dl_act = menu.addAction("\u21E9 Download & Decrypt")
            dl_act.triggered.connect(lambda _=False, mid=media_id: self._download_single(mid))
        conv_id = info.get("conversation_id")
        msg_id = info.get("message_id")
        if conv_id and msg_id:
            go_act = menu.addAction("\u2192 Go to Message in Chat")
            go_act.triggered.connect(lambda _=False, c=conv_id, m=msg_id: self.go_to_chat.emit(c, m))
        # Inject the same tag actions the grid context menu uses so the
        # workflow is identical regardless of view mode.  We need to
        # carry the file_hash through too for the "tag all shared
        # instances" path.
        if msg_id:
            try:
                _hash_row = Database.get().fetchone(
                    "SELECT file_hash FROM media WHERE id = ?", (media_id,)
                )
                if _hash_row and _hash_row[0]:
                    info["file_hash"] = _hash_row[0]
            except Exception:
                pass
            self._add_tag_actions_to_menu(menu, info, msg_id)
        if menu.actions():
            menu.exec(self._table.viewport().mapToGlobal(pos))

    def _open_preview(self, index: QModelIndex):
        """Open media preview dialog for the selected media."""
        info = index.data(MEDIA_INFO_ROLE) or {}
        self._open_preview_info(info)

    def _open_preview_info(self, info: dict):
        """Open the internal media preview/player for a media info dict."""
        resolved = info.get("resolved")
        if not resolved or not os.path.isfile(resolved):
            return
        try:
            from app.views.dialogs.media_viewer_dialog import MediaViewerDialog
            conv_id = info.get("conversation_id")
            msg_id = info.get("message_id")
            type_label = info.get("type_label") or ""
            mime = (info.get("mime") or "").lower()
            media_type = type_label or "image"
            if mime.startswith("video/"):
                media_type = "video"
            elif mime.startswith("audio/"):
                media_type = "audio"
            elif mime.startswith("image/"):
                media_type = "image"
            dlg = MediaViewerDialog(
                resolved, parent=self,
                file_size=info.get("size_bytes") or 0,
                sender_name=info.get("sender_name") or "",
                timestamp=info.get("timestamp"),
                media_type=media_type,
                message_id=msg_id,
                conversation_id=conv_id,
            )
            # Wire "Go to Chat" button to navigate to the message
            if conv_id and msg_id:
                def _goto(item, _c=conv_id, _m=msg_id, _d=dlg):
                    _d.close()
                    self.go_to_chat.emit(_c, _m)
                dlg.go_to_chat_requested.connect(_goto)
            dlg.exec()
        except Exception:
            self._open_file(resolved)

    # ---------------------------------------------------------------- #
    # Media-forensics report
    # ---------------------------------------------------------------- #

    def _open_media_report_dialog(self) -> None:
        """Open the customisable Media Forensics Report dialog and run
        the generator with the chosen scope / sections / layout / save
        path.  Result opens in the default browser.
        """
        import webbrowser
        from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog
        from app.views.dialogs.media_report_dialog import MediaReportDialog

        db = Database.get()
        # Conversations list — only those that actually have media,
        # so the search dropdown isn't cluttered with empty chats.
        try:
            convs = [
                {"id": r["id"],
                 "display_name":  r["display_name"] or f"#{r['id']}",
                 "chat_type":     r["chat_type"] or "personal",
                 "jid_raw_string": r["jid_raw_string"] or ""}
                for r in db.fetchall(
                    "SELECT DISTINCT c.id, c.display_name, c.chat_type, c.jid_raw_string"
                    " FROM conversation c"
                    " JOIN message m ON m.conversation_id = c.id"
                    " JOIN media me ON me.message_id = m.id"
                    " ORDER BY c.display_name"
                )
            ]
        except Exception as e:
            convs = []
            print(f"[MediaReport] conversation list build failed: {e}")

        try:
            case_dir = db.path.parent
        except Exception:
            from pathlib import Path
            case_dir = Path.home()
        default_dir = case_dir / "exports"
        try:
            default_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            default_dir = case_dir

        dlg = MediaReportDialog(self, default_dir, convs)
        if dlg.exec() != dlg.DialogCode.Accepted or not dlg.is_ok:
            return

        # The new dashboard is a folder-shaped artifact; the generator
        # writes index.html + data/ + vendor/ + thumbs/ inside the
        # picked output folder.  For huge cases the per-stage progress
        # dialog gives the analyst something to watch (chunk write +
        # thumbnail emission can be the slow steps).
        prog = QProgressDialog(
            "Building media dashboard…", None, 0, 0, self
        )
        prog.setWindowTitle("Media dashboard")
        prog.setLabelText("Preparing…")
        prog.setMinimumDuration(150)
        prog.setCancelButton(None)
        prog.show()
        QApplication.processEvents()

        def _on_stage(label, cur, total):
            prog.setLabelText(
                f"{label}\n  {cur:,} / {total:,}" if total else label
            )
            if total:
                prog.setMaximum(total)
                prog.setValue(min(cur, total))
            else:
                prog.setMaximum(0)
            QApplication.processEvents()

        try:
            generate_media_report = self._load_media_report_fn()
            out_path = generate_media_report(
                analysis_db_path=str(db.path),
                output_path=str(dlg.output_path),
                conversation_ids=dlg.selected_conv_ids,
                sections=dlg.sections,
                hide_stickers=dlg.hide_stickers,
                include_thumbnails=dlg.include_thumbnails,
                thumbnail_quality=dlg.thumb_quality,
                progress_cb=_on_stage,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            prog.close()
            QMessageBox.warning(self, "Dashboard failed",
                                 f"Could not build media dashboard:\n\n{e}")
            return
        prog.close()

        if dlg.open_after:
            try:
                from pathlib import Path
                if Path(out_path).exists():
                    webbrowser.open(Path(out_path).as_uri())
            except Exception:
                pass

    @staticmethod
    def _load_media_report_fn():
        """Load ``generate_media_report`` from backend/ via importlib so
        the gui's ``app`` package doesn't shadow the backend's
        ``app.reports`` import path.  Same loader pattern as the
        other report dialogs."""
        import importlib.util
        from pathlib import Path as _P
        here = _P(__file__).resolve()
        for parent in here.parents:
            candidate = parent / "backend" / "app" / "reports" / "media_report.py"
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location(
                    "wainsight_media_report", str(candidate))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod.generate_media_report
        raise FileNotFoundError(
            "Could not locate backend/app/reports/media_report.py"
        )

    def _resolve_by_hash(self):
        """Find missing media where another media with the same file_hash
        has file_exists=1, then update the missing record's resolved_file_path.

        FORENSIC CORRECTNESS: hash-linked rows MUST set
        ``recovery_method = 'hash_linked'``.  Without that flag the
        chat renderer's forensic info panel would treat the row as
        an original transfer to the device, even when the
        displayed bytes came from a different message — an analyst
        could mistake a hash-linked copy for proof of receipt.
        """
        import time as _time_mod
        db = Database.get()
        try:
            # Find missing media that have a hash-match with an on-disk file
            candidates = db.fetchall(
                """SELECT missing.id, donor.resolved_file_path
                   FROM media missing
                   JOIN media donor
                     ON donor.file_hash = missing.file_hash
                    AND donor.file_hash IS NOT NULL
                    AND donor.file_hash != ''
                    AND donor.file_exists = 1
                    AND donor.resolved_file_path IS NOT NULL
                    AND donor.resolved_file_path != ''
                   WHERE missing.file_exists = 0
                     AND missing.id != donor.id
                   GROUP BY missing.id"""
            )
            resolved_count = 0
            recovery_ts = int(_time_mod.time() * 1000)
            for row in candidates:
                mid = row[0]
                donor_path = row[1]
                if donor_path and os.path.isfile(donor_path):
                    db.execute_write(
                        "UPDATE media SET resolved_file_path = ?, file_exists = 1, "
                        "recovery_method = 'hash_linked', recovery_timestamp = ? "
                        "WHERE id = ?",
                        (donor_path, recovery_ts, mid),
                    )
                    resolved_count += 1

            if resolved_count > 0:
                db.reconnect_read()
                self._count_label.setText(
                    f"Resolved {resolved_count} file(s) by hash cross-reference"
                )
                self._apply()
                self._load_stats()
            else:
                self._count_label.setText("No additional files could be resolved by hash")
        except Exception as e:
            self._count_label.setText(f"Hash resolve error: {e}")

    def _open_file(self, path: str):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
