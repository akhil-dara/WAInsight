"""QWebEngineView-based chat renderer — replaces QListView + BubbleDelegate."""
from __future__ import annotations

import base64
import json
import os
import time as _time
from pathlib import Path

from PySide6.QtCore import Signal, QUrl
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings

from app.views.widgets.chat_bridge import ChatBridge
from app.views.widgets.chat_video_thumb_worker import ChatVideoThumbWorker

# Paths to JS/CSS assets (relative to this file)
_ASSETS_DIR = Path(__file__).parent

# Thumbnail cache — write BLOBs to temp .jpg files, serve as file:// URLs
import tempfile as _tempfile
_thumb_cache_dir = os.path.join(_tempfile.gettempdir(), "wa_forensic_thumbs")
os.makedirs(_thumb_cache_dir, exist_ok=True)


def _build_poll_option_image_payload(rows: list[dict]) -> list[dict]:
    """Convert per-option-image dicts into the shape JS wants.

    Each row from the chat_viewer auxiliary fetch carries:
      name (str), path (str), thumb (base64 JPEG), exists (bool),
      width (int), height (int).
    The JS poll renderer expects:
      {name, src} where `src` is either a `file://` URL (when the
      original is on disk) or a base64 data URL (when only the
      embedded thumb is available — common for channel-poll image
      options where the on-disk path is NULL).
    """
    out: list[dict] = []
    for r in rows:
        src = ""
        if r.get("exists") and r.get("path"):
            try:
                p = Path(r["path"])
                if p.exists():
                    src = p.as_uri()
            except Exception:
                src = ""
        if not src and r.get("thumb"):
            src = "data:image/jpeg;base64," + r["thumb"]
        out.append({
            "name": r.get("name", ""),
            "src":  src,
            "w":    r.get("width", 0),
            "h":    r.get("height", 0),
        })
    return out


class _ChatPage(QWebEnginePage):
    """Custom page to suppress navigation away from chat and log JS errors."""

    def acceptNavigationRequest(self, url, nav_type, is_main):
        # Block external navigation — all links go through bridge
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main)

    def javaScriptConsoleMessage(self, level, message, line, source):
        err_level = QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel
        if level == err_level:
            print(f"[WebView JS ERROR] {message} (line {line})")
        elif message.startswith('[JS]'):
            print(f"[WebView] {message}")


class ChatWebView(QWebEngineView):
    """Chromium-based chat view with QWebChannel bridge to Python."""

    # Signals matching old BubbleDelegate/ChatViewerPage interface
    quote_clicked = Signal(str)
    media_clicked = Signal(str)       # JSON string with path + id
    audio_play_requested = Signal(str, int)
    sender_clicked = Signal(int)
    mention_clicked = Signal(int)
    context_menu_requested = Signal(str, int, int)
    load_older_requested = Signal()
    load_range_requested = Signal(int)  # on-demand load around global index
    cancel_pending_requested = Signal()  # cancel in-flight tile requests
    download_requested = Signal(str)
    reaction_clicked = Signal(int)
    forensic_info_requested = Signal(int)  # msg_id — on-demand provenance
    vcard_download_requested = Signal(str, str)  # msg_id, contact_name
    url_clicked = Signal(str)
    scroll_to_unloaded_requested = Signal(str)    # msg_id not in tile map
    scroll_to_key_unloaded_requested = Signal(str)  # key_id not in tile map
    comments_requested = Signal(str)              # msg_id for comment thread
    receipt_detail_requested = Signal(int)         # msg_id — per-user receipt popup
    edit_history_requested = Signal(int)           # msg_id — show edit versions
    replies_requested = Signal(int, str)           # msg_id, source_key — show replies panel
    call_origin_nav_requested = Signal(int, int)   # conv_id, msg_id — jump to original group call

    def __init__(self, parent=None):
        super().__init__(parent)

        # Custom page (blocks external nav)
        page = _ChatPage(self)
        self.setPage(page)

        # Enable local file access (needed for sticker WebP from disk)
        page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )

        # QWebChannel bridge
        self._bridge = ChatBridge(self)
        self._channel = QWebChannel(self)
        self._channel.registerObject("bridge", self._bridge)
        page.setWebChannel(self._channel)

        # Connect bridge signals to our signals
        self._bridge.quote_nav.connect(self.quote_clicked)
        self._bridge.media_open.connect(self.media_clicked)
        self._bridge.audio_toggle.connect(self.audio_play_requested)
        self._bridge.sender_nav.connect(self.sender_clicked)
        self._bridge.mention_nav.connect(self.mention_clicked)
        self._bridge.url_open.connect(self.url_clicked)
        self._bridge.context_menu.connect(self.context_menu_requested)
        self._bridge.scroll_near_top.connect(self.load_older_requested)
        self._bridge.load_range.connect(self.load_range_requested)
        self._bridge.cancel_pending.connect(self.cancel_pending_requested)
        self._bridge.download.connect(self.download_requested)
        self._bridge.reaction_click.connect(self.reaction_clicked)
        self._bridge.forensic_info.connect(self.forensic_info_requested)
        self._bridge.vcard_download.connect(self.vcard_download_requested)
        self._bridge.scroll_to_unloaded.connect(self.scroll_to_unloaded_requested)
        self._bridge.scroll_to_key_unloaded.connect(self.scroll_to_key_unloaded_requested)
        self._bridge.comments_click.connect(self.comments_requested)
        self._bridge.receipt_detail.connect(self.receipt_detail_requested)
        self._bridge.edit_history_click.connect(self.edit_history_requested)
        self._bridge.replies_click.connect(self.replies_requested)
        self._bridge.call_origin_nav.connect(self.call_origin_nav_requested)

        # Ready state
        self._ready = False
        self._pending_calls: list[str] = []
        self._generation = 0
        self._bridge.js_ready.connect(self._on_js_ready)
        self.loadFinished.connect(self._on_load_finished)

        # Background video-frame extractor.  Kicked off whenever a
        # video bubble in the current batch hasn't got a cached
        # first-frame JPEG yet.  The signal fires on the GUI thread
        # (queued connection inside the worker), so it's safe to
        # call runJavaScript from this slot directly.
        self._vid_thumb_worker = ChatVideoThumbWorker.get()
        self._vid_thumb_worker.thumb_ready.connect(self._on_video_thumb_ready)

        # Load the shell HTML
        self._load_shell()

    @property
    def bridge(self) -> ChatBridge:
        return self._bridge

    def _load_shell(self):
        """Build and load the HTML shell with inlined CSS + JS."""
        css_path = _ASSETS_DIR / "chat_styles.css"
        js_path = _ASSETS_DIR / "chat_renderer.js"

        css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
        js = js_path.read_text(encoding="utf-8") if js_path.exists() else ""

        html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{css}</style>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body>
