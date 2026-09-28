"""匯出之後：同一個資料夾裡同一本書的舊檔（接續更新章節之後檔名換成「更新至第30章」，
「更新至第20章」那個就留在旁邊）。問過才移到資源回收筒，放錯了還能還原。"""

import os

from PySide6.QtCore import QFile
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel

from .widgets import dialog_frame, size_dialog


def move_to_trash(path: str) -> bool:
    """移到資源回收筒（不是永久刪除）；成功回傳 True。"""
    result = QFile.moveToTrash(path)
    return bool(result[0] if isinstance(result, tuple) else result)


class OldFilesDialog(QDialog):
    """files：[(路徑, 說明, 預設勾不勾)]。接受後 chosen() 是勾選的路徑。"""

    def __init__(self, new_name: str, files: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("移除舊檔")
        size_dialog(self, 620, 260)
        root, footer = dialog_frame(
            self, intro=f"已匯出「{new_name}」。資料夾裡還有同一本書的舊檔，要移到資源回收筒嗎？")
        self._boxes = []
        for path, note, checked in files:
            box = QCheckBox(os.path.basename(path) + (f"（{note}）" if note else ""))
            box.setChecked(checked)
            box.setToolTip(path)
            root.addWidget(box)
            self._boxes.append((box, path))
        hint = QLabel("移到資源回收筒之後還可以還原；不想再看到這個提示，在匯出設定關掉。")
        hint.setObjectName("fileLabel")
        hint.setWordWrap(True)
        root.addWidget(hint)
        root.addStretch(1)
        buttons = QDialogButtonBox()
        keep = buttons.addButton("保留", QDialogButtonBox.ButtonRole.RejectRole)
        keep.clicked.connect(self.reject)
        self.trash_button = buttons.addButton("移到資源回收筒", QDialogButtonBox.ButtonRole.AcceptRole)
        self.trash_button.setObjectName("primary")
        self.trash_button.clicked.connect(self.accept)
        footer.addWidget(buttons)
        for box, _path in self._boxes:
            box.toggled.connect(self._update_button)
        self._update_button()

    def chosen(self) -> list:
        return [path for box, path in self._boxes if box.isChecked()]

    def _update_button(self, *_args):
        self.trash_button.setEnabled(bool(self.chosen()))
