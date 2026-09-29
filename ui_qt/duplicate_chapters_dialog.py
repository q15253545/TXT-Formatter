"""重複章節（從章節管理「檢查章節」的結果開啟）。

列出目錄裡相鄰、章號相同的章節，一列一章，勾選要保留的；按「刪除未保留的章節」才動本文。
非模式：開著的時候可以直接在本文比對兩段內容；本文改過之後，主視窗會呼叫 reload() 重新找。
判斷規則、刪除規則見 core/duplicate_chapters.py。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QTableWidget, QTableWidgetItem,
)

from core.duplicate_chapters import RELATION_LABELS, apply_keep_choices, find_duplicate_groups, find_similar_groups
from . import dialogs, i18n
from .theme import active_tokens
from .widgets import ContextPreview, dialog_frame, size_dialog
from .sortable_table import HeaderCheckBox, PreviewTable, carry_over, make_item, setup_columns


class DuplicateChaptersDialog(QDialog):
    groupHighlighted = Signal(int, int)     # 選到的那一章：標題～正文最後一行的行號（0 起算）
    mergeReady = Signal(list)               # 刪除後的整份本文

    def __init__(self, raw_lines: list, title_rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("重複章節")
        size_dialog(self, 860, 600)
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows)
        self._groups: list = []
        self._keep: list[list[bool]] = []
        self._entries: list[tuple[int, int]] = []    # 表格第幾列 → (第幾組, 組裡第幾章)

        root, footer = dialog_frame(self, intro="章號相同或內容重複的章節，勾選要保留的，其餘刪除。")
        root.setSpacing(12)

        self.status_label = QLabel("")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        # 同一組上下相鄰：不開放點標題列排序，排了就看不出哪幾章是一組
        self.table = PreviewTable(0, 5)
        self.table.setHorizontalHeaderLabels(["保留", "組", "標題", "正文字數", "內容比對"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 96, 1: 84, 2: 280, 3: 90})
        self.header_check = HeaderCheckBox(
            self.table, lambda: (sum(map(sum, self._keep)), sum(map(len, self._keep))), self._set_all_checked)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table), 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.merge_button = buttons.addButton("刪除未保留的章節", QDialogButtonBox.ButtonRole.AcceptRole)
        self.merge_button.setObjectName("primary")
        close_button.clicked.connect(self.reject)
        self.merge_button.clicked.connect(self._merge_checked)
        footer.addWidget(buttons)

        self._scan()
        self._keep = [list(group["default_keep"]) for group in self._groups]
        self._refresh()

    @staticmethod
    def _group_key(group):
        return tuple(group["titles"])

    def reload(self, raw_lines, title_rows):
        """本文改過了：重新找。勾過／取消過的照標題對回去，新出現的照預設。"""
        old_groups = self._groups
        state = {index: list(keep) for index, keep in enumerate(self._keep)}
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows)
        self._scan()
        kept = carry_over(old_groups, self._groups, self._group_key, state)
        self._keep = [list(kept.get(index, group["default_keep"])) for index, group in enumerate(self._groups)]
        self._refresh()

    def _scan(self):
        adjacent = find_duplicate_groups(self._raw_lines, self._title_rows)
        self._groups = adjacent + find_similar_groups(self._raw_lines, self._title_rows, adjacent)

    def _refresh(self):
        self.table.blockSignals(True)
        self._entries = [(group_index, member) for group_index, group in enumerate(self._groups)
                         for member in range(len(group["rows"]))]
        self.table.setRowCount(len(self._entries))
        for row, (group_index, member) in enumerate(self._entries):
            group = self._groups[group_index]
            check_item = make_item("", row)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(
                Qt.CheckState.Checked if self._keep[group_index][member] else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, check_item)
            label = str(group_index + 1) if group.get("adjacent", True) else i18n.T(f"{group_index + 1} 不相鄰")
            self.table.setItem(row, 1, make_item(label, row))
            self.table.setItem(row, 2, make_item(group["titles"][member], row))
            count_item = make_item(f"{group['counts'][member]:,}", row)
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, count_item)
            note = group.get("notes", [""] * len(group["rows"]))[member]
            relation = group["relations"][member]
            self.table.setItem(row, 4, make_item(note or i18n.T(RELATION_LABELS.get(relation, "")), row))
        self.table.blockSignals(False)
        self._update_status()

    def _choices(self):
        return [(group, keep) for group, keep in zip(self._groups, self._keep) if any(keep) and not all(keep)]

    def _update_status(self):
        if not self._groups:
            text = "沒有找到章號相同或內容重複的章節"
        else:
            dropped = sum(keep.count(False) for _group, keep in self._choices())
            apart = sum(1 for group in self._groups if not group.get("adjacent", True))
            text = f"找到 {len(self._groups)} 組" + (f"（{apart} 組不相鄰）" if apart else "") + f"；會刪除 {dropped} 章"
        i18n.set_text(self.status_label, text)
        self.merge_button.setEnabled(bool(self._choices()))
        self.header_check.refresh()

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0 or not 0 <= item.row() < len(self._entries):
            return
        group_index, member = self._entries[item.row()]
        self._keep[group_index][member] = item.checkState() == Qt.CheckState.Checked
        self._update_status()

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or not 0 <= rows[0].row() < len(self._entries):
            self.preview.hide()
            return
        group_index, member = self._entries[rows[0].row()]
        group = self._groups[group_index]
        start, end = group["rows"][member], group["ends"][member] - 1
        self.preview.show_rows(self._raw_lines, start, end, active_tokens().text)
        self.groupHighlighted.emit(start, end)

    def _set_all_checked(self, checked: bool):
        self._keep = [[checked] * len(group["rows"]) for group in self._groups]
        self._refresh()

    def _merge_checked(self):
        choices = self._choices()
        if not choices:
            dialogs.info(self, "沒有要刪除的章節", "每一組都全部保留時不會改動本文。")
            return
        self.mergeReady.emit(apply_keep_choices(self._raw_lines, choices))
