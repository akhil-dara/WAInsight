"""
Theme manager -- persists light/dark preference and provides colour helpers.

Usage:
    from app.services.theme_manager import ThemeManager
    tm = ThemeManager.get()
    if tm.is_dark:
        ...
"""

from __future__ import annotations

from PySide6.QtCore import QSettings


class ThemeManager:
    """Simple singleton that stores the current theme name in QSettings."""

    LIGHT = "light"
    DARK = "dark"

    _instance: ThemeManager | None = None

    @classmethod
    def get(cls) -> ThemeManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._settings = QSettings()
        self._theme: str = self._settings.value("app/theme", self.LIGHT)

    # ---- properties ------------------------------------------------

    @property
    def theme(self) -> str:
        return self._theme

    @theme.setter
    def theme(self, value: str) -> None:
        self._theme = value
        self._settings.setValue("app/theme", value)

    @property
    def is_dark(self) -> bool:
        return self._theme == self.DARK

    @property
    def is_light(self) -> bool:
        return self._theme == self.LIGHT

    # ---- Qt material theme file ------------------------------------

    @property
    def qt_material_theme(self) -> str:
        return "dark_teal.xml" if self.is_dark else "light_teal.xml"

    @property
    def qss_filename(self) -> str:
        return "dark.qss" if self.is_dark else "light.qss"

    # ---- common inline style helpers --------------------------------

    def tab_bar_style(self) -> str:
        """Stylesheet for conversation type tab bar buttons."""
        if self.is_dark:
            return """
                QPushButton { padding: 6px 16px; border: none; border-radius: 0;
                              font-size: 12px; color: rgba(255,255,255,0.55);
                              background: transparent;
                              border-bottom: 2px solid transparent; }
                QPushButton:checked { color: #00bcd4; font-weight: bold;
                                      border-bottom: 2px solid #00bcd4; }
                QPushButton:hover:!checked { color: rgba(255,255,255,0.8);
                                             border-bottom: 2px solid rgba(255,255,255,0.2); }
            """
        return """
            QPushButton { padding: 6px 16px; border: none; border-radius: 0;
                          font-size: 12px; color: #666;
                          background: transparent;
                          border-bottom: 2px solid transparent; }
            QPushButton:checked { color: #00695c; font-weight: bold;
                                  border-bottom: 2px solid #00897b; }
            QPushButton:hover:!checked { color: #333;
                                         border-bottom: 2px solid #d0d7de; }
        """

    def filter_btn_style(self) -> str:
        """Stylesheet for checkable filter pill buttons."""
        if self.is_dark:
            return """
                QPushButton { padding: 4px 14px; border-radius: 14px;
                              border: 1px solid rgba(255,255,255,0.12); font-size: 11px;
                              color: rgba(255,255,255,0.7); background: transparent; }
                QPushButton:checked { background: rgba(0,188,212,0.2);
                                      border-color: #00bcd4; color: #00bcd4; font-weight: bold; }
                QPushButton:hover:!checked { background: rgba(255,255,255,0.05); }
            """
        return """
            QPushButton { padding: 4px 14px; border-radius: 14px;
                          border: 1px solid #d0d7de; font-size: 11px;
                          color: #555; background: #fff; }
            QPushButton:checked { background: rgba(0,137,123,0.12);
                                  border-color: #00897b; color: #00695c; font-weight: bold; }
            QPushButton:hover:!checked { background: #f6f8fa; border-color: #b0bec5; }
        """

    def context_menu_style(self) -> str:
        """Stylesheet for context menus."""
        if self.is_dark:
            return """
                QMenu { background: #1a2730; border: 1px solid #2a3a44; padding: 4px; }
                QMenu::item { padding: 6px 24px; color: #e0e0e0; }
                QMenu::item:selected { background: rgba(0,188,212,0.2); }
                QMenu::separator { height: 1px; background: #2a3a44; margin: 4px 8px; }
            """
        return """
            QMenu { background: #ffffff; border: 1px solid #d0d7de; padding: 4px;
                    border-radius: 8px; }
            QMenu::item { padding: 6px 24px; color: #1b1b1b; }
            QMenu::item:selected { background: rgba(0,137,123,0.1); color: #00695c; }
            QMenu::separator { height: 1px; background: #e8eaed; margin: 4px 8px; }
        """

    def search_box_style(self) -> str:
        """Stylesheet for search input boxes."""
        if self.is_dark:
            return ""  # Use QSS defaults
        return """
            QLineEdit { padding: 8px 14px; border-radius: 8px;
                        border: 1px solid #d0d7de; background: #fff;
                        color: #1b1b1b; font-size: 12px; }
            QLineEdit:focus { border-color: #00897b; }
            QLineEdit::placeholder { color: #8696a0; }
        """

    def header_label_style(self) -> str:
        """Stylesheet for count labels next to page titles."""
        if self.is_dark:
            return "color: rgba(255,255,255,0.5); font-size: 12px;"
        return "color: #667781; font-size: 12px;"

    def hint_label_style(self) -> str:
        """Stylesheet for hint labels below toolbars."""
        if self.is_dark:
            return "color: rgba(255,255,255,0.3); font-size: 10px;"
        return "color: #667781; font-size: 10px;"

    def view_toggle_btn_style(self) -> str:
        """Stylesheet for view toggle buttons (list/table)."""
        if self.is_dark:
            return """
                QPushButton { padding: 2px 10px; border-radius: 4px;
                              border: 1px solid rgba(255,255,255,0.1); font-size: 10px;
                              color: rgba(255,255,255,0.6); }
                QPushButton:checked { background: rgba(0,188,212,0.15); border-color: #00bcd4;
                                      color: #00bcd4; }
            """
        return """
            QPushButton { padding: 2px 10px; border-radius: 4px;
                          border: 1px solid #d0d7de; font-size: 10px;
                          color: #555; background: #fff; }
            QPushButton:checked { background: rgba(0,137,123,0.1); border-color: #00897b;
                                  color: #00695c; }
        """

    def list_view_style(self) -> str:
        """Stylesheet for WhatsApp-style list views."""
        if self.is_dark:
            return """
                QListView { background-color: transparent; border: none; }
                QListView::item { border: none; }
                QListView::item:selected { background: rgba(0,188,212,0.08); }
            """
        return """
            QListView { background-color: #ffffff; border: none; }
            QListView::item { border: none; }
            QListView::item:selected { background: rgba(0,137,123,0.08); }
            QListView::item:hover { background: #f5f6f6; }
        """

    def export_btn_style(self) -> str:
        """Stylesheet for export buttons."""
        if self.is_dark:
            return """
                QPushButton { padding: 4px 14px; border-radius: 14px;
                              border: 1px solid rgba(0,188,212,0.4);
                              background: rgba(0,188,212,0.1); color: #00bcd4;
                              font-size: 11px; font-weight: bold; }
                QPushButton:hover { background: rgba(0,188,212,0.2); }
            """
        return """
            QPushButton { padding: 4px 14px; border-radius: 14px;
                          border: 1px solid rgba(0,137,123,0.4);
                          background: rgba(0,137,123,0.08); color: #00695c;
                          font-size: 11px; font-weight: bold; }
            QPushButton:hover { background: rgba(0,137,123,0.15); }
        """

    def stat_frame_style(self) -> str:
        """Stylesheet for stat card frames."""
        if self.is_dark:
            return """
                QFrame { background: rgba(255,255,255,0.04);
                         border-radius: 8px; padding: 8px 16px; }
            """
        return """
            QFrame { background: #fff; border: 1px solid #e8eaed;
                     border-radius: 8px; padding: 8px 16px; }
        """

    def stat_label_style(self) -> str:
        """Stylesheet for stat label text."""
        if self.is_dark:
            return "color: rgba(255,255,255,0.5); font-size: 10px;"
        return "color: #667781; font-size: 10px;"

    def stat_value_style(self) -> str:
        """Stylesheet for stat value text."""
        if self.is_dark:
            return "color: #00bcd4; font-size: 14px; font-weight: bold;"
        return "color: #00897b; font-size: 14px; font-weight: bold;"

    def detail_panel_style(self, object_name: str) -> str:
        """Stylesheet for detail panels (calls, events)."""
        if self.is_dark:
            return f"""
                QFrame#{object_name} {{
                    background: #1a252c;
                    border: 1px solid rgba(255,255,255,0.06);
                    border-radius: 8px;
                }}
            """
        return f"""
            QFrame#{object_name} {{
                background: #ffffff;
                border: 1px solid #e0e3e7;
                border-radius: 8px;
            }}
        """

    def detail_title_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef; padding-bottom: 4px;"
        return "color: #1b1b1b; padding-bottom: 4px;"

    def detail_section_header_style(self) -> str:
        if self.is_dark:
            return "color: #00bcd4; font-size: 11px; font-weight: bold; padding: 6px 0 2px 0;"
        return "color: #00897b; font-size: 11px; font-weight: bold; padding: 6px 0 2px 0;"

    def detail_label_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.50); font-size: 10px;"
        return "color: #667781; font-size: 10px;"

    def detail_value_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef; font-size: 12px;"
        return "color: #1b1b1b; font-size: 12px;"

    def detail_value_accent_style(self) -> str:
        if self.is_dark:
            return "color: #00bcd4; font-size: 12px; font-weight: bold;"
        return "color: #00897b; font-size: 12px; font-weight: bold;"

    def detail_separator_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.06);"
        return "color: #d0d7de;"

    def detail_placeholder_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.25); font-size: 12px; padding: 40px;"
        return "color: #8696a0; font-size: 12px; padding: 40px;"

    def detail_info_text_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef; font-size: 12px;"
        return "color: #1b1b1b; font-size: 12px;"

    def detail_participants_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef; font-size: 11px; padding: 2px 0 4px 0;"
        return "color: #1b1b1b; font-size: 11px; padding: 2px 0 4px 0;"

    def splitter_handle_style(self) -> str:
        if self.is_dark:
            return "QSplitter::handle { background: rgba(255,255,255,0.06); }"
        return "QSplitter::handle { background: #e0e3e7; }"

    def chat_header_style(self) -> str:
        """Chat viewer header bar."""
        if self.is_dark:
            return """
                QFrame#chatHeader {
                    background-color: #1f2c34;
                    border-bottom: 1px solid rgba(255,255,255,0.06);
                }
            """
        return """
            QFrame#chatHeader {
                background-color: #f0f2f5;
                border-bottom: 1px solid #d0d7de;
            }
        """

    def chat_back_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: rgba(255,255,255,0.08);
                              border-radius: 18px; font-size: 20px;
                              border: none; color: #e9edef; }
                QPushButton:hover { background: rgba(255,255,255,0.16); }
            """
        return """
            QPushButton { background: rgba(0,0,0,0.08);
                          border-radius: 18px; font-size: 20px;
                          border: none; color: #3b4a54; }
            QPushButton:hover { background: rgba(0,0,0,0.14); }
        """

    def chat_title_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef;"
        return "color: #111b21;"

    def chat_info_label_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.45); font-size: 9px;"
        return "color: #667781; font-size: 9px;"

    def chat_hdr_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: transparent; border: none;
                              font-size: 13px; color: #aebac1; padding: 2px; }
                QPushButton:hover { color: #e9edef; }
            """
        return """
            QPushButton { background: transparent; border: none;
                          font-size: 13px; color: #667781; padding: 2px; }
            QPushButton:hover { color: #111b21; }
        """

    def chat_search_bar_style(self) -> str:
        if self.is_dark:
            return "QFrame { background: #1f2c34; border-bottom: 1px solid rgba(255,255,255,0.06); }"
        return "QFrame { background: #f0f2f5; border-bottom: 1px solid #d0d7de; }"

    def chat_search_input_style(self) -> str:
        if self.is_dark:
            return """
                QLineEdit { background: rgba(255,255,255,0.06); border: none;
                            border-radius: 14px; padding: 0 12px;
                            color: #e9edef; font-size: 11px; }
            """
        return """
            QLineEdit { background: #ffffff; border: 1px solid #d0d7de;
                        border-radius: 14px; padding: 0 12px;
                        color: #111b21; font-size: 11px; }
            QLineEdit:focus { border-color: #00897b; }
        """

    def chat_date_edit_style(self) -> str:
        if self.is_dark:
            return (
                "QDateEdit { background: rgba(255,255,255,0.06); border: none; "
                "border-radius: 4px; padding: 0 6px; color: #e9edef; font-size: 10px; }"
            )
        return (
            "QDateEdit { background: #fff; border: 1px solid #d0d7de; "
            "border-radius: 4px; padding: 0 6px; color: #111b21; font-size: 10px; }"
        )

    def chat_date_apply_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: rgba(0,188,212,0.2); border: 1px solid #00bcd4;
                              border-radius: 4px; color: #00bcd4; font-size: 10px; }
                QPushButton:hover { background: rgba(0,188,212,0.35); }
            """
        return """
            QPushButton { background: rgba(0,137,123,0.1); border: 1px solid #00897b;
                          border-radius: 4px; color: #00695c; font-size: 10px; }
            QPushButton:hover { background: rgba(0,137,123,0.2); }
        """

    def chat_date_clear_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: rgba(255,255,255,0.06); border: none;
                              border-radius: 4px; color: #aebac1; font-size: 10px; }
                QPushButton:hover { background: rgba(255,255,255,0.1); }
            """
        return """
            QPushButton { background: #f0f2f5; border: 1px solid #d0d7de;
                          border-radius: 4px; color: #667781; font-size: 10px; }
            QPushButton:hover { background: #e8eaed; }
        """

    def chat_list_style(self) -> str:
        if self.is_dark:
            return """
                QListView { background-color: #0b141a; border: none; padding: 4px 0px; }
                QListView::item { border: none; padding: 0px; }
                QListView::item:selected { background: rgba(0,188,212,0.06); }
            """
        return """
            QListView { background-color: #efeae2; border: none; padding: 4px 0px; }
            QListView::item { border: none; padding: 0px; }
            QListView::item:selected { background: rgba(0,137,123,0.06); }
        """

    def chat_date_overlay_style(self) -> str:
        if self.is_dark:
            return """
                QLabel { background: rgba(18, 28, 33, 0.9); border-radius: 11px;
                         padding: 0 12px; color: rgba(180, 195, 205, 0.95);
                         font-size: 9px; font-weight: bold; }
            """
        return """
            QLabel { background: rgba(255, 255, 255, 0.95); border-radius: 11px;
                     padding: 0 12px; color: #667781;
                     font-size: 9px; font-weight: bold;
                     border: 1px solid #e0e3e7; }
        """

    def chat_debug_panel_style(self) -> str:
        if self.is_dark:
            return """
                QFrame { background: #111b21;
                         border-left: 1px solid rgba(255,255,255,0.08); }
            """
        return """
            QFrame { background: #f8f9fa;
                     border-left: 1px solid #d0d7de; }
        """

    def chat_debug_header_style(self) -> str:
        if self.is_dark:
            return "color: #00bcd4; font-size: 11px; font-weight: bold;"
        return "color: #00897b; font-size: 11px; font-weight: bold;"

    def chat_debug_text_style(self) -> str:
        if self.is_dark:
            return """
                QTextEdit { background: rgba(255,255,255,0.02);
                            border: 1px solid rgba(255,255,255,0.06);
                            border-radius: 4px; padding: 4px;
                            font-family: Consolas, monospace; font-size: 9px;
                            color: #e9edef; }
            """
        return """
            QTextEdit { background: #fff;
                        border: 1px solid #d0d7de;
                        border-radius: 4px; padding: 4px;
                        font-family: Consolas, monospace; font-size: 9px;
                        color: #1b1b1b; }
        """

    def chat_detail_bar_style(self) -> str:
        if self.is_dark:
            return """
                QFrame { background: #1f2c34;
                         border-top: 1px solid rgba(255,255,255,0.06); }
            """
        return """
            QFrame { background: #f0f2f5;
                     border-top: 1px solid #d0d7de; }
        """

    def chat_detail_label_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.4); font-size: 9px;"
        return "color: #667781; font-size: 9px;"

    # ---- Contact detail page helpers --------------------------------

    def contact_detail_header_style(self) -> str:
        if self.is_dark:
            return """
                QFrame#contactDetailHeader {
                    background-color: #1f2c34;
                    border-bottom: 1px solid rgba(255,255,255,0.06);
                }
            """
        return """
            QFrame#contactDetailHeader {
                background-color: #f0f2f5;
                border-bottom: 1px solid #d0d7de;
            }
        """

    def contact_detail_scroll_style(self) -> str:
        if self.is_dark:
            return """
                QScrollArea { background-color: #0b141a; border: none; }
                QScrollBar:vertical { background: #0b141a; width: 8px; border: none; }
                QScrollBar::handle:vertical { background: rgba(255,255,255,0.12);
                    border-radius: 4px; min-height: 30px; }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            """
        return """
            QScrollArea { background-color: #fafafa; border: none; }
            QScrollBar:vertical { background: #fafafa; width: 8px; border: none; }
            QScrollBar::handle:vertical { background: rgba(0,0,0,0.12);
                border-radius: 4px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """

    def contact_detail_content_bg(self) -> str:
        if self.is_dark:
            return "background-color: #0b141a;"
        return "background-color: #fafafa;"

    def contact_detail_name_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef;"
        return "color: #111b21;"

    def contact_detail_phone_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.55); font-size: 11px;"
        return "color: #667781; font-size: 11px;"

    def contact_detail_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: rgba(255,255,255,0.06);
                              border-radius: 18px; font-size: 18px;
                              border: none; color: #e9edef; }
                QPushButton:hover { background: rgba(255,255,255,0.12); }
            """
        return """
            QPushButton { background: rgba(0,0,0,0.05);
                          border-radius: 18px; font-size: 18px;
                          border: none; color: #3b4a54; }
            QPushButton:hover { background: rgba(0,0,0,0.1); }
        """

    def contact_detail_copy_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton { background: rgba(255,255,255,0.06);
                              border-radius: 18px; font-size: 16px;
                              border: none; color: #aebac1; }
                QPushButton:hover { background: rgba(255,255,255,0.12); color: #e9edef; }
            """
        return """
            QPushButton { background: rgba(0,0,0,0.05);
                          border-radius: 18px; font-size: 16px;
                          border: none; color: #667781; }
            QPushButton:hover { background: rgba(0,0,0,0.1); color: #111b21; }
        """

    def contact_detail_stats_bar_style(self) -> str:
        if self.is_dark:
            return """
                QFrame#contactStatsBar {
                    background: rgba(0,229,255,0.06);
                    border: 1px solid rgba(0,229,255,0.15);
                    border-radius: 8px;
                }
            """
        return """
            QFrame#contactStatsBar {
                background: rgba(0,137,123,0.06);
                border: 1px solid rgba(0,137,123,0.15);
                border-radius: 8px;
            }
        """

    def contact_detail_stat_value_style(self) -> str:
        if self.is_dark:
            return "color: #00e5ff;"
        return "color: #00897b;"

    def contact_detail_stat_label_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.5); font-size: 10px;"
        return "color: #667781; font-size: 10px;"

    def contact_detail_section_frame_style(self) -> str:
        if self.is_dark:
            return """
                QFrame { background: rgba(255,255,255,0.03);
                         border: 1px solid rgba(255,255,255,0.06);
                         border-radius: 8px; }
            """
        return """
            QFrame { background: #ffffff;
                     border: 1px solid #e0e3e7;
                     border-radius: 8px; }
        """

    def contact_detail_row_label_style(self) -> str:
        if self.is_dark:
            return "color: rgba(255,255,255,0.5); font-size: 11px;"
        return "color: #667781; font-size: 11px;"

    def contact_detail_row_value_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef; font-size: 11px;"
        return "color: #111b21; font-size: 11px;"

    def contact_detail_direct_btn_style(self) -> str:
        if self.is_dark:
            return """
                QPushButton {
                    background: rgba(0,229,255,0.12); border: 1px solid rgba(0,229,255,0.35);
                    border-radius: 6px; color: #00e5ff;
                    font-size: 12px; font-weight: bold; padding: 0 24px;
                }
                QPushButton:hover { background: rgba(0,229,255,0.2); border-color: #00e5ff; }
                QPushButton:disabled { background: rgba(255,255,255,0.03);
                    border-color: rgba(255,255,255,0.08); color: rgba(255,255,255,0.2); }
            """
        return """
            QPushButton {
                background: rgba(0,137,123,0.1); border: 1px solid rgba(0,137,123,0.35);
                border-radius: 6px; color: #00695c;
                font-size: 12px; font-weight: bold; padding: 0 24px;
            }
            QPushButton:hover { background: rgba(0,137,123,0.2); border-color: #00897b; }
            QPushButton:disabled { background: #f5f5f5;
                border-color: #e0e3e7; color: #a0a0a0; }
        """

    def contact_detail_groups_header_style(self) -> str:
        if self.is_dark:
            return "color: #e9edef;"
        return "color: #111b21;"

    def contact_detail_groups_list_style(self) -> str:
        if self.is_dark:
            return """
                QListWidget { background-color: #0b141a;
                    border: 1px solid rgba(255,255,255,0.06);
                    border-radius: 8px; color: #e9edef; font-size: 12px; outline: none; }
                QListWidget::item { padding: 10px 14px;
                    border-bottom: 1px solid rgba(255,255,255,0.03); }
                QListWidget::item:selected { background: rgba(0,229,255,0.1); }
                QListWidget::item:hover { background: rgba(255,255,255,0.03); }
                QScrollBar:vertical { background: #0b141a; width: 8px; border: none; }
                QScrollBar::handle:vertical { background: rgba(255,255,255,0.12);
                    border-radius: 4px; min-height: 30px; }
                QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
            """
        return """
            QListWidget { background-color: #ffffff;
                border: 1px solid #e0e3e7;
                border-radius: 8px; color: #1b1b1b; font-size: 12px; outline: none; }
            QListWidget::item { padding: 10px 14px;
                border-bottom: 1px solid #f0f2f5; }
            QListWidget::item:selected { background: rgba(0,137,123,0.08); }
            QListWidget::item:hover { background: #f5f6f6; }
            QScrollBar:vertical { background: #fff; width: 8px; border: none; }
            QScrollBar::handle:vertical { background: rgba(0,0,0,0.12);
                border-radius: 4px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
        """

    def contact_detail_sep_style(self) -> str:
        if self.is_dark:
            return "background: rgba(255,255,255,0.08);"
        return "background: #e0e3e7;"

    # ---- Accent color for the event detail panel highlight ----------

    def accent_highlight_color(self) -> str:
        """CSS color for accent highlights."""
        return "#00e5ff" if self.is_dark else "#00897b"
