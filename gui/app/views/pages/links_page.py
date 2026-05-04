"""Links page — every URL shared in any conversation, with a rich
forensic-analysis UI.

Why this is a first-class page
==============================
A WhatsApp case routinely contains 100k - 2M shared URLs.  Analysts
investigate them along several axes that a flat table can't satisfy:

  * **Per-domain rollup** — what domains dominate?  Are there
    suspicious newcomers?  How much does each actor concentrate on
    a handful of domains?
  * **Per-sender rollup** — who shares the most links?  Does the
    device owner share their own links?
  * **Suspicious-URL triage** — known link shorteners, raw-IP URLs,
    very long URLs, suspicious TLDs (.zip / .mov / .cc / .top etc.)
    are common signals in scam / phishing investigations.
  * **Cross-chat propagation** — same URL shared in N chats =
    common indicator of a forwarded scam or coordinated campaign.
  * **Navigate to the originating message** — every link row must
    one-click open the chat at the exact message that shared it.

Layout
======
Left rail (240 px):
    Domain facet list — auto-sorted by count desc, click to filter,
    free-text search box at top, "All" + "⚠ Risky only" pseudo-rows.

Top of right pane:
    * Stats summary cards — totals, unique URLs, unique domains,
      suspicious count, top sender, busiest day
    * Filter row — search, sender combo, conversation combo, date
      from / to, risky-only toggle, export buttons
    * Top-domains horizontal bar chart (top 10)

Bottom of right pane:
    Rich table — domain pill, page title, URL (truncated, clickable),
    sender (owner-aware) + JID, conversation + chat-type tag,
    timestamp.  Right-click on any row for context menu:
    Open in browser / Copy URL / Copy domain / Find shared with /
    Go to message.

Owner-aware
===========
``message.from_me = 1`` rows have NULL ``sender_id``, so the LEFT
JOIN against ``contact`` returns NULLs.  We pull the device-owner
identity from ``case_metadata`` and stamp owner rows with the real
name + JID instead of "Unknown".
"""

from __future__ import annotations

import csv
import html as _html
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

from PySide6.QtCore import QDate, QModelIndex, QPoint, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QFont, QGuiApplication,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QCompleter, QDateEdit, QDialog,
    QDialogButtonBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton,
    QScrollArea, QSplitter, QStackedWidget, QStyleOptionViewItem, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from app.services.database import Database
from app.services.theme_manager import ThemeManager


# ---------------------------------------------------------------------- #
# Risky URL detection
# ---------------------------------------------------------------------- #

# Domain shorteners — a URL through one of these hides its real
# destination, which is the textbook scam pattern in WhatsApp
# investigations.
_SHORTENERS = frozenset({
    "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "rebrand.ly", "cutt.ly", "shorturl.at", "t.co", "trib.al",
    "rb.gy", "v.gd", "soo.gd", "shrtco.de", "tiny.cc", "po.st",
    "lnkd.in", "wp.me", "fb.me", "youtu.be", "amzn.to", "fxo.co",
    "qr.codes", "shorturl.com", "yourl.com", "snip.ly", "su.pr",
})

# TLDs frequently abused for phishing / malware drops.  Browser
# vendors keep their own lists; this is a curated subset of the
# top-offenders that show up in WhatsApp scam corpora.
_RISKY_TLDS = frozenset({
    "zip", "mov", "cc", "top", "xyz", "tk", "ml", "ga", "cf", "gq",
    "click", "country", "stream", "download", "kim", "win", "loan",
    "cricket", "racing", "review", "science", "work", "men", "party",
})

_IP_REGEX = re.compile(
    r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]+)?$"
)
_URL_LONG_THRESHOLD = 200       # chars
_TITLE_TRUNC = 80
_URL_TRUNC = 100
_DOMAINS_RAIL_CAP = 500
_TOP_DOMAINS_CHART_N = 10


def _classify_risk(url: str, domain: str) -> tuple[bool, str]:
    """Return ``(is_risky, reason)`` for a URL.  Empty reason means
    not risky.  Used to surface a ⚠ pill + a hover tooltip."""
    if not url:
        return False, ""
    d = (domain or "").lower().strip()
    u = url.strip()
    # Raw IP host — no domain at all
    try:
        host = urlparse(u).hostname or ""
    except Exception:
        host = ""
    if host and _IP_REGEX.match(host):
        return True, "Raw IP address — no domain (common phishing)"
    if d in _SHORTENERS:
        return True, "URL shortener — destination hidden"
    if "." in d:
        tld = d.rsplit(".", 1)[-1]
        if tld in _RISKY_TLDS:
            return True, f"Suspicious TLD .{tld}"
    if len(u) > _URL_LONG_THRESHOLD:
        return True, f"Very long URL ({len(u)} chars)"
    if "@" in u and "://" in u:
        # username@host syntax — classic deceptive URL trick
        scheme_end = u.find("://") + 3
        rest = u[scheme_end:].split("/", 1)[0]
        if "@" in rest:
            return True, "URL contains @ (deceptive host syntax)"
    return False, ""


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

def _fmt_count(n: int) -> str:
    if n is None:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".rstrip("0").rstrip(".")
    return f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".")


