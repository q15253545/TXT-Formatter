"""本文上方的「逐筆檢查」列：非正文內容（廣告、作者感言與作品資訊、重複段落）一筆一筆跳過去看、刪掉。

從內容檢查卡片的「逐筆檢查」、非正文內容視窗的「在本文逐筆檢查」、F8 開始；開著的時候本文用字色標出來，
按 ✕（或 Esc）結束、字色一起收掉。要看哪幾類、哪幾種信心在「篩選」裡勾（記住，下次照舊）。
一次刪很多筆留給非正文內容視窗（有表格可以先看過）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from . import i18n
from .widgets import IconButton, IconTextButton, StayOpenMenu

REVIEW_TYPES = (("ad", "廣告與網頁字元"), ("note", "作者感言與作品資訊"), ("repeat", "重複段落"))
CONFIDENCE_LEVELS = ("高", "中", "低")
DEFAULT_REVIEW_TYPES = {"ad", "note"}          # 重複段落多半是作者慣用的句子，預設不看
DEFAULT_MARK_CONFIDENCE = {"高", "中"}


class ReviewBar(QFrame):
    previous_requested = Signal()
    next_requested = Signal()
    delete_requested = Signal()
    close_requested = Signal()
    types_changed = Signal()          # 要看的類型變了：要重新掃描
    confidence_changed = Signal()     # 信心篩選變了：掃描結果不變，重新篩就好

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("reviewBar")
        self._active = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 6, 10, 6)
        layout.setSpacing(8)
        title = QLabel("逐筆檢查")
        title.setObjectName("reviewTitle")
        layout.addWidget(title)
        self.position_label = QLabel("0 / 0")
        i18n.skip(self.position_label)
        layout.addWidget(self.position_label)
        self.info_label = QLabel("")
        self.info_label.setObjectName("fileLabel")
        layout.addWidget(self.info_label, 1)

        self.filter_button = IconTextButton("chevron-down", "篩選", size=14)
        self.filter_button.setObjectName("barToggle")
        menu = StayOpenMenu(self.filter_button)
        self._type_actions = {}
        self._confidence_actions = {}
        heading = menu.addAction(i18n.T("類型"))
        heading.setEnabled(False)
        for key, label in REVIEW_TYPES:
            action = menu.addAction("　" + i18n.T(label))
            action.setCheckable(True)
            action.setChecked(key in DEFAULT_REVIEW_TYPES)
            action.triggered.connect(self.types_changed.emit)
            self._type_actions[key] = action
        menu.addSeparator()
        heading = menu.addAction(i18n.T("信心"))
        heading.setEnabled(False)
        for level in CONFIDENCE_LEVELS:
            action = menu.addAction("　" + level)
            action.setCheckable(True)
            action.setChecked(level in DEFAULT_MARK_CONFIDENCE)
            action.triggered.connect(self.confidence_changed.emit)
            self._confidence_actions[level] = action
        self.filter_button.clicked.connect(
            lambda: menu.exec(self.filter_button.mapToGlobal(self.filter_button.rect().bottomLeft())))
        layout.addWidget(self.filter_button)

        self.previous_button = IconButton("chevron-left", "上一筆（Shift+F8）", size=16)
        self.previous_button.clicked.connect(self.previous_requested.emit)
        self.next_button = IconButton("chevron-right", "下一筆（F8）", size=16)
        self.next_button.clicked.connect(self.next_requested.emit)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        self.delete_button = QPushButton("刪除這筆")
        self.delete_button.clicked.connect(self.delete_requested.emit)
        layout.addWidget(self.delete_button)
        self.close_button = IconButton("x", "結束逐筆檢查（Esc）", size=14)
        self.close_button.clicked.connect(self.close_requested.emit)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.hide()

    # ------------------------------------------------------------------ 狀態

    def is_active(self) -> bool:
        return self._active

    def set_active(self, active: bool):
        self._active = active
        self.setVisible(active)

    def review_types(self) -> set:
        return {key for key, action in self._type_actions.items() if action.isChecked()}

    def set_review_types(self, keys):
        for key, action in self._type_actions.items():
            action.setChecked(key in keys)

    def marking(self) -> set:
        """本文要標哪幾種顏色："ad"（廣告、重複段落）、"note"（作者感言與作品資訊）；沒在檢查時是空的。"""
        if not self._active:
            return set()
        types = self.review_types()
        return ({"ad"} if types & {"ad", "repeat"} else set()) | ({"note"} if "note" in types else set())

    def mark_confidence(self) -> set:
        return {level for level, action in self._confidence_actions.items() if action.isChecked()}

    def set_mark_confidence(self, levels):
        for level, action in self._confidence_actions.items():
            action.setChecked(level in levels)

    def set_position(self, current: int, total: int, info: str = ""):
        """第幾筆／共幾筆、這一筆是什麼（類型 · 信心）；current 是 -1 表示還沒選到任何一筆。"""
        self.position_label.setText(f"{current + 1 if current >= 0 else 0} / {total}")
        i18n.set_text(self.info_label, info)
        self.delete_button.setEnabled(current >= 0)

    def set_colors(self, tokens):
        for button in (self.previous_button, self.next_button, self.close_button):
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        self.filter_button.set_colors(tokens.icon, tokens.icon_hover, tokens.checked_text, tokens.text_faint)
