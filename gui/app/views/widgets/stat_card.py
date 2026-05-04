"""
Reusable stat card widget for dashboard overview numbers.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from app.services.theme_manager import ThemeManager


class StatCard(QFrame):
    """Displays a single statistic with label and value."""

    def __init__(
        self,
        label: str,
        value: str = "0",
        accent_color: str = "#00bcd4",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setFixedHeight(100)
        self.setMinimumWidth(180)

        tm = ThemeManager.get()
        if tm.is_light:
            self.setStyleSheet(f"""
                QFrame#statCard {{
                    background-color: #ffffff;
                    border: 1px solid #e8eaed;
                    border-left: 3px solid {accent_color};
                    border-radius: 8px;
                    padding: 12px;
                }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame#statCard {{
                    background-color: rgba(255, 255, 255, 0.05);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-left: 3px solid {accent_color};
                    border-radius: 8px;
                    padding: 12px;
                }}
            """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)

        self._label = QLabel(label)
        self._label.setObjectName("statCardLabel")
        label_font = QFont()
        label_font.setPointSize(9)
        self._label.setFont(label_font)
        self._label.setStyleSheet(
            "color: #667781;" if tm.is_light else "color: rgba(255,255,255,0.6);"
        )

        self._value = QLabel(value)
        self._value.setObjectName("statCardValue")
        value_font = QFont()
        value_font.setPointSize(22)
        value_font.setBold(True)
        self._value.setFont(value_font)
        self._value.setStyleSheet(
            "color: #111b21;" if tm.is_light else "color: #e9edef;"
        )

        layout.addWidget(self._label)
        layout.addWidget(self._value)
        layout.addStretch()

    def set_value(self, value: str) -> None:
        self._value.setText(value)

    def set_label(self, label: str) -> None:
        self._label.setText(label)
