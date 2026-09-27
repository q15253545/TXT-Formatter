"""「排版設定」卡片（工具列「排版設定」）：排版開關、下拉與套用按鈕。

開關狀態存在面板自己身上；呼叫端只在套用格式時用 current_options() 讀一次。
「合併下行標題」在章節管理（預覽＋套用到本文），不在這裡。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from core.format_options import FormatOptions
from . import i18n, icons
from .widgets import Divider, IconButton, PanelScroll, ToggleSwitch, make_card_header

# 順序照排版時想事情的順序：空行 → 段落 → 縮排 → 對話。
_CHECKBOX_FIELDS = [
    ("remove_extra_empty", "刪除所有空行"),
    ("add_empty", "章節間插入空行"),
    ("format_title", "標題前後插入空行"),
    ("add_paragraph_empty", "段落間插入空行"),
    ("reflow_paragraphs", "整理段落換行"),
    ("remove_extra_spaces", "刪除多餘空格"),
    ("auto_indent", "增加縮排"),
    ("remove_indent", "去除縮排"),
    ("format_dialogue", "對話框引號格式化"),
]

# 開關不放滑鼠提示：切換之後在狀態列說一句這個開關會做什麼。
OPTION_STATUS = {
    "remove_extra_empty": "排版時刪掉所有空行",
    "add_empty": "排版時在每一章的標題前面空兩行，章與章之間隔開",
    "format_title": "排版時在標題前後各留一行空行",
    "add_paragraph_empty": "排版時在段落之間插入一行空行",
    "reflow_paragraphs": "排版時把固定字數斷開的段落接回同一行",
    "remove_extra_spaces": "排版時刪掉行尾與中文字之間的空白，中英數之間的空格保留",
    "auto_indent": "排版時每段開頭加兩個全形空格",
    "remove_indent": "排版時去掉每段開頭的空白",
    "format_dialogue": "排版時把對話的引號統一成「」『』",
}

NUM_STYLE_CHOICES = ["保留原文", "中文數字", "阿拉伯數字"]
SEP_STYLE_CHOICES = ["保留原文", "半形空格", "全形空格", "冒號"]
PUNCT_CHOICES = ["不轉換", "轉全形", "轉半形"]
DIGIT_CHOICES = ["不轉換", "轉全形", "轉半形"]


def describe_options(options: FormatOptions) -> list:
    """目前會生效的排版項目，用來寫在確認視窗裡。

    使用者按「套用格式到選取章節」時看不到格式選項面板（它在另一張卡片上），
    所以要把即將套用的項目列出來，不能只說「要套用格式嗎」。"""
    items = [label for field, label in _CHECKBOX_FIELDS if getattr(options, field)]
    if options.num_style != "保留原文":
        items.append(f"章節編號：{options.num_style}")
    if options.sep_style != "保留原文":
        items.append(f"編號與標題間隔：{options.sep_style}")
    if options.normalize_punct:
        items.append("標點符號：轉全形")
    elif options.halfwidth_punct:
        items.append("標點符號：轉半形")
    if options.fullwidth_digits:
        items.append("數字：轉全形")
    elif options.halfwidth_digits:
        items.append("數字：轉半形")
    return items


class OptionsPanel(QWidget):
    apply_requested = Signal()
    apply_selected_requested = Signal()
    # 使用者切換了某個開關：（開關名稱, 開或關, 一句說明）→ 主視窗顯示在狀態列
    option_toggled = Signal(str, bool, str)
    save_one_click_requested = Signal()
    closed = Signal()

    def __init__(self, initial: FormatOptions, parent=None):
        super().__init__(parent)
        self._checkboxes: dict[str, QCheckBox] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header, header_layout = make_card_header("排版設定")
        self.close_button = IconButton("panel-left-close", "收起排版設定", size=16)
        self.close_button.clicked.connect(self.closed.emit)
        header_layout.addWidget(self.close_button)
        root.addWidget(header)

        # 視窗矮時選項要能捲動；「套用格式」固定在捲動區外面的底部，不會被捲走。
        scroll = PanelScroll()
        root.addWidget(scroll, 1)

        body = QVBoxLayout(scroll.content)
        body.setContentsMargins(16, 12, 16, 12)
        body.setSpacing(12)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for index, (field, label) in enumerate(_CHECKBOX_FIELDS):
            # 設定類選項用開關：文字在左、開關靠右
            checkbox = ToggleSwitch(label)
            checkbox.setChecked(getattr(initial, field))
            # clicked 只在使用者自己切換時送出；還原設定、一鍵排版後歸零不會洗掉狀態列
            checkbox.clicked.connect(
                lambda on, label=label, field=field: self.option_toggled.emit(label, on, OPTION_STATUS[field]))
            self._checkboxes[field] = checkbox
            grid.addWidget(checkbox, index, 0)
        body.addLayout(grid)
        body.addWidget(Divider())

        combos = QGridLayout()
        combos.setHorizontalSpacing(10)
        combos.setVerticalSpacing(8)
        combos.setColumnStretch(1, 1)
        body.addLayout(combos)

        punct_default = "轉全形" if initial.normalize_punct else "轉半形" if initial.halfwidth_punct else "不轉換"
        digit_default = "轉全形" if initial.fullwidth_digits else "轉半形" if initial.halfwidth_digits else "不轉換"
        self.num_style_combo = self._add_combo(combos, 0, "章節編號", NUM_STYLE_CHOICES, initial.num_style)
        self.sep_style_combo = self._add_combo(combos, 1, "編號與標題間隔", SEP_STYLE_CHOICES, initial.sep_style)
        self.punct_combo = self._add_combo(combos, 2, "標點符號", PUNCT_CHOICES, punct_default)
        self.digit_combo = self._add_combo(combos, 3, "數字", DIGIT_CHOICES, digit_default)

        body.addStretch(1)

        # 底部三顆按鈕固定在捲動區外面、不跟著捲動，上面一條分隔線跟選項分開：
        # 存成一鍵排版的組合、只排目錄選取的章、排整份。
        root.addWidget(Divider())
        footer = QVBoxLayout()
        footer.setContentsMargins(16, 12, 16, 14)
        footer.setSpacing(8)
        self.save_one_click_button = QPushButton("套用到一鍵排版")
        self.save_one_click_button.clicked.connect(self.save_one_click_requested.emit)
        footer.addWidget(self.save_one_click_button)
        self.apply_selected_button = QPushButton("套用格式到選取章節")
        self.apply_selected_button.clicked.connect(self.apply_selected_requested.emit)
        footer.addWidget(self.apply_selected_button)
        self.apply_button = QPushButton("套用格式到全文")
        self.apply_button.setObjectName("primary")
        self.apply_button.setIcon(icons.make_icon("check", "#FFFFFF", 16))
        self.apply_button.clicked.connect(self.apply_requested.emit)
        self._primary_text = self._disabled_text = "#FFFFFF"
        footer.addWidget(self.apply_button)
        root.addLayout(footer)

    @staticmethod
    def _add_combo(grid: QGridLayout, row: int, label_text, choices, current_value) -> QComboBox:
        """標籤放左、下拉放右——欄位比較窄時，這樣比標籤壓在上面省一半高度。"""
        label = QLabel(label_text)
        label.setObjectName("fileLabel")
        combo = QComboBox()
        combo.addItems(choices)
        if current_value in choices:
            i18n.set_combo_value(combo, current_value)
        grid.addWidget(label, row, 0)
        grid.addWidget(combo, row, 1)
        return combo

    def current_options(self, structure_mode: str) -> FormatOptions:
        punct = i18n.combo_value(self.punct_combo)
        digit = i18n.combo_value(self.digit_combo)
        values = {field: checkbox.isChecked() for field, checkbox in self._checkboxes.items()}
        return FormatOptions(
            **values,
            normalize_punct=(punct == "轉全形"),
            halfwidth_punct=(punct == "轉半形"),
            fullwidth_digits=(digit == "轉全形"),
            halfwidth_digits=(digit == "轉半形"),
            num_style=i18n.combo_value(self.num_style_combo),
            sep_style=i18n.combo_value(self.sep_style_combo),
            structure=structure_mode,
        )

    def set_icon_colors(self, color: str, primary_text: str, accent: str = "", hover: str = "",
                        disabled: str = ""):
        self.close_button.set_colors(color, hover or color, color)
        self._primary_text, self._disabled_text = primary_text, disabled or primary_text
        self._refresh_apply_icon()

    def _refresh_apply_icon(self):
        color = self._primary_text if self.apply_button.isEnabled() else self._disabled_text
        self.apply_button.setIcon(icons.make_icon("check", color, 16))

    def set_apply_enabled(self, enabled: bool):
        self.apply_button.setEnabled(enabled)
        self._refresh_apply_icon()

    def options_state(self) -> dict:
        """目前的勾選與下拉，存成可以寫進 JSON 的樣子（下次開程式時還原）。"""
        return {
            "checks": {field: checkbox.isChecked() for field, checkbox in self._checkboxes.items()},
            "num_style": i18n.combo_value(self.num_style_combo),
            "sep_style": i18n.combo_value(self.sep_style_combo),
            "punct": i18n.combo_value(self.punct_combo),
            "digit": i18n.combo_value(self.digit_combo),
        }

    def restore_options_state(self, state: dict):
        """還原 options_state() 存下來的值；認不得的欄位直接略過。"""
        checks = state.get("checks") if isinstance(state.get("checks"), dict) else {}
        for field, checked in checks.items():
            if field in self._checkboxes:
                self._checkboxes[field].setChecked(bool(checked))
        for key, combo, choices in (("num_style", self.num_style_combo, NUM_STYLE_CHOICES),
                                    ("sep_style", self.sep_style_combo, SEP_STYLE_CHOICES),
                                    ("punct", self.punct_combo, PUNCT_CHOICES),
                                    ("digit", self.digit_combo, DIGIT_CHOICES)):
            if state.get(key) in choices:
                i18n.set_combo_value(combo, state[key])

    def reset_to_defaults(self):
        """一鍵排版用自己的固定組合、不管面板目前勾了什麼；套用後把面板歸零，
        避免看起來像「這些勾選也是一鍵排版套用的」而造成誤解。"""
        for checkbox in self._checkboxes.values():
            checkbox.setChecked(False)
        for combo in (self.num_style_combo, self.sep_style_combo, self.punct_combo, self.digit_combo):
            combo.setCurrentIndex(0)
