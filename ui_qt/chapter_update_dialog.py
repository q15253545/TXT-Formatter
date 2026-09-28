"""接續更新章節（選擇檔案旁邊的箭頭、或把檔案拖到「接續更新章節」那一區）。

列出新檔案的每一章跟本文比對的結果，勾選的才加入本文；判斷規則見 core/chapter_update.py。
模式對話框：按下之後整份本文一次改好（一步復原），不像掃描視窗那樣開著邊看邊改。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
)

from core.chapter_update import LONGER, MISSING, NEW, SAME
from . import i18n
from .sortable_table import PreviewTable, data_index, enable_sorting, make_item, resort, setup_columns
from .theme import active_tokens
from .widgets import ContextPreview, ToggleSwitch, dialog_frame, size_dialog

# 使用者按了哪一個
APPLY, APPEND_ALL, OTHER_FILE = "apply", "append_all", "other_file"


def status_text(item) -> str:
    status = item["status"]
    if status == SAME:
        return "本文已經有"
    if status == LONGER:
        return "新檔比較長，可能補完了：勾選會換成新檔的正文"
    if status == NEW:
        return "新章節：接在最後"
    if not item["suggested"]:
        return "本文沒有：可能是整理時刪掉的"
    if item["after"] is None:
        return "本文缺少：補在本文最前面的章節前"
    return f"本文缺少：補在「{item['after']}」後面"


def _count_text(item) -> str:
    body = "—" if item["body_count"] is None else f"{item['body_count']:,}"
    return f"{body} → {item['new_count']:,}"


class ChapterUpdateDialog(QDialog):
    def __init__(self, file_name: str, plan: list, new_lines: list, new_rows: list, convert_label: str | None,
                 parent=None):
        """new_rows：新檔每一項標題的行號（跟 plan 的 index 對應），選到一列時顯示那一章的開頭。
        convert_label：新檔跟本文繁簡不同時開關的文字（「加入時轉成繁體」），相同就是 None。"""
        super().__init__(parent)
        self.setWindowTitle("接續更新章節")
        size_dialog(self, 900, 680)
        self.choice = None
        self._plan = plan
        self._new_lines = new_lines
        self._new_rows = new_rows
        self._checked = {item["index"] for item in plan if item["suggested"]}

        root, footer = dialog_frame(
            self, intro="比對新下載的檔案和本文：本文缺少的章節補進原本的位置，新章節接在最後。")
        root.setSpacing(12)

        file_row = QHBoxLayout()
        self.file_label = QLabel(file_name)
        i18n.skip(self.file_label)
        file_row.addWidget(self.file_label, 1)
        other_button = QPushButton("換一個檔案")
        other_button.clicked.connect(lambda: self._finish(OTHER_FILE))
        file_row.addWidget(other_button)
        root.addLayout(file_row)

        self.convert_toggle = None
        if convert_label:
            self.convert_toggle = ToggleSwitch(convert_label, fill=False)
            self.convert_toggle.setChecked(True)
            root.addWidget(self.convert_toggle)

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for label, handler in (("勾選缺少和新章節", self._check_suggested), ("全選", self._check_all),
                               ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["加入", "章節", "狀態", "字數（本文 → 新檔）"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 56, 1: 240, 2: 300})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        # 選到一列：下面顯示新檔那一章的開頭
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table), 1)

        buttons = QDialogButtonBox()
        # 認不出章節、或想自己整理時的退路：新檔整份接到最後
        self.append_all_button = buttons.addButton("整份接到最後", QDialogButtonBox.ButtonRole.ResetRole)
        self.append_all_button.clicked.connect(lambda: self._finish(APPEND_ALL))
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        close_button.clicked.connect(self.reject)
        self.apply_button = buttons.addButton("加入已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(lambda: self._finish(APPLY))
        footer.addWidget(buttons)

        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_scrolled", False):
            # 前面多半是一長串「本文已經有」：一打開就捲到第一個要處理的
            self._scrolled = True
            first = next((row for row in range(self.table.rowCount())
                          if self.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable), None)
            if first is not None:
                self.table.scrollToItem(self.table.item(first, 1), QTableWidget.ScrollHint.PositionAtTop)

    # ------------------------------------------------------------------ 結果

    def checked(self) -> set:
        return set(self._checked)

    def convert_enabled(self) -> bool:
        return self.convert_toggle is not None and self.convert_toggle.isChecked()

    def _finish(self, choice: str):
        if choice == APPLY and not self._checked:
            return
        self.choice = choice
        self.accept()

    # ------------------------------------------------------------------ 表格

    def _actionable(self, item) -> bool:
        return item["status"] != SAME

    def _refresh(self):
        faint = QColor(active_tokens().text_faint)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._plan))
        for row, item in enumerate(self._plan):
            index = item["index"]
            check_item = make_item("", index)
            if self._actionable(item):
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                check_item.setCheckState(Qt.CheckState.Checked if index in self._checked else Qt.CheckState.Unchecked)
            else:
                check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 0, check_item)
            # 章節標題是書的內容，不跟著介面切換繁簡（表格的格子本來就不翻）
            cells = (make_item(item["title"], index), make_item(i18n.T(status_text(item)), index),
                     make_item(_count_text(item), index, item["new_count"]))
            for column, cell in enumerate(cells, start=1):
                if not self._actionable(item):
                    cell.setForeground(faint)
                self.table.setItem(row, column, cell)
        self.table.blockSignals(False)
        resort(self.table)
        self._update_status()

    def _update_status(self):
        counts = {status: sum(1 for item in self._plan if item["status"] == status)
                  for status in (SAME, LONGER, MISSING, NEW)}
        if not self._plan:
            text = "新檔案認不出章節：可以整份接到最後，再自己整理"
        else:
            parts = [f"{counts[SAME] + counts[LONGER]} 章本文已經有"]
            if counts[LONGER]:
                parts.append(f"{counts[LONGER]} 章新檔比較長")
            if counts[MISSING]:
                parts.append(f"{counts[MISSING]} 章本文缺少")
            parts.append(f"{counts[NEW]} 章是新的")
            text = f"新檔案 {len(self._plan)} 章：" + "、".join(parts) + f"；已勾選 {len(self._checked)} 章"
        i18n.set_text(self.status_label, text)
        self.apply_button.setEnabled(bool(self._checked))

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
            self.preview.hide()
            return
        index = data_index(self.table, rows[0].row())
        row = self._new_rows[index]
        self.preview.show_rows(self._new_lines, row, row, active_tokens().accent)

    def _check_suggested(self):
        self._checked = {item["index"] for item in self._plan if item["suggested"]}
        self._refresh()

    def _check_all(self):
        self._checked = {item["index"] for item in self._plan if self._actionable(item)}
        self._refresh()

    def _uncheck_all(self):
        self._checked = set()
        self._refresh()
