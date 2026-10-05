"""合併多個檔案（選擇檔案旁邊的箭頭、一次拖好幾個檔進視窗）。

照檔名裡的數字排好順序，可以上移、下移調整；合併成一份新的本文（core/file_merge.py）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget,
)

from core.file_merge import first_line, starts_with_heading
from core.word_count import char_count
from . import i18n
from .sortable_table import PreviewTable, make_item, setup_columns
from .widgets import ContextPreview, ToggleSwitch, dialog_frame, size_dialog


class MergeFilesDialog(QDialog):
    def __init__(self, parts: list, title_from_name: bool = True, parent=None, skipped=()):
        """parts：[(路徑, 文字)]，已經照檔名排好；skipped：[(檔名, 原因)] 沒讀到、略過的檔。
        接受後結果在 result_parts、result_title_from_name。"""
        super().__init__(parent)
        self.setWindowTitle("合併多個檔案")
        size_dialog(self, 820, 600)
        self._parts = list(parts)
        self._skipped = list(skipped)
        self.result_parts = self.result_title_from_name = None

        root, footer = dialog_frame(self, intro="照檔名裡的數字排好順序，合併成一份新的本文。")
        root.setSpacing(12)

        order_row = QHBoxLayout()
        order_row.setSpacing(8)
        self.up_button = QPushButton("上移")
        self.down_button = QPushButton("下移")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button.clicked.connect(lambda: self._move(1))
        order_row.addWidget(self.up_button)
        order_row.addWidget(self.down_button)
        order_row.addStretch(1)
        root.addLayout(order_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("fileLabel")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        # 順序就是合併的順序：不開放點標題列排序，要調整用上移、下移
        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["順序", "檔名", "字數", "第一行"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: 56, 1: 260, 2: 80})
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table, click_again_closes=False), 1)   # 選取用來上移、下移

        self.title_toggle = ToggleSwitch("開頭幾行沒有章節標題時，用檔名當章名", fill=False)
        self.title_toggle.setChecked(title_from_name)
        root.addWidget(self.title_toggle)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        merge_button = buttons.addButton("合併", QDialogButtonBox.ButtonRole.AcceptRole)
        merge_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        merge_button.clicked.connect(self._accept)
        footer.addWidget(buttons)
        self._refresh(0)

    def _refresh(self, selected: int):
        self.table.setRowCount(len(self._parts))
        without_title = 0
        for row, (path, text) in enumerate(self._parts):
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            order = make_item(str(row + 1), row)
            order.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 0, order)
            self.table.setItem(row, 1, make_item(name, row))
            count = make_item(f"{char_count(text):,}", row)
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 2, count)
            self.table.setItem(row, 3, make_item(first_line(text)[:40], row))
            if not starts_with_heading(text):
                without_title += 1
        text = f"共 {len(self._parts)} 個檔案"
        if without_title:
            text += f"；{without_title} 個檔案開頭沒有章節標題"
        if self._skipped:
            names = "、".join(f"{name}（{reason}）" for name, reason in self._skipped[:3])
            more = f"等 {len(self._skipped)} 個" if len(self._skipped) > 3 else ""
            text += f"；沒讀到、不會合併：{names}{more}"
        i18n.set_text(self.status_label, text)
        if 0 <= selected < len(self._parts):
            self.table.selectRow(selected)

    def _selected(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _move(self, step: int):
        index = self._selected()
        target = index + step
        if index < 0 or not 0 <= target < len(self._parts):
            return
        self._parts[index], self._parts[target] = self._parts[target], self._parts[index]
        self._refresh(target)

    def _on_selection_changed(self):
        index = self._selected()
        self.up_button.setEnabled(index > 0)
        self.down_button.setEnabled(0 <= index < len(self._parts) - 1)
        if index < 0:
            self.preview.hide()
            return
        lines = self._parts[index][1].split("\n")[:ContextPreview.MAX_BODY]
        self.preview.show_rows(lines, 0, len(lines) - 1, "text")

    def _accept(self):
        self.result_parts = list(self._parts)
        self.result_title_from_name = self.title_toggle.isChecked()
        self.accept()
