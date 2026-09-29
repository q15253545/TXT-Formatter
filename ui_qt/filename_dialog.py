"""匯出設定（匯出按鈕旁邊的箭頭）：連載中／已完結兩個檔名格式（各自即時預覽）、插入變數的小標籤、
檔名繁簡、匯出格式（TXT／EPUB），以及匯出時要不要移除章節標記。"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
)

from core.filename_meta import (
    DEFAULT_COMPLETED_TEMPLATE, DEFAULT_ONGOING_TEMPLATE, FILENAME_VARIABLES, build_smart_filename,
)
from core.script_convert import SCRIPT_CHOICES, convert_script
from . import i18n
from .widgets import Divider, ToggleSwitch, dialog_frame, flow_container, keep_on_screen


class _TemplateInput(QLineEdit):
    """記得最後一個有游標的格式欄，變數按鈕才知道要插到哪一欄。"""
    focused = Signal()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focused.emit()


EXPORT_FORMATS = ("TXT", "EPUB")


class FilenameDialog(QDialog):
    """接受後結果在 result_ongoing、result_completed、result_script、result_strip_markers、result_ask_old_files、
    result_format。"""

    def __init__(self, ongoing: str, completed: str, script: str, fields: dict, status: str, parent=None,
                 strip_markers: bool = True, ask_old_files: bool = True, export_format: str = "TXT"):
        super().__init__(parent)
        self.setWindowTitle("匯出設定")
        self.setMinimumWidth(660)
        keep_on_screen(self)
        self._fields = fields
        self.result_ongoing = self.result_completed = self.result_script = self.result_strip_markers = None
        self.result_ask_old_files = self.result_format = None

        root, footer = dialog_frame(self, (24, 20, 24, 14), enter_submits=True,
                                    intro="檔名照書籍資料組成，連載中、已完結各一種格式。")
        root.setSpacing(6)

        using_completed = status == "已完結"
        self.ongoing_input, self.ongoing_preview = self._add_template(
            root, "連載中", ongoing or DEFAULT_ONGOING_TEMPLATE, not using_completed)
        self.completed_input, self.completed_preview = self._add_template(
            root, "已完結", completed or DEFAULT_COMPLETED_TEMPLATE, using_completed)
        self._target = self.completed_input if using_completed else self.ongoing_input

        # 變數是一排小標籤，插到最後一個有游標的格式；變數名稱放在滑鼠提示
        chip_box, chip_flow = flow_container(h_spacing=6, v_spacing=6)
        for name, label in FILENAME_VARIABLES:
            chip = QPushButton(label)
            chip.setObjectName("tagChip")
            chip.setToolTip("{" + name + "}")
            chip.clicked.connect(lambda _checked=False, token="{" + name + "}": self._insert(token))
            chip_flow.addWidget(chip)
        optional = QPushButton("[ ] 可省略")
        optional.setObjectName("tagChip")
        optional.setToolTip("括起來的段落，變數沒有值時整段省略")
        optional.clicked.connect(self._insert_optional)
        chip_flow.addWidget(optional)
        root.addWidget(chip_box)
        # 最小寬度照小標籤算：換字型、換縮放時也排得成一列
        chip_width = sum(chip_flow.itemAt(i).widget().sizeHint().width() + 6 for i in range(chip_flow.count()))
        self.setMinimumWidth(max(660, chip_width + 48))

        # 章節標記（[::] 這類）只在匯出時有差：跟檔名放在一起
        root.addSpacing(6)
        root.addWidget(Divider())
        root.addSpacing(6)
        format_row = QHBoxLayout()
        format_row.setSpacing(10)
        format_row.addWidget(QLabel("匯出格式"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(EXPORT_FORMATS)
        self.format_combo.setCurrentText(export_format if export_format in EXPORT_FORMATS else "TXT")
        self.format_combo.currentIndexChanged.connect(self._update_previews)
        format_row.addWidget(self.format_combo)
        format_row.addStretch(1)
        root.addLayout(format_row)
        root.addSpacing(4)
        self.strip_markers_toggle = ToggleSwitch("匯出時移除章節標記", fill=False)
        self.strip_markers_toggle.setChecked(strip_markers)
        root.addWidget(self.strip_markers_toggle)
        # 接續更新章節之後檔名會換（更新至第20章 → 第30章），舊檔留在旁邊：匯出後問要不要移到資源回收筒
        self.ask_old_files_toggle = ToggleSwitch("匯出後詢問是否移除同一本書的舊檔", fill=False)
        self.ask_old_files_toggle.setChecked(ask_old_files)
        root.addWidget(self.ask_old_files_toggle)

        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.addWidget(QLabel("檔名繁簡"))
        self.script_combo = QComboBox()
        self.script_combo.addItems(SCRIPT_CHOICES)
        i18n.set_combo_value(self.script_combo, script)
        self.script_combo.currentIndexChanged.connect(self._update_previews)
        bottom.addWidget(self.script_combo)
        bottom.addStretch(1)

        box = QDialogButtonBox()
        reset_button = box.addButton("還原預設", QDialogButtonBox.ButtonRole.ResetRole)
        cancel_button = box.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        ok_button = box.addButton("確定", QDialogButtonBox.ButtonRole.AcceptRole)
        ok_button.setObjectName("primary")
        reset_button.clicked.connect(self._reset)
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self._accept)
        bottom.addWidget(box)
        footer.addLayout(bottom)

        self._update_previews()
        self._target.setFocus()

    def showEvent(self, event):
        # 開窗時游標放在格式最後面，不要整段反白（一按鍵就把格式整個蓋掉）
        super().showEvent(event)
        QTimer.singleShot(0, self._place_cursor)

    def _place_cursor(self):
        self._target.deselect()
        self._target.setCursorPosition(len(self._target.text()))

    def _add_template(self, root, label_text: str, value: str, in_use: bool):
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(QLabel(label_text))
        if in_use:
            badge = QLabel("本書適用")
            badge.setObjectName("badge")
            header.addWidget(badge)
        header.addStretch(1)
        root.addSpacing(8)
        root.addLayout(header)
        edit = _TemplateInput(value)
        edit.focused.connect(lambda: setattr(self, "_target", edit))
        edit.textChanged.connect(self._update_previews)
        root.addWidget(edit)
        preview = QLabel("")
        preview.setObjectName("fileLabel")
        preview.setWordWrap(True)
        preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        i18n.skip(preview)            # 預覽是書的內容，不跟著介面轉簡體
        root.addWidget(preview)
        root.addSpacing(6)
        return edit, preview

    def _insert(self, text: str):
        self._target.insert(text)
        self._target.setFocus()

    def _insert_optional(self):
        """有選取文字就把它括起來，沒有就放一對 [ ]、游標在中間。"""
        edit = self._target
        selected = edit.selectedText()
        edit.insert("[" + selected + "]")
        if not selected:
            edit.setCursorPosition(edit.cursorPosition() - 1)
        edit.setFocus()

    def _preview(self, template: str, default: str) -> str:
        name = build_smart_filename(self._fields, template.strip() or default)
        if self.format_combo.currentText() == "EPUB" and name.lower().endswith(".txt"):
            name = name[:-4] + ".epub"
        return convert_script(name, i18n.combo_value(self.script_combo))

    def _update_previews(self, *_args):
        self.ongoing_preview.setText(self._preview(self.ongoing_input.text(), DEFAULT_ONGOING_TEMPLATE))
        self.completed_preview.setText(self._preview(self.completed_input.text(), DEFAULT_COMPLETED_TEMPLATE))

    def _reset(self):
        self.ongoing_input.setText(DEFAULT_ONGOING_TEMPLATE)
        self.completed_input.setText(DEFAULT_COMPLETED_TEMPLATE)
        self.strip_markers_toggle.setChecked(True)
        self.ask_old_files_toggle.setChecked(True)
        self.format_combo.setCurrentText("TXT")

    def _accept(self):
        self.result_ongoing = self.ongoing_input.text().strip() or DEFAULT_ONGOING_TEMPLATE
        self.result_completed = self.completed_input.text().strip() or DEFAULT_COMPLETED_TEMPLATE
        self.result_script = i18n.combo_value(self.script_combo)
        self.result_strip_markers = self.strip_markers_toggle.isChecked()
        self.result_ask_old_files = self.ask_old_files_toggle.isChecked()
        self.result_format = self.format_combo.currentText()
        self.accept()
