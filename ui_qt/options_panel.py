"""「排版設定」卡片（工具列「排版設定」）：排版開關、下拉與套用按鈕。

開關狀態存在面板自己身上；呼叫端只在套用格式時用 current_options() 讀一次。
「合併下行標題」在章節管理（預覽＋套用到本文），不在這裡；只排選取的章在目錄右鍵。
"""

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QLabel, QPushButton, QStyle, QStyleOptionComboBox, QVBoxLayout, QWidget,
)

from core.format_options import FormatOptions
from core.paragraph_split import SPLIT_CHOICES, SPLIT_OFF
from core.text_format import QUOTE_KEEP, QUOTE_STYLES
from . import i18n, icons
from .widgets import Divider, IconButton, PanelScroll, ToggleSwitch, make_card_header

_CHECKBOX_FIELDS = [
    ("reflow_paragraphs", "整理段落換行"),
    ("remove_extra_spaces", "刪除多餘空格"),
]

# 開關不放滑鼠提示：切換之後在狀態列說一句這個開關會做什麼。
OPTION_STATUS = {
    "reflow_paragraphs": "排版時把固定字數斷開的段落接回同一行",
    "remove_extra_spaces": "排版時刪掉行尾與中文字之間的空白，中英數之間的空格保留",
}

# 空行、縮排用下拉直接寫出結果：分成幾個開關的話，有些組合會互相打架（兩種縮排同時開、
# 段落間空行卻沒刪掉原本的空行），也看不出幾個開關合起來是什麼樣子。
# 選項 → 要打開的排版欄位（沒列的都是關）；第一個是「不改」。
PARAGRAPH_CHOICES = {"保留原樣": {}, "不空行": {"remove_extra_empty": True},
                     "空一行": {"remove_extra_empty": True, "add_paragraph_empty": True}}
TITLE_SPACING_CHOICES = {"不另加": {}, "前後各一行": {"format_title": True},
                         "前兩行、後一行": {"add_empty": True, "format_title": True}}
INDENT_CHOICES = {"保留原樣": {}, "兩個全形空格": {"auto_indent": True},
                  "四個半形空格": {"auto_indent": True, "halfwidth_indent": True}, "不縮排": {"remove_indent": True}}


def paragraph_choice(options: FormatOptions) -> str:
    return "空一行" if options.add_paragraph_empty else "不空行" if options.remove_extra_empty else "保留原樣"


def title_spacing_choice(options: FormatOptions) -> str:
    return "前兩行、後一行" if options.add_empty else "前後各一行" if options.format_title else "不另加"


def indent_choice(options: FormatOptions) -> str:
    if options.auto_indent:
        return "四個半形空格" if options.halfwidth_indent else "兩個全形空格"
    return "不縮排" if options.remove_indent else "保留原樣"


NUM_STYLE_CHOICES = ["保留原文", "中文數字", "阿拉伯數字"]
SEP_STYLE_CHOICES = ["保留原文", "半形空格", "全形空格", "冒號"]
PUNCT_CHOICES = ["不轉換", "轉全形", "轉半形"]
DIGIT_CHOICES = ["不轉換", "轉全形", "轉半形"]


