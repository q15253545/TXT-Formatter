"""全文簡繁轉換的設定視窗：「轉換」分頁選方向、選範圍，附一行範例；「詞表」分頁是不轉換的詞與用詞對照。

轉換會改動整份正文，所以這裡把「轉出來長什麼樣子」直接顯示出來——
尤其是「台灣用語」那個選項會連詞彙一起換（軟件→軟體、界面→介面），
不是每個人都想要，光看選項名稱看不出差別。
詞表是兩個可以直接打字、刪除的文字框（不用勾選）：用詞對照預先填好常見的兩岸用詞，不要的整行刪掉。
"""

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget,
)

from core.script_convert import (
    BODY_SCRIPT_CHOICES, BODY_SCRIPT_SAMPLE_SOURCE, BODY_SCRIPT_SAMPLES,
)
from . import i18n
from .widgets import ScopeToggle, dialog_frame, keep_on_screen


class ScriptConvertDialog(QDialog):
    def __init__(self, parent=None, selected_count: int = 0, mode: str | None = None,
                 keep_words: str = "", vocabulary: str = ""):
        super().__init__(parent)
        self.setWindowTitle("繁簡轉換")
        self.setMinimumWidth(480)
        keep_on_screen(self)

        # 詞表的文字框自己吃掉 Enter（換行），其他地方按 Enter 照舊是「開始轉換」
        outer, footer = dialog_frame(self, enter_submits=True, intro="本文轉成繁體或簡體，可以只轉選取的章節。")
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        convert_page = QWidget()
        root = QVBoxLayout(convert_page)
        root.setContentsMargins(0, 12, 0, 0)
        root.setSpacing(10)

        self.scope_check = ScopeToggle("轉換", selected_count)
        root.addWidget(self.scope_check)

        title = QLabel("轉換方向")
        title.setObjectName("appTitle")
        root.addWidget(title)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(BODY_SCRIPT_CHOICES)
        # 讀寫都用原始選項值（簡體介面顯示的是翻譯過的文字）
        if mode in BODY_SCRIPT_CHOICES:
            i18n.set_combo_value(self.mode_combo, mode)
        self.mode_combo.currentIndexChanged.connect(lambda _index: self._update_sample())
        root.addWidget(self.mode_combo)

        self.sample_label = QLabel("")
        self.sample_label.setObjectName("fileLabel")
        self.sample_label.setWordWrap(True)
        i18n.skip(self.sample_label)   # 範例本身是被轉換的內容，不跟著介面切換
        root.addWidget(self.sample_label)
        root.addStretch(1)
        self.tabs.addTab(convert_page, "轉換")

        words_page = QWidget()
        words = QVBoxLayout(words_page)
        words.setContentsMargins(0, 12, 0, 0)
        words.setSpacing(8)
        keep_label = QLabel("不轉換的詞（一行一個）")
        keep_label.setObjectName("fileLabel")
        words.addWidget(keep_label)
        self.keep_edit = QPlainTextEdit(keep_words)
        self.keep_edit.setObjectName("wordList")
        self.keep_edit.setFixedHeight(96)
        i18n.skip(self.keep_edit)      # 使用者自己打的詞，不跟著介面轉簡體
        words.addWidget(self.keep_edit)
        vocabulary_label = QLabel("用詞對照（一行一組「左邊 = 右邊」；轉繁體時左換右，轉簡體時右換左）")
        vocabulary_label.setObjectName("fileLabel")
        vocabulary_label.setWordWrap(True)
        words.addWidget(vocabulary_label)
        self.vocabulary_edit = QPlainTextEdit(vocabulary)
        self.vocabulary_edit.setObjectName("wordList")
        i18n.skip(self.vocabulary_edit)
        words.addWidget(self.vocabulary_edit, 1)
        self.tabs.addTab(words_page, "詞表")

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

    def keep_words_text(self) -> str:
        return self.keep_edit.toPlainText()

    def vocabulary_text(self) -> str:
        return self.vocabulary_edit.toPlainText()
