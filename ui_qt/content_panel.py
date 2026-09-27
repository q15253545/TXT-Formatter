"""左側「內容檢查」卡片（工具列「內容檢查」）：掃描無關連內容、作者感言與作品資訊、
標點校對、繁簡轉換，以及「在本文標示顏色」的兩個開關、顯示內文空格與章節標記的開關。

字色標示要標哪些類型，照兩個掃描視窗裡（記住的）勾選；顏色的意思寫在檔名列的「說明」裡。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QScrollArea, QVBoxLayout, QWidget

from .widgets import Divider, HoverIconButton, IconButton, ToggleSwitch, make_card_header

_ACTIONS = [
    ("scan-search", "掃描無關連內容", "ad_scan_requested"),
    ("notebook-pen", "作者感言與作品資訊", "note_scan_requested"),
    ("quote", "標點校對", "quote_check_requested"),
    ("file-text", "章節字數", "word_count_requested"),
    ("text-select", "繁簡轉換", "script_convert_requested"),
]


class ContentPanel(QWidget):
    closed = Signal()
    ad_scan_requested = Signal()
    note_scan_requested = Signal()
    quote_check_requested = Signal()
    word_count_requested = Signal()
    script_convert_requested = Signal()
    marking_changed = Signal()          # 兩個字色標示開關任一個變了
    show_markers_toggled = Signal(bool)
    strip_markers_toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: dict[str, HoverIconButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, header_layout = make_card_header("內容檢查")
        self.close_button = IconButton("panel-left-close", "收起內容檢查", size=16)
        self.close_button.clicked.connect(self.closed.emit)
        header_layout.addWidget(self.close_button)
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("panelScrollContent")
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        body = QVBoxLayout(content)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(10)
        for icon_name, text, signal_name in _ACTIONS:
            button = HoverIconButton(icon_name, text)
            button.clicked.connect(getattr(self, signal_name).emit)
            self._buttons[signal_name] = button
            body.addWidget(button)

        body.addWidget(Divider())
        # 廣告、作者感言分開開關；同一行兩種都是時用廣告的顏色。
        self.mark_ad_toggle = ToggleSwitch("標示廣告")
        self.mark_note_toggle = ToggleSwitch("標示作者感言與作品資訊")
        for toggle in (self.mark_ad_toggle, self.mark_note_toggle):
            toggle.toggled.connect(lambda _checked: self.marking_changed.emit())
            body.addWidget(toggle)

        # 顯示內文空格；
        # 章節標記（[::] 這類，寫在檔案裡保存目錄的手動調整）：顯示與否、匯出時要不要拿掉
        body.addWidget(Divider())
        self.show_whitespace_toggle = ToggleSwitch("顯示內文空格")
        body.addWidget(self.show_whitespace_toggle)
        self.show_markers_toggle = ToggleSwitch("顯示章節標記")
        self.show_markers_toggle.toggled.connect(self.show_markers_toggled.emit)
        body.addWidget(self.show_markers_toggle)
        self.strip_markers_toggle = ToggleSwitch("匯出時移除章節標記")
        self.strip_markers_toggle.toggled.connect(self.strip_markers_toggled.emit)
        body.addWidget(self.strip_markers_toggle)
        body.addStretch(1)

    def button(self, signal_name: str) -> HoverIconButton:
        return self._buttons[signal_name]

    def set_colors(self, tokens):
        self.close_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        for button in self._buttons.values():
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)

    def set_actions_enabled(self, enabled: bool):
        for button in self._buttons.values():
            button.setEnabled(enabled)

    def marking(self) -> set:
        """目前要標示哪些："ad"、"note"。"""
        kinds = set()
        if self.mark_ad_toggle.isChecked():
            kinds.add("ad")
        if self.mark_note_toggle.isChecked():
            kinds.add("note")
        return kinds

    def set_marking(self, kinds):
        """程式自己設定（還原上次狀態）時不送出 marking_changed。"""
        for toggle, kind in ((self.mark_ad_toggle, "ad"), (self.mark_note_toggle, "note")):
            toggle.blockSignals(True)
            toggle.setChecked(kind in kinds)
            toggle.blockSignals(False)
