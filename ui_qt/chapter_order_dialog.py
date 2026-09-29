"""依章號重排（缺章檢查結果裡「順序錯亂」的「依章號重排」）。

列出放錯位置的章：哪一章、目前在哪、要搬到哪；勾選的才搬，搬的是整章（標題到下一個目錄項目前）。
判斷規則見 core/collection.misplaced_chapters。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
)

from . import i18n
from .sortable_table import PreviewTable, make_item, setup_columns
from .theme import active_tokens
from .widgets import ContextPreview, dialog_frame, size_dialog


class ChapterOrderDialog(QDialog):
    def __init__(self, raw_lines: list, moves: list, parent=None):
        """moves：[{"title", "start", "end", "destination", "now", "to"}]，now／to 是寫給使用者看的位置
        （「第 7 章後面」），start／end／destination 是 core.collection.move_chapter_blocks 用的行號。"""
        super().__init__(parent)
        self.setWindowTitle("依章號重排")
        size_dialog(self, 760, 560)
        self._raw_lines = raw_lines
        self._moves = moves
        self._checked = set(range(len(moves)))

        root, footer = dialog_frame(self, intro="放錯位置的章；勾選要搬的，整章搬到章號該在的位置。")
        root.setSpacing(12)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for label, handler in (("全選", self._check_all), ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["移動", "章", "目前在", "移到"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 56, 1: 260, 2: 150})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table), 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.move_button = buttons.addButton("移動已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.move_button.setObjectName("primary")
        close_button.clicked.connect(self.reject)
        self.move_button.clicked.connect(self.accept)
        footer.addWidget(buttons)
        self._refresh()

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._moves))
        for row, move in enumerate(self._moves):
            check_item = make_item("", row)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(Qt.CheckState.Checked if row in self._checked else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check_item)
            self.table.setItem(row, 1, make_item(move["title"], row))
            self.table.setItem(row, 2, make_item(i18n.T(move["now"]), row))
            self.table.setItem(row, 3, make_item(i18n.T(move["to"]), row))
        self.table.blockSignals(False)
        self._update_status()

    def _update_status(self):
        i18n.set_text(self.status_label, f"找到 {len(self._moves)} 章；已勾選 {len(self._checked)} 章")
        self.move_button.setEnabled(bool(self._checked))

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(item.row())
        else:
            self._checked.discard(item.row())
        self._update_status()

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.preview.hide()
            return
        move = self._moves[rows[0].row()]
        self.preview.show_rows(self._raw_lines, move["start"], move["end"] - 1, active_tokens().text)

    def _check_all(self):
        self._checked = set(range(len(self._moves)))
        self._refresh()

    def _uncheck_all(self):
        self._checked = set()
        self._refresh()

    def checked_moves(self) -> list:
        return [self._moves[index] for index in sorted(self._checked)]