def _fmt_ts_short(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return ""


# ====================================================================== #
# LinksPage
# ====================================================================== #

class LinksPage(QWidget):
    """Forensic Links browser — see module docstring."""

    # Emitted when the analyst right-clicks → "Go to message".  Wired
    # by main_window to switch the chat viewer to that conversation
    # at that exact message.
    from PySide6.QtCore import Signal as _Sig
    conversation_selected = _Sig(int, int)

    BATCH_SIZE = 500   # rows pulled per scroll-load page

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tm = ThemeManager.get()
        self._owner_name = ""
        self._owner_jid = ""
        self._selected_domain: Optional[str] = None
        self._risky_only = False
        self._all_rows: list[dict] = []      # currently loaded rows
        self._domain_counts: dict[str, int] = {}  # domain → count (full case)
        self._date_min: Optional[QDate] = None
        self._date_max: Optional[QDate] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(10)

        self._build_header(root)
        self._build_body(root)

        # First load — keep the spinner up briefly while the heavy
        # facet build runs in a single sweep.
        QTimer.singleShot(50, self._initial_load)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_header(self, root: QVBoxLayout) -> None:
        header = QHBoxLayout()
        title = QLabel("Links")
        f = QFont(); f.setPointSize(18); f.setBold(True)
        title.setFont(f)
        header.addWidget(title)

        self._sub_label = QLabel("")
        self._sub_label.setStyleSheet("color: #78909c; font-size: 12px;")
        header.addWidget(self._sub_label)
        header.addStretch()

        # Export buttons — top-right
        for label, slot in (
            ("⇩ CSV",  self._export_csv),
            ("⇩ HTML", self._export_html),
        ):
            btn = QPushButton(label)
            btn.setFixedHeight(28)
            btn.setStyleSheet(self._tm.filter_btn_style())
            btn.clicked.connect(slot)
            header.addWidget(btn)

        root.addLayout(header)

    def _build_body(self, root: QVBoxLayout) -> None:
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_rail())
        splitter.addWidget(self._build_right_pane())
        splitter.setSizes([240, 1100])
        root.addWidget(splitter, 1)

    def _build_left_rail(self) -> QWidget:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: rgba(128,128,128,0.04); "
            "         border-radius: 8px; "
            "         border: 1px solid rgba(128,128,128,0.10); }"
        )
        col = QVBoxLayout(wrap)
        col.setContentsMargins(10, 12, 10, 10)
        col.setSpacing(6)

        title = QLabel("DOMAINS")
        title.setStyleSheet(
            "color: #607d8b; font-size: 10px; font-weight: 700; "
            "letter-spacing: 0.06em;"
        )
        col.addWidget(title)

        self._domain_search = QLineEdit()
        self._domain_search.setPlaceholderText("filter domains…")
        self._domain_search.setClearButtonEnabled(True)
        self._domain_search.setFixedHeight(28)
        self._domain_search.textChanged.connect(self._refresh_domain_rail)
        col.addWidget(self._domain_search)

        self._domain_list = QListWidget()
        self._domain_list.setStyleSheet(
            "QListWidget { background: transparent; border: none; "
            "              outline: none; font-size: 12px; }"
            "QListWidget::item { padding: 5px 8px; border-radius: 3px; }"
            "QListWidget::item:hover { background: rgba(0,137,123,0.08); }"
            "QListWidget::item:selected { background: rgba(0,137,123,0.18); "
            "                              color: #00695c; font-weight: 600; }"
        )
        self._domain_list.itemClicked.connect(self._on_domain_clicked)
        col.addWidget(self._domain_list, 1)
        return wrap

    def _build_right_pane(self) -> QWidget:
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(10)

        # Stats summary cards
        col.addWidget(self._build_stats_strip())
        # Filter row
        col.addWidget(self._build_filter_row())
        # Top-domains chart
        col.addWidget(self._build_top_chart())
        # Table
        col.addWidget(self._build_table(), 1)
        return wrap

    def _build_stats_strip(self) -> QFrame:
        strip = QFrame()
        strip.setStyleSheet(
            "QFrame { background: rgba(0,137,123,0.06); "
            "         border-radius: 8px; "
            "         border: 1px solid rgba(0,137,123,0.16); }"
        )
        sl = QHBoxLayout(strip)
        sl.setContentsMargins(16, 10, 16, 10)
        sl.setSpacing(28)

        self._stat_labels: dict[str, QLabel] = {}
        for key, label in (
            ("total",      "Total links"),
            ("unique_url", "Unique URLs"),
            ("domains",    "Domains"),
            ("risky",      "⚠ Risky"),
            ("top_sender", "Top sender"),
            ("date_range", "Date range"),
        ):
            box = QVBoxLayout()
            box.setSpacing(2)
            cap = QLabel(label)
            cap.setStyleSheet(
                "color: #607d8b; font-size: 10px; font-weight: 600; "
                "text-transform: uppercase; letter-spacing: 0.04em;"
            )
            val = QLabel("…")
            val.setStyleSheet(
                "color: #00695c; font-size: 14px; font-weight: 700;"
            )
            box.addWidget(cap)
            box.addWidget(val)
            sl.addLayout(box)
            self._stat_labels[key] = val
        sl.addStretch()
        return strip

    def _build_filter_row(self) -> QFrame:
        row = QFrame()
        row.setStyleSheet("QFrame { background: transparent; }")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "\U0001F50D  filename, title, URL, domain, sender, JID, conversation…"
        )
        self._search.setFixedHeight(34)
        self._search.setClearButtonEnabled(True)
        rl.addWidget(self._search, 1)

        self._sender_combo = QComboBox()
        self._sender_combo.setEditable(True)
        self._sender_combo.setInsertPolicy(QComboBox.NoInsert)
        self._sender_combo.lineEdit().setPlaceholderText("any sender…")
        self._sender_combo.setFixedHeight(34)
        self._sender_combo.setMinimumWidth(180)
        rl.addWidget(self._sender_combo)

        self._conv_combo = QComboBox()
        self._conv_combo.setEditable(True)
        self._conv_combo.setInsertPolicy(QComboBox.NoInsert)
        self._conv_combo.lineEdit().setPlaceholderText("any conversation…")
        self._conv_combo.setFixedHeight(34)
        self._conv_combo.setMinimumWidth(200)
        rl.addWidget(self._conv_combo)

        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_from.setFixedHeight(34)
        self._date_from.setFixedWidth(125)
        rl.addWidget(self._date_from)

        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._date_to.setFixedHeight(34)
        self._date_to.setFixedWidth(125)
        rl.addWidget(self._date_to)

        self._risky_btn = QPushButton("⚠  Risky only")
        self._risky_btn.setCheckable(True)
        self._risky_btn.setFixedHeight(34)
        self._risky_btn.setStyleSheet(
            "QPushButton { padding: 4px 12px; border-radius: 4px; "
            "              border: 1px solid #fdd835; "
            "              background: #fff8e1; color: #e65100; "
            "              font-weight: 600; font-size: 11px; }"
            "QPushButton:checked { background: #e65100; color: white; }"
        )
        self._risky_btn.clicked.connect(self._on_risky_toggled)
        rl.addWidget(self._risky_btn)

        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setFixedHeight(34)
        self._reset_btn.setStyleSheet(self._tm.filter_btn_style())
        self._reset_btn.clicked.connect(self._reset_filters)
        rl.addWidget(self._reset_btn)

        # Wire all the live filters
        self._search_timer = QTimer()
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(280)
        self._search_timer.timeout.connect(self._apply_filters)
        self._search.textChanged.connect(lambda: self._search_timer.start())
        self._sender_combo.currentIndexChanged.connect(self._apply_filters)
        self._conv_combo.currentIndexChanged.connect(self._apply_filters)
        self._date_from.dateChanged.connect(self._apply_filters)
        self._date_to.dateChanged.connect(self._apply_filters)

        return row

    def _build_top_chart(self) -> QFrame:
        wrap = QFrame()
        wrap.setStyleSheet(
            "QFrame { background: rgba(128,128,128,0.04); "
            "         border-radius: 8px; "
            "         border: 1px solid rgba(128,128,128,0.10); }"
        )
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 10, 16, 10)
        wl.setSpacing(4)
        cap = QLabel("TOP DOMAINS  (in current selection)")
        cap.setStyleSheet(
            "color: #607d8b; font-size: 10px; font-weight: 700; "
            "text-transform: uppercase; letter-spacing: 0.06em;"
        )
        wl.addWidget(cap)
        self._chart_container = QVBoxLayout()
        self._chart_container.setSpacing(2)
        wl.addLayout(self._chart_container)
        return wrap

    def _build_table(self) -> QTableWidget:
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels([
            "Domain", "Title", "URL", "Sender",
            "Conversation", "When", ""
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(36)
        h = self._table.horizontalHeader()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.Interactive); self._table.setColumnWidth(0, 150)
        h.setSectionResizeMode(1, QHeaderView.Interactive); self._table.setColumnWidth(1, 220)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.Interactive); self._table.setColumnWidth(3, 170)
        h.setSectionResizeMode(4, QHeaderView.Interactive); self._table.setColumnWidth(4, 200)
        h.setSectionResizeMode(5, QHeaderView.Interactive); self._table.setColumnWidth(5, 130)
        h.setSectionResizeMode(6, QHeaderView.Fixed);       self._table.setColumnWidth(6, 56)

        # Open URL on double-click; right-click for context menu
        self._table.cellDoubleClicked.connect(self._on_row_dblclick)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_row_context_menu)
        return self._table

    # ------------------------------------------------------------------ #
    # Initial heavy load (runs once)
    # ------------------------------------------------------------------ #

    def _initial_load(self) -> None:
        try:
            db = Database.get()
        except Exception:
            return

        # Owner identity
        try:
            kv = {
                r["key"]: r["value"]
                for r in db.fetchall(
                    "SELECT key, value FROM case_metadata "
                    "WHERE key IN ('device_owner_name','device_owner_phone',"
                    "              'device_owner_jid')"
                )
            }
        except Exception:
            kv = {}
        self._owner_name = kv.get("device_owner_name") or ""
        phone = (kv.get("device_owner_phone") or "").replace("@s.whatsapp.net", "")
        self._owner_jid = kv.get("device_owner_jid") or (
            f"{phone}@s.whatsapp.net" if phone else ""
        )

        # Pre-compute the per-domain count rollup (case-wide).  We do it
        # ONCE so the left rail counts stay stable as the user filters
        # — that way the analyst can see "the case has 235k youtube
        # links" even after filtering down to "today only".  Click on
        # a domain row → filter the table by that domain.
        rows = db.fetchall(
            "SELECT COALESCE(domain, '(no domain)') AS d, COUNT(*) AS n "
            "FROM message_link_detail "
            "GROUP BY d ORDER BY n DESC"
        )
        self._domain_counts = {r["d"]: r["n"] for r in rows}

        # Build sender + conv combos once, sorted by activity.
        self._populate_sender_combo(db)
        self._populate_conv_combo(db)
        self._populate_date_range(db)

        # Render the rail + apply default (no) filters
        self._refresh_domain_rail()
        self._apply_filters()

    def _populate_sender_combo(self, db) -> None:
        self._sender_combo.blockSignals(True)
        self._sender_combo.clear()
        self._sender_combo.addItem("All senders", None)
        # Owner first
        owner_label = (
            f"{self._owner_name} (you)" if self._owner_name else "You (owner)"
        )
        self._sender_combo.addItem(owner_label, "__owner__")
        # Top 200 link-sharing contacts so the combo stays usable
        try:
            rows = db.fetchall(
                "SELECT m.sender_id, "
                "       COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
                "                REPLACE(c.phone_jid,'@s.whatsapp.net','')) AS name, "
                "       COUNT(*) AS n "
                "FROM message_link_detail mld "
                "JOIN message m ON m.id = mld.message_id "
                "LEFT JOIN contact c ON c.id = m.sender_id "
                "WHERE m.from_me = 0 AND m.sender_id IS NOT NULL "
                "GROUP BY m.sender_id "
                "ORDER BY n DESC LIMIT 200"
            )
        except Exception:
            rows = []
        for r in rows:
            nm = r["name"] or f"#{r['sender_id']}"
            self._sender_combo.addItem(f"{nm}  ({r['n']:,})", int(r["sender_id"]))
        compl = QCompleter(self._sender_combo.model(), self._sender_combo)
        compl.setFilterMode(Qt.MatchContains)
        compl.setCaseSensitivity(Qt.CaseInsensitive)
        self._sender_combo.setCompleter(compl)
        self._sender_combo.blockSignals(False)

    def _populate_conv_combo(self, db) -> None:
        self._conv_combo.blockSignals(True)
        self._conv_combo.clear()
        self._conv_combo.addItem("All conversations", None)
        try:
            rows = db.fetchall(
                "SELECT cv.id, cv.display_name, cv.chat_type, COUNT(*) AS n "
                "FROM message_link_detail mld "
                "JOIN message m ON m.id = mld.message_id "
                "JOIN conversation cv ON cv.id = m.conversation_id "
                "GROUP BY cv.id "
                "ORDER BY n DESC LIMIT 500"
            )
        except Exception:
            rows = []
        for r in rows:
            nm = r["display_name"] or f"#{r['id']}"
            tag = (r["chat_type"] or "personal")[0].upper()
            self._conv_combo.addItem(f"[{tag}]  {nm}  ({r['n']:,})", int(r["id"]))
        compl = QCompleter(self._conv_combo.model(), self._conv_combo)
        compl.setFilterMode(Qt.MatchContains)
        compl.setCaseSensitivity(Qt.CaseInsensitive)
        self._conv_combo.setCompleter(compl)
        self._conv_combo.blockSignals(False)

    def _populate_date_range(self, db) -> None:
        try:
            row = db.fetchone(
                "SELECT MIN(m.timestamp) AS lo, MAX(m.timestamp) AS hi "
                "FROM message_link_detail mld "
                "JOIN message m ON m.id = mld.message_id "
                "WHERE m.timestamp > 0"
            )
        except Exception:
            row = None
        self._date_from.blockSignals(True)
        self._date_to.blockSignals(True)
        try:
            if row and row["lo"]:
                d = datetime.fromtimestamp(row["lo"] / 1000)
                qd = QDate(d.year, d.month, d.day)
                self._date_min = qd
                self._date_from.setMinimumDate(qd)
                self._date_to.setMinimumDate(qd)
                self._date_from.setDate(qd)
            else:
                self._date_from.setDate(QDate(2009, 1, 1))
            if row and row["hi"]:
                d = datetime.fromtimestamp(row["hi"] / 1000)
                qd = QDate(d.year, d.month, d.day)
                self._date_max = qd
                self._date_from.setMaximumDate(qd)
                self._date_to.setMaximumDate(qd)
                self._date_to.setDate(qd)
            else:
                self._date_to.setDate(QDate.currentDate())
        finally:
            self._date_from.blockSignals(False)
            self._date_to.blockSignals(False)

    # ------------------------------------------------------------------ #
    # Domain rail
    # ------------------------------------------------------------------ #

    def _refresh_domain_rail(self) -> None:
        q = (self._domain_search.text() or "").strip().lower()
        self._domain_list.clear()
        # Pseudo rows
        all_n = sum(self._domain_counts.values())
        risky_n = self._estimate_risky_count()
        all_item = QListWidgetItem(f"📋  All  ({all_n:,})")
        all_item.setData(Qt.UserRole, None)
        self._domain_list.addItem(all_item)
        risky_item = QListWidgetItem(f"⚠  Risky only  (~{risky_n:,})")
        risky_item.setData(Qt.UserRole, "__risky__")
        risky_item.setForeground(QBrush(QColor("#e65100")))
        self._domain_list.addItem(risky_item)

        # Normal domain rows
        items = list(self._domain_counts.items())
        if q:
            items = [(d, n) for d, n in items if q in d.lower()]
        items.sort(key=lambda kv: -kv[1])
        for d, n in items[:_DOMAINS_RAIL_CAP]:
            risky, _ = _classify_risk("https://" + d, d)
            display = (f"⚠  {d}" if risky else d) + f"   ({n:,})"
            it = QListWidgetItem(display)
            it.setData(Qt.UserRole, d)
            if risky:
                it.setForeground(QBrush(QColor("#bf360c")))
            self._domain_list.addItem(it)
        if len(items) > _DOMAINS_RAIL_CAP:
            tail = QListWidgetItem(
                f"…  {len(items) - _DOMAINS_RAIL_CAP} more (refine search)"
            )
            tail.setFlags(Qt.NoItemFlags)
            self._domain_list.addItem(tail)

        # Restore current selection visually
        for i in range(self._domain_list.count()):
            it = self._domain_list.item(i)
            d = it.data(Qt.UserRole)
            if (self._risky_only and d == "__risky__") or \
               (not self._risky_only and d == self._selected_domain) or \
               (not self._risky_only and self._selected_domain is None and d is None):
                self._domain_list.setCurrentRow(i)
                break

    def _estimate_risky_count(self) -> int:
        """Rough estimate of risky URLs in the case — based on
        flagged-domain rollup + shortener domains.  Doesn't open each
        URL (would be too slow at 1.7M), so it slightly under-counts
        very-long-URL or @-trick rows.  Good enough for the rail label.
        """
        n = 0
        for d, c in self._domain_counts.items():
            if d in _SHORTENERS:
                n += c
                continue
            if "." in d and d.rsplit(".", 1)[-1] in _RISKY_TLDS:
                n += c
                continue
            try:
                host = urlparse("https://" + d).hostname or ""
                if host and _IP_REGEX.match(host):
                    n += c
            except Exception:
                pass
        return n

    def _on_domain_clicked(self, item: QListWidgetItem) -> None:
        v = item.data(Qt.UserRole)
        if v == "__risky__":
            self._risky_only = True
            self._risky_btn.setChecked(True)
            self._selected_domain = None
        else:
            self._risky_only = False
            self._risky_btn.setChecked(False)
            self._selected_domain = v   # may be None (the All row)
        self._apply_filters()

    def _on_risky_toggled(self) -> None:
        self._risky_only = self._risky_btn.isChecked()
        if self._risky_only:
            self._selected_domain = None
        self._apply_filters()
        self._refresh_domain_rail()

    def _reset_filters(self) -> None:
        self._search.clear()
        self._sender_combo.setCurrentIndex(0)
        self._conv_combo.setCurrentIndex(0)
        self._risky_only = False
        self._risky_btn.setChecked(False)
        self._selected_domain = None
        if self._date_min:
            self._date_from.setDate(self._date_min)
        if self._date_max:
            self._date_to.setDate(self._date_max)
        self._refresh_domain_rail()
        self._apply_filters()

    # ------------------------------------------------------------------ #
    # Apply filters → fetch rows → render table + chart + stats
    # ------------------------------------------------------------------ #

    def _apply_filters(self) -> None:
        try:
            db = Database.get()
        except Exception:
            return

        where: list[str] = ["1=1"]
        params: list = []
        text = (self._search.text() or "").strip()
        if text:
            pat = f"%{text}%"
            where.append(
                "(mld.url LIKE ? OR mld.page_title LIKE ? OR "
                " mld.description LIKE ? OR mld.domain LIKE ? OR "
                " COALESCE(c.resolved_name, c.wa_name, c.phone_number, "
                "          c.phone_jid, '') LIKE ? OR "
                " COALESCE(cv.display_name, '') LIKE ?)"
            )
            params.extend([pat] * 6)

        sender_v = self._sender_combo.currentData()
        if sender_v == "__owner__":
            where.append("m.from_me = 1")
        elif isinstance(sender_v, int):
            where.append("m.sender_id = ?")
            params.append(sender_v)

        conv_v = self._conv_combo.currentData()
        if isinstance(conv_v, int):
            where.append("m.conversation_id = ?")
            params.append(conv_v)

        if self._selected_domain:
            where.append("mld.domain = ?")
            params.append(self._selected_domain)

        # Date range
        df = self._date_from.date()
        dt = self._date_to.date()
        if df.isValid() and dt.isValid():
            from_ts = int(datetime(df.year(), df.month(), df.day()).timestamp() * 1000)
            to_ts = int(datetime(dt.year(), dt.month(), dt.day(), 23, 59, 59).timestamp() * 1000)
            where.append("m.timestamp BETWEEN ? AND ?")
            params.extend([from_ts, to_ts])

        where_sql = " AND ".join(where)

        # Cap fetched rows so the table stays responsive on huge
        # datasets — analysts narrow with filters, then export the
        # full filtered set via the CSV/HTML buttons (which use the
        # same WHERE without the LIMIT).
        sql = f"""
            SELECT mld.id, mld.url, mld.domain, mld.page_title,
                   mld.description, mld.message_id,
                   m.from_me, m.timestamp, m.sender_id,
                   m.conversation_id,
                   COALESCE(c.resolved_name, c.wa_name, c.phone_number,
                            REPLACE(c.phone_jid,'@s.whatsapp.net',''))
                       AS sender_name,
                   c.phone_jid AS sender_jid,
                   cv.display_name AS conv_name,
                   cv.chat_type    AS conv_type
            FROM message_link_detail mld
            JOIN message m ON m.id = mld.message_id
            LEFT JOIN contact c ON c.id = m.sender_id
            LEFT JOIN conversation cv ON cv.id = m.conversation_id
            WHERE {where_sql}
            ORDER BY m.timestamp DESC
            LIMIT 5000
        """
        try:
            rows = [dict(r) for r in db.fetchall(sql, tuple(params))]
        except Exception as e:
            print(f"[LinksPage] query failed: {e}")
            rows = []

        # Risky filter is applied client-side because the heuristic
        # checks involve URL parsing per row.
        if self._risky_only:
            rows = [
                r for r in rows
                if _classify_risk(r["url"] or "", r["domain"] or "")[0]
            ]

        self._all_rows = rows
        self._render_table(rows)
        self._render_top_chart(rows)
        self._render_stats(rows, where_sql, params, db)

    def _render_stats(self, rows: list[dict], where_sql: str,
                      params: list, db) -> None:
        # The visible table is capped at 5000 — but the strip cards
        # report the TRUE total + unique counts via separate queries
        # so the analyst sees the real scope of their filter.
        try:
            total_row = db.fetchone(
                f"SELECT COUNT(*) AS n, COUNT(DISTINCT mld.url) AS u, "
                f"       COUNT(DISTINCT mld.domain) AS d "
                f"FROM message_link_detail mld "
                f"JOIN message m ON m.id = mld.message_id "
                f"LEFT JOIN contact c ON c.id = m.sender_id "
                f"LEFT JOIN conversation cv ON cv.id = m.conversation_id "
                f"WHERE {where_sql}",
                tuple(params)
            )
        except Exception:
            total_row = None
        total = (total_row["n"] if total_row else 0) or 0
        unique_url = (total_row["u"] if total_row else 0) or 0
        unique_dom = (total_row["d"] if total_row else 0) or 0

        risky_visible = sum(
            1 for r in rows
            if _classify_risk(r["url"] or "", r["domain"] or "")[0]
        )

        # Top sender — owner-aware
        sender_counts: dict[str, int] = {}
        for r in rows:
            if r["from_me"]:
                key = (self._owner_name or "You") + " (you)"
            else:
                key = r["sender_name"] or "Unknown"
            sender_counts[key] = sender_counts.get(key, 0) + 1
        top_sender = (
            f"{max(sender_counts, key=sender_counts.get)}"
            f"  ({max(sender_counts.values()):,})"
            if sender_counts else "—"
        )

        # Date range of visible rows
        if rows:
            mn = min(r["timestamp"] for r in rows if r["timestamp"])
            mx = max(r["timestamp"] for r in rows if r["timestamp"])
            range_str = f"{_fmt_ts_short(mn)}  →  {_fmt_ts_short(mx)}"
        else:
            range_str = "—"

        self._stat_labels["total"].setText(f"{total:,}")
        self._stat_labels["unique_url"].setText(f"{unique_url:,}")
        self._stat_labels["domains"].setText(f"{unique_dom:,}")
        self._stat_labels["risky"].setText(
            f"{risky_visible:,} (visible)"
        )
        self._stat_labels["top_sender"].setText(top_sender)
        self._stat_labels["date_range"].setText(range_str)

        # Sub-label under the page title
        cap_note = (
            f"  ·  showing first 5,000 of {total:,}"
            if total > 5000 else ""
        )
        self._sub_label.setText(
            f"{len(rows):,} link{'s' if len(rows) != 1 else ''} in view"
            f"{cap_note}"
        )

    def _render_top_chart(self, rows: list[dict]) -> None:
        # Clear existing chart rows
        while self._chart_container.count():
            it = self._chart_container.takeAt(0)
            w = it.widget() if it else None
            if w:
                w.deleteLater()

        counts: dict[str, int] = {}
        for r in rows:
            d = r["domain"] or "(no domain)"
            counts[d] = counts.get(d, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:_TOP_DOMAINS_CHART_N]
        if not top:
            empty = QLabel("(no domains in current selection)")
            empty.setStyleSheet("color:#9aa3ac; font-size: 11px; padding: 4px 0;")
            self._chart_container.addWidget(empty)
            return
        max_n = top[0][1] or 1
        for d, n in top:
            row_w = QWidget()
            rl = QHBoxLayout(row_w)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            lbl = QLabel(d)
            lbl.setFixedWidth(180)
            lbl.setStyleSheet("color:#37474f; font-size: 11px;")
            rl.addWidget(lbl)
            bar = QFrame()
            bar.setFixedHeight(14)
            pct = n / max_n
            bar.setMinimumWidth(max(2, int(pct * 600)))
            bar.setMaximumWidth(max(2, int(pct * 600)))
            bar.setStyleSheet(
                "QFrame { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                " stop:0 #00897b, stop:1 #4dd0e1); border-radius: 3px; }"
            )
            rl.addWidget(bar)
            cnt = QLabel(f"{n:,}")
            cnt.setFixedWidth(80)
            cnt.setStyleSheet(
                "color:#00695c; font-size: 11px; font-weight: 600; "
                "font-variant-numeric: tabular-nums;"
            )
            rl.addWidget(cnt)
            rl.addStretch()
            self._chart_container.addWidget(row_w)

    def _render_table(self, rows: list[dict]) -> None:
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            url = r["url"] or ""
            domain = r["domain"] or ""
            risky, reason = _classify_risk(url, domain)

            # Domain cell — pill style + ⚠ marker
            dom_text = (f"⚠  {domain}" if risky else domain) or "(no domain)"
            dom_it = QTableWidgetItem(dom_text)
            dom_it.setData(Qt.UserRole, r)
            if risky:
                dom_it.setForeground(QBrush(QColor("#bf360c")))
                dom_it.setToolTip(f"⚠ {reason}")
            else:
                dom_it.setForeground(QBrush(QColor("#00695c")))
            self._table.setItem(i, 0, dom_it)

            # Title (truncated)
            title = (r["page_title"] or "").strip()
            t_disp = title[:_TITLE_TRUNC] + ("…" if len(title) > _TITLE_TRUNC else "")
            t_it = QTableWidgetItem(t_disp or "—")
            if title:
                t_it.setToolTip(title)
            else:
                t_it.setForeground(QBrush(QColor("#9aa3ac")))
            self._table.setItem(i, 1, t_it)

            # URL (truncated; tooltip = full)
            u_disp = url[:_URL_TRUNC] + ("…" if len(url) > _URL_TRUNC else "")
            u_it = QTableWidgetItem(u_disp)
            u_it.setToolTip(url)
            u_it.setForeground(QBrush(QColor("#1565c0")))
            self._table.setItem(i, 2, u_it)

            # Sender (owner-aware)
            if r["from_me"]:
                s_text = (self._owner_name or "You") + " (you)"
                s_jid = self._owner_jid
            else:
                s_text = r["sender_name"] or "Unknown"
                s_jid = r["sender_jid"] or ""
            s_it = QTableWidgetItem(s_text)
            if s_jid:
                s_it.setToolTip(f"{s_text}\n{s_jid}")
            self._table.setItem(i, 3, s_it)

            # Conversation
            conv_name = r["conv_name"] or "—"
            conv_type = r["conv_type"] or "personal"
            c_it = QTableWidgetItem(f"[{conv_type[0].upper()}]  {conv_name}")
            c_it.setToolTip(f"{conv_name}\n[{conv_type}]")
            self._table.setItem(i, 4, c_it)

            # When
            ts = r["timestamp"] or 0
            ts_str = _fmt_ts_short(ts)
            ts_it = QTableWidgetItem(ts_str)
            ts_it.setData(Qt.UserRole, int(ts))
            self._table.setItem(i, 5, ts_it)

            # Open URL action button
            open_btn = QPushButton("↗")
            open_btn.setCursor(Qt.PointingHandCursor)
            open_btn.setFixedHeight(22)
            open_btn.setStyleSheet(
                "QPushButton { background: #00897b; color: white; border: none; "
                "             border-radius: 4px; font-size: 11px; "
                "             font-weight: 700; padding: 0 8px; }"
                "QPushButton:hover { background: #00796b; }"
            )
            open_btn.setToolTip("Open in default browser")
            open_btn.clicked.connect(
                lambda _=False, u=url: QDesktopServices.openUrl(QUrl(u))
            )
            self._table.setCellWidget(i, 6, open_btn)
        self._table.setSortingEnabled(True)

    # ------------------------------------------------------------------ #
    # Row interactions
    # ------------------------------------------------------------------ #

    def _on_row_dblclick(self, row: int, col: int) -> None:
        it = self._table.item(row, 0)
        if not it:
            return
        r = it.data(Qt.UserRole)
        if not r:
            return
        # Double-click on URL → browser; elsewhere → open conversation
        if col == 2:
            QDesktopServices.openUrl(QUrl(r["url"] or ""))
        else:
            self.conversation_selected.emit(
                int(r["conversation_id"] or 0),
                int(r["message_id"] or 0)
            )

    def _on_row_context_menu(self, pos: QPoint) -> None:
        idx = self._table.indexAt(pos)
        if not idx.isValid():
            return
        it = self._table.item(idx.row(), 0)
        if not it:
            return
        r = it.data(Qt.UserRole)
        if not r:
            return

        url = r["url"] or ""
        domain = r["domain"] or ""

        menu = QMenu(self._table)
        menu.setStyleSheet(
            "QMenu { background: white; border: 1px solid #d0d7de; padding: 4px; }"
            "QMenu::item { padding: 6px 16px; font-size: 12px; }"
            "QMenu::item:selected { background: #e0f2f1; color: #00695c; }"
            "QMenu::separator { height: 1px; background: #e0e7ed; "
            "                    margin: 4px 8px; }"
        )

        open_act = QAction("↗  Open URL in browser", menu)
        open_act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        menu.addAction(open_act)

        goto_act = QAction("→  Go to message in chat viewer", menu)
        goto_act.triggered.connect(
            lambda: self.conversation_selected.emit(
                int(r["conversation_id"] or 0),
                int(r["message_id"] or 0)
            )
        )
        menu.addAction(goto_act)

        # "Find shared with" — surface every chat where this URL appears
        share_act = QAction("\U0001F50D  Find every chat that shared this URL", menu)
        share_act.triggered.connect(lambda: self._show_url_shares(url))
        menu.addAction(share_act)

        menu.addSeparator()

        copy_url = QAction("⎘  Copy URL", menu)
        copy_url.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(url)
        )
        menu.addAction(copy_url)

        copy_dom = QAction("⎘  Copy domain", menu)
        copy_dom.triggered.connect(
            lambda: QGuiApplication.clipboard().setText(domain)
        )
        menu.addAction(copy_dom)

        if r.get("sender_jid"):
            copy_jid = QAction("⎘  Copy sender JID", menu)
            copy_jid.triggered.connect(
                lambda: QGuiApplication.clipboard().setText(r["sender_jid"])
            )
            menu.addAction(copy_jid)

        menu.addSeparator()

        filter_dom = QAction(f"\U0001F50D  Filter by domain: {domain}", menu)
        filter_dom.triggered.connect(
            lambda: (self._set_domain_filter(domain), self._apply_filters())
        )
        menu.addAction(filter_dom)

        if r["from_me"]:
            filter_sender = QAction("\U0001F464  Filter by sender: You (owner)", menu)
            filter_sender.triggered.connect(
                lambda: self._set_sender_filter("__owner__")
            )
        elif r.get("sender_id"):
            sender_label = r["sender_name"] or "?"
            filter_sender = QAction(
                f"\U0001F464  Filter by sender: {sender_label}", menu)
            filter_sender.triggered.connect(
                lambda sid=int(r["sender_id"]): self._set_sender_filter(sid)
            )
        else:
            filter_sender = None
        if filter_sender:
            menu.addAction(filter_sender)

        if r.get("conversation_id"):
            cn = r["conv_name"] or "?"
            filter_conv = QAction(
                f"\U0001F4AC  Filter by conversation: {cn}", menu)
            filter_conv.triggered.connect(
                lambda cid=int(r["conversation_id"]): self._set_conv_filter(cid)
            )
            menu.addAction(filter_conv)

        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _set_domain_filter(self, domain: str) -> None:
        self._selected_domain = domain
        self._refresh_domain_rail()

    def _set_sender_filter(self, value) -> None:
        for i in range(self._sender_combo.count()):
            if self._sender_combo.itemData(i) == value:
                self._sender_combo.setCurrentIndex(i)
                return

    def _set_conv_filter(self, value) -> None:
        for i in range(self._conv_combo.count()):
            if self._conv_combo.itemData(i) == value:
                self._conv_combo.setCurrentIndex(i)
                return

    def _show_url_shares(self, url: str) -> None:
        """Modal dialog listing every chat where the same URL was
        shared, with a Go button per row to jump the chat viewer.
        Same UX as the Documents page's "Find where shared" popup.
        """
        if not url:
            return
        try:
            db = Database.get()
        except Exception:
            return
        shares = db.fetchall(
            "SELECT m.id AS msg_id, m.conversation_id, m.timestamp, m.from_me, "
            "       cv.display_name AS conv_name, cv.chat_type AS conv_type, "
            "       COALESCE(c.resolved_name, c.wa_name, c.phone_number) AS sender_name, "
            "       c.phone_jid AS sender_jid "
            "FROM message_link_detail mld "
            "JOIN message m ON m.id = mld.message_id "
            "LEFT JOIN contact c ON c.id = m.sender_id "
            "LEFT JOIN conversation cv ON cv.id = m.conversation_id "
            "WHERE mld.url = ? "
            "ORDER BY m.timestamp DESC LIMIT 200",
            (url,)
        )
        dlg = QDialog(self)
        dlg.setWindowTitle("Where this URL was shared")
        dlg.setMinimumSize(720, 460)
        dl = QVBoxLayout(dlg)
        dl.setContentsMargins(16, 16, 16, 16)
        dl.setSpacing(10)

        head = QLabel(
            f"<b>{_html.escape(url[:140])}{'…' if len(url) > 140 else ''}</b>"
            f"<br><small style='color:#666'>"
            f"Found in {len(shares)} message{'s' if len(shares) != 1 else ''} "
            f"across {len({s['conversation_id'] for s in shares})} "
            f"conversation{'s' if len({s['conversation_id'] for s in shares}) != 1 else ''}"
            f"</small>"
        )
        head.setTextFormat(Qt.RichText)
        head.setWordWrap(True)
        dl.addWidget(head)

        tbl = QTableWidget()
        tbl.setColumnCount(5)
        tbl.setHorizontalHeaderLabels(["Conversation", "Sender", "When", "Direction", ""])
        tbl.setRowCount(len(shares))
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tbl.horizontalHeader().setStretchLastSection(False)
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            tbl.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        for i, s in enumerate(shares):
            cn = s["conv_name"] or "?"
            ct = s["conv_type"] or "personal"
            tbl.setItem(i, 0, QTableWidgetItem(f"[{ct[0].upper()}]  {cn}"))
            sender = (
                f"{self._owner_name or 'You'} (you)" if s["from_me"]
                else (s["sender_name"] or (s["sender_jid"] or "").split("@", 1)[0] or "Unknown")
            )
            tbl.setItem(i, 1, QTableWidgetItem(sender))
            tbl.setItem(i, 2, QTableWidgetItem(_fmt_ts_short(s["timestamp"] or 0)))
            tbl.setItem(i, 3, QTableWidgetItem("Outgoing" if s["from_me"] else "Incoming"))
            go_btn = QPushButton("Go →")
            go_btn.setCursor(Qt.PointingHandCursor)
            go_btn.setStyleSheet(
                "QPushButton { background: #00897b; color: white; border: none; "
                "             border-radius: 4px; font-size: 10px; font-weight: 600; "
                "             padding: 4px 10px; }"
                "QPushButton:hover { background: #00796b; }"
            )
            cid = s["conversation_id"]; mid = s["msg_id"]
            def _go(_=False, c=cid, m=mid, d=dlg):
                d.accept()
                self.conversation_selected.emit(int(c), int(m))
            go_btn.clicked.connect(_go)
            tbl.setCellWidget(i, 4, go_btn)
        dl.addWidget(tbl, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        dl.addWidget(bb)
        dlg.exec()

    # ------------------------------------------------------------------ #
    # Exports
    # ------------------------------------------------------------------ #

    def _export_csv(self) -> None:
        rows = self._all_rows
        if not rows:
            QMessageBox.information(self, "Export", "No links in current selection.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Links to CSV", "links_export.csv",
            "CSV (*.csv)"
        )
        if not path:
            return
        cols = ["timestamp", "domain", "url", "page_title", "description",
                "sender", "sender_jid", "conversation", "conv_type",
                "from_me", "is_risky", "risk_reason", "msg_id", "conv_id"]
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for r in rows:
                    risky, reason = _classify_risk(r["url"] or "", r["domain"] or "")
                    if r["from_me"]:
                        sender = f"{self._owner_name or 'You'} (you)"
                        sjid = self._owner_jid
                    else:
                        sender = r["sender_name"] or ""
                        sjid = r["sender_jid"] or ""
                    w.writerow([
                        _fmt_ts_short(r["timestamp"] or 0),
                        r["domain"] or "", r["url"] or "",
                        r["page_title"] or "", r["description"] or "",
                        sender, sjid,
                        r["conv_name"] or "", r["conv_type"] or "",
                        bool(r["from_me"]), risky, reason,
                        r["message_id"] or 0, r["conversation_id"] or 0,
                    ])
            QMessageBox.information(self, "Export", f"Wrote {len(rows):,} rows to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))

    def _export_html(self) -> None:
        rows = self._all_rows
        if not rows:
            QMessageBox.information(self, "Export", "No links in current selection.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Links to HTML", "links_export.html",
            "HTML (*.html)"
        )
        if not path:
            return
        try:
            sb = ['<!DOCTYPE html><html><head><meta charset="utf-8">'
                  '<title>WAInsight — Links Export</title>'
                  '<style>body{font-family:-apple-system,Segoe UI,sans-serif;'
                  'font-size:12px;color:#222;padding:24px;}'
                  'h1{color:#00695c;font-size:20px}'
                  'table{border-collapse:collapse;width:100%;font-size:11px}'
                  'th,td{border:1px solid #ddd;padding:4px 8px;'
                  'text-align:left;vertical-align:top;}'
                  'th{background:#f5f5f5;font-size:10px;text-transform:uppercase}'
                  'tr.risky{background:#fff3e0}'
                  'a{color:#1565c0}'
                  '.tag{background:#e0f2f1;color:#00695c;padding:1px 6px;'
                  'border-radius:8px;font-size:9px;font-weight:600;'
                  'text-transform:uppercase;letter-spacing:0.04em}'
                  '.warn{background:#ffe0b2;color:#bf360c;'
                  'padding:1px 6px;border-radius:8px;font-size:9px;font-weight:700;'
                  'text-transform:uppercase}</style></head><body>',
                  f'<h1>WAInsight — {len(rows):,} link{"s" if len(rows)!=1 else ""}</h1>',
                  '<table><thead><tr><th>When</th><th>Domain</th>'
                  '<th>URL</th><th>Title</th><th>Sender</th>'
                  '<th>Conversation</th><th>Risk</th></tr></thead><tbody>']
            for r in rows:
                risky, reason = _classify_risk(r["url"] or "", r["domain"] or "")
                row_cls = ' class="risky"' if risky else ''
                sender = (
                    f"{self._owner_name or 'You'} (you)" if r["from_me"]
                    else (r["sender_name"] or "Unknown")
                )
                sb.append(
                    f'<tr{row_cls}>'
                    f'<td>{_html.escape(_fmt_ts_short(r["timestamp"] or 0))}</td>'
                    f'<td><span class="tag">{_html.escape(r["domain"] or "")}</span></td>'
                    f'<td><a href="{_html.escape(r["url"] or "")}" target="_blank">'
                    f'{_html.escape((r["url"] or "")[:120])}</a></td>'
                    f'<td>{_html.escape((r["page_title"] or "")[:120])}</td>'
                    f'<td>{_html.escape(sender)}</td>'
                    f'<td>{_html.escape(r["conv_name"] or "")}</td>'
                    f'<td>{("<span class=&quot;warn&quot;>⚠ " + _html.escape(reason) + "</span>") if risky else ""}</td>'
                    '</tr>'
                )
            sb.append('</tbody></table></body></html>')
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("".join(sb))
            QMessageBox.information(self, "Export", f"Wrote {len(rows):,} rows to:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))

    # ------------------------------------------------------------------ #
    # Public refresh hook (called by main_window when case loads)
    # ------------------------------------------------------------------ #

    def reload(self) -> None:
        self._initial_load()
