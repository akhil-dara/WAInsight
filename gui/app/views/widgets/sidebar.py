"""
Navigation sidebar with section headers and page items.
Uses a page_id→row mapping (NOT row index = stack index) for navigation.
"""

from __future__ import annotations

from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout, QWidget,
)

from app.config import PAGES
from app.services.theme_manager import ThemeManager

# Unicode icons as fallback (no external icon package needed)
ICON_MAP = {
    "dashboard":    "\u25A6",   # ▦
    "chat":         "\u2637",   # ☷
    "people":       "\u2630",   # ☰
    "image":        "\u25A3",   # ▣
    "call":         "\u260E",   # ☎
    "event":        "\u2637",   # ☷
    "search":       "\u2315",   # ⌕
    "bar_chart":    "\u2261",   # ≡
    "groups":       "\u2302",   # ⌂
    "shield":       "\u2718",   # ✘
    "edit":         "\u270E",   # ✎
    "delete":       "\u2716",   # ✖
    "warning":      "\u26A0",   # ⚠
    "location_on":  "\u2316",   # ⌖
    "link":         "\u26D3",   # ⛓
    "poll":         "\u2630",   # ☰
    "flag":         "\u2691",   # ⚑
    "timeline":     "\u23F1",   # ⏱
    "download":     "\u21E9",   # ⇩
    "document":     "\u2398",   # ⎘ (fallback-safe document mark)
    "broken_image": "\u2716",   # ✖
    "star":         "\u2605",   # ★
    "settings":     "\u2699",   # ⚙
}


