"""章節字數（從章節管理「檢查章節」的結果開啟）：全書與每一章的字數，標出特別短、特別長的章。
非模式：點一列跳到那一章；本文改過之後主視窗會呼叫 reload() 重算。判斷規則見 core/word_count.py。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTableWidget

from . import i18n
from .sortable_table import PreviewTable, data_index, enable_sorting, make_item, resort, setup_columns
from .widgets import CompactToggle, dialog_frame, size_dialog


class WordCountDialog(QDialog):
    chapterSelected = Signal(int)       # 標題所在的行號（0 起算）

    def __init__(self, entries, summary, parent=None):
        super().__init__(parent)
        self.setWindowTitle("章節字數")
        size_dialog(self, 760, 600)
        root, footer = dialog_frame(self, intro="全書與每一章的字數，標出沒有正文、偏短、偏長的章。")
        root.setSpacing(12)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("appTitle")
        root.addWidget(self.summary_label)

        self.flagged_only = CompactToggle("只看偏短、偏長、沒有正文的章節")
        self.flagged_only.toggled.connect(self._refresh)
        root.addWidget(self.flagged_only)

        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["字數", "備註", "卷", "章節"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 90, 1: 80, 2: 160})
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        root.addWidget(self.table, 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        close_button.clicked.connect(self.reject)
        footer.addWidget(buttons)
        self.reload(entries, summary)

    def reload(self, entries, summary):
        self._entries = list(entries)
        i18n.set_text(self.summary_label,
                      f"全書 {summary['total']:,} 字 · {summary['chapters']} 章 · "
                      f"每章平均 {summary['average']:,} 字")
        i18n.set_text(self.flagged_only, f"只看偏短、偏長、沒有正文的章節（{summary['flagged']} 章）")
        self._refresh()

    def _refresh(self, *_args):
        indices = [index for index, entry in enumerate(self._entries)
                   if entry["note"] or not self.flagged_only.isChecked()]
        # 唯讀的清單：全部列出（不像掃描視窗截在前 2000 筆），排序才會照所有的章
        self.table.setRowCount(len(indices))
        for row, index in enumerate(indices):
            entry = self._entries[index]
            count_item = make_item(f"{entry['count']:,}", index, entry["count"])
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, count_item)
            self.table.setItem(row, 1, make_item(i18n.T(entry["note"]), index))
            self.table.setItem(row, 2, make_item(entry["volume"], index))
            self.table.setItem(row, 3, make_item(entry["title"], index))
        resort(self.table)

    def _on_selection_changed(self):
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.chapterSelected.emit(self._entries[data_index(self.table, rows[0].row())]["row"])
