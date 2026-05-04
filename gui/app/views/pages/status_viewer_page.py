"""
Status Viewer page -- browse WhatsApp status updates ("Stories") grouped
by contact, with thumbnail tiles, download badges, and stats.
"""

from __future__ import annotations

import os
from datetime import datetime

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFrame, QGridLayout, QLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app.config import format_timestamp
from app.services.database import Database
from app.services.theme_manager import ThemeManager


class WrapLayout(QLayout):
    """Flow layout for compact chips and tiles."""

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 10, v_spacing: int = 10):
        super().__init__(parent)
        self._items = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if line_height > 0 and next_x - self._h_spacing > effective.right() and x > effective.x():
                x = effective.x()
                y += line_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return max((y - rect.y()) + line_height + margins.bottom(), 0)


class StatusViewerPage(QWidget):
    """Dedicated page for browsing status updates grouped by contact."""

    conversation_selected = Signal(int, str)  # conv_id, display_name
    go_to_message = Signal(int, int)          # conv_id, msg_id (for jumping to a specific status)
    contact_requested = Signal(int)           # contact_id

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._loaded = False
        self._contact_filter = ""
        self._type_filter = "all"
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(10)

        hero = QFrame()
        hero.setStyleSheet(self._tm.stat_frame_style())
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 16, 18, 16)
        hero_layout.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Status Updates")
        f = QFont()
        f.setPointSize(18)
        f.setBold(True)
        title.setFont(f)
        title_col.addWidget(title)
        self._subtitle_label = QLabel("Track story activity, media availability, and latest status evidence.")
        self._subtitle_label.setStyleSheet(self._tm.hint_label_style())
        title_col.addWidget(self._subtitle_label)
        top_row.addLayout(title_col, 1)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search contact name, WA name, or phone...")
        self._search.setMinimumWidth(320)
        self._search.setFixedHeight(34)
        self._search.setStyleSheet(self._tm.search_box_style())
        self._search.textChanged.connect(self._on_search_changed)
        top_row.addWidget(self._search)
        hero_layout.addLayout(top_row)

        self._stat_labels: dict[str, QLabel] = {}
        stats_host = QWidget()
        self._stats_wrap = WrapLayout(stats_host, h_spacing=10, v_spacing=10)
        stats_host.setLayout(self._stats_wrap)
        for key, label_text in [
            ("total", "Total Updates"),
            ("contacts", "Active Contacts"),
            ("images", "Images"),
            ("videos", "Videos"),
            ("text", "Text"),
            ("on_disk", "Recovered"),
        ]:
            frame = QFrame()
            frame.setStyleSheet(self._tm.stat_frame_style())
            frame.setMinimumSize(132, 72)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(14, 10, 14, 10)
            fl.setSpacing(2)
            lbl = QLabel(label_text)
            lbl.setStyleSheet(self._tm.stat_label_style())
            lf = QFont()
            lf.setPointSize(9)
            lbl.setFont(lf)
            fl.addWidget(lbl)
            val = QLabel("0")
            val.setStyleSheet(self._tm.stat_value_style())
            vf = QFont()
            vf.setPointSize(15)
            vf.setBold(True)
            val.setFont(vf)
            fl.addWidget(val)
            self._stats_wrap.addWidget(frame)
            self._stat_labels[key] = val
        hero_layout.addWidget(stats_host)

        filter_label = QLabel("Filter By Type")
        filter_label.setStyleSheet(self._tm.header_label_style() + " font-weight: bold;")
        hero_layout.addWidget(filter_label)

        filter_host = QWidget()
        self._filter_wrap = WrapLayout(filter_host, h_spacing=8, v_spacing=8)
        filter_host.setLayout(self._filter_wrap)

        self._type_btns: dict[str, QPushButton] = {}
        for fid, label in [
            ("all", "All"),
            ("image", "Images"),
            ("video", "Videos"),
            ("text", "Text"),
            ("voice", "Voice"),
            ("gif", "GIFs"),
        ]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(32)
            btn.setMinimumWidth(max(96, btn.sizeHint().width() + 10))
            btn.setProperty("filter_id", fid)
            btn.setStyleSheet(self._tm.filter_btn_style())
            btn.clicked.connect(self._on_type_filter)
            if fid == "all":
                btn.setChecked(True)
            self._filter_wrap.addWidget(btn)
            self._type_btns[fid] = btn
        hero_layout.addWidget(filter_host)
        layout.addWidget(hero)

        self._results_label = QLabel("")
        self._results_label.setStyleSheet(self._tm.hint_label_style())
        layout.addWidget(self._results_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_content = QWidget()
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 4, 0, 0)
        self._scroll_layout.setSpacing(14)
        self._scroll_layout.addStretch()
        scroll.setWidget(self._scroll_content)
        layout.addWidget(scroll, 1)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._loaded:
            QTimer.singleShot(50, self._load_data)

    def refresh(self) -> None:
        """Force reload data."""
        self._loaded = False
        self._load_data()

    def refresh_for_timezone_change(self) -> None:
        self.refresh()

    def _load_data(self) -> None:
        """Load status posts from analysis.db grouped by contact."""
        self._loaded = True
        db = Database.get()

        # Check if status_post table exists
        try:
            db.scalar("SELECT 1 FROM status_post LIMIT 1")
        except Exception:
            self._results_label.setText("No status data available. Re-ingest to populate.")
            return

        # Build WHERE clause
        where_parts = []
        params = []

        if self._type_filter != "all":
            where_parts.append("sp.type_label = ?")
            params.append(self._type_filter)

        if self._contact_filter:
            where_parts.append(
                "(c.resolved_name LIKE ? OR c.phone_number LIKE ? OR c.wa_name LIKE ?)"
            )
            pat = f"%{self._contact_filter}%"
            params.extend([pat, pat, pat])

        where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        # Stats
        total = db.scalar(f"SELECT COUNT(*) FROM status_post sp LEFT JOIN contact c ON c.id = sp.contact_id{where_clause}", tuple(params)) or 0
        contacts_count = db.scalar(f"SELECT COUNT(DISTINCT sp.contact_id) FROM status_post sp LEFT JOIN contact c ON c.id = sp.contact_id{where_clause}", tuple(params)) or 0

        # Type counts (unfiltered by type, but filtered by contact search)
        contact_where_parts = []
        contact_params = []
        if self._contact_filter:
            contact_where_parts.append(
                "(c.resolved_name LIKE ? OR c.phone_number LIKE ? OR c.wa_name LIKE ?)"
            )
            pat = f"%{self._contact_filter}%"
            contact_params = [pat, pat, pat]

        def _count_where(extra_cond: str) -> int:
            parts = list(contact_where_parts) + [extra_cond]
            sql = f"SELECT COUNT(*) FROM status_post sp LEFT JOIN contact c ON c.id = sp.contact_id WHERE {' AND '.join(parts)}"
            return db.scalar(sql, tuple(contact_params)) or 0

        images = _count_where("sp.type_label = 'image'")
        videos = _count_where("sp.type_label = 'video'")
        texts = _count_where("sp.type_label = 'text'")
        on_disk = _count_where("sp.media_file_path IS NOT NULL AND sp.media_file_path != ''")

        self._stat_labels["total"].setText(f"{total:,}")
        self._stat_labels["contacts"].setText(f"{contacts_count:,}")
        self._stat_labels["images"].setText(f"{images:,}")
        self._stat_labels["videos"].setText(f"{videos:,}")
        self._stat_labels["text"].setText(f"{texts:,}")
        self._stat_labels["on_disk"].setText(f"{on_disk:,}")

        # Get contacts with status posts, ordered by most recent
        contact_rows = db.fetchall(f"""
            SELECT
                sp.contact_id,
                COALESCE(c.resolved_name, c.phone_number, 'Unknown') AS name,
                c.phone_number,
                COUNT(*) AS post_count,
                MAX(sp.timestamp) AS latest_ts,
                c.avatar_blob
            FROM status_post sp
            LEFT JOIN contact c ON c.id = sp.contact_id
            {where_clause}
            GROUP BY sp.contact_id
            ORDER BY latest_ts DESC
        """, tuple(params))

        self._results_label.setText(
            f"{total:,} updates across {contacts_count:,} contacts"
        )

        # Clear existing layout
        self._clear_scroll_layout()

        if not contact_rows:
            empty_label = QLabel("No status updates found.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(
                f"color: {'#667781' if self._tm.is_light else '#94a3b8'}; "
                "font-size: 14px; padding: 40px;"
            )
            self._scroll_layout.insertWidget(0, empty_label)
            return

        # Build per-contact sections
        for row in contact_rows:
            contact_id = row[0]
            contact_name = row[1] or "Unknown"
            phone = row[2] or ""
            post_count = row[3]
            latest_ts = row[4]
            avatar_blob = row[5]

            section = self._build_contact_section(
                contact_id, contact_name, phone, post_count,
                latest_ts, avatar_blob, where_parts, params,
            )
            # Insert before the stretch
            self._scroll_layout.insertWidget(
                self._scroll_layout.count() - 1, section
            )

    def _build_contact_section(
        self, contact_id, name, phone, post_count,
        latest_ts, avatar_blob, where_parts, params,
    ) -> QWidget:
        """Build a contact section with header + tile grid."""
        db = Database.get()

        section = QFrame()
        section.setStyleSheet(
            f"QFrame {{ background: {'#ffffff' if self._tm.is_light else '#1b2430'}; "
            f"border-radius: 16px; border: 1px solid {'#e5e7eb' if self._tm.is_light else 'rgba(255,255,255,0.08)'}; }}"
        )
        sl = QVBoxLayout(section)
        sl.setContentsMargins(18, 16, 18, 16)
        sl.setSpacing(14)

        # Contact header row
        header = QHBoxLayout()
        header.setSpacing(14)

        # Avatar
        avatar_label = QLabel()
        avatar_label.setFixedSize(54, 54)
        if avatar_blob:
            pix = QPixmap()
            pix.loadFromData(bytes(avatar_blob))
            pix = pix.scaled(54, 54, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            # Circular clip
            rounded = QPixmap(54, 54)
            rounded.fill(Qt.transparent)
            painter = QPainter(rounded)
            painter.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath()
            path.addEllipse(0, 0, 54, 54)
            painter.setClipPath(path)
            painter.drawPixmap(0, 0, pix)
            painter.end()
            avatar_label.setPixmap(rounded)
        else:
            # Initials avatar
            initials = "".join(w[0].upper() for w in (name or "?").split()[:2])
            colors = ["#00897b", "#1e88e5", "#43a047", "#fb8c00", "#e53935", "#8e24aa"]
            bg = colors[(contact_id or 0) % len(colors)]
            avatar_label.setStyleSheet(
                f"background: {bg}; color: white; border-radius: 27px; "
                f"font-size: 17px; font-weight: bold;"
            )
            avatar_label.setAlignment(Qt.AlignCenter)
            avatar_label.setText(initials)
        avatar_label.setCursor(Qt.PointingHandCursor)
        if contact_id:
            avatar_label.mousePressEvent = lambda e, cid=contact_id: self.contact_requested.emit(cid)
        header.addWidget(avatar_label)

        # Name + metadata
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        name_label = QLabel(name)
        name_font = QFont()
        name_font.setPointSize(14)
        name_font.setBold(True)
        name_label.setFont(name_font)
        name_label.setCursor(Qt.PointingHandCursor)
        if contact_id:
            name_label.mousePressEvent = lambda e, cid=contact_id: self.contact_requested.emit(cid)
        info_layout.addWidget(name_label)

        meta_row = QHBoxLayout()
        meta_row.setSpacing(8)
        phone_badge = QLabel(phone or "Unknown number")
        phone_badge.setStyleSheet(
            f"padding: 4px 10px; border-radius: 11px; "
            f"background: {'#f3f4f6' if self._tm.is_light else 'rgba(255,255,255,0.08)'}; "
            f"color: {'#475467' if self._tm.is_light else '#cbd5e1'}; font-size: 11px;"
        )
        meta_row.addWidget(phone_badge)

        if latest_ts:
            latest_badge = QLabel(f"Latest {format_timestamp(latest_ts, 'bubble')}")
            latest_badge.setStyleSheet(
                f"padding: 4px 10px; border-radius: 11px; "
                f"background: {'rgba(0,137,123,0.10)' if self._tm.is_light else 'rgba(45,212,191,0.12)'}; "
                f"color: {'#00695c' if self._tm.is_light else '#7dd3fc'}; font-size: 11px; font-weight: bold;"
            )
            meta_row.addWidget(latest_badge)
        meta_row.addStretch()
        info_layout.addLayout(meta_row)
        header.addLayout(info_layout, 1)

        count_badge = QLabel(f"{post_count} update{'s' if post_count != 1 else ''}")
        count_badge.setStyleSheet(
            f"padding: 6px 12px; border-radius: 14px; "
            f"background: {'rgba(0,137,123,0.12)' if self._tm.is_light else 'rgba(45,212,191,0.12)'}; "
            f"color: {'#00695c' if self._tm.is_light else '#99f6e4'}; "
            "font-size: 11px; font-weight: bold;"
        )
        header.addWidget(count_badge)

        sl.addLayout(header)

        tile_host = QWidget()
        tile_layout = WrapLayout(tile_host, h_spacing=12, v_spacing=12)
        tile_host.setLayout(tile_layout)

        # Fetch posts for this contact
        extra_where = " AND ".join(where_parts) if where_parts else "1=1"
        posts = db.fetchall(f"""
            SELECT
                sp.id, sp.timestamp, sp.type_label, sp.text_content,
                sp.has_media, sp.media_file_path, sp.media_downloadable,
                sp.view_count, sp.reaction_count, sp.media_mime_type,
                sp.thumbnail_available, sp.message_id,
                med.thumbnail_blob, med.file_exists
            FROM status_post sp
            LEFT JOIN media med ON med.message_id = sp.message_id
            LEFT JOIN contact c ON c.id = sp.contact_id
            WHERE sp.contact_id {'= ?' if contact_id else 'IS NULL'}
              AND {extra_where}
            ORDER BY sp.timestamp DESC
            LIMIT 50
        """, tuple([contact_id] + list(params) if contact_id else params))

        for post in posts:
            tile_layout.addWidget(self._build_tile(post))

        sl.addWidget(tile_host)
        return section

    def _build_tile(self, post) -> QWidget:
        """Build a single status tile widget."""
        (sp_id, timestamp, type_label, text_content, has_media,
         file_path, downloadable, view_count, reaction_count,
         mime_type, thumb_available, message_id,
         thumbnail_blob, file_exists) = post

        tile = QFrame()
        tile.setFixedSize(168, 212)
        tile.setCursor(Qt.PointingHandCursor)

        # Determine file status for badge color
        if file_exists:
            border_color = "#2e7d32"
            status_label = "Recovered"
        elif downloadable:
            border_color = "#1e88e5"
            status_label = "Remote"
        else:
            border_color = "#fb8c00"
            status_label = "Unavailable"

        tile.setStyleSheet(
            f"QFrame {{ background: {'#ffffff' if self._tm.is_light else '#202938'}; "
            f"border-radius: 14px; border: 1px solid {border_color}; }}"
        )

        tl = QVBoxLayout(tile)
        tl.setContentsMargins(10, 10, 10, 10)
        tl.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)
        type_badge = QLabel((type_label or "unknown").upper())
        type_badge.setStyleSheet(
            f"padding: 3px 8px; border-radius: 10px; "
            f"background: {'#eef2ff' if self._tm.is_light else 'rgba(99,102,241,0.18)'}; "
            f"color: {'#4f46e5' if self._tm.is_light else '#c7d2fe'}; font-size: 9px; font-weight: bold;"
        )
        top_row.addWidget(type_badge)
        top_row.addStretch()
        status_badge = QLabel(status_label)
        status_badge.setStyleSheet(
            f"padding: 3px 8px; border-radius: 10px; background: {border_color}; "
            "color: white; font-size: 9px; font-weight: bold;"
        )
        top_row.addWidget(status_badge)
        tl.addLayout(top_row)

        # Thumbnail / preview area
        preview = QLabel()
        preview.setFixedHeight(118)
        preview.setAlignment(Qt.AlignCenter)
        preview.setStyleSheet(
            f"border: none; border-radius: 10px; "
            f"background: {'#f4f6f8' if self._tm.is_light else '#111827'};"
        )

        if thumbnail_blob:
            pix = QPixmap()
            pix.loadFromData(bytes(thumbnail_blob))
            pix = pix.scaled(148, 112, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            preview.setPixmap(pix)
        elif text_content and type_label == "text":
            preview.setText((text_content or "")[:90])
            preview.setWordWrap(True)
            preview.setStyleSheet(
                "border: none; border-radius: 10px; padding: 10px; "
                f"background: {'#f8fafc' if self._tm.is_light else '#0f172a'}; "
                f"color: {'#334155' if self._tm.is_light else '#e2e8f0'}; "
                "font-size: 11px; line-height: 1.3;"
            )
        else:
            label_map = {
                "image": "IMAGE",
                "video": "VIDEO",
                "voice": "VOICE",
                "audio": "AUDIO",
                "gif": "GIF",
                "sticker": "STICKER",
                "document": "DOC",
            }
            preview.setText(label_map.get(type_label, "STATUS"))
            preview.setStyleSheet(
                f"border: none; font-size: 18px; font-weight: bold; letter-spacing: 1px; "
                f"color: {'#64748b' if self._tm.is_light else '#94a3b8'}; "
                f"background: {'#f8fafc' if self._tm.is_light else '#0f172a'}; "
                f"border-radius: 10px;"
            )
        tl.addWidget(preview)

        meta = QLabel(format_timestamp(timestamp, "system") if timestamp else "")
        meta.setWordWrap(True)
        meta.setStyleSheet(
            f"font-size: 10px; color: {'#475467' if self._tm.is_light else '#cbd5e1'}; border: none;"
        )
        tl.addWidget(meta)

        # View/reaction row
        if view_count or reaction_count:
            vr_row = QHBoxLayout()
            vr_row.setContentsMargins(0, 0, 0, 0)
            vr_row.setSpacing(8)
            if view_count:
                eye = QLabel(f"Views {view_count}")
                eye.setStyleSheet("font-size: 9px; color: gray; border: none;")
                vr_row.addWidget(eye)
            if reaction_count:
                heart = QLabel(f"Reactions {reaction_count}")
                heart.setStyleSheet("font-size: 9px; color: gray; border: none;")
                vr_row.addWidget(heart)
            vr_row.addStretch()
            tl.addLayout(vr_row)
        else:
            tl.addSpacing(4)

        # Click to open chat viewer
        if message_id:
            tile.mousePressEvent = lambda e, mid=message_id: self._on_tile_clicked(mid)

        return tile

    def _on_tile_clicked(self, message_id: int) -> None:
        """Handle click on a status tile -- navigate to the specific status message.

        Uses go_to_message (carries msg_id) so the chat viewer jumps to and
        highlights the exact status post, instead of just opening the chat
        at its default position.
        """
        db = Database.get()
        row = db.fetchone(
            "SELECT conversation_id FROM message WHERE id = ?", (message_id,)
        )
        if row:
            conv_id = row[0]
            self.go_to_message.emit(conv_id, message_id)

    def _on_type_filter(self) -> None:
        fid = self.sender().property("filter_id")
        for k, b in self._type_btns.items():
            b.setChecked(k == fid)
        self._type_filter = fid
        self._load_data()

    def _on_search_changed(self, text: str) -> None:
        self._contact_filter = text.strip()
        # Debounce
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer()
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._load_data)
        self._search_timer.start(300)

    def _clear_scroll_layout(self) -> None:
        """Remove all contact sections from the scroll layout."""
        while self._scroll_layout.count() > 1:
            item = self._scroll_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def load_contact_status(self, contact_id: int) -> None:
        """Filter to show only statuses from a specific contact."""
        db = Database.get()
        name = db.scalar(
            "SELECT resolved_name FROM contact WHERE id = ?", (contact_id,)
        ) or ""
        self._search.setText(name)
        self._contact_filter = name
        self._load_data()