def describe_options(options: FormatOptions) -> list:
    """目前會生效的排版項目，用來寫在確認視窗裡。

    從目錄右鍵「套用格式到這幾章」時不一定看得到排版設定卡片，
    所以要把即將套用的項目列出來，不能只說「要套用格式嗎」。"""
    items = [label for field, label in _CHECKBOX_FIELDS if getattr(options, field)]
    for label, choice, unchanged in (("段落之間", paragraph_choice(options), "保留原樣"),
                                     ("標題前後", title_spacing_choice(options), "不另加"),
                                     ("段首縮排", indent_choice(options), "保留原樣")):
        if choice != unchanged:
            items.append(f"{label}：{choice}")
    if options.long_paragraph in SPLIT_CHOICES and options.long_paragraph != SPLIT_OFF:
        items.append(f"長段落：{options.long_paragraph}")
    if options.quote_style != QUOTE_KEEP:
        items.append(f"對話引號：{options.quote_style}")
    if options.num_style != "保留原文":
        items.append(f"章節編號：{options.num_style}")
    if options.sep_style != "保留原文":
        items.append(f"編號間隔：{options.sep_style}")
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
        scroll = self._scroll = PanelScroll()
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
        # 標籤最多四個字：標籤欄寬是最長的標籤，太長會把每個下拉都擠窄
        self.paragraph_combo = self._add_combo(combos, 0, "段落之間", list(PARAGRAPH_CHOICES),
                                               paragraph_choice(initial))
        self.title_spacing_combo = self._add_combo(combos, 1, "標題前後", list(TITLE_SPACING_CHOICES),
                                                   title_spacing_choice(initial))
        self.indent_combo = self._add_combo(combos, 2, "段首縮排", list(INDENT_CHOICES), indent_choice(initial))
        # 長段落：在引號外的句末拆開，分高、中信心（core/paragraph_split.py）
        self.long_combo = self._add_combo(combos, 3, "長段落", list(SPLIT_CHOICES),
                                          initial.long_paragraph if initial.long_paragraph in SPLIT_CHOICES
                                          else SPLIT_OFF)
        # 對話引號：有方向的引號照原本的內外層對應，一行一行轉（core/text_format.convert_quotes）
        self.quote_combo = self._add_combo(combos, 4, "對話引號", list(QUOTE_STYLES), initial.quote_style)
        self.num_style_combo = self._add_combo(combos, 5, "章節編號", NUM_STYLE_CHOICES, initial.num_style)
        self.sep_style_combo = self._add_combo(combos, 6, "編號間隔", SEP_STYLE_CHOICES, initial.sep_style)
        self.punct_combo = self._add_combo(combos, 7, "標點符號", PUNCT_CHOICES, punct_default)
        self.digit_combo = self._add_combo(combos, 8, "數字", DIGIT_CHOICES, digit_default)

        body.addStretch(1)

        # 底部按鈕固定在捲動區外面、不跟著捲動，上面一條分隔線跟選項分開：
        # 存成一鍵排版的組合（不改本文，跟下面的套用再用一條線隔開）、排整份。
        root.addWidget(Divider())
        footer = QVBoxLayout()
        footer.setContentsMargins(16, 12, 16, 14)
        footer.setSpacing(8)
        self.save_one_click_button = QPushButton("保存到一鍵排版")
        self.save_one_click_button.clicked.connect(self.save_one_click_requested.emit)
        footer.addWidget(self.save_one_click_button)
        footer.addSpacing(4)
        footer.addWidget(Divider())
        footer.addSpacing(4)
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

    def fit_combos(self):
        """下拉框至少放得下最長的選項。全域規則是下拉框不照選項撐寬（見 widgets.AppWidgetPolisher），
        但這張卡片的選項都短，照實際字寬（含樣式表的內距）算，字型變大時卡片跟著變寬、不截字。
        套用主題、切換繁簡之後由主視窗重算卡片寬度前呼叫。"""
        for combo in self.findChildren(QComboBox):
            metrics = combo.fontMetrics()
            longest = max(metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
            option = QStyleOptionComboBox()
            combo.initStyleOption(option)
            size = combo.style().sizeFromContents(QStyle.ContentsType.CT_ComboBox, option,
                                                  QSize(longest, metrics.height()), combo)
            combo.setMinimumWidth(size.width())
        # the combos sit in the scroll area's own layout, which re-lays out only on its next event:
        # do it now so the card's minimum width measured right after already counts them
        layout = self._scroll.content.layout()
        layout.invalidate()
        layout.activate()

    def current_options(self, structure_mode: str) -> FormatOptions:
        punct = i18n.combo_value(self.punct_combo)
        digit = i18n.combo_value(self.digit_combo)
        values = {field: checkbox.isChecked() for field, checkbox in self._checkboxes.items()}
        for combo, choices in ((self.paragraph_combo, PARAGRAPH_CHOICES),
                               (self.title_spacing_combo, TITLE_SPACING_CHOICES),
                               (self.indent_combo, INDENT_CHOICES)):
            values.update(choices.get(i18n.combo_value(combo), {}))
        return FormatOptions(
            **values,
            normalize_punct=(punct == "轉全形"),
            halfwidth_punct=(punct == "轉半形"),
            fullwidth_digits=(digit == "轉全形"),
            halfwidth_digits=(digit == "轉半形"),
            long_paragraph=i18n.combo_value(self.long_combo),
            quote_style=i18n.combo_value(self.quote_combo),
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
            "paragraph": i18n.combo_value(self.paragraph_combo),
            "title_spacing": i18n.combo_value(self.title_spacing_combo),
            "indent": i18n.combo_value(self.indent_combo),
            "long_paragraph": i18n.combo_value(self.long_combo),
            "quote_style": i18n.combo_value(self.quote_combo),
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
        for key, combo, choices in (("paragraph", self.paragraph_combo, list(PARAGRAPH_CHOICES)),
                                    ("title_spacing", self.title_spacing_combo, list(TITLE_SPACING_CHOICES)),
                                    ("indent", self.indent_combo, list(INDENT_CHOICES)),
                                    ("long_paragraph", self.long_combo, list(SPLIT_CHOICES)),
                                    ("quote_style", self.quote_combo, list(QUOTE_STYLES)),
                                    ("num_style", self.num_style_combo, NUM_STYLE_CHOICES),
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
        for combo in (self.paragraph_combo, self.title_spacing_combo, self.indent_combo, self.long_combo,
                      self.quote_combo,
                      self.num_style_combo,
                      self.sep_style_combo, self.punct_combo, self.digit_combo):
            combo.setCurrentIndex(0)
