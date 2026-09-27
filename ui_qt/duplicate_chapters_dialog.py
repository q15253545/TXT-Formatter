"""合併重複章節（從「章節管理」開啟）。

列出目錄裡相鄰、章號相同的章節，勾選後才合併。非模式：開著的時候可以
直接在本文比對兩段內容；本文改過之後，主視窗會呼叫 reload() 重新找。
判斷規則見 core/duplicate_chapters.py。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
)

from core.duplicate_chapters import find_duplicate_groups, merge_duplicate_groups
from . import dialogs, i18n
from .widgets import dialog_frame, size_dialog
from .sortable_table import PreviewTable, carry_over, data_index, enable_sorting, make_item, resort, setup_columns


class DuplicateChaptersDialog(QDialog):
    groupHighlighted = Signal(int, int)     # 第一個標題～最後一個標題的行號（0 起算）
    mergeReady = Signal(list)               # 合併後的整份本文

    def __init__(self, raw_lines: list, title_rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("合併重複章節")
        size_dialog(self, 860, 560)
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows)
        self._groups: list = []
        self._checked: set[int] = set()

        root, footer = dialog_frame(self, intro="相鄰、章號相同的章節；勾選要合併的，正文併進留下的那一章。")
        root.setSpacing(12)

        note = QLabel("相鄰、章號相同的章節：合併後保留第一個標題，兩個標題之間的內容併入同一章。")
        note.setObjectName("fileLabel")
        note.setWordWrap(True)
        root.addWidget(note)

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
        self.table.setHorizontalHeaderLabels(["合併", "保留的標題", "刪除的標題", "中間夾的內容"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 56, 1: 260, 2: 260})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.merge_button = buttons.addButton("合併已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.merge_button.setObjectName("primary")
        close_button.clicked.connect(self.reject)
        self.merge_button.clicked.connect(self._merge_checked)
        footer.addWidget(buttons)

        self._scan()
        self._checked = {index for index, group in enumerate(self._groups) if group["same_title"]}
        self._refresh()

    @staticmethod
    def _group_key(group):
        return tuple(group["titles"])

    def reload(self, raw_lines, title_rows):
        """本文改過了：重新找。勾過／取消過的照標題對回去，新出現的照預設。"""
        old_groups = self._groups
        state = {index: index in self._checked for index in range(len(old_groups))}
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows)
        self._scan()
        kept = carry_over(old_groups, self._groups, self._group_key, state)
        self._checked = {index for index, group in enumerate(self._groups)
                         if kept.get(index, group["same_title"])}
        self._refresh()

    def _scan(self):
        self._groups = find_duplicate_groups(self._raw_lines, self._title_rows)

    def _refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._groups))
        for row, group in enumerate(self._groups):
            check_item = make_item("", row)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(Qt.CheckState.Checked if row in self._checked else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check_item)
            self.table.setItem(row, 1, make_item(group["keep_title"], row))
            keep_index = group["titles"].index(group["keep_title"])
            dropped = [title for index, title in enumerate(group["titles"]) if index != keep_index]
            self.table.setItem(row, 2, make_item("、".join(dropped), row))
            between = sum(group["between"])
            between_text = i18n.T("沒有內容（只有空行）") if not between else i18n.T(f"{between} 行")
            if not group["same_title"]:
                between_text += i18n.T("；標題不同")
            self.table.setItem(row, 3, make_item(between_text, row, between))
        self.table.blockSignals(False)
        resort(self.table)
        self._update_status()

    def _update_status(self):
        if not self._groups:
            text = "沒有找到相鄰、章號相同的章節"
        else:
            text = f"找到 {len(self._groups)} 組；已勾選 {len(self._checked)} 組"
        i18n.set_text(self.status_label, text)
        self.merge_button.setEnabled(bool(self._checked))

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        index = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_status()

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        group = self._groups[data_index(self.table, rows[0].row())]
        self.groupHighlighted.emit(group["rows"][0], group["rows"][-1])

    def _check_all(self):
        self._checked = set(range(len(self._groups)))
        self._refresh()

    def _uncheck_all(self):
        self._checked = set()
        self._refresh()

    def _merge_checked(self):
        if not self._checked:
            dialogs.info(self, "尚未勾選", "請先勾選要合併的章節。")
            return
        groups = [self._groups[index] for index in sorted(self._checked)]
        self.mergeReady.emit(merge_duplicate_groups(self._raw_lines, groups))
