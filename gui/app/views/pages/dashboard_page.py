"""
Dashboard page -- overview statistics, message type breakdown, chat types, date range.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from app.config import CHART_COLORS, CHAT_TYPE_LABELS, MESSAGE_TYPE_LABELS
from app.services.database import Database
from app.services.theme_manager import ThemeManager
from app.views.widgets.stat_card import StatCard


class DashboardPage(QScrollArea):
    """Overview dashboard with stat cards and charts."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(24, 20, 24, 24)
        self._layout.setSpacing(20)
        self.setWidget(container)

        self._build_header()
        self._build_owner_info()
        self._build_date_range()
        self._build_stat_cards()
        self._build_message_type_section()
        self._build_chat_type_section()

        self._layout.addStretch()

        # Load data after widget is shown
        QTimer.singleShot(100, self._load_data)

    def _build_header(self) -> None:
        header = QHBoxLayout()
        title = QLabel("Dashboard")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)

        subtitle = QLabel("WhatsApp Forensic Analysis Overview")
        sub_font = QFont()
        sub_font.setPointSize(11)
        subtitle.setFont(sub_font)
        tm = ThemeManager.get()
        subtitle.setStyleSheet(
            "color: #667781;" if tm.is_light
            else "color: rgba(255,255,255,0.5);"
        )
        header.addWidget(subtitle)
        header.addStretch()
        self._layout.addLayout(header)

    def _build_owner_info(self) -> None:
        """Build device owner info section from case_metadata."""
        self._owner_frame = QFrame()
        tm = ThemeManager.get()
        if tm.is_light:
            self._owner_frame.setStyleSheet("""
                QFrame { background: rgba(0,137,123,0.08);
                         border-radius: 8px;
                         border: 1px solid rgba(0,137,123,0.2);
                         padding: 12px; }
            """)
        else:
            self._owner_frame.setStyleSheet("""
                QFrame { background: rgba(0,229,255,0.06);
                         border-radius: 8px;
                         border: 1px solid rgba(0,229,255,0.15);
                         padding: 12px; }
            """)
        fl = QHBoxLayout(self._owner_frame)
        fl.setContentsMargins(16, 10, 16, 10)
        self._owner_label = QLabel("Loading device info...")
        self._owner_label.setStyleSheet(
            "color: #1b1b1b; font-size: 12px; font-weight: bold;" if tm.is_light
            else "color: #e9edef; font-size: 12px; font-weight: bold;"
        )
        fl.addWidget(self._owner_label)
        fl.addStretch()
        self._layout.addWidget(self._owner_frame)

    def _build_date_range(self) -> None:
        self._date_range_frame = QFrame()
        tm = ThemeManager.get()
        if tm.is_light:
            self._date_range_frame.setStyleSheet("""
                QFrame { background: rgba(0,137,123,0.06);
                         border-radius: 8px;
                         border: 1px solid rgba(0,137,123,0.15);
                         padding: 12px; }
            """)
        else:
            self._date_range_frame.setStyleSheet("""
                QFrame { background: rgba(0,188,212,0.08);
                         border-radius: 8px;
                         border: 1px solid rgba(0,188,212,0.2);
                         padding: 12px; }
            """)
        fl = QHBoxLayout(self._date_range_frame)
        fl.setContentsMargins(16, 10, 16, 10)
        self._date_range_label = QLabel("\u23F3 Loading date range...")
        self._date_range_label.setStyleSheet(
            "color: #546e7a; font-size: 12px;" if tm.is_light
            else "color: rgba(255,255,255,0.7); font-size: 12px;"
        )
        fl.addWidget(self._date_range_label)
        fl.addStretch()
        self._layout.addWidget(self._date_range_frame)

    def _build_stat_cards(self) -> None:
        grid = QGridLayout()
        grid.setSpacing(12)

        colors = CHART_COLORS
        self._cards = {
            "messages": StatCard("Total Messages", "...", colors[0]),
            "conversations": StatCard("Conversations", "...", colors[1]),
            "contacts": StatCard("Contacts", "...", colors[2]),
            "media": StatCard("Media Files", "...", colors[3]),
            "calls": StatCard("Call Records", "...", colors[4]),
            "reactions": StatCard("Reactions", "...", colors[5]),
            "groups": StatCard("Group Chats", "...", colors[6]),
            "mentions": StatCard("@Mentions", "...", colors[7]),
            "edits": StatCard("Edited Messages", "...", colors[8]),
            "revoked": StatCard("Revoked Messages", "...", colors[9]),
            "locations": StatCard("Locations", "...", colors[10]),
            "polls": StatCard("Polls", "...", colors[11]),
        }

        for i, (key, card) in enumerate(self._cards.items()):
            row, col = divmod(i, 4)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            grid.addWidget(card, row, col)

        self._layout.addLayout(grid)

    def _build_message_type_section(self) -> None:
        section = QFrame()
        tm = ThemeManager.get()
        if tm.is_light:
            section.setStyleSheet("""
                QFrame { background: #ffffff;
                         border-radius: 8px; border: 1px solid #e8eaed; }
            """)
        else:
            section.setStyleSheet("""
                QFrame { background: rgba(255,255,255,0.02);
                         border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); }
            """)
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(8)

        label = QLabel("Message Types")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        self._msg_type_container = QVBoxLayout()
        self._msg_type_container.setSpacing(4)
        sl.addLayout(self._msg_type_container)
        self._layout.addWidget(section)

    def _build_chat_type_section(self) -> None:
        section = QFrame()
        tm = ThemeManager.get()
        if tm.is_light:
            section.setStyleSheet("""
                QFrame { background: #ffffff;
                         border-radius: 8px; border: 1px solid #e8eaed; }
            """)
        else:
            section.setStyleSheet("""
                QFrame { background: rgba(255,255,255,0.02);
                         border-radius: 8px; border: 1px solid rgba(255,255,255,0.06); }
            """)
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(8)

        label = QLabel("Chat Types")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        self._chat_type_container = QVBoxLayout()
        self._chat_type_container.setSpacing(4)
        sl.addLayout(self._chat_type_container)
        self._layout.addWidget(section)

    def _load_data(self) -> None:
        db = Database.get()

        # Device owner info from case_metadata
        owner_name = db.scalar(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_name'"
        )
        owner_phone = db.scalar(
            "SELECT value FROM case_metadata WHERE key = 'device_owner_phone'"
        )
        db_size = db.size_mb
        parts = []
        if owner_name:
            parts.append(owner_name)
        if owner_phone:
            formatted = f"+{owner_phone[:2]} {owner_phone[2:7]} {owner_phone[7:]}" if len(owner_phone) >= 10 else f"+{owner_phone}"
            parts.append(f"\u260E  {formatted}")
        parts.append(f"{db_size:.0f} MB")
        if parts:
            self._owner_label.setText("   |   ".join(parts))
        else:
            self._owner_frame.hide()

        # Stat card counts
        stats = {
            "messages": db.scalar("SELECT COUNT(*) FROM message WHERE message_type != 7"),
            "conversations": db.scalar(
                "SELECT COUNT(*) FROM conversation WHERE COALESCE(group_type, 0) != 3"
            ),
            "contacts": db.scalar("SELECT COUNT(*) FROM contact"),
            "media": db.scalar("SELECT COUNT(*) FROM media"),
            "calls": db.scalar("SELECT COUNT(*) FROM call_record"),
            "reactions": db.scalar("SELECT COUNT(*) FROM reaction"),
            "groups": db.scalar("SELECT COUNT(*) FROM conversation WHERE chat_type = 'group'"),
            "mentions": db.scalar("SELECT COUNT(*) FROM mention"),
            "edits": db.scalar("SELECT COUNT(*) FROM message WHERE is_edited = 1"),
            "revoked": db.scalar("SELECT COUNT(*) FROM message WHERE is_revoked = 1"),
            "locations": db.scalar("SELECT COUNT(*) FROM location"),
            "polls": db.scalar("SELECT COUNT(*) FROM poll"),
        }

        for key, val in stats.items():
            self._cards[key].set_value(f"{val:,}" if val else "0")

        # Message type breakdown (top 10)
        type_rows = db.fetchall(
            "SELECT message_type, COUNT(*) as cnt FROM message "
            "WHERE message_type != 7 "
            "GROUP BY message_type ORDER BY cnt DESC LIMIT 10"
        )
        for i, row in enumerate(type_rows):
            mt, cnt = row["message_type"], row["cnt"]
            label_text = MESSAGE_TYPE_LABELS.get(mt, f"Type {mt}")
            color = CHART_COLORS[i % len(CHART_COLORS)]
            bar = self._make_bar_row(label_text, cnt, stats["messages"] or 1, color)
            self._msg_type_container.addLayout(bar)

        # Chat type breakdown (exclude community meta groups to avoid double-counting)
        chat_rows = db.fetchall(
            "SELECT chat_type, COUNT(*) as cnt FROM conversation "
            "WHERE COALESCE(group_type, 0) != 3 "
            "GROUP BY chat_type ORDER BY cnt DESC"
        )
        total_chats = stats["conversations"] or 1
        for i, row in enumerate(chat_rows):
            ct, cnt = row["chat_type"], row["cnt"]
            label_text = CHAT_TYPE_LABELS.get(ct, ct or "Unknown")
            color = CHART_COLORS[(i + 3) % len(CHART_COLORS)]
            bar = self._make_bar_row(label_text, cnt, total_chats, color)
            self._chat_type_container.addLayout(bar)

        # Date range
        first_ts = db.scalar("SELECT MIN(timestamp) FROM message WHERE timestamp > 0")
        last_ts = db.scalar("SELECT MAX(timestamp) FROM message")
        if first_ts and last_ts:
            from app.config import format_timestamp, timestamp_to_local_datetime
            # Compute days between dates using tz-aware datetimes so
            # daylight-savings transitions don't off-by-one the count.
            first_dt = timestamp_to_local_datetime(first_ts)
            last_dt = timestamp_to_local_datetime(last_ts)
            days = (last_dt.date() - first_dt.date()).days
            self._date_range_label.setText(
                f"Data Range: {format_timestamp(first_ts, '%b %d, %Y')} \u2014 "
                f"{format_timestamp(last_ts, '%b %d, %Y')}  ({days:,} days  |  "
                f"{stats.get('messages', 0):,} messages)"
            )

    def _make_bar_row(self, label: str, count: int, total: int,
                      color: str = "#00bcd4") -> QHBoxLayout:
        """Create a horizontal bar row with label, bar, and count."""
        row = QHBoxLayout()
        row.setSpacing(12)

        tm = ThemeManager.get()
        lbl = QLabel(label)
        lbl.setFixedWidth(130)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl.setStyleSheet(
            "color: #546e7a; font-size: 11px;" if tm.is_light
            else "color: rgba(255,255,255,0.7); font-size: 11px;"
        )

        pct = (count / total * 100) if total else 0
        bar = QFrame()
        bar.setFixedHeight(18)
        bar.setMinimumWidth(8)
        bar.setMaximumWidth(max(8, int(pct * 4)))
        bar.setStyleSheet(
            f"background-color: {color}; border-radius: 3px;"
        )

        cnt_lbl = QLabel(f"{count:,} ({pct:.1f}%)")
        cnt_lbl.setStyleSheet(
            "color: #667781; font-size: 11px;" if tm.is_light
            else "color: rgba(255,255,255,0.5); font-size: 11px;"
        )

        row.addWidget(lbl)
        row.addWidget(bar)
        row.addWidget(cnt_lbl)
        row.addStretch()
        return row

    def refresh_for_timezone_change(self) -> None:
        """Reload after a global timezone change so cached
        formatted timestamps re-render in the new tz."""
        try:
            if hasattr(self, "_load_data") and callable(getattr(self, "_load_data")):
                self._load_data()
        except Exception:
            pass

