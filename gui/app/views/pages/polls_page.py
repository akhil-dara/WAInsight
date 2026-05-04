"""
Polls page -- poll questions with options and vote counts.
Click a poll to see its options in a detail panel.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QSplitter, QTableView, QTextEdit, QVBoxLayout, QWidget,
)

from app.models.base_table_model import BaseLazyTableModel
from app.services.database import Database


class PollsModel(BaseLazyTableModel):
    _columns = [
        ("poll_question", "Question"),
        ("option_count", "Options"),
        ("total_votes", "Total Votes"),
        ("selectable_count", "Max Selections"),
    ]

    _base_sql = """
        SELECT COALESCE(m.text_content, 'Poll #' || p.id) AS poll_question,
               (SELECT COUNT(*) FROM poll_option po WHERE po.poll_id = p.id) AS option_count,
               (SELECT COUNT(*) FROM poll_vote pv WHERE pv.poll_id = p.id) AS total_votes,
               p.selectable_count, p.id,
               p.message_id, m.conversation_id
        FROM poll p
        LEFT JOIN message m ON m.id = p.message_id
    """
    _count_sql = "SELECT COUNT(*) FROM poll"
    _default_order = "p.id DESC"

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row_data = self._data[index.row()]
        col = index.column()

        # Raw row layout: poll_question(0), option_count(1),
        # total_votes(2), selectable_count(3), id(4),
        # message_id(5), conversation_id(6)

        if role == Qt.DisplayRole:
            raw = row_data[col]
            if col == 0:  # Question
                return str(raw) if raw else "Untitled Poll"
            if col in (1, 2, 3):  # Numeric columns
                return str(raw) if raw is not None else "0"
            return str(raw) if raw is not None else ""

        if role == Qt.TextAlignmentRole and col in (1, 2, 3):
            return Qt.AlignCenter

        if role == Qt.UserRole:
            # Return poll_id for detail lookup
            return row_data[4]
        if role == Qt.UserRole + 1:
            return row_data[5] if len(row_data) > 5 else None  # message_id
        if role == Qt.UserRole + 2:
            return row_data[6] if len(row_data) > 6 else None  # conversation_id

        return None


class PollsPage(QWidget):
    navigate_to_message = Signal(int, int)  # conv_id, msg_id

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        title = QLabel("Polls")
        f = QFont(); f.setPointSize(18); f.setBold(True); title.setFont(f)
        header.addWidget(title)
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #78909c; font-size: 12px;")
        header.addWidget(self._count_label)
        header.addStretch()
        layout.addLayout(header)

        hint = QLabel("Click headers to sort  |  Click a poll to view its options below  |  Double-click to navigate to message")
        hint.setStyleSheet("color: #90a4ae; font-size: 10px;")
        layout.addWidget(hint)

        # Splitter: table on top, detail panel on bottom
        splitter = QSplitter(Qt.Vertical)

        # Table
        self._model = PollsModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setDefaultSectionSize(30)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in [(1, 90), (2, 100), (3, 120)]:
            hdr.setSectionResizeMode(col, QHeaderView.Fixed)
            hdr.resizeSection(col, w)
        splitter.addWidget(self._table)

        # Detail panel
        detail_frame = QFrame()
        detail_frame.setStyleSheet("""
            QFrame { background: rgba(128,128,128,0.06);
                     border-radius: 8px; border: 1px solid rgba(128,128,128,0.12); }
        """)
        df_layout = QVBoxLayout(detail_frame)
        df_layout.setContentsMargins(16, 12, 16, 12)
        df_layout.setSpacing(6)

        self._detail_title = QLabel("Poll Options")
        self._detail_title.setStyleSheet(
            "color: #607d8b; font-size: 11px; font-weight: bold;"
        )
        df_layout.addWidget(self._detail_title)

        self._detail_text = QTextEdit()
        self._detail_text.setReadOnly(True)
        self._detail_text.setPlaceholderText("Select a poll above to view its options and votes")
        self._detail_text.setStyleSheet("""
            QTextEdit { background: transparent; border: none;
                        color: #546e7a; font-size: 12px; }
        """)
        self._detail_text.setMaximumHeight(180)
        df_layout.addWidget(self._detail_text)
        splitter.addWidget(detail_frame)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

        # Connect click signal
        self._table.clicked.connect(self._on_poll_clicked)
        self._table.doubleClicked.connect(self._on_poll_double_clicked)

        QTimer.singleShot(50, self._apply)

    def _apply(self):
        self._model.load()
        self._count_label.setText(f"{self._model.total_rows:,} polls")

    def _on_poll_clicked(self, index: QModelIndex):
        poll_id = self._model.data(index, Qt.UserRole)
        if poll_id is None:
            return

        # Get poll question for title
        question = self._model.data(
            self._model.index(index.row(), 0), Qt.DisplayRole
        )
        self._detail_title.setText(f"{question}")

        # Fetch options from database
        db = Database.get()
        options = db.fetchall("""
            SELECT po.option_name, po.vote_total, po.option_index
            FROM poll_option po
            WHERE po.poll_id = ?
            ORDER BY po.option_index ASC
        """, (poll_id,))

        if not options:
            self._detail_text.setPlainText("No options found for this poll.")
            return

        # Calculate total votes for percentage bar
        total_votes = sum((row[1] or 0) for row in options)

        lines = []
        for row in options:
            name = row[0] or "Unnamed option"
            votes = row[1] or 0
            pct = (votes / total_votes * 100) if total_votes > 0 else 0
            bar_len = int(pct / 5)  # 20 chars max
            bar = "\u2588" * bar_len + "\u2591" * (20 - bar_len)
            lines.append(f"  {name}")
            lines.append(f"    {bar}  {votes} votes ({pct:.1f}%)")
            lines.append("")

        self._detail_text.setPlainText("\n".join(lines))

    def _on_poll_double_clicked(self, index: QModelIndex):
        """Navigate to the poll message in the chat viewer."""
        msg_id = self._model.data(index, Qt.UserRole + 1)
        conv_id = self._model.data(index, Qt.UserRole + 2)
        if conv_id and msg_id:
            self.navigate_to_message.emit(conv_id, msg_id)
