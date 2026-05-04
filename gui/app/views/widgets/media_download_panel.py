"""Media Recovery Panel — side panel showing media stats + batch download controls."""
from __future__ import annotations

import re
import time
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QScrollArea, QWidget,
)
from PySide6.QtCore import Qt, Signal


def _parse_url_expiry(url: str | None) -> float:
    """Extract oe= expiry timestamp (UTC) from WhatsApp CDN URL. Returns 0 if not found."""
    if not url:
        return 0
    m = re.search(r'oe=([0-9A-Fa-f]+)', url)
    return int(m.group(1), 16) if m else 0


def _is_light() -> bool:
    try:
        from app.services.theme_manager import ThemeManager
        return not ThemeManager.get().is_dark
    except Exception:
        return True


class MediaDownloadPanel(QFrame):
    """Right-side panel showing media recovery stats and batch download controls."""

    download_chat_requested = Signal(int)       # conv_id
    download_all_requested = Signal()
    navigate_to_chat = Signal(int)              # conv_id
    close_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(380)
        self._conv_id: int | None = None
        self._conv_name: str = ""
        self._setup_ui()

    def _setup_ui(self):
        lt = _is_light()
        bg = "#fafafa" if lt else "#111b21"
        text = "#111b21" if lt else "#e9edef"
        dim = "#667781" if lt else "#8696a0"
        accent = "#008069" if lt else "#00a884"
        border = "#e0e0e0" if lt else "#2a3942"
        card_bg = "#ffffff" if lt else "#1f2c34"

        self.setStyleSheet(f"""
            MediaDownloadPanel {{
                background: {bg};
                border-left: 1px solid {border};
            }}
            QLabel {{ color: {text}; }}
            .section-title {{
                font-size: 12px; font-weight: 700; color: {accent};
                padding: 8px 0 4px 0;
            }}
            .stat-row {{
                font-size: 12px; padding: 3px 0;
            }}
            .stat-value {{
                font-weight: 700; font-size: 13px;
            }}
            .chat-item {{
                background: {card_bg}; border: 1px solid {border};
                border-radius: 6px; padding: 6px 10px; margin: 2px 0;
            }}
            .chat-item:hover {{
                background: {"#f0f0f0" if lt else "#2a3942"};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("Media Recovery")
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {accent};")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet(f"""
            QPushButton {{ background: none; border: none; font-size: 16px; color: {dim}; }}
            QPushButton:hover {{ color: {text}; }}
        """)
        close_btn.clicked.connect(self.close_requested.emit)
        hdr.addWidget(close_btn)
        layout.addLayout(hdr)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {border}; max-height: 1px;")
        layout.addWidget(sep)

        # Scroll area for content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        self._content_layout = QVBoxLayout(content)
        self._content_layout.setContentsMargins(0, 4, 0, 4)
        self._content_layout.setSpacing(2)

        # ── Current Chat Section ──
        self._chat_title = QLabel("No chat selected")
        self._chat_title.setProperty("class", "section-title")
        self._content_layout.addWidget(self._chat_title)

        self._chat_stats = QLabel("")
        self._chat_stats.setWordWrap(True)
        self._content_layout.addWidget(self._chat_stats)

        self._dl_chat_btn = QPushButton("Download All (Current Chat)")
        self._dl_chat_btn.setStyleSheet(f"""
            QPushButton {{
                background: {accent}; color: white; border: none;
                padding: 8px 16px; border-radius: 6px; font-weight: 600; font-size: 12px;
            }}
            QPushButton:hover {{ background: {"#006b5a" if lt else "#00c49a"}; }}
            QPushButton:disabled {{ background: {dim}; }}
        """)
        self._dl_chat_btn.clicked.connect(self._on_download_chat)
        self._content_layout.addWidget(self._dl_chat_btn)

        # Progress bar (hidden)
        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedHeight(14)
        self._progress_bar.setVisible(False)
        self._progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {border}; border-radius: 4px;
                background: {card_bg}; text-align: center; font-size: 10px;
            }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 3px; }}
        """)
        self._content_layout.addWidget(self._progress_bar)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet(f"font-size: 10px; color: {dim};")
        self._progress_label.setVisible(False)
        self._content_layout.addWidget(self._progress_label)

        # ── Separator ──
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet(f"background: {border}; max-height: 1px; margin: 6px 0;")
        self._content_layout.addWidget(sep2)

        # ── Global Section ──
        global_title = QLabel("All Conversations")
        global_title.setProperty("class", "section-title")
        self._content_layout.addWidget(global_title)

        self._global_stats = QLabel("")
        self._global_stats.setWordWrap(True)
        self._content_layout.addWidget(self._global_stats)

        self._dl_all_btn = QPushButton("Download All Chats")
        self._dl_all_btn.setStyleSheet(self._dl_chat_btn.styleSheet())
        self._dl_all_btn.clicked.connect(self.download_all_requested.emit)
        self._content_layout.addWidget(self._dl_all_btn)

        # ── Top Chats ──
        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet(f"background: {border}; max-height: 1px; margin: 6px 0;")
        self._content_layout.addWidget(sep3)

        top_title = QLabel("Top Chats with Downloadable Media")
        top_title.setProperty("class", "section-title")
        self._content_layout.addWidget(top_title)

        self._top_chats_container = QVBoxLayout()
        self._top_chats_container.setSpacing(2)
        self._content_layout.addLayout(self._top_chats_container)

        self._content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def load_conversation_stats(self, conv_id: int | None, conv_name: str = ""):
        """Load and display stats for the current conversation."""
        self._conv_id = conv_id
        self._conv_name = conv_name
        if conv_id is None:
            self._chat_title.setText("No chat selected")
            self._chat_stats.setText("")
            self._dl_chat_btn.setEnabled(False)
            return

        self._chat_title.setText(f"Current: {conv_name or f'Chat #{conv_id}'}")

        try:
            from app.services.database import Database
            db = Database.get()
            stats = self._compute_stats(db, conv_id)
            self._render_chat_stats(stats)
        except Exception as e:
            self._chat_stats.setText(f"Error: {e}")

    def load_global_stats(self):
        """Load and display stats across ALL conversations."""
        try:
            from app.services.database import Database
            db = Database.get()
            stats = self._compute_stats(db, None)
            self._render_global_stats(stats)
            self._load_top_chats(db)
        except Exception as e:
            self._global_stats.setText(f"Error: {e}")

    def _compute_stats(self, db, conv_id: int | None) -> dict:
        """Compute media stats. conv_id=None for global."""
        where = "AND m.conversation_id = ?" if conv_id else ""
        params = (conv_id,) if conv_id else ()

        rows = db.fetchall(
            f"SELECT me.media_url, "
            f"  me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32 AS has_key, "
            f"  me.file_exists "
            f"FROM media me "
            f"JOIN message m ON m.id = me.message_id "
            f"WHERE me.media_url IS NOT NULL {where}",
            params,
        )

        now_ts = int(time.time())
        on_disk = 0
        downloadable = 0
        expired = 0
        no_key = 0
        total = len(rows)

        for url, has_key, file_exists in rows:
            if file_exists:
                on_disk += 1
            elif has_key:
                exp_ts = _parse_url_expiry(url)
                if exp_ts > now_ts or exp_ts == 0:
                    downloadable += 1
                else:
                    expired += 1
            else:
                no_key += 1

        # Also count media without URL at all
        no_url = db.scalar(
            f"SELECT COUNT(*) FROM media me "
            f"JOIN message m ON m.id = me.message_id "
            f"WHERE me.media_url IS NULL {where}",
            params,
        ) or 0

        return {
            "on_disk": on_disk,
            "downloadable": downloadable,
            "expired": expired,
            "no_key": no_key,
            "no_url": no_url,
            "total": total + no_url,
        }

    def _render_chat_stats(self, stats: dict):
        html = self._stats_html(stats)
        self._chat_stats.setText(html)
        self._chat_stats.setTextFormat(Qt.RichText)

        dl_count = stats["downloadable"]
        self._dl_chat_btn.setEnabled(dl_count > 0)
        self._dl_chat_btn.setText(
            f"Download {dl_count:,} Files (Current Chat)" if dl_count > 0
            else "No downloadable media"
        )

    def _render_global_stats(self, stats: dict):
        html = self._stats_html(stats)
        self._global_stats.setText(html)
        self._global_stats.setTextFormat(Qt.RichText)

        dl_count = stats["downloadable"]
        self._dl_all_btn.setEnabled(dl_count > 0)
        self._dl_all_btn.setText(
            f"Download {dl_count:,} Files (All Chats)" if dl_count > 0
            else "No downloadable media"
        )

    @staticmethod
    def _stats_html(stats: dict) -> str:
        total = stats["total"]
        if total == 0:
            return "<i style='color:#888'>No media in this conversation</i>"

        def pct(n):
            return f" ({n*100/total:.0f}%)" if total > 0 else ""

        rows = [
            ("#2e7d32", "On Disk", stats["on_disk"], pct(stats["on_disk"])),
            ("#1565c0", "Downloadable (URL valid)", stats["downloadable"], pct(stats["downloadable"])),
            ("#e65100", "URL Expired", stats["expired"], pct(stats["expired"])),
            ("#c62828", "Key Missing", stats["no_key"], pct(stats["no_key"])),
            ("#888888", "No URL", stats["no_url"], pct(stats["no_url"])),
        ]
        html = "<table cellspacing='4' style='font-size:12px'>"
        for color, label, count, p in rows:
            dot = f"<span style='color:{color};font-size:16px'>\u25CF</span>"
            html += (
                f"<tr><td>{dot}</td>"
                f"<td style='padding:0 8px'>{label}</td>"
                f"<td style='font-weight:700;text-align:right'>{count:,}</td>"
                f"<td style='color:#888'>{p}</td></tr>"
            )
        html += (
            f"<tr style='border-top:1px solid #ccc'><td></td>"
            f"<td style='padding:0 8px;font-weight:600'>Total</td>"
            f"<td style='font-weight:700;text-align:right'>{total:,}</td>"
            f"<td></td></tr>"
        )
        html += "</table>"
        return html

    def _load_top_chats(self, db):
        """Load top 10 chats with most downloadable media."""
        # Clear existing
        while self._top_chats_container.count():
            item = self._top_chats_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        now_ts = int(time.time())

        rows = db.fetchall(
            "SELECT m.conversation_id, c.display_name, "
            "  me.media_url, "
            "  me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32 AS has_key, "
            "  me.file_exists "
            "FROM media me "
            "JOIN message m ON m.id = me.message_id "
            "JOIN conversation c ON c.id = m.conversation_id "
            "WHERE me.media_url IS NOT NULL "
            "AND (me.file_exists = 0 OR me.file_exists IS NULL) "
            "AND me.media_key IS NOT NULL AND LENGTH(me.media_key) = 32",
        )

        # Count downloadable per chat
        chat_counts: dict[int, tuple[str, int]] = {}
        for conv_id, name, url, has_key, _ in rows:
            exp_ts = _parse_url_expiry(url)
            if exp_ts > now_ts or exp_ts == 0:
                if conv_id not in chat_counts:
                    chat_counts[conv_id] = (name or f"Chat #{conv_id}", 0)
                n, c = chat_counts[conv_id]
                chat_counts[conv_id] = (n, c + 1)

        # Sort by count descending, take top 10
        top = sorted(chat_counts.items(), key=lambda x: -x[1][1])[:10]

        lt = _is_light()
        for conv_id, (name, count) in top:
            btn = QPushButton(f"{name[:30]}  —  {count:,} files")
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {"#ffffff" if lt else "#1f2c34"};
                    border: 1px solid {"#e0e0e0" if lt else "#2a3942"};
                    border-radius: 6px; padding: 6px 10px; text-align: left;
                    font-size: 11px; color: {"#111b21" if lt else "#e9edef"};
                }}
                QPushButton:hover {{
                    background: {"#f0f0f0" if lt else "#2a3942"};
                }}
            """)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, cid=conv_id: self.navigate_to_chat.emit(cid))
            self._top_chats_container.addWidget(btn)

        if not top:
            lbl = QLabel("<i style='color:#888;font-size:11px'>No downloadable media found</i>")
            lbl.setTextFormat(Qt.RichText)
            self._top_chats_container.addWidget(lbl)

    def _on_download_chat(self):
        if self._conv_id:
            self.download_chat_requested.emit(self._conv_id)

    def set_progress(self, current: int, total: int, status: str):
        """Update progress bar during download."""
        self._progress_bar.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)
        self._progress_label.setText(f"[{current}/{total}] {status}")
        self._dl_chat_btn.setEnabled(False)

    def on_download_finished(self, downloaded: int, failed: int, skipped: int):
        """Handle download completion — refresh stats."""
        self._progress_bar.setVisible(False)
        self._progress_label.setVisible(False)
        self._progress_label.setText(
            f"Done: {downloaded} downloaded, {failed} failed, {skipped} skipped"
        )
        self._progress_label.setVisible(True)
        # Refresh stats
        if self._conv_id:
            self.load_conversation_stats(self._conv_id, self._conv_name)
        self.load_global_stats()
