"""全文簡繁轉換的設定視窗：選方向、選範圍，附一行範例。

轉換會改動整份正文，所以這裡把「轉出來長什麼樣子」直接顯示出來——
尤其是「台灣用語」那個選項會連詞彙一起換（軟件→軟體、界面→介面），
不是每個人都想要，光看選項名稱看不出差別。
"""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel,
)

from core.script_convert import (
    BODY_SCRIPT_CHOICES, BODY_SCRIPT_SAMPLE_SOURCE, BODY_SCRIPT_SAMPLES,
)
from . import i18n
from .widgets import CompactToggle, ScopeToggle, dialog_frame, keep_on_screen


class ScriptConvertDialog(QDialog):
    def __init__(self, parent=None, selected_count: int = 0, mode: str | None = None,
                 convert_metadata: bool = True):
        super().__init__(parent)
        self.setWindowTitle("繁簡轉換")
        self.setMinimumWidth(460)
        keep_on_screen(self)
        self._selected_count = selected_count

        root, footer = dialog_frame(self, enter_submits=True, intro="本文轉成繁體或簡體，可以只轉選取的章節。")
        root.setSpacing(10)

        self.scope_check = ScopeToggle("轉換", selected_count)
        root.addWidget(self.scope_check)

        title = QLabel("轉換方向")
        title.setObjectName("appTitle")
        root.addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(BODY_SCRIPT_CHOICES)
        # 讀寫都用原始選項值（簡體介面顯示的是翻譯過的文字）；存成翻譯後文字的舊設定也認得。
        saved = next((choice for choice in BODY_SCRIPT_CHOICES if mode in (choice, i18n.T(choice))), None)
        if saved is not None:
            i18n.set_combo_value(self.mode_combo, saved)
        self.mode_combo.currentIndexChanged.connect(lambda _index: self._update_sample())
        root.addWidget(self.mode_combo)

        self.sample_label = QLabel("")
        self.sample_label.setObjectName("fileLabel")
        self.sample_label.setWordWrap(True)
        i18n.skip(self.sample_label)   # 範例本身是被轉換的內容，不跟著介面切換
        root.addWidget(self.sample_label)

        self.title_check = CompactToggle("一併轉換書名與作者欄位")
        self.title_check.setChecked(convert_metadata)
        root.addWidget(self.title_check)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        convert_button = buttons.addButton("開始轉換", QDialogButtonBox.ButtonRole.AcceptRole)
        convert_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        convert_button.clicked.connect(self.accept)
        footer.addWidget(buttons)

        self._update_sample()

    def _update_sample(self):
        mode = self.mode()
        self.sample_label.setText(
            f"{BODY_SCRIPT_SAMPLE_SOURCE}\n　↓\n{BODY_SCRIPT_SAMPLES.get(mode, '')}")

    def mode(self) -> str:
        return i18n.combo_value(self.mode_combo)

    def selected_only(self) -> bool:
        return self.scope_check.isChecked()

    def convert_metadata(self) -> bool:
        return self.title_check.isChecked()
