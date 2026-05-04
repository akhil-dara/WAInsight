"""Background extractor of first-frame thumbnails for video bubbles
in the chat web view.

WhatsApp's embedded thumbnails (msgstore.message_thumbnail) are
typically small (~100×100) and not always populated for every
video.  An ``<img>`` poster sourced from the actual on-disk video
gives a sharper, always-available thumbnail.

We can't ask Qt WebEngine to extract that frame in-page because
WebEngine ships without proprietary codec support — HEVC / H.265
/ AV1 video tags render blank.  Instead, this module uses Qt's
native media stack (``QMediaPlayer`` + ``QVideoSink``), which
uses the host OS decoder (Windows Media Foundation,
AVFoundation, gstreamer) and decodes every codec the OS can.
Each extracted frame is JPEG-encoded once and cached in the same
SQLite L2 store the Media Gallery uses, so thumbnails persist
across app restarts and are shared between chat and gallery
views.

Architecture:
  * Single worker ``QThread`` — ``QMediaPlayer`` does not run
    well concurrently on every platform (notably Windows audio).
  * Persistent L2 cache keyed by ``(media_id, kind)`` in
    ``<case>/_gallery_thumbcache.db``.
  * Per-process L1: a temp-dir JPEG so ``QWebEngineView`` can
    serve the frame via ``file://`` URL without re-reading the
    SQLite blob on every chat reload.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from queue import Queue, Empty
from typing import Optional

from PySide6.QtCore import (
    QObject, QThread, Signal, QTimer, QUrl, QEventLoop,
    QBuffer, QByteArray, QIODevice, Qt,
)


# Same temp dir convention as chat_web_view's other thumb caches so we
# don't fragment the temp directory tree.
_THUMB_CACHE_DIR = os.path.join(tempfile.gettempdir(), "wa_forensic_thumbs")
os.makedirs(_THUMB_CACHE_DIR, exist_ok=True)

# Persistent-cache "kind" string.  Distinct from the gallery's
# ``"vid"`` namespace because the chat keys lookups by
# ``message.id`` while the gallery keys by ``media.id`` — same
# integer value can refer to different rows in those two tables, so
# sharing one namespace would risk a false-positive hit (gallery's
# media.id=15 thumb being served as the chat's message.id=15
# bubble).  ``"vid_chat"`` keeps the two clearly partitioned in the
# shared SQLite L2 store.
_CACHE_KIND = "vid_chat"


def _temp_jpeg_path(media_id: int) -> str:
    """Path on disk where the chat view stores the cached JPEG so
    QWebEngineView can serve it via ``file://`` URL."""
    return os.path.join(_THUMB_CACHE_DIR, f"vid_{media_id}.jpg")


def _temp_jpeg_url(media_id: int) -> str:
    return Path(_temp_jpeg_path(media_id)).as_uri()


def _get_persistent_cache():
    """Return the gallery's class-shared L2 thumbnail cache,
    instantiating it on first call if the gallery hasn't been
    opened yet.  Returns None if the analysis DB isn't open
    (shouldn't happen during chat rendering, but defensive)."""
    try:
        from app.views.pages.media_gallery_page import (
            MediaThumbnailDelegate, _PersistentThumbCache,
        )
        if MediaThumbnailDelegate._persistent_cache is None:
            from app.services.database import Database
            inst = Database.get()
            db_path = getattr(inst, "_db_path", None)
            if db_path is None:
                db_path = getattr(inst, "path", None)
            if db_path:
                base = os.path.dirname(str(db_path))
                MediaThumbnailDelegate._persistent_cache = (
                    _PersistentThumbCache(base)
                )
        return MediaThumbnailDelegate._persistent_cache
    except Exception:
        return None


