"""
Analytics page -- daily activity chart, hourly heatmap, top contacts.
Uses precomputed stats tables from the ingestion pipeline.
Theme-aware: works in both light and dark modes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from app.config import CHART_COLORS
from app.services.database import Database


def _is_light_theme() -> bool:
    try:
        from app.services.theme_manager import ThemeManager
        return ThemeManager.get().is_light
    except Exception:
        return False


class AnalyticsPage(QScrollArea):
    """Analytics dashboard with activity charts and stats."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self._light = _is_light_theme()

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(24, 20, 24, 24)
        self._layout.setSpacing(20)
        self.setWidget(container)

        self._build_header()
        self._build_activity_summary()
        self._build_top_contacts()
        self._build_hourly_heatmap()
        self._build_daily_breakdown()
        self._layout.addStretch()

        QTimer.singleShot(100, self._load_data)

    # ---- theme-aware color helpers ----

    def _section_style(self) -> str:
        if self._light:
            return """
                QFrame { background: #ffffff;
                         border-radius: 8px; border: 1px solid #e0e0e0;
                         }
            """
        return """
            QFrame { background: rgba(255,255,255,0.03);
                     border-radius: 8px; border: 1px solid rgba(255,255,255,0.08); }
        """

    def _muted_text(self) -> str:
        return "color: #78909c;" if self._light else "color: rgba(255,255,255,0.5);"

    def _body_text(self) -> str:
        return "color: #37474f;" if self._light else "color: rgba(255,255,255,0.7);"

    def _accent_text(self) -> str:
        return "color: #00897b;" if self._light else "color: #00bcd4;"

    def _heatmap_empty(self) -> str:
        return "rgba(0,0,0,0.04)" if self._light else "rgba(255,255,255,0.02)"

    # ---- build UI ----

    def _build_header(self) -> None:
        header = QHBoxLayout()
        title = QLabel("Analytics")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title)

        subtitle = QLabel("Activity Patterns & Top Contacts")
        subtitle.setStyleSheet(f"{self._muted_text()} font-size: 11px;")
        header.addWidget(subtitle)
        header.addStretch()
        self._layout.addLayout(header)

    def _build_activity_summary(self) -> None:
        """Summary cards row."""
        self._summary_frame = QFrame()
        self._summary_frame.setStyleSheet(self._section_style())
        sl = QHBoxLayout(self._summary_frame)
        sl.setContentsMargins(16, 12, 16, 12)
        sl.setSpacing(24)

        self._summary_labels: dict[str, QLabel] = {}
        for key, label in [("avg_daily", "Avg/Day"), ("peak_day", "Peak Day"),
                           ("total_sent", "You Sent"), ("total_received", "Received"),
                           ("busiest_hour", "Busiest Hour")]:
            frame = QVBoxLayout()
            frame.setSpacing(2)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"{self._muted_text()} font-size: 10px;")
            val = QLabel("...")
            val.setStyleSheet(f"{self._accent_text()} font-size: 14px; font-weight: bold;")
            frame.addWidget(lbl)
            frame.addWidget(val)
            sl.addLayout(frame)
            self._summary_labels[key] = val
        sl.addStretch()
        self._layout.addWidget(self._summary_frame)

    def _build_top_contacts(self) -> None:
        section = QFrame()
        section.setStyleSheet(self._section_style())
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(6)

        label = QLabel("Top 15 Contacts by Messages")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        self._top_contacts_container = QVBoxLayout()
        self._top_contacts_container.setSpacing(3)
        sl.addLayout(self._top_contacts_container)
        self._layout.addWidget(section)

    def _build_hourly_heatmap(self) -> None:
        section = QFrame()
        section.setStyleSheet(self._section_style())
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(8)

        label = QLabel("Hourly Activity Heatmap")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        self._heatmap_grid = QGridLayout()
        self._heatmap_grid.setSpacing(2)
        sl.addLayout(self._heatmap_grid)
        self._layout.addWidget(section)

    def _build_daily_breakdown(self) -> None:
        section = QFrame()
        section.setStyleSheet(self._section_style())
        sl = QVBoxLayout(section)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.setSpacing(6)

        label = QLabel("Day-of-Week Breakdown")
        font = QFont()
        font.setPointSize(13)
        font.setBold(True)
        label.setFont(font)
        sl.addWidget(label)

        self._daily_container = QVBoxLayout()
        self._daily_container.setSpacing(3)
        sl.addLayout(self._daily_container)
        self._layout.addWidget(section)

    def _load_data(self) -> None:
        db = Database.get()

        # Activity summary — aggregate across all conversations at query
        # time.  ``stats_daily_activity`` is per-conversation (each row =
        # one conv on one day); there is no pre-rolled global row, so an
        # earlier ``WHERE conversation_id IS NULL`` always returned NULL
        # and the cards stayed blank.
        avg = db.scalar(
            "SELECT AVG(daily_total) FROM ("
            "  SELECT date_str, SUM(total_messages) AS daily_total "
            "  FROM stats_daily_activity "
            "  GROUP BY date_str"
            ")"
        )
        self._summary_labels["avg_daily"].setText(
            f"{int(avg):,}" if avg else "0"
        )

        peak = db.fetchone(
            "SELECT date_str, SUM(total_messages) AS total "
            "FROM stats_daily_activity "
            "GROUP BY date_str "
            "ORDER BY total DESC "
            "LIMIT 1"
        )
        if peak and peak["total"]:
            self._summary_labels["peak_day"].setText(
                f"{peak['date_str']} ({peak['total']:,})"
            )

        sent = db.scalar("SELECT COUNT(*) FROM message WHERE from_me = 1 AND message_type != 7")
        received = db.scalar("SELECT COUNT(*) FROM message WHERE from_me = 0 AND message_type != 7")
        self._summary_labels["total_sent"].setText(f"{sent:,}" if sent else "0")
        self._summary_labels["total_received"].setText(f"{received:,}" if received else "0")

        busiest_hour = db.fetchone(
            "SELECT hour_of_day, SUM(message_count) as total "
            "FROM stats_hourly_heatmap GROUP BY hour_of_day "
            "ORDER BY total DESC LIMIT 1"
        )
        if busiest_hour:
            h = busiest_hour["hour_of_day"]
            self._summary_labels["busiest_hour"].setText(
                f"{h:02d}:00 ({busiest_hour['total']:,} msgs)"
            )

        # Top contacts — show personal + group breakdown
        # We pull more rows than we'll display so that, after we splice
        # in the device-owner row, the ranking is still correct.
        top_contacts = [dict(r) for r in db.fetchall(
            "SELECT CASE WHEN c.is_saved = 1 THEN "
            "  COALESCE(NULLIF(c.display_name,''), NULLIF(c.resolved_name,''), 'Unknown') "
            "  ELSE CASE WHEN c.wa_name IS NOT NULL AND c.wa_name != '' THEN '~' || c.wa_name "
            "    WHEN c.phone_number IS NOT NULL AND c.phone_number != '' THEN c.phone_number "
            "    ELSE COALESCE(NULLIF(c.phone_jid,''), 'Unknown') END "
            "  END AS name, "
            "  c.message_count, c.personal_msg_count, c.group_msg_count, "
            "  0 AS is_owner "
            "FROM contact c "
            "WHERE c.message_count > 0 "
            "ORDER BY c.message_count DESC LIMIT 30"
        )]

        # Device-owner row injection — owner messages (m.from_me = 1)
        # have sender_id = NULL, so the owner never shows up in the
        # ``contact`` aggregate.  Compute the owner's totals directly
        # from ``message`` and splice the synthetic row into the
        # ranking with the case_metadata identity.
        try:
            owner_meta = {
                r["key"]: r["value"]
                for r in db.fetchall(
                    "SELECT key, value FROM case_metadata WHERE key IN ("
                    "'device_owner_name','device_owner_phone',"
                    "'device_owner_jid','device_owner_lid_jid')"
                )
            }
        except Exception:
            owner_meta = {}
        owner_name = (owner_meta.get("device_owner_name") or "").strip()
        owner_jid = owner_meta.get("device_owner_jid") or ""
        owner_label = (
            f"{owner_name} (you)" if owner_name else "You (Device Owner)"
        )

        owner_split = db.fetchone(
            "SELECT "
            "  SUM(CASE WHEN cv.chat_type = 'personal' THEN 1 ELSE 0 END) AS p, "
            "  SUM(CASE WHEN cv.chat_type IN ('group','community','broadcast') "
            "           THEN 1 ELSE 0 END) AS g, "
            "  COUNT(*) AS total "
            "FROM message m "
            "LEFT JOIN conversation cv ON cv.id = m.conversation_id "
            "WHERE m.from_me = 1 AND m.message_type != 7"
        )
        if owner_split and owner_split["total"]:
            top_contacts.append({
                "name": owner_label,
                "message_count": owner_split["total"] or 0,
                "personal_msg_count": owner_split["p"] or 0,
                "group_msg_count": owner_split["g"] or 0,
                "is_owner": 1,
            })
            # Re-sort and cap to 15 — owner now ranks naturally by send
            # count, with deterministic tie-breaking on name.
            top_contacts.sort(
                key=lambda r: (-(r["message_count"] or 0), (r["name"] or "").lower())
            )

        top_contacts = top_contacts[:15]
        if top_contacts:
            max_msgs = top_contacts[0]["message_count"] or 1
            for i, row in enumerate(top_contacts):
                name = row["name"] or "Unknown"
                total = row["message_count"] or 0
                personal = row["personal_msg_count"] or 0
                group = row["group_msg_count"] or 0
                # Owner gets a fixed accent colour so the analyst can
                # spot themselves in the ranking at a glance — every
                # other slot cycles through CHART_COLORS as before.
                if row.get("is_owner"):
                    color = "#00897b"
                    label_text = (
                        f"{i+1}. {name}  ★"   # ★ marker
                    )
                else:
                    color = CHART_COLORS[i % len(CHART_COLORS)]
                    label_text = f"{i+1}. {name}"
                detail = f"{total:,}  (P: {personal:,} | G: {group:,})"
                bar = self._make_bar_row(
                    label_text, total, max_msgs, color,
                    count_text=detail,
                )
                self._top_contacts_container.addLayout(bar)

        # Hourly heatmap (7 days x 24 hours)
        heatmap_data = db.fetchall(
            "SELECT day_of_week, hour_of_day, SUM(message_count) as total "
            "FROM stats_hourly_heatmap "
            "GROUP BY day_of_week, hour_of_day"
        )
        grid: dict[tuple[int, int], int] = {}
        max_val = 1
        for row in heatmap_data:
            key = (row["day_of_week"], row["hour_of_day"])
            grid[key] = row["total"]
            if row["total"] > max_val:
                max_val = row["total"]

        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        hm_muted = self._muted_text()

        # Header row (hours)
        blank = QLabel("")
        blank.setFixedSize(40, 18)
        self._heatmap_grid.addWidget(blank, 0, 0)
        for h in range(24):
            hl = QLabel(f"{h:02d}")
            hl.setFixedSize(22, 18)
            hl.setAlignment(Qt.AlignCenter)
            hl.setStyleSheet(f"{hm_muted} font-size: 8px;")
            self._heatmap_grid.addWidget(hl, 0, h + 1)

        # Data rows
        for d_idx, day_name in enumerate(days):
            dl = QLabel(day_name)
            dl.setFixedSize(40, 20)
            dl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            dl.setStyleSheet(f"{hm_muted} font-size: 10px;")
            self._heatmap_grid.addWidget(dl, d_idx + 1, 0)

            for h in range(24):
                val = grid.get((d_idx, h), 0)
                intensity = val / max_val if max_val > 0 else 0
                cell = QFrame()
                cell.setFixedSize(22, 20)

                if self._light:
                    # Light mode: teal on white
                    if intensity > 0.8:
                        bg = "rgba(0,137,123,0.85)"
                    elif intensity > 0.6:
                        bg = "rgba(0,137,123,0.6)"
                    elif intensity > 0.4:
                        bg = "rgba(0,137,123,0.4)"
                    elif intensity > 0.2:
                        bg = "rgba(0,137,123,0.22)"
                    elif intensity > 0:
                        bg = "rgba(0,137,123,0.1)"
                    else:
                        bg = self._heatmap_empty()
                else:
                    # Dark mode: cyan on dark
                    if intensity > 0.8:
                        bg = "rgba(0,188,212,0.9)"
                    elif intensity > 0.6:
                        bg = "rgba(0,188,212,0.65)"
                    elif intensity > 0.4:
                        bg = "rgba(0,188,212,0.4)"
                    elif intensity > 0.2:
                        bg = "rgba(0,188,212,0.2)"
                    elif intensity > 0:
                        bg = "rgba(0,188,212,0.08)"
                    else:
                        bg = self._heatmap_empty()

                cell.setStyleSheet(f"background: {bg}; border-radius: 2px;")
                cell.setToolTip(f"{day_name} {h:02d}:00 - {val:,} messages")
                self._heatmap_grid.addWidget(cell, d_idx + 1, h + 1)

        # Day of week breakdown
        dow_data = db.fetchall(
            "SELECT day_of_week, SUM(message_count) as total "
            "FROM stats_hourly_heatmap "
            "GROUP BY day_of_week ORDER BY day_of_week"
        )
        if dow_data:
            max_dow = max(r["total"] for r in dow_data) or 1
            dow_color = "#00897b" if self._light else "#26a69a"
            for row in dow_data:
                day_name = days[row["day_of_week"]] if row["day_of_week"] < 7 else "?"
                bar = self._make_bar_row(
                    day_name, row["total"], max_dow, dow_color
                )
                self._daily_container.addLayout(bar)

    def _make_bar_row(self, label: str, count: int, max_val: int,
                      color: str = "#00bcd4",
                      count_text: str | None = None) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        lbl = QLabel(label)
        lbl.setFixedWidth(160)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lbl.setStyleSheet(f"{self._body_text()} font-size: 11px;")

        pct = (count / max_val) if max_val else 0
        bar = QFrame()
        bar.setFixedHeight(16)
        bar.setMinimumWidth(6)
        bar.setMaximumWidth(max(6, int(pct * 350)))
        bar.setStyleSheet(f"background-color: {color}; border-radius: 3px;")

        cnt_lbl = QLabel(count_text if count_text else f"{count:,}")
        cnt_lbl.setStyleSheet(f"{self._muted_text()} font-size: 11px;")

        row.addWidget(lbl)
        row.addWidget(bar)
        row.addWidget(cnt_lbl)
        row.addStretch()
        return row
