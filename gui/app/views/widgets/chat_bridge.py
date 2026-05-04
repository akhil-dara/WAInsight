"""QWebChannel bridge — Python<->JS communication for chat renderer."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot


class ChatBridge(QObject):
    """Exposed to JS via QWebChannel. JS calls @Slot methods, which emit signals
    that ChatWebView/ChatViewerPage connect to existing handlers."""

    # Signals emitted when JS triggers actions
    quote_nav = Signal(str)            # reply_to_key_id
    media_open = Signal(str)           # JSON: {path, id}
    audio_toggle = Signal(str, int)    # file_path, msg_id
    audio_seek = Signal(float)         # fraction 0-1
    sender_nav = Signal(int)           # contact_id
    mention_nav = Signal(int)          # contact_id
    url_open = Signal(str)             # URL string
    context_menu = Signal(str, int, int)  # msg_id_str, screenX, screenY
    scroll_near_top = Signal()         # request older messages
    load_range = Signal(int)           # on-demand load around global index
    download = Signal(str)             # msg_id as string
    reaction_click = Signal(int)       # msg_id
    forensic_info = Signal(int)        # msg_id — request provenance on-demand
    vcard_download = Signal(str, str)  # msg_id_str, contact_name
    cancel_pending = Signal()            # cancel all in-flight tile requests
    js_ready = Signal()                # JS renderer initialized
    scroll_to_unloaded = Signal(str)   # msg_id_str — scroll to msg not yet in tile map
    scroll_to_key_unloaded = Signal(str)  # key_id — scroll to msg by key not in tile map
    comments_click = Signal(str)       # msg_id_str — open comment thread panel
    receipt_detail = Signal(int)       # msg_id — show per-user receipt detail popup
    edit_history_click = Signal(int)   # msg_id — show edit version history popup
    replies_click = Signal(int, str)   # msg_id, source_key — show who replied panel
    call_origin_nav = Signal(int, int) # conv_id, msg_id — jump to original group/multi-person call

    @Slot(str)
    def onQuoteClick(self, key_id: str):
        self.quote_nav.emit(key_id)

    @Slot(str)
    def onMediaClick(self, json_str: str):
        self.media_open.emit(json_str)

    @Slot(str, int)
    def onAudioClick(self, path: str, msg_id: int):
        self.audio_toggle.emit(path, msg_id)

    @Slot(float)
    def onAudioSeek(self, frac: float):
        self.audio_seek.emit(frac)

    @Slot(int)
    def onSenderClick(self, cid: int):
        self.sender_nav.emit(cid)

    @Slot(int)
    def onMentionClick(self, cid: int):
        self.mention_nav.emit(cid)

    @Slot(str)
    def onUrlClick(self, url: str):
        self.url_open.emit(url)

    @Slot(str, int, int)
    def onContextMenu(self, msg_id_str: str, x: int, y: int):
        self.context_menu.emit(msg_id_str, x, y)

    @Slot()
    def onScrollNearTop(self):
        self.scroll_near_top.emit()

    @Slot(int)
    def onLoadRange(self, global_idx: int):
        self.load_range.emit(global_idx)

    @Slot(str)
    def onDownloadClick(self, msg_id_str: str):
        self.download.emit(msg_id_str)

    @Slot(int)
    def onReactionClick(self, msg_id: int):
        self.reaction_click.emit(msg_id)

    @Slot(int)
    def onForensicInfo(self, msg_id: int):
        self.forensic_info.emit(msg_id)

    @Slot(str, str)
    def onVcardDownload(self, msg_id_str: str, contact_name: str):
        self.vcard_download.emit(msg_id_str, contact_name)

    @Slot()
    def onCancelPending(self):
        self.cancel_pending.emit()

    @Slot(str)
    def onCopyToClipboard(self, text: str):
        """Route clipboard writes through Qt's QClipboard.

        Why we need this: QWebEngineView restricts ``navigator.clipboard``
        for security (only fires on a real user gesture from inside the
        page, and even then it can silently fail without HTTPS / focus).
        ``document.execCommand('copy')`` is also being phased out.  The
        Copy button in the Forensic Info panel therefore goes through
        the bridge - the host process owns the clipboard handle so the
        write always succeeds.
        """
        try:
            from PySide6.QtWidgets import QApplication
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(text or "")
        except Exception as e:
            print(f"[ChatBridge] copyToClipboard failed: {e}")

    @Slot(str)
    def onCommentsClick(self, msg_id_str: str):
        self.comments_click.emit(msg_id_str)

    @Slot(int)
    def onReceiptDetail(self, msg_id: int):
        self.receipt_detail.emit(msg_id)

    @Slot(int)
    def onEditHistoryClick(self, msg_id: int):
        self.edit_history_click.emit(msg_id)

    @Slot(int, str)
    def onRepliesClick(self, msg_id: int, source_key: str):
        self.replies_click.emit(msg_id, source_key)

    @Slot(int, int)
    def onCallOriginNav(self, conv_id: int, msg_id: int):
        """JS click on the 'Go to group call' pill on a reconstructed
        per-participant call message.  Emits a signal the parent
        ChatWebView re-emits to ChatViewerPage, which forwards to
        MainWindow._switch_to_conversation(conv_id, msg_id).
        """
        self.call_origin_nav.emit(conv_id, msg_id)

    @Slot(str)
    def onScrollToUnloaded(self, msg_id_str: str):
        self.scroll_to_unloaded.emit(msg_id_str)

    @Slot(str)
    def onScrollToKeyUnloaded(self, key_id: str):
        self.scroll_to_key_unloaded.emit(key_id)

    @Slot()
    def onReady(self):
        self.js_ready.emit()
