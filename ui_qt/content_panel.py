"""左側「內容檢查」卡片（左側圖示列「檢查」）：掃描非正文內容、逐筆檢查、標點校對、繁簡轉換，
以及只影響畫面的開關：顯示內文空格。

「逐筆檢查」打開本文上方的逐筆檢查列（review_bar.py）：非正文內容一筆一筆跳過去看、刪掉；
整批刪除在非正文內容視窗裡，可以先逐筆看過再刪。顏色的意思寫在檔名列的「說明」裡。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .widgets import Divider, HoverIconButton, PanelScroll, ToggleSwitch, make_card_header

_ACTIONS = [
    ("scan-search", "掃描非正文內容", "ad_scan_requested"),
    ("step-forward", "逐筆檢查", "review_requested"),
    ("quote", "標點校對", "quote_check_requested"),
    ("languages", "繁簡轉換", "script_convert_requested"),
]


class ContentPanel(QWidget):
    ad_scan_requested = Signal()
    review_requested = Signal()
    quote_check_requested = Signal()
    script_convert_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: dict[str, HoverIconButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, _header_layout = make_card_header("內容檢查")
        outer.addWidget(header)

        scroll = PanelScroll()
        outer.addWidget(scroll, 1)

        body = QVBoxLayout(scroll.content)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)
        for icon_name, text, signal_name in _ACTIONS:
            button = HoverIconButton(icon_name, text)
            button.clicked.connect(getattr(self, signal_name).emit)
            self._buttons[signal_name] = button
            body.addWidget(button)

        body.addWidget(Divider())
        # 只影響畫面的開關一律叫「顯示…」（用詞表見 UI_RULES.md）
        self.show_whitespace_toggle = ToggleSwitch("顯示內文空格")
        body.addWidget(self.show_whitespace_toggle)
        body.addStretch(1)

    def button(self, signal_name: str) -> HoverIconButton:
        return self._buttons[signal_name]

    def set_colors(self, tokens):
        for button in self._buttons.values():
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)

    def set_actions_enabled(self, enabled: bool):
        for button in self._buttons.values():
            button.setEnabled(enabled)
