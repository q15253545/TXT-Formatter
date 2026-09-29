"""左側「內容檢查」卡片（左側圖示列「檢查」）：掃描無關連內容、作者感言與作品資訊、
標點校對、繁簡轉換，以及只影響畫面的開關：本文字色、內文空格。

本文字色一個開關同時標廣告與作者感言、作品資訊；要標哪些類型照兩個掃描視窗裡（記住的）勾選，
想只看其中一種，把另一個視窗的類型都取消就好。顏色的意思寫在檔名列的「說明」裡。
字色開著時下面多一段快速處理：照信心篩選標出來的內容，一筆一筆跳過去看、刪掉；
整批刪除在掃描視窗裡，可以先逐筆看過再刪。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import i18n
from .widgets import Divider, HoverIconButton, IconButton, PanelScroll, ToggleSwitch, make_card_header

_ACTIONS = [
    ("scan-search", "掃描無關連內容", "ad_scan_requested"),
    ("notebook-pen", "作者感言與作品資訊", "note_scan_requested"),
    ("quote", "標點校對", "quote_check_requested"),
    ("text-select", "繁簡轉換", "script_convert_requested"),
]
CONFIDENCE_LEVELS = ("高", "中", "低")
DEFAULT_MARK_CONFIDENCE = {"高", "中"}


class ContentPanel(QWidget):
    ad_scan_requested = Signal()
    note_scan_requested = Signal()
    quote_check_requested = Signal()
    script_convert_requested = Signal()
    marking_changed = Signal()          # 本文字色開關變了
    confidence_changed = Signal()       # 字色要標哪些信心變了（不必重掃，只重畫）
    previous_mark_requested = Signal()
    next_mark_requested = Signal()
    delete_mark_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: dict[str, HoverIconButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, header_layout = make_card_header("內容檢查")
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
        # 只影響畫面的開關一律叫「顯示…」（用詞表見 UI_RULES.md）。同一行兩種都是時用廣告的顏色。
        self.mark_toggle = ToggleSwitch("顯示本文字色")
        self.mark_toggle.toggled.connect(self._on_mark_toggled)
        body.addWidget(self.mark_toggle)
        body.addWidget(self._build_mark_tools())
        self.show_whitespace_toggle = ToggleSwitch("顯示內文空格")
        body.addWidget(self.show_whitespace_toggle)
        body.addStretch(1)

    def _build_mark_tools(self) -> QWidget:
        """字色開著時才出現：信心篩選（積木，跟辨識章節同一種）、第幾筆／上一筆／下一筆（跟尋找取代同一種）、
        刪除這筆。"""
        self.mark_tools = QWidget()
        tools = QVBoxLayout(self.mark_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        tools.setSpacing(8)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(6)
        label = QLabel("信心")
        label.setObjectName("fileLabel")
        chip_row.addWidget(label)
        self.confidence_chips: dict[str, QPushButton] = {}
        for level in CONFIDENCE_LEVELS:
            chip = QPushButton(level)
            chip.setObjectName("blockChip")
            chip.setCheckable(True)
            chip.setChecked(level in DEFAULT_MARK_CONFIDENCE)
            chip.clicked.connect(lambda _checked=False: self.confidence_changed.emit())
            self.confidence_chips[level] = chip
            chip_row.addWidget(chip, 1)
        tools.addLayout(chip_row)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        self.mark_position_label = QLabel("0/0")
        self.mark_position_label.setObjectName("fileLabel")
        i18n.skip(self.mark_position_label)
        nav_row.addWidget(self.mark_position_label)
        nav_row.addStretch(1)
        self.previous_mark_button = IconButton("chevron-left", "上一筆", size=16)
        self.previous_mark_button.clicked.connect(self.previous_mark_requested.emit)
        self.next_mark_button = IconButton("chevron-right", "下一筆", size=16)
        self.next_mark_button.clicked.connect(self.next_mark_requested.emit)
        nav_row.addWidget(self.previous_mark_button)
        nav_row.addWidget(self.next_mark_button)
        tools.addLayout(nav_row)

        self.delete_mark_button = QPushButton("刪除這筆")
        self.delete_mark_button.clicked.connect(self.delete_mark_requested.emit)
        tools.addWidget(self.delete_mark_button)
        self.mark_tools.setVisible(False)
        return self.mark_tools

    def _on_mark_toggled(self, on: bool):
        self.mark_tools.setVisible(on)
        self.marking_changed.emit()

    def button(self, signal_name: str) -> HoverIconButton:
        return self._buttons[signal_name]

    def set_colors(self, tokens):
        for button in (self.previous_mark_button, self.next_mark_button):
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        for button in self._buttons.values():
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)

    def set_actions_enabled(self, enabled: bool):
        for button in self._buttons.values():
            button.setEnabled(enabled)

    def marking(self) -> set:
        """目前要標示哪些："ad"、"note"（一個開關，兩種一起）。"""
        return {"ad", "note"} if self.mark_toggle.isChecked() else set()

    def set_marking(self, kinds):
        """程式自己設定（還原上次狀態）時不送出 marking_changed。"""
        self.mark_toggle.blockSignals(True)
        self.mark_toggle.setChecked(bool(kinds))
        self.mark_toggle.blockSignals(False)
        self.mark_tools.setVisible(bool(kinds))

    def mark_confidence(self) -> set:
        return {level for level, chip in self.confidence_chips.items() if chip.isChecked()}

    def set_mark_confidence(self, levels):
        for level, chip in self.confidence_chips.items():
            chip.setChecked(level in levels)

    def set_mark_position(self, current: int, total: int):
        """第幾筆／共幾筆；current 是 -1 表示還沒選到任何一筆。"""
        self.mark_position_label.setText(f"{current + 1 if current >= 0 else 0}/{total}")
        self.delete_mark_button.setEnabled(current >= 0)