class SidebarWidget(QFrame):
    """Left navigation sidebar with grouped page entries.

    Collapsible: toggle between full (220px) and icon-only (50px) modes.
    """

    page_selected = Signal(str)  # Emits page_id

    EXPANDED_WIDTH = 220
    COLLAPSED_WIDTH = 48

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self._expanded = True
        self.setFixedWidth(self.EXPANDED_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # App title row with collapse button
        title_frame = QFrame()
        title_frame.setObjectName("sidebarTitleFrame")
        title_frame.setFixedHeight(48)
        title_layout = QHBoxLayout(title_frame)
        title_layout.setContentsMargins(12, 0, 4, 0)
        title_layout.setSpacing(4)
        self._title_label = QLabel("WA Forensic")
        self._title_label.setObjectName("sidebarTitle")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        self._title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        title_layout.addWidget(self._title_label, 1)

        # Collapse/expand toggle button — SOLID white bg, large, always visible
        self._toggle_btn = QPushButton("\u25C0 Hide")  # ◀ Hide (starts expanded)
        self._toggle_btn.setFixedSize(60, 30)
        self._toggle_btn.setCursor(Qt.PointingHandCursor)
        self._toggle_btn.setToolTip("Collapse sidebar (Ctrl+B)")
        self._toggle_btn.setStyleSheet(
            "QPushButton { border: 2px solid #ffffff; border-radius: 6px;"
            " font-size: 11px; font-weight: bold;"
            " color: #00695c;"
            " background: #ffffff;"
            " padding: 2px 6px; }"
            "QPushButton:hover { background: #e0f2f1;"
            " color: #004d40; border-color: #e0f2f1; }"
        )
        self._toggle_btn.clicked.connect(self.toggle_collapsed)
        title_layout.addWidget(self._toggle_btn)
        layout.addWidget(title_frame)

        # Navigation list
        self._list = QListWidget()
        self._list.setObjectName("sidebarList")
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self._list)

        self._page_id_for_row: dict[int, str] = {}  # QListWidget row -> page_id
        self._row_for_page_id: dict[str, int] = {}  # page_id -> QListWidget row
        self._build_items()

        # Start in expanded state so navigation is visible
        self._expanded = True
        self.setFixedWidth(self.EXPANDED_WIDTH)
        self._toggle_btn.setText("\u25C0 Hide")  # ◀ Hide
        self._toggle_btn.setToolTip("Collapse sidebar (Ctrl+B)")
        self._title_label.show()
        self._rebuild_labels(collapsed=False)

        self._list.currentRowChanged.connect(self._on_row_changed)

    def _build_items(self) -> None:
        row = 0
        for page_id, label, icon_name, is_header in PAGES:
            if is_header:
                if label:  # Non-empty section header
                    item = QListWidgetItem(f"    {label.upper()}")
                    item.setFlags(Qt.NoItemFlags)
                    header_font = QFont()
                    header_font.setPointSize(8)
                    header_font.setBold(True)
                    item.setFont(header_font)
                    tm = ThemeManager.get()
                    if tm.is_light:
                        item.setForeground(QColor(70, 80, 90))
                    else:
                        item.setForeground(QColor(255, 255, 255, 80))
                    item.setSizeHint(QSize(250, 32))
                    self._list.addItem(item)
                    row += 1
                continue

            icon = ICON_MAP.get(icon_name, "")
            item = QListWidgetItem(f"  {icon}  {label}")
            item.setData(Qt.UserRole, page_id)
            item_font = QFont()
            item_font.setPointSize(10)
            item.setFont(item_font)
            item.setSizeHint(QSize(250, 38))
            self._list.addItem(item)

            self._page_id_for_row[row] = page_id
            self._row_for_page_id[page_id] = row
            row += 1

    def _on_row_changed(self, row: int) -> None:
        page_id = self._page_id_for_row.get(row)
        if page_id:
            self.page_selected.emit(page_id)

    def select_page(self, page_id: str) -> None:
        """Programmatically select a page by ID."""
        row = self._row_for_page_id.get(page_id)
        if row is not None:
            self._list.setCurrentRow(row)

    def toggle_collapsed(self) -> None:
        """Toggle between expanded and collapsed sidebar."""
        self._expanded = not self._expanded
        if self._expanded:
            self.setFixedWidth(self.EXPANDED_WIDTH)
            self._toggle_btn.setText("\u25C0 Hide")  # ◀ Hide
            self._toggle_btn.setFixedSize(60, 30)
            self._toggle_btn.setToolTip("Collapse sidebar (Ctrl+B)")
            self._title_label.show()
            # Restore full labels
            self._rebuild_labels(collapsed=False)
        else:
            self.setFixedWidth(self.COLLAPSED_WIDTH)
            self._toggle_btn.setText("\u25B6")  # ▶
            self._toggle_btn.setFixedSize(36, 30)
            self._toggle_btn.setToolTip("Expand sidebar (Ctrl+B)")
            self._title_label.hide()
            # Show only icons
            self._rebuild_labels(collapsed=True)

    def _rebuild_labels(self, collapsed: bool) -> None:
        """Update list item labels for collapsed/expanded mode."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            page_id = item.data(Qt.UserRole)
            if page_id is None:
                # Section header
                item.setHidden(collapsed)
                continue
            if collapsed:
                # Find the icon for this page_id
                icon = ""
                for pid, label, icon_name, is_header in PAGES:
                    if pid == page_id and icon_name:
                        icon = ICON_MAP.get(icon_name, "")
                        break
                item.setText(f" {icon}")
                item.setSizeHint(QSize(self.COLLAPSED_WIDTH, 36))
                item.setTextAlignment(Qt.AlignCenter)
            else:
                icon = ""
                label = ""
                for pid, lbl, icon_name, is_header in PAGES:
                    if pid == page_id:
                        if icon_name:
                            icon = ICON_MAP.get(icon_name, "")
                        label = lbl
                        break
                # Apply count badge if available
                counts = getattr(self, '_counts', {})
                count = counts.get(page_id, -1)
                if count >= 0:
                    if count >= 1000:
                        count_str = f"{count / 1000:.1f}K"
                    else:
                        count_str = str(count)
                    item.setText(f"  {icon}  {label}  ({count_str})" if count > 0 else f"  {icon}  {label}")
                else:
                    item.setText(f"  {icon}  {label}")
                item.setSizeHint(QSize(self.EXPANDED_WIDTH, 38))
                item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

    def update_counts(self, counts: dict[str, int]) -> None:
        """Update sidebar items with count badges.

        Args:
            counts: Mapping of page_id → count (e.g. {"locations": 179, "calls": 5900})
        """
        self._counts = counts
        if self._expanded:
            self._apply_counts()

    def _apply_counts(self) -> None:
        """Apply count badges to expanded sidebar labels."""
        counts = getattr(self, '_counts', {})
        if not counts:
            return
        for i in range(self._list.count()):
            item = self._list.item(i)
            page_id = item.data(Qt.UserRole)
            if page_id is None or page_id not in counts:
                continue
            count = counts[page_id]
            # Find original label
            icon = ""
            label = ""
            for pid, lbl, icon_name, is_header in PAGES:
                if pid == page_id:
                    if icon_name:
                        icon = ICON_MAP.get(icon_name, "")
                    label = lbl
                    break
            if count > 0:
                if count >= 1000:
                    count_str = f"{count / 1000:.1f}K"
                else:
                    count_str = str(count)
                item.setText(f"  {icon}  {label}  ({count_str})")
            else:
                item.setText(f"  {icon}  {label}")
            # Dim items with 0 count
            if count == 0:
                tm = ThemeManager.get()
                item.setForeground(QColor(150, 150, 150, 120) if tm.is_dark else QColor(180, 180, 180))

    @property
    def is_collapsed(self) -> bool:
        return not self._expanded