class _ChatVidWorker(QObject):
    """Lives on a dedicated QThread.  Pulls (media_id, src_path)
    jobs off the queue, extracts the first frame using
    QMediaPlayer + QVideoSink, writes JPEG bytes to (a) the shared
    persistent SQLite cache and (b) the per-process temp dir, then
    emits ``frame_ready(media_id, file_url)``.
    """
    frame_ready = Signal(int, str)
    extract_failed = Signal(int)

    def __init__(self, queue: Queue):
        super().__init__()
        self._queue = queue
        self._stop = False

    def stop(self):
        self._stop = True
        self._queue.put(None)

    def run_loop(self):
        while not self._stop:
            try:
                job = self._queue.get(timeout=0.5)
            except Empty:
                continue
            if job is None or self._stop:
                break
            media_id, src = job
            ok = False
            try:
                # L2 first — if the persistent SQLite cache has
                # the JPEG (gallery extracted it earlier, or a
                # previous app run did), materialise it to the
                # temp dir without paying the QMediaPlayer cost.
                ok = self._materialize_from_l2(media_id, src)
                if not ok:
                    ok = self._extract_one(media_id, src)
            except Exception as e:
                print(f"[ChatVidThumb] extract {media_id} failed: {e}")
            if ok:
                self.frame_ready.emit(media_id, _temp_jpeg_url(media_id))
            else:
                self.extract_failed.emit(media_id)

    def _materialize_from_l2(self, media_id: int, src: str) -> bool:
        """Check the shared SQLite cache; if it has the JPEG bytes,
        write them to the per-process temp file and return True.
        Returns False on miss — caller will fall through to
        extraction.  This is the path that lets a video extracted
        once (by gallery or chat) skip the multi-second
        QMediaPlayer roundtrip on subsequent chat opens.
        """
        try:
            cache = _get_persistent_cache()
            if cache is None:
                return False
            jpeg = cache.get_jpeg_bytes(media_id, _CACHE_KIND)
            if not jpeg or len(jpeg) <= 100:
                return False
            try:
                with open(_temp_jpeg_path(media_id), "wb") as f:
                    f.write(jpeg)
                return True
            except Exception:
                return False
        except Exception:
            return False

    def _extract_one(self, media_id: int, src: str) -> bool:
        """Mirrors media_gallery_page.py _extract_video_frame:
        QMediaPlayer plays the file briefly, QVideoSink captures
        the first frame, the QImage is JPEG-encoded and persisted.
        Inner QEventLoop is safe here because we're on a worker
        thread (no painter / no GUI re-entrance to worry about).
        """
        if not src or not os.path.isfile(src):
            return False
        try:
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
            if ao:
                ao.setMuted(True)
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
                    img = img.scaled(
                        target, target, Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
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
        try:
            player.stop()
        except Exception:
            pass

        img = result["img"]
        if img is None or img.isNull():
            return False
        try:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            ok = img.save(buf, "JPG", 85)
            buf.close()
            if not ok or ba.size() < 100:
                return False
            jpeg_bytes = bytes(ba)
        except Exception:
            return False
        try:
            with open(_temp_jpeg_path(media_id), "wb") as f:
                f.write(jpeg_bytes)
        except Exception:
            return False
        try:
            cache = _get_persistent_cache()
            if cache is not None:
                mtime = os.path.getmtime(src)
                cache.put_jpeg_bytes(media_id, _CACHE_KIND, jpeg_bytes, mtime)
        except Exception:
            pass
        return True


class ChatVideoThumbWorker(QObject):
    """Process-singleton facade.  ``ChatWebView.__init__`` connects
    ``thumb_ready`` to a JS-bridge call that swaps the bubble's
    <img src> once a frame is ready.

    Cache lookups (``lookup_or_request``) are synchronous and called
    from the GUI thread during chat payload serialization — so the
    initial render still gets a thumbnail (from L1 / L2 cache) when
    one exists, and falls back to whatever ``msg.thumb`` already
    holds (embedded msgstore thumb) for true cache misses.
    """
    _instance: Optional["ChatVideoThumbWorker"] = None
    thumb_ready = Signal(int, str)

    @classmethod
    def get(cls) -> "ChatVideoThumbWorker":
        if cls._instance is None:
            cls._instance = ChatVideoThumbWorker()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._queue: Queue = Queue()
        # ``inflight`` prevents the same media_id from being queued
        # twice from successive set_messages_at calls.  ``failed``
        # prevents a permanent codec-failure from re-queueing on
        # every scroll.  ``known_l1`` is a per-process memo of
        # media_ids whose temp JPEG we've already verified — avoids
        # paying the os.path.exists syscall on every chat reload.
        self._inflight: set[int] = set()
        self._failed: set[int] = set()
        self._known_l1: set[int] = set()
        self._thread = QThread()
        self._thread.setObjectName("ChatVidThumbWorker")
        self._worker = _ChatVidWorker(self._queue)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run_loop)
        # Worker thread → GUI thread (queued) so the JS-bridge call
        # below runs on the right thread.
        self._worker.frame_ready.connect(
            self._on_frame_ready, Qt.QueuedConnection,
        )
        self._worker.extract_failed.connect(
            self._on_extract_failed, Qt.QueuedConnection,
        )
        self._thread.start()

    def lookup_or_request(self, media_id: int, src_path: str) -> Optional[str]:
        """Return a ``file://`` URL if the temp-JPEG L1 cache
        already has it, otherwise queue async work and return
        None — the worker will check the persistent SQLite L2
        cache, materialise from it if it hits, or extract from
        the source video if it misses.

        Critical: this runs on the GUI thread, so anything beyond
        a single fast syscall is forbidden.  Cloud-synced storage
        (OneDrive, iCloud) can stall ``stat`` calls for hundreds
        of ms while it checks remote state, so we only stat the
        local temp dir here and defer L2 lookup + isfile check +
        extraction to the worker thread.
        """
        if not media_id or not src_path:
            return None
        # Per-process memo — once we've confirmed L1 hit for an id
        # we don't need to stat() again on subsequent renders.
        if media_id in self._known_l1:
            return _temp_jpeg_url(media_id)
        temp_path = _temp_jpeg_path(media_id)
        try:
            # Single stat in %TEMP%.  Stays on local disk.
            if os.path.exists(temp_path):
                self._known_l1.add(media_id)
                return _temp_jpeg_url(media_id)
        except OSError:
            pass
        if media_id in self._inflight or media_id in self._failed:
            return None
        # Defer everything else (L2 lookup, isfile check, extract)
        # to the worker thread — those touch the source-video
        # path and must not block the GUI thread.
        self._inflight.add(media_id)
        self._queue.put((media_id, src_path))
        return None

    def _on_frame_ready(self, media_id: int, file_url: str):
        self._inflight.discard(media_id)
        # Memoize so the next ``lookup_or_request`` call returns
        # immediately without a stat() syscall.
        self._known_l1.add(media_id)
        self.thumb_ready.emit(media_id, file_url)

    def _on_extract_failed(self, media_id: int):
        self._inflight.discard(media_id)
        self._failed.add(media_id)

    def stop(self):
        self._worker.stop()
        self._thread.quit()
        self._thread.wait(2000)