<div id="chatArea">
  <div id="loadingSpinner"><div class="spinner"></div></div>
  <div id="scrollContent">
    <div id="messages"></div>
  </div>
</div>
<div id="scrollDate"></div>
<script>{js}</script>
</body></html>"""

        # Use file:// base URL so <img src="file:///..."> works for stickers
        self.setHtml(html, QUrl.fromLocalFile(os.getcwd() + "/"))

    def _on_load_finished(self, ok: bool):
        print(f"[WebView] _on_load_finished: ok={ok}, pending={len(self._pending_calls)}")
        if ok:
            self._ready = True
            # Flush pending calls
            for call in self._pending_calls:
                self.page().runJavaScript(call)
            self._pending_calls.clear()

    def _on_js_ready(self):
        """Called when QWebChannel is connected in JS."""
        print("[WebView] _on_js_ready: QWebChannel connected")
        self._ready = True

    def _run_js(self, code: str):
        """Run JavaScript, queuing if page not ready yet."""
        if self._ready:
            self.page().runJavaScript(code)
        else:
            self._pending_calls.append(code)

    # ---- Public API ----

    def set_generation(self, generation: int):
        """Set the active conversation generation for stale JS-call rejection."""
        self._generation = int(generation or 0)
        self._run_js(f"setLoadGeneration({self._generation});")

    def set_config(self, is_group: bool, owner_label: str = ""):
        """Configure renderer for current conversation."""
        cfg = json.dumps({"is_group": is_group, "owner_label": owner_label})
        self._run_js(f"setConfig({cfg});")

    def set_total_count(self, total: int):
        """Set total message count — creates spacer for full scrollbar range."""
        self._run_js(f"setTotalCount({total});")

    def set_messages_at(self, global_start: int, messages: list[dict]):
        """Send a batch of messages at a specific global index via JSON runJS.

        Args:
            global_start: The global index (SQL OFFSET) of the first message
            messages: List of message dicts (raw, no date separators)
        """
        # Filter out date separators — JS handles them via renderMsg
        raw = [m for m in messages if m.get("message_type") != -1]
        t0 = _time.perf_counter()
        serialized = self._serialize_messages(raw)
        t1 = _time.perf_counter()
        payload_kb = len(serialized) / 1024

        self._run_js(f"loadMessages({global_start}, {serialized}, {self._generation});")
        t2 = _time.perf_counter()
        if payload_kb > 100 or t1 - t0 > 0.1:
            print(f"[WebView] set_messages_at: {len(raw)} msgs, "
                  f"payload={payload_kb:.0f}KB | "
                  f"serialize={t1-t0:.3f}s, runJS={t2-t1:.3f}s")

    def clear(self):
        """Clear all rendered messages."""
        self._run_js("clearMessages();")

    def set_first_unread_msg_id(self, msg_id: int):
        """Tell the renderer which message id is the first unread.
        JS will insert an 'Unread messages' divider above that message when
        it comes into the DOM, and the header's jump-to-unread button uses
        this to target."""
        try:
            mid = int(msg_id or 0)
        except (TypeError, ValueError):
            mid = 0
        self._run_js(f"setFirstUnreadMsgId({mid});")

    def scroll_to_message(self, msg_id: int, placement: str = "center"):
        """Scroll to a specific message by ID.

        ``placement`` selects where the target lands in the viewport:
            * ``"center"`` (default) — target at ~1/3 from the top.
              Used for search hits and quote-nav, so the user can read
              context above and below the message.
            * ``"start"`` — target at the very top of the viewport.
              Used by the first-unread auto-jump on chat open: the
              divider goes to the top so unread messages stack below it,
              matching the native WhatsApp UX.  Avoids the "scrolled to
              a random position above" feel caused by scroll-settle
              repositioning the target when older tiles arrive.
        """
        # Sanitize — only allow the two known modes.
        place = "start" if placement == "start" else "center"
        self._run_js(f"scrollToMessage({msg_id}, false, '{place}');")

    def set_pending_scroll(self, msg_id: int):
        """Pre-set the pending scroll target BEFORE loading tiles.
        This prevents doScroll from scrolling to bottom on first load."""
        self._run_js(f"_pendingScrollMsgId = {msg_id};")

    def highlight_album_child(self, parent_msg_id: int, child_msg_id: int,
                               pos_1_based: int) -> None:
        """Tell the renderer to pulse a specific child cell inside an album
        bubble.  Used after a Go-to-Chat from the media gallery on an
        album member: we scrolled to the parent message, and now we want
        the user to SEE which of the N grid cells corresponds to the
        photo they clicked."""
        try:
            pid = int(parent_msg_id)
            cid = int(child_msg_id)
            pos = int(pos_1_based)
        except (TypeError, ValueError):
            return
        self._run_js(f"highlightAlbumChild({pid}, {cid}, {pos});")

    def scroll_to_key(self, key_id: str):
        """Scroll to a message by source_key_id."""
        escaped = key_id.replace("'", "\\'")
        self._run_js(f"scrollToKey('{escaped}');")

    def highlight_search(self, text: str):
        """Apply search highlighting in JS."""
        escaped = text.replace("'", "\\'").replace("\\", "\\\\")
        self._run_js(f"highlightSearch('{escaped}');")

    def set_search_target(self, msg_id: int | None):
        """Set persistent highlight on a specific message (teal border)."""
        self._run_js(f"setSearchTarget({msg_id or 'null'});")

    def update_audio_progress(self, msg_id: int, progress: float):
        """Update waveform progress for playing audio."""
        self._run_js(f"updateAudioProgress({msg_id}, {progress:.4f});")

    def update_audio_stopped(self, msg_id: int):
        """Reset waveform when audio stops."""
        self._run_js(f"updateAudioStopped({msg_id});")

    def update_tagged_messages(self, tagged_ids: set[int]):
        """Update tagged message indicators."""
        ids_json = json.dumps(list(tagged_ids))
        self._run_js(f"updateTaggedMessages('{ids_json}');")

    def set_font_size(self, size: int):
        """Adjust chat font size."""
        self._run_js(f"setFontSize({size});")

    def set_theme(self, dark: bool):
        """Switch dark/light theme."""
        self._run_js(f"setTheme({'true' if dark else 'false'});")

    def _on_video_thumb_ready(self, media_id: int, file_url: str):
        """Worker finished extracting a first-frame JPEG.  Ask
        the renderer to swap the matching bubble's ``<img src>``.
        The integer key here is the same id the worker uses
        for its cache lookup, which is also stamped on the
        bubble's ``data-vid-msg-id`` attribute, so a single DOM
        query locates the right element."""
        if not file_url:
            return
        safe_url = file_url.replace("\\", "\\\\").replace("'", "\\'")
        self._run_js(
            f"if (typeof updateVideoThumb === 'function') "
            f"updateVideoThumb({int(media_id)}, '{safe_url}');"
        )

    def set_timezone(self, tz_name: str):
        """Update the IANA timezone the chat renderer formats
        timestamps in.

        Day dividers, bubble timestamps and the forensic-info
        panel all re-render in the new timezone — without this
        push, JS would fall back to the host machine's local
        timezone, which usually isn't what the analyst wants in
        a forensic case.
        """
        self._run_js(f"setTimezone({json.dumps(tz_name or '')});")

    def show_loading(self, visible: bool):
        """Show/hide the top loading spinner."""
        self._run_js(f"showLoading({'true' if visible else 'false'});")

    def send_provenance(self, msg_id: int, provenance_json: str):
        """Send on-demand forensic provenance data to JS for a single message."""
        self._run_js(f"receiveProvenance({msg_id}, {provenance_json});")

    # ---- Serialization ----

    @staticmethod
    def _compute_device_label(msg: dict) -> str:
        """Compute human-readable device label from message metadata."""
        dev_num = msg.get("sender_device_number", -1)
        origin = msg.get("origin", 0)
        plat = msg.get("sender_platform_label", "") or ""
        if msg.get("from_me"):
            if origin:
                return f"origin={origin}"
            if plat == "android":
                return "Android (You)"
            return "Phone (You)"
        # Use key_id-classified platform_label if available
        _PLAT_DISPLAY = {
            "android": "Android",
            "iphone": "iPhone",
            "android_linked": "Android (Linked)",
            "iphone_linked": "iPhone (Linked)",
            "companion": "Web/Desktop",
            "newsletter": "Newsletter",
            "channel_bot": "Channel",
        }
        if plat in _PLAT_DISPLAY:
            label = _PLAT_DISPLAY[plat]
            if dev_num is not None and dev_num > 0 and plat in ("companion",):
                label = f"Web/Desktop #{dev_num}"
            return label
        # Fallback for old analysis.db without key_id classification
        if dev_num is not None and dev_num >= 0:
            if dev_num == 0:
                return "Phone"
            return f"Web/Desktop #{dev_num}"
        return ""

    @staticmethod
    def _check_hash_resolved(orig_path: str | None, resolved_path: str | None) -> bool:
        """Check if file was truly found via hash/filename search (different dir)
        vs just path concatenation (same relative path, different prefix)."""
        if not orig_path or not resolved_path or orig_path == resolved_path:
            return False
        # Normalize separators
        orig_norm = orig_path.replace("\\", "/").strip("/")
        resolved_norm = resolved_path.replace("\\", "/").strip("/")
        # If the resolved path ends with the original relative path, it was found
        # at its expected location (just with a media root prefix) → not hash
        if resolved_norm.endswith(orig_norm):
            return False
        # Also check without "Media/" prefix (common WhatsApp DB format)
        if orig_norm.startswith("Media/") and resolved_norm.endswith(orig_norm[6:]):
            return False
        # Different directory structure → truly resolved via hash/filename search
        return True

    def _serialize_messages(self, messages: list[dict]) -> str:
        """Convert message dicts to compact JSON for JS consumption."""
        import re

        # Build JID→name lookup for resolving @mentions in text/quoted_text
        _jid_name_cache = {}
        def _resolve_jid_mentions(text):
            """Replace @{digits} and @{Name|digits} JID mentions with @{contact_name}."""
            if not text or '@' not in text:
                return text
            def _repl_digits(m):
                jid_num = m.group(1)
                if jid_num not in _jid_name_cache:
                    try:
                        from app.services.database import Database
                        db = Database.get()
                        name = db.scalar(
                            "SELECT resolved_name FROM contact WHERE phone_number = ? "
                            "OR phone_jid LIKE ? || '%' OR lid_jid LIKE ? || '%' LIMIT 1",
                            (jid_num, jid_num, jid_num),
                        )
                        _jid_name_cache[jid_num] = name or jid_num
                    except Exception:
                        _jid_name_cache[jid_num] = jid_num
                return '@' + _jid_name_cache[jid_num]
            def _repl_name_pipe(m):
                """Handle @Name|digits pattern (e.g. @Meta AI|867051314767696)."""
                display = m.group(1)  # "Meta AI"
                return '@' + display
            # First handle @Name|digits patterns (Meta AI bot mentions)
            text = re.sub(r'@([^@\n]+?)\|(\d{10,})', _repl_name_pipe, text)
            # Then handle plain @digits
            text = re.sub(r'@(\d{10,})', _repl_digits, text)
            return text

        # ── Album resilience pass ──
        # Pull message_album / message_association for ANY album-parent in
        # this batch directly from analysis.db, regardless of whether the
        # linker stamped them.  This makes the renderer self-sufficient
        # (even if the linker silently failed in some edge case the album
        # header still shows the right count).  msg_id -> meta dict and
        # parent_id -> [child_id...] maps are computed once per batch.
        _album_meta_by_id: dict[int, dict] = {}
        _album_children_by_id: dict[int, list[int]] = {}
        try:
            _album_parent_ids = [
                m.get("id") for m in messages
                if m.get("type_label") == "album" and m.get("id")
            ]
            if _album_parent_ids:
                from app.services.database import Database
                _db = Database.get()
                _qm = ",".join("?" * len(_album_parent_ids))
                # message_album rows
                for r in _db.fetchall(
                    f"SELECT message_id, image_count, video_count, "
                    f"       expected_image_count, expected_video_count, "
                    f"       missing_image_count, missing_video_count, "
                    f"       actual_child_count, note "
                    f"FROM message_album WHERE message_id IN ({_qm})",
                    tuple(_album_parent_ids),
                ):
                    _album_meta_by_id[r[0]] = {
                        "image_count": r[1] or 0,
                        "video_count": r[2] or 0,
                        "expected_image_count": r[3],
                        "expected_video_count": r[4],
                        "missing_image_count": r[5] or 0,
                        "missing_video_count": r[6] or 0,
                        "actual_child_count": r[7] or 0,
                        "note": r[8],
                    }
                # message_association rows (album members only)
                for r in _db.fetchall(
                    f"SELECT parent_message_id, child_message_id "
                    f"FROM message_association "
                    f"WHERE association_type = 2 AND parent_message_id IN ({_qm}) "
                    f"ORDER BY parent_message_id, sort_order",
                    tuple(_album_parent_ids),
                ):
                    _album_children_by_id.setdefault(r[0], []).append(r[1])
        except Exception as _e:
            # Tables don't exist (older analysis.db) - linker fallback
            # in chat_viewer_page handles the heuristic case.
            pass

        # Build per-batch index of msgs by id so we can pull child media
        # metadata when stamping album_children.  Mirror what _build_msg_dict
        # would produce so children render with correct file/thumb URLs.
        _msgs_by_id = {m.get("id"): m for m in messages if m.get("id")}

        result = []
        for msg in messages:
            msg_type = msg.get("message_type", 0)

            # If this is an album parent and album_meta wasn't stamped by
            # the linker, stamp it now from the local map.
            if msg.get("type_label") == "album":
                if not msg.get("album_meta"):
                    _meta = _album_meta_by_id.get(msg.get("id"))
                    if _meta:
                        msg["album_meta"] = _meta
                # Same for album_children: rebuild from message_association
                # if linker missed it OR produced an empty list.
                if not msg.get("album_children"):
                    _kids_ids = _album_children_by_id.get(msg.get("id"), [])
                    if _kids_ids:
                        _kids = []
                        for _kid_id in _kids_ids:
                            _ck = _msgs_by_id.get(_kid_id)
                            if not _ck:
                                continue
                            _kids.append({
                                "id": _ck.get("id"),
                                "thumbnail_blob": _ck.get("thumbnail_blob"),
                                "has_thumb": _ck.get("has_thumb"),
                                "type_label": _ck.get("type_label"),
                                "mime_type": _ck.get("mime_type"),
                                "file_path": _ck.get("file_path"),
                                "resolved_file_path": _ck.get("resolved_file_path"),
                                "media_file_exists": _ck.get("media_file_exists"),
                                "media_url": _ck.get("media_url"),
                                "media_width": _ck.get("media_width"),
                                "media_height": _ck.get("media_height"),
                            })
                            # Mark the child so chat_renderer.js hides it
                            # from the main stream (the parent grid renders it).
                            _ck["album_parent_id"] = msg.get("id")
                        if _kids:
                            msg["album_children"] = _kids

            # Date separators (type -1)
            if msg_type == -1:
                result.append({
                    "id": -1,
                    "type": -1,
                    "text": msg.get("display_text", ""),
                    "ts": msg.get("timestamp"),
                })
                continue

            # System event text construction
            system_text = None
            if msg_type == 7 or msg_type == 112:
                system_text = msg.get("display_text") or msg.get("text_content") or ""

            # Thumbnail → file:// URL (write BLOB to temp cache, no base64 in JSON)
            msg_id = msg.get("id", 0)
            thumb_url = None
            thumb_blob = msg.get("thumbnail_blob")
            if thumb_blob and msg.get("has_thumb"):
                try:
                    thumb_path = os.path.join(_thumb_cache_dir, f"{msg_id}.jpg")
                    if not os.path.exists(thumb_path):
                        with open(thumb_path, "wb") as _tf:
                            _tf.write(thumb_blob)
                    thumb_url = Path(thumb_path).as_uri()
                except Exception:
                    pass
            # Fallback: for TEXT messages that have a link-preview thumbnail
            # (og:image captured from msgstore.message_thumbnail), write that
            # BLOB instead. renderLinks() uses msg.thumb as the link card's
            # image, so reusing the same field means no JS changes needed.
            if thumb_url is None:
                link_thumb_blob = msg.get("link_thumb_blob")
                if link_thumb_blob and len(link_thumb_blob) > 50:
                    try:
                        lt_path = os.path.join(_thumb_cache_dir, f"lnk_{msg_id}.jpg")
                        if not os.path.exists(lt_path):
                            with open(lt_path, "wb") as _lf:
                                _lf.write(link_thumb_blob)
                        thumb_url = Path(lt_path).as_uri()
                    except Exception:
                        pass

            # Quoted message thumbnail → file:// URL
            quoted_thumb_url = None
            qt_blob = msg.get("quoted_thumb_blob")
            if qt_blob and len(qt_blob) > 50:
                try:
                    qt_path = os.path.join(_thumb_cache_dir, f"qt_{msg_id}.jpg")
                    if not os.path.exists(qt_path):
                        with open(qt_path, "wb") as _qf:
                            _qf.write(qt_blob)
                    quoted_thumb_url = Path(qt_path).as_uri()
                except Exception:
                    pass

            # Location thumbnail → file:// URL
            loc_thumb_url = None
            loc_thumb_blob = msg.get("loc_thumbnail_blob")
            if loc_thumb_blob and len(loc_thumb_blob) > 50:
                try:
                    loc_thumb_path = os.path.join(_thumb_cache_dir, f"loc_{msg_id}.jpg")
                    if not os.path.exists(loc_thumb_path):
                        with open(loc_thumb_path, "wb") as _lf:
                            _lf.write(loc_thumb_blob)
                    loc_thumb_url = Path(loc_thumb_path).as_uri()
                except Exception:
                    pass

            # Sticker → file:// URL (animated stickers can be 1-2MB)
            sticker_file_url = None
            if msg.get("type_label") == "sticker":
                fp = msg.get("resolved_file_path") or msg.get("file_path") or ""
                if fp and msg.get("media_file_exists", False):
                    try:
                        p = Path(fp)
                        if p.exists() and p.stat().st_size < 3_000_000:
                            sticker_file_url = p.as_uri()
                    except Exception:
                        pass

            # Media file → file:// URL for direct browser rendering
            file_url = None
            if msg.get("media_file_exists", False):
                fp = msg.get("resolved_file_path") or msg.get("file_path") or ""
                if fp:
                    try:
                        p = Path(fp)
                        if p.exists():
                            file_url = p.as_uri()
                    except Exception:
                        pass

            # Video first-frame poster.  WhatsApp's embedded
            # ``message_thumbnail`` blob (already in
            # ``thumb_url`` above) is small (~100×100) and looks
            # pixelated at chat-bubble size, and is sometimes
            # not populated at all (older chats, opened
            # view-once, channel forwards).  Qt WebEngine ships
            # without proprietary codec support, so embedding
            # the mp4 directly via ``<video>`` would render
            # blank for HEVC / AV1 / many H.264 profiles.
            #
            # Solution: ``ChatVideoThumbWorker`` extracts the
            # first frame from the on-disk file using Qt's
            # native media stack (Windows Media Foundation /
            # AVFoundation / gstreamer — whatever the host OS
            # can decode) and caches the JPEG in the same L2
            # SQLite store the gallery uses.  A cache miss
            # returns ``None`` here and emits ``thumb_ready``
            # asynchronously; ``updateVideoThumb`` then swaps
            # the bubble's ``<img src>`` once the frame is
            # ready.
            type_label = msg.get("type_label", "")
            mime = msg.get("mime_type") or ""
            is_video_msg = (
                type_label == "video"
                or (mime and mime.startswith("video/"))
            )
            if is_video_msg and file_url and msg_id:
                src_path = (
                    msg.get("resolved_file_path")
                    or msg.get("file_path")
                    or ""
                )
                cached_thumb = self._vid_thumb_worker.lookup_or_request(
                    msg_id, src_path
                )
                if cached_thumb:
                    # Override the embedded msgstore thumb with the
                    # high-quality first frame.  When neither cache
                    # nor extraction succeeds we fall back to the
                    # original ``thumb_url`` (which may be the
                    # embedded thumb or None).
                    thumb_url = cached_thumb

            # HD-twin file_url: when the SD parent has a hd_twin_msg_id
            # and the HD twin's file is on disk, expose its file:// URL
            # so the renderer can prefer the HD bytes for video display.
            hd_twin_file_url = None
            hd_twin_path = msg.get("hd_twin_path") or ""
            if hd_twin_path and msg.get("hd_twin_exists"):
                try:
                    p_hd = Path(hd_twin_path)
                    if p_hd.exists():
                        hd_twin_file_url = p_hd.as_uri()
                except Exception:
                    pass

            # Motion-photo (type-11) twin file_url: parent = still image,
            # child = 1-2s motion clip.  The renderer shows a "▶ Live"
            # badge that plays this clip on click.
            motion_video_file_url = None
            motion_video_path = msg.get("motion_video_path") or ""
            if motion_video_path and msg.get("motion_video_exists"):
                try:
                    p_mv = Path(motion_video_path)
                    if p_mv.exists():
                        motion_video_file_url = p_mv.as_uri()
                except Exception:
                    pass

            # Album children thumbnails → file:// URLs
            album_children = None
            if msg.get("album_children"):
                album_children = []
                for child in msg["album_children"]:
                    c_thumb_url = None
                    c_blob = child.get("thumbnail_blob")
                    c_id = child.get("id", 0)
                    if c_blob and child.get("has_thumb"):
                        try:
                            c_path = os.path.join(_thumb_cache_dir, f"{c_id}.jpg")
                            if not os.path.exists(c_path):
                                with open(c_path, "wb") as _cf:
                                    _cf.write(c_blob)
                            c_thumb_url = Path(c_path).as_uri()
                        except Exception:
                            pass
                    c_file_url = None
                    c_fp = child.get("resolved_file_path") or child.get("file_path") or ""
                    if c_fp and child.get("media_file_exists", False):
                        try:
                            cp = Path(c_fp)
                            if cp.exists():
                                c_file_url = cp.as_uri()
                        except Exception:
                            pass
                    # For video children with the original mp4 on
                    # disk, prefer a frame extracted from the
                    # original via ChatVideoThumbWorker over the
                    # tiny ~100×100 embedded msgstore thumb.  Same
                    # codec-gap fix as for stand-alone video
                    # bubbles (see comment in the parent video
                    # block above).  When the worker has the frame
                    # cached we override ``c_thumb_url`` with its
                    # file:// URL so the album cell renders the
                    # full-quality first frame; on cache miss the
                    # worker queues async extraction and
                    # ``updateVideoThumb`` swaps the <img src> on
                    # the matching ``[data-vid-msg-id]`` cell once
                    # done.
                    c_type = child.get("type_label", "")
                    c_mime = child.get("mime_type") or ""
                    c_is_video = (
                        c_type == "video"
                        or (c_mime and c_mime.startswith("video/"))
                    )
                    if c_is_video and c_file_url and c_id and c_fp:
                        c_extracted = self._vid_thumb_worker.lookup_or_request(
                            c_id, c_fp
                        )
                        if c_extracted:
                            c_thumb_url = c_extracted
                    album_children.append({
                        "id": c_id,
                        "thumb": c_thumb_url,
                        "file_url": c_file_url,
                        "type_label": child.get("type_label"),
                        "mime": child.get("mime_type") or "",
                        "file_path": child.get("resolved_file_path") or child.get("file_path") or "",
                        "file_exists": child.get("media_file_exists", False),
                        "has_url": bool(child.get("media_url")),
                    })

            # Sender avatar → file:// URL (write BLOB to temp cache)
            avatar_url = None
            avatar_blob = msg.get("sender_avatar_blob")
            sender_id = msg.get("sender_id")
            if avatar_blob and sender_id:
                try:
                    avatar_path = os.path.join(_thumb_cache_dir, f"avatar_{sender_id}.jpg")
                    if not os.path.exists(avatar_path):
                        with open(avatar_path, "wb") as _af:
                            _af.write(avatar_blob)
                    avatar_url = Path(avatar_path).as_uri()
                except Exception:
                    pass

            js_msg = {
                "id": msg.get("id", 0),
                "from_me": 1 if msg.get("from_me") else 0,
                "text": _resolve_jid_mentions(msg.get("text_content", "") or ""),
                "type": msg_type,
                "type_label": msg.get("type_label", ""),
                "ts": msg.get("timestamp"),
                "status": msg.get("status", 0),
                "sender": msg.get("sender_name", ""),
                "sender_id": msg.get("sender_id"),
                "sender_phone": msg.get("phone_jid", "").replace("@s.whatsapp.net", "") if msg.get("phone_jid") else "",
                "avatar": avatar_url,
                "is_ghost": msg.get("is_ghost", False),
                "is_tagged": msg.get("is_tagged", False),
                "is_fwd": msg.get("is_forwarded", False),
                "fwd_score": msg.get("forward_score"),
                "is_starred": msg.get("is_starred", False),
                "is_edited": msg.get("is_edited", False),
                "is_revoked": msg.get("is_revoked", False),
                "revoked_by": msg.get("revoked_by_admin_name") or None,
                "is_bot": msg.get("is_bot_message", False),
                "quoted_text": _resolve_jid_mentions(msg.get("quoted_text")) or None,
                "quoted_type": msg.get("quoted_type") or None,
                "quoted_sender": msg.get("quoted_sender") or None,
                "quoted_thumb": quoted_thumb_url,
                "reply_key": msg.get("reply_to_key_id") or None,
                "source_key": msg.get("source_key_id") or None,
                "src_id": msg.get("source_msg_id"),  # Original msgstore.db _id
                "file_path": msg.get("resolved_file_path") or msg.get("file_path") or "",
                "file_exists": msg.get("media_file_exists", False),
                "has_url": bool(msg.get("media_url")),
                "has_key": bool(msg.get("media_key")),
                "cdn_url": msg.get("media_url") or None,
                "mime": msg.get("mime_type") or "",
                "caption": msg.get("media_caption") or None,
                "duration_ms": msg.get("media_duration_ms"),
                "media_w": msg.get("media_width"),
                "media_h": msg.get("media_height"),
                "media_width": msg.get("media_width"),
                "media_height": msg.get("media_height"),
                "file_size": msg.get("file_size"),
                "media_name": msg.get("media_name") or "",
                "page_count": msg.get("page_count") or 0,
                "file_hash": msg.get("file_hash") or "",
                "recovery_method": msg.get("recovery_method") or "",
                "media_status": msg.get("media_status") or "",
                "is_hash_resolved": (msg.get("recovery_method") == "hash_linked"),
                "is_recovered_download": (msg.get("recovery_method") == "downloaded"),
                "orig_file_path": msg.get("file_path") or "",
                "thumb": thumb_url,
                "file_url": file_url,
                # HD-twin: WhatsApp dual-quality send.  When set, the
                # bubble is the SD parent and these fields describe an
                # HD sibling that may be on disk with higher-resolution
                # bytes — JS prefers `hd_file_url` for the <video>/<img>
                # `src` and shows an "HD" badge.
                "hd_msg_id":   msg.get("hd_twin_msg_id") or 0,
                "hd_file_url": hd_twin_file_url,
                "hd_path":     msg.get("hd_twin_path") or "",
                "hd_exists":   bool(msg.get("hd_twin_exists")),
                "hd_size":     msg.get("hd_twin_size") or 0,
                "hd_w":        msg.get("hd_twin_width") or 0,
                "hd_h":        msg.get("hd_twin_height") or 0,
                "hd_hash":     msg.get("hd_twin_hash") or "",
                # HD twin's own download eligibility — independent
                # of the SD parent.  WhatsApp uploads each quality
                # tier as a separate CDN object with its own URL +
                # key, so the SD parent being on-disk says nothing
                # about whether the HD twin can still be fetched.
                # Renderer uses these to decide whether to show a
                # "Download HD" pill on bubbles where the SD bytes
                # are visible but the HD bytes aren't yet.
                "hd_has_url":  bool(msg.get("hd_twin_url")),
                "hd_has_key":  bool(msg.get("hd_twin_key")),
                "hd_status":   msg.get("hd_twin_status") or "",
                "hd_recovery": msg.get("hd_twin_recovery") or "",
                # HD-pair role markers.  ``hd_pair_role`` is 'sd'
                # for the SD parent, 'hd' for the HD twin, '' for
                # non-paired messages.  ``hd_pair_twin_id`` is the
                # message # of the OTHER member of the pair, so
                # the renderer can show clear "↗ HD #X" / "↘ SD
                # #Y" cross-references on each bubble and let the
                # analyst click through to inspect the twin.
                "hd_pair_role":    msg.get("is_hd_pair_role") or "",
                "hd_pair_twin_id": msg.get("hd_pair_twin_id") or 0,
                # When this row IS the HD twin: details of the SD
                # parent so the bubble can summarise the pair.
                "sd_parent_exists":   bool(msg.get("sd_parent_exists")),
                "sd_parent_size":     msg.get("sd_parent_size") or 0,
                "sd_parent_w":        msg.get("sd_parent_width") or 0,
                "sd_parent_h":        msg.get("sd_parent_height") or 0,
                "sd_parent_recovery": msg.get("sd_parent_recovery") or "",
                # Motion-photo: parent=still image, child=1-2s clip.
                # Bubble shows "▶ Live" badge → plays this on click.
                "motion_msg_id":     msg.get("motion_video_msg_id") or 0,
                "motion_file_url":   motion_video_file_url,
                "motion_path":       msg.get("motion_video_path") or "",
                "motion_exists":     bool(msg.get("motion_video_exists")),
                "motion_duration":   msg.get("motion_video_duration_ms") or 0,
                # Poll image options (channel polls with images per
                # option) — list of {name, path, thumb, exists, width,
                # height}.  thumb is already base64-encoded JPEG;
                # `path` becomes file:// if exists.  Renderer matches
                # each option_name against poll_options to attach
                # images to the right poll row.
                "poll_option_images": _build_poll_option_image_payload(
                    msg.get("poll_option_images") or []
                ),
                "is_view_once": msg.get("is_view_once", False),
                "vo_state": msg.get("view_once_state"),  # 0=not opened, 1=opened, 2=played
                "sticker_url": sticker_file_url,
                # System event
                "system_text": system_text,
                "event_label": msg.get("system_event_label"),
                "event_data": msg.get("system_event_data"),
                "se_actor": msg.get("system_event_actor") or None,
                "se_target": msg.get("system_event_target") or None,
                "se_actor_jid": msg.get("se_actor_phone_jid") or None,
                "se_actor_lid": msg.get("se_actor_lid_jid") or None,
                "se_target_jid": msg.get("se_target_phone_jid") or None,
                "se_target_lid": msg.get("se_target_lid_jid") or None,
                # Number change details
                "nc_old_phone": msg.get("nc_old_phone") or None,
                "nc_new_phone": msg.get("nc_new_phone") or None,
                "nc_old_name": msg.get("nc_old_name") or None,
                "nc_new_name": msg.get("nc_new_name") or None,
                # Reactions
                "reactions": msg.get("reactions_str") or None,
                "reaction_count": msg.get("reaction_count", 0) or 0,
                "reactions_detail": msg.get("reactions_detail") or None,
                # Reply / quote count
                "reply_count": msg.get("reply_count", 0) or 0,
                # Comment thread
                "comment_count": msg.get("comment_count", 0) or 0,
                # Link details
                "link": msg.get("link_details") or None,
                # Poll
                "poll": msg.get("poll_options") or None,
                "poll_voters": msg.get("poll_total_voters", 0) or 0,
                "poll_voter_names": msg.get("poll_voters") or "",
                # Location
                "lat": msg.get("loc_latitude"),
                "lon": msg.get("loc_longitude"),
                "place": msg.get("loc_place_name"),
                "place_addr": msg.get("loc_place_address"),
                "loc_is_live": msg.get("loc_is_live", False),
                "loc_dur": msg.get("loc_live_duration"),
                "loc_final_lat": msg.get("loc_final_lat"),
                "loc_final_lon": msg.get("loc_final_lon"),
                "loc_final_ts": msg.get("loc_final_ts"),
                "loc_thumb": loc_thumb_url,
                "loc_map_url": msg.get("loc_map_url"),
                # Call
                "call_dur": msg.get("call_duration"),
                "call_video": msg.get("call_is_video", False),
                "call_result": msg.get("call_result_label"),
                "call_is_group": msg.get("call_is_group", False),
                "call_participants": msg.get("call_participants"),
                "call_category": msg.get("call_category"),
                "call_creator": msg.get("call_creator_name"),
                # Origin chat for synthetic per-participant call echoes —
                # populated when the call's "home" conversation differs
                # from the chat being viewed (group / multi-person /
                # voice-chat reconstructed copies).  Renderer shows a
                # "from <group>" line + Go-to-original button.
                "call_origin_conv_id":   msg.get("call_origin_conv_id") or 0,
                "call_origin_conv_name": msg.get("call_origin_conv_name") or "",
                "call_origin_chat_type": msg.get("call_origin_chat_type") or "",
                "call_origin_msg_id":    msg.get("call_origin_msg_id") or 0,
                # Mark synthesized call-log entries so the
                # renderer can show a "reconstructed" indicator.
                # ``call_ingester`` stamps these with negative
                # ``source_msg_id`` values; real msgstore ``_id``
                # values are always positive, so a negative
                # value is an unambiguous synthetic marker that
                # also avoids ``UNIQUE`` collisions on the
                # source-id index.
                "is_synthesized": (msg.get("message_type") == 90
                                   and (msg.get("source_msg_id") or 0) < 0),
                # vCard
                "vcard": msg.get("vcard_data") or None,
                # Mentions
                "mentions": msg.get("mentions_str") or None,
                # Scheduled event
                "scheduled_event_data": msg.get("scheduled_event_data") or None,
                # Forensic timestamps
                "delivered_ts": msg.get("first_delivered_ts"),
                "read_ts": msg.get("first_read_ts"),
                "origin": msg.get("origin", 0),
                "oflags": msg.get("origination_flags", 0),
                # Device label for forensic panel
                "device_label": self._compute_device_label(msg),
                # Platform detection from key_id
                "platform": msg.get("sender_platform_label", "") or "",
                "device_num": msg.get("sender_device_number", -1),
                # Business / Meta Verified
                "is_verified": msg.get("sender_is_meta_verified", False),
                "is_biz": msg.get("sender_is_business", False),
                # Forensic: original JIDs and raw text
                "phone_jid_full": msg.get("phone_jid") or "",
                "lid_jid_full": msg.get("lid_jid") or "",
                "raw_text": msg.get("text_content") or "",
                # Forensic: raw msgstore.db row IDs for cross-referencing
                "sender_jid_row_id": msg.get("sender_jid_row_id"),
                "source_chat_row_id": msg.get("source_chat_row_id"),
                "source_media_row_id": msg.get("source_media_row_id"),
                # Member label
                "member_label": msg.get("member_label"),
                # Album
                "album_parent_id": msg.get("album_parent_id"),
                "album_children": album_children,
                "album_meta": msg.get("album_meta"),  # {image_count, video_count, expected_*, missing_*, note} from message_album
                # Recovery tracking (already set above via recovery_method/media_status)
            }
            result.append(js_msg)

        try:
            import orjson
            return orjson.dumps(result).decode("utf-8")
        except ImportError:
            return json.dumps(result, separators=(",", ":"), ensure_ascii=False)

    def contextMenuEvent(self, event):
        """Suppress default QWebEngineView context menu — we handle it via bridge."""
        # Don't call super — the JS contextmenu handler sends to bridge instead
        pass
