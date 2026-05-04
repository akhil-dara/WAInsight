"""
Main application window -- sidebar + stacked content area + status bar.
Navigation via page_id strings (not stack indices).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenuBar,
    QMessageBox, QProgressBar, QStackedWidget, QStatusBar, QVBoxLayout,
    QWidget,
)

from app.config import APP_NAME, get_current_timezone_display, get_timezone_notifier
from app.services.database import Database
from app.services.theme_manager import ThemeManager
from app.views.pages.calls_page import CallsPage
from app.views.pages.chat_viewer_page import ChatViewerPage
from app.views.pages.contact_detail_page import ContactDetailPage
from app.views.pages.contacts_page import ContactsPage
from app.views.pages.conversations_page import ConversationsPage
from app.views.pages.dashboard_page import DashboardPage
from app.views.pages.edit_history_page import EditHistoryPage
from app.views.pages.events_page import EventsPage
from app.views.pages.ghost_messages_page import GhostMessagesPage
from app.views.pages.group_info_page import GroupInfoPage
from app.views.pages.links_page import LinksPage
from app.views.pages.locations_page import LocationsPage
from app.views.pages.media_recovery_page import MediaRecoveryPage
from app.views.pages.media_gallery_page import MediaGalleryPage
from app.views.pages.documents_page import DocumentsPage
from app.views.pages.image_similarity_page import ImageSimilarityPage
from app.views.pages.analytics_page import AnalyticsPage
from app.views.pages.cross_contact_page import CrossContactPage
from app.views.pages.export_page import ExportPage
from app.views.pages.polls_page import PollsPage
from app.views.pages.revoked_messages_page import RevokedMessagesPage
from app.views.pages.search_page import SearchPage
from app.views.pages.settings_page import SettingsPage
from app.views.pages.status_viewer_page import StatusViewerPage
from app.views.pages.system_events_page import SystemEventsPage
from app.views.pages.tagged_messages_page import TaggedMessagesPage
from app.views.widgets.sidebar import SidebarWidget


class MainWindow(QMainWindow):
    """Application shell with sidebar navigation and page stack."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1200, 700)
        self.resize(1440, 900)

        # Central widget
        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.setCentralWidget(central)

        # Sidebar (push layout, not overlay)
        self._sidebar = SidebarWidget()
        self._sidebar.page_selected.connect(self._navigate_to)
        main_layout.addWidget(self._sidebar)

        # Content area
        self._stack = QStackedWidget()
        main_layout.addWidget(self._stack, 1)

        # Status bar
        self._setup_status_bar()
        get_timezone_notifier().timezone_changed.connect(self._handle_timezone_change)

        # Register pages
        self._pages: dict[str, QWidget] = {}
        self._register_pages()

        # Menu bar
        self._setup_menu_bar()

        # Navigation history stack for back/forward
        self._nav_stack: list[QWidget] = []
        self._nav_forward: list[QWidget] = []

        # Navigate to dashboard by default
        self._sidebar.select_page("dashboard")

        # Load sidebar counts (deferred to not block startup)
        QTimer.singleShot(500, self._update_sidebar_counts)

        # Keyboard shortcuts
        self._setup_shortcuts()

    def _register_pages(self) -> None:
        """Register all pages."""

        # Conversations page with chat viewer
        self._conversations_page = ConversationsPage()
        self._chat_viewer = ChatViewerPage()
        self._group_info = GroupInfoPage()
        self._contact_detail = ContactDetailPage()
        self._conversations_page.conversation_selected.connect(self._open_chat)
        self._chat_viewer.back_requested.connect(self.navigate_back)
        self._chat_viewer.group_info_requested.connect(self._open_group_info)
        self._chat_viewer.contact_requested.connect(self._open_contact_detail)
        self._chat_viewer.conversation_switch_requested.connect(
            self._switch_to_conversation
        )
        self._chat_viewer.find_similar_requested.connect(self._find_similar_images)
        self._group_info.back_requested.connect(
            lambda: self._stack.setCurrentWidget(self._chat_viewer)
        )
        self._contact_detail.back_requested.connect(
            lambda: self._stack.setCurrentWidget(self._chat_viewer)
        )
        self._contact_detail.conversation_requested.connect(self._open_chat)

        # Search page with conversation navigation
        self._search_page = SearchPage()
        self._search_page.conversation_requested.connect(self._open_chat)
        self._search_page.navigate_to_message.connect(self._switch_to_conversation)

        # Implemented pages
        self._add_page("dashboard", DashboardPage())
        self._add_page("conversations", self._conversations_page)
        self._contacts_page = ContactsPage()
        self._contacts_page.contact_selected.connect(self._open_contact_from_list)
        self._add_page("contacts", self._contacts_page)

        # Status Viewer page
        self._status_page = StatusViewerPage()
        self._status_page.conversation_selected.connect(self._open_chat)
        # go_to_message carries msg_id so the status tile lands on its exact post
        self._status_page.go_to_message.connect(self._switch_to_conversation)
        self._status_page.contact_requested.connect(self._open_contact_detail)
        self._add_page("status", self._status_page)

        self._add_page("search", self._search_page)
        self._calls_page = CallsPage()
        self._calls_page.conversation_selected.connect(self._open_chat)
        self._calls_page.navigate_to_message.connect(self._switch_to_conversation)
        self._calls_page.contact_requested.connect(self._open_contact_detail)
        self._add_page("calls", self._calls_page)

        # Chat viewer, group info, contact detail (not in sidebar, accessed via navigation)
        self._stack.addWidget(self._chat_viewer)
        self._stack.addWidget(self._group_info)
        self._stack.addWidget(self._contact_detail)

        # Fully implemented pages
        self._media_gallery = MediaGalleryPage()
        self._media_gallery.go_to_chat.connect(self._switch_to_conversation)
        self._media_gallery.find_similar_requested.connect(self._find_similar_images)
        self._add_page("media", self._media_gallery)

        # Documents — dedicated browser with extension buckets + shared-count
        self._documents_page = DocumentsPage()
        self._documents_page.conversation_selected.connect(self._switch_to_conversation)
        self._add_page("documents", self._documents_page)

        # Community Intelligence page with conversation navigation
        # Cross-Contact Analysis: pick N contacts → see what they
        # share (groups, calls, files, mentions, common chats).
        # Replaces the old Community Intel page.
        self._cross_contact_page = CrossContactPage()
        self._cross_contact_page.conversation_selected.connect(self._switch_to_conversation)
        self._add_page("cross_contact", self._cross_contact_page)

        # Ghost messages page with conversation navigation
        self._ghost_page = GhostMessagesPage()
        self._ghost_page.conversation_selected.connect(self._open_chat)
        self._add_page("ghost", self._ghost_page)

        # Edit history page with conversation navigation
        self._edits_page = EditHistoryPage()
        self._edits_page.conversation_selected.connect(self._open_chat)
        self._add_page("edits", self._edits_page)

        # Revoked messages page with conversation navigation
        self._revoked_page = RevokedMessagesPage()
        self._revoked_page.conversation_selected.connect(self._open_chat)
        self._add_page("revoked", self._revoked_page)
        # System events page with conversation navigation
        self._system_events_page = SystemEventsPage()
        self._system_events_page.conversation_selected.connect(self._open_chat)
        self._system_events_page.navigate_to_message.connect(self._switch_to_conversation)
        self._add_page("system_events", self._system_events_page)

        self._media_recovery_page = MediaRecoveryPage()
        self._media_recovery_page.conversation_selected.connect(self._open_chat)
        self._add_page("media_recovery", self._media_recovery_page)

        self._similarity_page = ImageSimilarityPage()
        self._similarity_page.navigate_to_message.connect(self._switch_to_conversation)
        self._add_page("image_similarity", self._similarity_page)

        # Orphaned media page
        from app.views.pages.orphaned_media_page import OrphanedMediaPage
        self._orphaned_media_page = OrphanedMediaPage()
        self._orphaned_media_page.go_to_chat.connect(self._switch_to_conversation)
        self._add_page("orphaned_media", self._orphaned_media_page)

        # Starred messages page (WhatsApp stars by device owner)
        from app.views.pages.starred_messages_page import StarredMessagesPage
        self._starred_page = StarredMessagesPage()
        self._starred_page.conversation_selected.connect(self._open_chat)
        # go_to_message carries msg_id so double-click lands on the exact starred msg
        self._starred_page.go_to_message.connect(self._switch_to_conversation)
        self._add_page("starred", self._starred_page)

        # Tagged messages page (investigator bookmarks)
        self._tagged_page = TaggedMessagesPage()
        self._tagged_page.conversation_selected.connect(self._open_chat)
        # msg-aware navigation: lands on the exact tagged message with the
        # highlight pulse, via the same stable path media-gallery uses.
        self._tagged_page.go_to_message.connect(self._switch_to_conversation)
        self._add_page("tagged", self._tagged_page)

        # Locations page with conversation navigation
        self._locations_page = LocationsPage()
        self._locations_page.conversation_selected.connect(self._open_chat)
        self._locations_page.go_to_message.connect(self._switch_to_conversation)
        self._add_page("locations", self._locations_page)
        # Links page — right-click "Go to message" + the Find-shared
        # popup both emit ``conversation_selected(conv_id, msg_id)`` so
        # the chat viewer can jump to the originating message.
        self._links_page = LinksPage()
        self._links_page.conversation_selected.connect(self._switch_to_conversation)
        self._add_page("links", self._links_page)
        self._polls_page = PollsPage()
        self._polls_page.navigate_to_message.connect(self._switch_to_conversation)
        self._add_page("polls", self._polls_page)

        # Analytics, Export, Settings
        self._add_page("analytics", AnalyticsPage())
        self._add_page("export", ExportPage())
        self._add_page("settings", SettingsPage())

        # Scheduled Events page (full implementation)
        self._events_page = EventsPage()
        self._events_page.conversation_selected.connect(self._open_chat)
        self._add_page("events", self._events_page)


    def _add_page(self, page_id: str, widget: QWidget) -> None:
        self._pages[page_id] = widget
        self._stack.addWidget(widget)

    def _navigate_to(self, page_id: str) -> None:
        page = self._pages.get(page_id)
        if page:
            current = self._stack.currentWidget()
            if current and current is not page:
                self._nav_stack.append(current)
                self._nav_forward.clear()
                if len(self._nav_stack) > 50:
                    self._nav_stack = self._nav_stack[-50:]
            self._stack.setCurrentWidget(page)

    def navigate_back(self) -> None:
        """Go back to the previous page in the navigation history."""
        if self._nav_stack:
            current = self._stack.currentWidget()
            if current:
                self._nav_forward.append(current)
            prev = self._nav_stack.pop()
            self._stack.setCurrentWidget(prev)

    def _update_sidebar_counts(self) -> None:
        """Load counts for sidebar items from the database."""
        try:
            db = Database.get()
            counts = {}
            # Quick scalar queries for each page
            _queries = {
                "conversations": "SELECT COUNT(*) FROM conversation WHERE message_count > 0",
                "contacts": "SELECT COUNT(*) FROM contact",
                "calls": "SELECT COUNT(*) FROM call_record",
                "media": "SELECT COUNT(*) FROM media",
                "locations": "SELECT COUNT(*) FROM location",
                "links": "SELECT COUNT(*) FROM message_link_detail",
                "polls": "SELECT COUNT(*) FROM poll",
                "ghost": "SELECT COUNT(*) FROM ghost_message",
                "edits": "SELECT COUNT(*) FROM edit_history",
                "revoked": "SELECT COUNT(*) FROM message WHERE is_revoked = 1",
                "system_events": "SELECT COUNT(*) FROM system_event",
                "orphaned_media": "SELECT COUNT(*) FROM orphaned_media",
                "starred": "SELECT COUNT(*) FROM message WHERE is_starred = 1",
                "tagged": "SELECT COUNT(*) FROM message_tag",
            }
            for page_id, sql in _queries.items():
                try:
                    counts[page_id] = db.scalar(sql) or 0
                except Exception:
                    counts[page_id] = 0
            self._sidebar.update_counts(counts)
        except Exception:
            pass  # DB not ready yet

    def _open_chat(self, conv_id: int, display_name: str) -> None:
        """Open chat viewer for a conversation (from double-click)."""
        current = self._stack.currentWidget()
        if current and current is not self._chat_viewer:
            self._nav_stack.append(current)
            self._nav_forward.clear()
        self._chat_viewer.load_conversation(conv_id, display_name)
        self._stack.setCurrentWidget(self._chat_viewer)

    def _open_group_info(self, conv_id: int, display_name: str) -> None:
        """Open group info page (from clicking group name in chat viewer)."""
        self._group_info.load_group(conv_id, display_name)
        self._stack.setCurrentWidget(self._group_info)

    def _open_contact_detail(self, contact_id: int) -> None:
        """Open contact detail view (from clicking sender name in chat)."""
        self._contact_detail.load_contact(contact_id)
        self._stack.setCurrentWidget(self._contact_detail)

    def _switch_to_conversation(self, conv_id: int, msg_id: int = 0) -> None:
        """Switch chat viewer to a different conversation and navigate
        to a message.

        Dedupe guard: when a popup-style navigator (links / documents
        sharing dialog) emits the signal, the dialog's ``accept()`` +
        the explicit ``conversation_selected.emit(...)`` can both
        bubble up to this slot inside the same Qt event-loop tick
        (the dialog close repaints the row underneath, which can
        re-fire its click handler).  The chat viewer would then run
        ``load_conversation`` twice, opening to the right message,
        scrolling away to bottom, then jumping back — which the user
        sees as "it went there and came back".  We swallow a duplicate
        call to the same (conv_id, msg_id) within 800 ms.
        """
        import time
        last = getattr(self, "_last_switch_to", None)
        now = time.monotonic()
        if last and last[0] == conv_id and last[1] == msg_id and (now - last[2]) < 0.8:
            return  # duplicate signal; first one is already handling it
        self._last_switch_to = (conv_id, msg_id, now)

        current = self._stack.currentWidget()
        if current and current is not self._chat_viewer:
            self._nav_stack.append(current)
            self._nav_forward.clear()
        db = Database.get()
        display_name = db.scalar(
            "SELECT COALESCE(display_name, jid_raw_string) FROM conversation WHERE id = ?",
            (conv_id,),
        ) or f"#{conv_id}"
        # Show busy cursor during load
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._chat_viewer.load_conversation(conv_id, display_name, target_msg_id=msg_id)
            self._stack.setCurrentWidget(self._chat_viewer)
        finally:
            QApplication.restoreOverrideCursor()

    def _find_similar_images(self, message_id: int) -> None:
        """Navigate to the Image Similarity page with a query message."""
        self._similarity_page.set_query_message_id(message_id)
        self._sidebar.select_page("image_similarity")

    def _open_contact_from_list(self, contact_id: int) -> None:
        """Open contact detail view (from contacts page double-click)."""
        # Set back button to return to contacts page instead of chat viewer
        try:
            self._contact_detail.back_requested.disconnect()
        except RuntimeError:
            pass
        self._contact_detail.back_requested.connect(
            lambda: self._stack.setCurrentWidget(self._contacts_page)
        )
        self._contact_detail.load_contact(contact_id)
        self._stack.setCurrentWidget(self._contact_detail)

    def _setup_menu_bar(self) -> None:
        # Slim File menu — case management lives on the launch screen,
        # re-ingestion lives in the ingestion wizard, and exports live
        # on the relevant pages (Tagged Messages → bundle, Group Info
        # → report, etc.).  Cluttering the menu bar with "New Case",
        # "Open Case", "Re-Ingest", "Export HTML Chats" duplicated
        # those entry points and (worse) suggested the tool itself
        # extracts data from a phone — which it doesn't.  Keep only
        # the universal Quit action here.
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("File")

        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # View menu -- timezone (IANA-based)
        view_menu = menu_bar.addMenu("View")
        tz_menu = view_menu.addMenu("Timezone")
        from app.config import (
            get_timezone_name, get_timezone_display, IANA_TIMEZONES,
        )
        current_iana = get_timezone_name()
        for iana_name, _abbr in IANA_TIMEZONES:
            display = get_timezone_display(iana_name)
            act = QAction(display, self)
            act.setCheckable(True)
            act.setData(iana_name)
            if iana_name == current_iana:
                act.setChecked(True)
            act.triggered.connect(
                lambda checked=False, name=iana_name: self._set_timezone_iana(name)
            )
            tz_menu.addAction(act)

        # Help menu
        help_menu = menu_bar.addMenu("Help")
        help_menu.addAction("About WAInsight", self._show_about)

    def _show_about(self):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QLabel, QScrollArea, QWidget,
        )
        from PySide6.QtGui import QPixmap, QDesktopServices
        from PySide6.QtCore import Qt, QUrl
        from app.config import APP_NAME, APP_SUBTITLE, APP_VERSION

        dlg = QDialog(self)
        dlg.setWindowTitle(f"About {APP_NAME}")
        dlg.setFixedSize(560, 640)
        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(body)

        # ---- Logo ----
        import os
        logo_path = os.path.join(
            os.path.dirname(__file__), "..", "resources", "logo.png")
        if os.path.isfile(logo_path):
            logo_lbl = QLabel()
            pxm = QPixmap(logo_path).scaled(
                200, 100, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            logo_lbl.setPixmap(pxm)
            logo_lbl.setAlignment(Qt.AlignCenter)
            layout.addWidget(logo_lbl)

        title = QLabel(f"<h1 style='color:#00897b;margin:0;'>{APP_NAME}</h1>")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            f"<p style='color:#607d8b;font-size:13px;margin:0;'>"
            f"{APP_SUBTITLE}</p>"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        version = QLabel(
            f"<p style='margin-top:6px;'>Version <b>{APP_VERSION}</b></p>"
        )
        version.setAlignment(Qt.AlignCenter)
        layout.addWidget(version)

        # ---- Honest description ----
        # The previous text claimed the tool "extracts" data from
        # phones, which is wrong — WAInsight reads ALREADY-EXTRACTED
        # WhatsApp databases.  Phone extraction itself is the job of
        # the analyst's chosen acquisition tool (Cellebrite, Magnet
        # AXIOM, Oxygen, manual ADB pull, etc.).
        desc = QLabel(
            "<p style='font-size:12px;color:#37474f;line-height:1.6;'>"
            "<b>WAInsight does not extract data from a phone.</b>  Point it "
            "at an already-acquired Android WhatsApp folder "
            "(<code>msgstore.db</code> / <code>wa.db</code> + the "
            "<code>Media/</code> + <code>Avatars/</code> directories) and "
            "it ingests them, in 29 sequential stages, into a normalised "
            "<code>analysis.db</code> with 47 indexed tables."
            "</p>"
            "<p style='font-size:12px;color:#37474f;line-height:1.6;'>"
            "From there it presents 30 pages of forensic analysis: full "
            "chat browsing with edits / revokes / receipts / replies, "
            "per-message platform attribution (Android / iPhone / Web "
            "Desktop / companion N) shown directly on every bubble, "
            "media recovery, perceptual-hash visual search, "
            "cross-contact analysis, group + contact PDF / HTML reports, "
            "and a folder-shaped offline Media Dashboard that scales to "
            "200k+ media rows."
            "</p>"
            "<p style='font-size:12px;color:#37474f;line-height:1.6;'>"
            "The source <code>msgstore.db</code> is opened with "
            "<code>?mode=ro&amp;immutable=1</code> — the tool never "
            "writes to evidence.  Every ingest is journaled to "
            "<code>chain_of_custody.jsonl</code> with SHA-256 hashes."
            "</p>"
        )
        desc.setTextFormat(Qt.RichText)
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # ---- Acknowledgements ----
        # The schema work + 29-stage ingester is the author's own
        # research and reverse-engineering of msgstore.db / wa.db,
        # cross-checked against the EUSAL book below where useful.
        # The companion SQLite GUI Analyzer was the development
        # accelerator (search-everywhere + side-by-side row panels +
        # copy-schema flow).
        ack = QLabel(
            "<hr style='border:none;border-top:1px solid #cfd8dc;margin:8px 0;'/>"
            "<p style='font-size:11px;color:#546e7a;margin:0;'>"
            "<b style='color:#00695c;'>Acknowledgements</b>"
            "</p>"
            "<p style='font-size:11px;color:#546e7a;line-height:1.55;margin-top:4px;'>"
            "The schema research, the 29-stage ingestion pipeline, and the "
            "30 analysis pages here are my own work — built up by "
            "reverse-engineering <code>msgstore.db</code> + "
            "<code>wa.db</code> over many months."
            "</p>"
            "<p style='font-size:11px;color:#546e7a;line-height:1.55;margin-top:6px;'>"
            "Part of that work was usefully <i>cross-checked</i> against the "
            "published research of <b>Francisco Arenaz Benito</b> in "
            "<i>Análisis forense de la aplicación WhatsApp en sistemas "
            "Android e iOS</i> "
            "<span style='color:#90a4ae;'>"
            "(Ediciones Universidad de Salamanca · Ágora Policial · "
            "ISBN 978-84-1091-202-1)</span>. "
            "His book confirmed several hypotheses I'd already formed "
            "and saved time on validation — credit and thanks where due.<br>"
            "<a href='https://eusal.es/producto/analisis-forense-de-la-aplicacion-whatsapp-en-sistemas-android-e-ios/'>"
            "eusal.es / 978-84-1091-202-1</a>"
            "</p>"
            "<p style='font-size:11px;color:#546e7a;line-height:1.55;margin-top:6px;'>"
            "Schema discovery during development was driven by my own "
            "<b>SQLite GUI Analyzer</b>: paste any value (poll id, JID, "
            "SHA-256) → it searches every column of every table; "
            "double-click each hit, line up several rows side-by-side, "
            "copy schemas, validate foreign-key relationships directly "
            "against the live evidence DB.  That click-search-validate "
            "loop is what surfaced things like the "
            "<code>message_quoted_text</code> ghost recovery, album "
            "parent/child linkage and HD/SD twin association.<br>"
            "<a href='https://github.com/akhil-dara/sqlite-gui-analyzer'>"
            "github.com/akhil-dara/sqlite-gui-analyzer</a>"
            "</p>"
            "<p style='font-size:11px;color:#546e7a;line-height:1.55;margin-top:6px;'>"
            "The per-message Android / iPhone / Web / companion-device "
            "platform tag is <i>separate</i> empirical research — sample "
            "collection from my own handset, friends on iPhone, and "
            "linked Web/Desktop sessions, until the <code>key_id</code> "
            "prefix-pattern classifier held up against new data.  "
            "That part wasn't tool-driven."
            "</p>"
        )
        ack.setTextFormat(Qt.RichText)
        ack.setWordWrap(True)
        ack.setOpenExternalLinks(True)
        layout.addWidget(ack)

        layout.addStretch(1)

        dlg.exec()

    def _set_timezone_iana(self, iana_name: str) -> None:
        from app.config import set_timezone, get_timezone_display
        set_timezone(iana_name)
        # Uncheck all other timezone actions, re-check the selected one
        for act in self.menuBar().findChildren(QAction):
            if act.isCheckable() and act.data():
                act.setChecked(act.data() == iana_name)
        display = get_timezone_display(iana_name)
        self.statusBar().showMessage(f"Timezone set to {display}.", 5000)

    # ---- Global download progress ----

    def show_download_progress(self, current: int, total: int, label: str = ""):
        """Update the global download progress bar in the status bar."""
        self._dl_label.setVisible(True)
        self._dl_progress_bar.setVisible(True)
        if total > 0:
            pct = int(current / total * 100)
            self._dl_progress_bar.setMaximum(total)
            self._dl_progress_bar.setValue(current)
            self._dl_progress_bar.setFormat(f"{current}/{total}")
        if label:
            self._dl_label.setText(f"\u2B07 {label}")
        else:
            self._dl_label.setText(f"\u2B07 Downloading: {current}/{total}")

    def hide_download_progress(self, summary: str = ""):
        """Hide the download progress and show a brief summary."""
        self._dl_progress_bar.setVisible(False)
        if summary:
            self._dl_label.setText(f"\u2705 {summary}")
            from PySide6.QtCore import QTimer
            QTimer.singleShot(8000, lambda: self._dl_label.setVisible(False))
        else:
            self._dl_label.setVisible(False)

    # ---- Global image index management ----

    def start_image_indexing(self):
        """Start image similarity indexing in background (callable from any page)."""
        from app.views.pages.image_similarity_page import IndexWorker
        if IndexWorker.is_running():
            return
        self._index_label.setText("Indexing images...")
        self._index_label.setVisible(True)
        self._index_progress_bar.setVisible(True)
        self._index_progress_bar.setValue(0)

        worker = IndexWorker.get_or_create(self)
        worker.progress.connect(self._on_global_index_progress)
        worker.finished.connect(self._on_global_index_finished)
        worker.start()

    def _on_global_index_progress(self, current: int, total: int):
        if total > 0:
            pct = int(current / total * 100)
            self._index_progress_bar.setMaximum(100)
            self._index_progress_bar.setValue(pct)
            self._index_progress_bar.setFormat(f"{pct}%")
            self._index_label.setText(f"Indexing: {current:,}/{total:,}")

    def _on_global_index_finished(self, count: int, error: str):
        self._index_progress_bar.setVisible(False)
        if error:
            self._index_label.setText(f"Index failed: {error[:40]}")
        else:
            self._index_label.setText(f"Indexed: {count:,} images")
        # Hide after 5 seconds
        from PySide6.QtCore import QTimer
        QTimer.singleShot(5000, lambda: self._index_label.setVisible(False))

    def _setup_status_bar(self) -> None:
        status = QStatusBar()
        self.setStatusBar(status)

        db = Database.get()
        db_path = str(db.path)
        if len(db_path) > 80:
            db_path = "..." + db_path[-77:]

        msgs = db.scalar("SELECT COUNT(*) FROM message") or 0
        convs = db.scalar("SELECT COUNT(*) FROM conversation") or 0

        tm = ThemeManager.get()
        is_light = tm.is_light

        info = QLabel(f"  {msgs:,} messages | {convs:,} conversations | {db.size_mb:.0f} MB")
        info.setStyleSheet(
            "color: #667781; font-size: 11px;" if is_light
            else "color: rgba(255,255,255,0.45); font-size: 11px;"
        )
        status.addWidget(info, 1)

        # Download progress (shown during media download/recovery)
        self._dl_progress_bar = QProgressBar()
        self._dl_progress_bar.setFixedWidth(180)
        self._dl_progress_bar.setFixedHeight(14)
        self._dl_progress_bar.setVisible(False)
        self._dl_progress_bar.setTextVisible(True)
        self._dl_progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #3b4a54; border-radius: 3px;"
            " background: #1a2026; text-align: center; color: #e9edef; font-size: 8px; }"
            "QProgressBar::chunk { background: #1565c0; border-radius: 2px; }"
        )
        self._dl_label = QLabel("")
        self._dl_label.setStyleSheet("color: #64b5f6; font-size: 9px;")
        self._dl_label.setVisible(False)
        status.addPermanentWidget(self._dl_label)
        status.addPermanentWidget(self._dl_progress_bar)

        # Indexing progress (shown during image similarity index build)
        self._index_progress_bar = QProgressBar()
        self._index_progress_bar.setFixedWidth(150)
        self._index_progress_bar.setFixedHeight(14)
        self._index_progress_bar.setVisible(False)
        self._index_progress_bar.setTextVisible(True)
        self._index_progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #3b4a54; border-radius: 3px;"
            " background: #1a2026; text-align: center; color: #e9edef; font-size: 8px; }"
            "QProgressBar::chunk { background: #00897b; border-radius: 2px; }"
        )
        self._index_label = QLabel("")
        self._index_label.setStyleSheet("color: #80cbc4; font-size: 9px;")
        self._index_label.setVisible(False)
        status.addPermanentWidget(self._index_label)
        status.addPermanentWidget(self._index_progress_bar)

        self._tz_label = QLabel(f"Timezone: {get_current_timezone_display()}  ")
        self._tz_label.setStyleSheet(
            "color: #00796b; font-size: 10px; font-weight: bold;" if is_light
            else "color: #80cbc4; font-size: 10px; font-weight: bold;"
        )
        status.addPermanentWidget(self._tz_label)

        path_label = QLabel(f"{db_path}  ")
        path_label.setStyleSheet(
            "color: #999; font-size: 10px;" if is_light
            else "color: rgba(255,255,255,0.3); font-size: 10px;"
        )
        status.addPermanentWidget(path_label)

    def _handle_timezone_change(self, _iana_name: str) -> None:
        self._tz_label.setText(f"Timezone: {get_current_timezone_display()}  ")
        for widget in self.findChildren(QWidget):
            refresh = getattr(widget, "refresh_for_timezone_change", None)
            if callable(refresh):
                refresh()
            else:
                widget.update()
        self.statusBar().showMessage(
            f"All timestamps updated to {get_current_timezone_display()}.", 5000
        )

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+F"), self, lambda: self._sidebar.select_page("search"))
        QShortcut(QKeySequence("Ctrl+D"), self, lambda: self._sidebar.select_page("dashboard"))
        QShortcut(QKeySequence("Ctrl+B"), self, lambda: self._sidebar.toggle_collapsed())
        QShortcut(QKeySequence("Escape"), self, lambda: self._sidebar.select_page("conversations"))
        QShortcut(QKeySequence("Alt+Left"), self, self.navigate_back)
