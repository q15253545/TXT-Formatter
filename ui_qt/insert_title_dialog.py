"""插入章節標題對話框：依游標位置推算建議編號。"""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
)

from core.insert_suggestions import INSERTABLE_KINDS, NUMERIC_KINDS, build_inserted_title
from .widgets import dialog_frame, keep_on_screen
from . import dialogs, i18n


class InsertTitleDialog(QDialog):
    """建構時吃 (suggestions, default_kind)；接受後結果放在 self.result_text。"""

    def __init__(self, suggestions: dict, default_kind: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新增章節")
        self.setMinimumWidth(440)
        keep_on_screen(self)
        self._suggestions = suggestions
        self.result_text = ""

        root, footer = dialog_frame(self, (24, 20, 24, 14), enter_submits=True)
        root.setSpacing(14)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("新增種類"), 0, 0)
        self.kind_combo = QComboBox()
        self.kind_combo.addItems(INSERTABLE_KINDS)
        if default_kind in INSERTABLE_KINDS:
            i18n.set_combo_value(self.kind_combo, default_kind)
        grid.addWidget(self.kind_combo, 0, 1)

        grid.addWidget(QLabel("自動編號"), 1, 0)
        self.number_input = QLineEdit()
        grid.addWidget(self.number_input, 1, 1)

        grid.addWidget(QLabel("章節名稱"), 2, 0)
        self.title_input = QLineEdit("空白標題")
        grid.addWidget(self.title_input, 2, 1)
        root.addLayout(grid)

        parts = [f"{kind}：{data['number']}" for kind, data in suggestions.items()]
        self.suggestion_label = QLabel("偵測建議｜" + "　".join(parts))
        self.suggestion_label.setObjectName("fileLabel")
        self.suggestion_label.setWordWrap(True)
        root.addWidget(self.suggestion_label)

        self.preview_label = QLabel()
        i18n.skip(self.preview_label)
        self.preview_label.setWordWrap(True)
        root.addWidget(self.preview_label)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        insert_button = buttons.addButton("插入", QDialogButtonBox.ButtonRole.AcceptRole)
        insert_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        insert_button.clicked.connect(self._try_accept)
        footer.addWidget(buttons)

        self.kind_combo.currentIndexChanged.connect(self._update_preview)
        self.number_input.textChanged.connect(self._update_preview)
        self.title_input.textChanged.connect(self._update_preview)
        self._update_preview()
        self.title_input.setFocus()
        self.title_input.selectAll()

    def _current_data(self):
        kind = i18n.combo_value(self.kind_combo)
        return kind, self._suggestions.get(kind, {"number": 1, "reference": f"第一{kind}"})

    def _update_preview(self, *_args):
        kind, data = self._current_data()
        self.number_input.setEnabled(kind in NUMERIC_KINDS)
        if kind in NUMERIC_KINDS and not self.number_input.hasFocus():
            suggested = str(data["number"])
            if self.number_input.text() != suggested:
                self.number_input.blockSignals(True)
                self.number_input.setText(suggested)
                self.number_input.blockSignals(False)
        try:
            preview = build_inserted_title(kind, self.number_input.text(), self.title_input.text(),
                                            data.get("reference", ""))
        except ValueError:
            preview = i18n.T("請輸入有效編號")
        # 預覽的是要插進本文的標題（內容），只轉換前面的說明字。
        self.preview_label.setText(i18n.T("預覽｜") + preview)

    def _try_accept(self):
        kind, data = self._current_data()
        try:
            self.result_text = build_inserted_title(
                kind, self.number_input.text(), self.title_input.text(), data.get("reference", ""))
        except ValueError as error:
            dialogs.error(self, "無法新增", str(error))
            return
        self.accept()
