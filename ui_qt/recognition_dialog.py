"""「辨識章節」對話框（章節管理的第一顆按鈕）：設定哪些寫法算章、卷——用積木組合，或自己寫正則；
特殊標題的層級；所有自動辨識共用的標題長度與章名結尾；「可疑章節」分頁列出像標題、還沒進目錄的行。

章、卷各一頁：左邊是清單（最上面是內建的「第N章」「第N卷」，只能改單位、不能刪；接著是積木組合、
自己寫的規則），右邊選到積木組合時是六欄積木（外框、前綴、數字、單位、分隔、章名），選到自己寫的規則時
換成正則編輯區（從範例產生、常用片段）。組合存成自訂規則（多一個 blocks 欄位，見 core/title_blocks.py）；
自己寫的規則沒有 blocks，照清單順序排在組合前面比對。"""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTabWidget, QVBoxLayout,
    QWidget,
)

from core.chapter_parse import (
    DEFAULT_TITLE_TAIL_ALLOWED, MAX_TITLE_LENGTH, SPECIAL_LEVELS, SPECIAL_WORDS, build_title_check, heading_word,
    looks_like_heading, may_have_heading_word, title_tail_groups, weak_candidate_to_user_rule, word_key,
)
from core import safe_regex
from core.persistence import RULES_FILE, _save_json
from core.title_blocks import (
    COLUMNS, auto_name, block_rule, blocks_from_sample, confidence, options, template, templates_by_confidence,
)
from core.title_markers import strip_persistent_title_marker
from core.user_rules import (
    SPECIAL_WORD_MAX, is_risky_pattern, match_user_chapter_rule, preset_rule, rule_from_sample, special_word_rule,
    special_word_variants,
)
from . import dialogs, i18n
from .sortable_table import PreviewTable, make_item, setup_columns
from .suspects_page import SuspectsPage
from .theme import active_tokens
from .widgets import (
    ContextPreview, GripSplitter, IconTextButton, ToggleSwitch, dialog_frame, flow_container, pinned_section, size_dialog,
    slider_with_spin,
)

_COLUMN_NAMES = {"frame": "外框", "prefix": "前綴", "number": "數字", "unit": "單位", "sep": "分隔", "title": "章名"}
_LEVEL_NAMES = {2: "章", 1: "卷"}
# 內建組合＝自動辨識：前綴、數字…都固定，只有單位可以開關（對到「關掉的字」）
_BUILTIN_UNITS = {2: ("章", "回", "節", "折", "幕"), 1: ("卷", "部", "篇", "集", "季")}
_BUILTIN_NAMES = {2: "第N章", 1: "第N卷"}
_NUMBER_SAMPLES = {"一二三": "十二", "123": "12", "全形１２": "１２", "壹貳參": "拾貳"}
_SEP_SHOWN = {"無": "", "空格": " ", ".": ". ", "-": " - "}
_NAMED_VOLUME = "named_volume"
_PART_VOLUME = "part_volume"
# 特殊標題清單裡用開關控制的內建格式（存成 preset 規則，不是關掉的字）
_TOGGLE_PRESETS = (_NAMED_VOLUME, _PART_VOLUME)
_SPECIAL_ROWS = ([(key, label) for key, label, _variants in SPECIAL_WORDS]
                 + [("番外", "番外"), ("外傳", "外傳"), ("終章", "終章"), (_NAMED_VOLUME, "名稱＋篇（青雲篇、上卷）"),
                    (_PART_VOLUME, "上中下部／卷／集（上部、書名（下部））")])
_LINES_SHOWN = 300
LEFT_MIN_WIDTH = 200       # combo list: the "+ 新增組合" button and a combo name still fit
LEFT_DEFAULT_WIDTH = 250
_SUSPECTS_TAB = 3
# 自己寫規則的常用片段（跟尋找取代的「＋」同一組寫法）：(名稱, 插入的正則, 說明)。
# 直接擺成一排小按鈕，點了插入到游標位置；說明是給不懂正則的人看的白話，不解釋語法。
_SNIPPETS = (
    ("章號", r"(?P<number>[0-9０-９]{1,8})", "數字章號（001、12…）；缺章檢查與連續編號都靠它"),
    ("中文章號", r"(?P<number>[一二兩两三四五六七八九十百千零〇]{1,8})", "中文數字章號（一、十二…）"),
    ("章名", r"(?P<title>\S.*?)", "章號後面那段標題文字"),
    ("任意文字", ".*", "任何字都可以，長度不限"),
    ("空白", r"\s*", "可有可無的空白"),
    ("行首", "^", "從這一行的開頭開始比對"),
    ("行尾", "$", "比對到這一行結束"),
)


def managed_rule(rule: dict) -> bool:
    """積木組合、名稱＋篇、自訂特殊標題（其餘是自己寫的正則規則）。"""
    return bool(rule.get("blocks")) or rule.get("preset") in _TOGGLE_PRESETS or bool(rule.get("special"))


def _builtin_blocks(level: int, disabled: set) -> dict:
    return {"frame": list(options("frame", level)), "prefix": ["第"], "number": ["一二三", "123", "全形１２"],
            "unit": [unit for unit in _BUILTIN_UNITS[level] if unit not in disabled],
            "sep": list(options("sep", level)), "title": "可有可無"}


def _copy_splitter(source: QSplitter, target: QSplitter):
    if target.sizes() != source.sizes():
        target.setSizes(source.sizes())


def _scrolling(page: QWidget) -> QScrollArea:
    """分頁內容放進捲動區：直立螢幕、視窗很窄或很矮時捲動，不會把視窗的最小尺寸撐到螢幕外。"""
    scroll = QScrollArea()
    scroll.setObjectName("panelScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    page.setObjectName("panelScrollContent")
    scroll.setWidget(page)
    return scroll


def is_written_rule(rule) -> bool:
    """清單上的一項是自己寫的正則規則（不是內建、不是積木組合）。"""
    return rule is not None and not rule.get("blocks")


def pattern_problem(pattern: str) -> str:
    """正則有問題時回傳一句說明（空白、寫錯）；可以用就回傳空字串。"""
    if not pattern.strip():
        return "規則是空的"
    try:
        safe_regex.compile(pattern, re.IGNORECASE)
    except safe_regex.errors as error:
        return f"正則錯誤：{error}"
    return ""


RISKY_WARNING = ("⚠ 這個正則有巢狀量詞（例如 (a+)+、(.*)*），在長段落上可能跑很久；"
                 "它會在每次重建目錄時對每一行執行，可能讓程式卡住")


def _count_text(collected: int, pending: int) -> str:
    if not collected and not pending:
        return "本文沒有"
    parts = ([f"已收錄 {collected}"] if collected else []) + ([f"未收錄 {pending}"] if pending else [])
    return "、".join(parts) + " 行"


class _LevelPage(QWidget):
    """章或卷的一頁：左邊組合清單，右邊積木。"""

    def __init__(self, dialog, level: int):
        super().__init__()
        self._dialog = dialog
        self.level = level
        self._last: dict = {}             # 每一欄最後點的選項：範例照這個顯示
        self._updating = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(0)
        # Combo list | blocks: a draggable divider, both sides keep a minimum width (the dialog's minimum
        # width follows, see RecognitionDialog._fit_minimum_width), so a narrow window never squashes the blocks.
        # 分隔線就是拖的地方（GripSplitter），跟積木／本文的行、表格／前後文的分隔同一種
        self.splitter = GripSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(29)
        layout.addWidget(self.splitter)

        left_host = QFrame()
        left_host.setObjectName("comboPane")
        left_host.setMinimumWidth(LEFT_MIN_WIDTH)
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        self.add_button = QPushButton("＋ 新增組合")
        self.add_button.setObjectName("menuButton")
        menu = QMenu(self.add_button)
        menu.addAction(i18n.T("空白組合"), self._add_blank)
        menu.addAction(i18n.T("自己寫規則（正則）…"), self._add_written)
        for grade, items in templates_by_confidence(level):
            menu.addSeparator()
            header = menu.addAction(i18n.T(f"{grade}信心"))
            header.setEnabled(False)
            for key, label in items:
                menu.addAction("　" + label, lambda key=key: self._add_template(key))
        self.add_button.setMenu(menu)
        left.addWidget(self.add_button)
        self.sample_input = QLineEdit()
        self.sample_input.setPlaceholderText(i18n.T("貼一行標題"))
        self.sample_input.returnPressed.connect(self._add_from_sample)
        left.addWidget(self.sample_input)
        self.sample_message = QLabel("")
        self.sample_message.setObjectName("fileLabel")
        self.sample_message.setWordWrap(True)
        self.sample_message.hide()
        left.addWidget(self.sample_message)
        self.combo_list = QListWidget()
        self.combo_list.setObjectName("comboList")
        self.combo_list.currentRowChanged.connect(lambda _row: self.show_current())
        left.addWidget(self.combo_list, 1)
        self.splitter.addWidget(left_host)

        right_host = QWidget()
        right = QVBoxLayout(right_host)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_label = QLabel("名稱")
        name_label.setObjectName("fileLabel")
        name_row.addWidget(name_label)
        self.name_input = QLineEdit()
        self.name_input.textEdited.connect(self._on_name_edited)
        name_row.addWidget(self.name_input, 1)
        self.confidence_label = QLabel("")
        self.confidence_label.setObjectName("confidence")
        name_row.addWidget(self.confidence_label)
        self.delete_button = QPushButton("刪除組合")
        self.delete_button.setObjectName("inlineLink")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.clicked.connect(self._delete_current)
        name_row.addWidget(self.delete_button)
        right.addLayout(name_row)
        # 積木在上、結果列＋「看本文的行」在下，中間的分隔可以拖：展開看本文的行時積木區自己捲動，
        # 改積木時下面的行數、表格跟著變，兩邊一起看得到。分隔線在積木跟結果列之間（結果列是下面那一區的標題，
        # 跟著下面走），跟其他視窗「線在兩區正中間、拖的就是那條線」一樣；收起來時沒有東西可以調，線也不顯示。
        # 捲動區跟內容都透明（panelScroll）：不然沒有欄底色的那幾欄會露出捲動區預設的底色（深色）
        blocks_scroll = _scrolling(self._build_blocks())
        blocks_scroll.setMinimumHeight(140)
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)
        # 選到自己寫的規則時，積木換成正則編輯區（名稱列、下面的結果列共用）
        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(blocks_scroll)
        self.editor_stack.addWidget(self._build_written_editor())
        top_layout.addWidget(self.editor_stack, 1)

        bar = QFrame()
        bar.setObjectName("resultBar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 8, 12, 8)
        self.result_label = QLabel("")
        self.result_label.setObjectName("resultLine")
        i18n.skip(self.result_label)
        bar_layout.addWidget(self.result_label, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("fileLabel")
        bar_layout.addWidget(self.count_label)
        # 展開、收起一段內容：跟檔名列的「書籍資料」同一種（文字＋上下箭頭）
        self.lines_button = IconTextButton("chevron-down", "看本文的行", checkable=True, size=14)
        self.lines_button.setObjectName("barToggle")
        tokens = active_tokens()
        self.lines_button.set_colors(tokens.icon, tokens.icon_hover, tokens.checked_text, tokens.text_faint)
        self.lines_button.toggled.connect(self._toggle_lines)
        bar_layout.addWidget(self.lines_button)
        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 0, 0, 0)
        lower_layout.setSpacing(10)
        lower_layout.addWidget(bar)
        self._lines_host = lower
        self.lines_table = PreviewTable(0, 2)
        self.lines_table.setHorizontalHeaderLabels(["狀態", "內容"])
        self.lines_table.verticalHeader().setVisible(False)
        self.lines_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lines_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.lines_table, {0: 80})
        self.lines_table.itemSelectionChanged.connect(self._on_line_selected)
        # 選到一列：下面顯示那一行加上前後文（跟非正文內容、可疑章節一樣）
        self.lines_preview = ContextPreview()
        self.lines_box = self.lines_preview.stacked_under(self.lines_table)
        self.lines_box.setMinimumHeight(160)
        self.lines_box.hide()
        lower_layout.addWidget(self.lines_box, 1)
        self.blocks_splitter = GripSplitter()
        self.blocks_splitter.addWidget(top)
        self.blocks_splitter.addWidget(lower)
        self.blocks_splitter.setStretchFactor(0, 1)
        self.blocks_splitter.setStretchFactor(1, 0)
        self._toggle_lines(False)
        right.addWidget(self.blocks_splitter, 1)
        right_host.setMinimumWidth(right_host.minimumSizeHint().width())
        self.splitter.addWidget(right_host)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([LEFT_DEFAULT_WIDTH, 10000])

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(120)
        self._count_timer.timeout.connect(self._update_counts)
        self._line_rows: list = []

    # ------------------------------------------------------------------ 版面

    def _build_written_editor(self) -> QWidget:
        """自己寫的規則：從範例產生、正則、常用片段（點了插入到游標位置）、片段說明與錯誤提示。"""
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        labels = []
        for row, text in enumerate(("從範例產生", "規則", "插入")):
            label = QLabel(text)
            label.setObjectName("fileLabel")
            labels.append(label)
            grid.addWidget(label, row, 0)
        sample_row = QHBoxLayout()
        sample_row.setSpacing(8)
        self.written_sample = QLineEdit()
        self.written_sample.setPlaceholderText(i18n.T("貼上一行章節標題，例如：Chapter 12 過河"))
        self.written_sample.returnPressed.connect(self._written_from_sample)
        sample_row.addWidget(self.written_sample, 1)
        generate = QPushButton("產生")
        generate.clicked.connect(self._written_from_sample)
        sample_row.addWidget(generate)
        grid.addLayout(sample_row, 0, 1)
        self.pattern_input = QLineEdit()
        self.pattern_input.setObjectName("patternInput")
        self.pattern_input.textEdited.connect(self._on_pattern_edited)
        grid.addWidget(self.pattern_input, 1, 1)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for name, text, hint in _SNIPPETS:
            chip = QPushButton(name)
            chip.setObjectName("blockChip")
            chip.clicked.connect(lambda _checked=False, text=text, hint=hint, name=name:
                                 self._insert_snippet(name, text, hint))
            chips.addWidget(chip)
        chips.addStretch(1)
        grid.addLayout(chips, 2, 1)
        self.written_message = QLabel("")
        self.written_message.setObjectName("fileLabel")
        self.written_message.setWordWrap(True)
        grid.addWidget(self.written_message, 3, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(4, 1)
        return host

    def _build_blocks(self) -> QWidget:
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(0)
        self.chips: dict = {}
        self.examples: dict = {}
        for index, column in enumerate(COLUMNS):
            if index % 2 == 0:
                band = QFrame()
                band.setObjectName("blockBand")
                grid.addWidget(band, 0, index)
            cell_host = QWidget()
            cell = QVBoxLayout(cell_host)
            cell.setContentsMargins(6, 8, 6, 8)
            cell.setSpacing(6)
            head = QLabel(_COLUMN_NAMES[column])
            head.setObjectName("blockHead")
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.addWidget(head)
            rule = QFrame()
            rule.setObjectName("blockRule")
            rule.setFixedHeight(2)
            cell.addWidget(rule)
            example = QLabel("")
            example.setObjectName("blockExample")
            example.setAlignment(Qt.AlignmentFlag.AlignCenter)
            i18n.skip(example)
            self.examples[column] = example
            cell.addWidget(example)
            self.chips[column] = {}
            for option in options(column, self.level):
                chip = QPushButton(option)
                chip.setObjectName("blockChip")
                chip.setCheckable(True)
                chip.clicked.connect(lambda _checked=False, column=column, option=option: self._on_chip(column, option))
                self.chips[column][option] = chip
                cell.addWidget(chip)
            cell.addStretch(1)
            grid.addWidget(cell_host, 0, index)
            grid.setColumnStretch(index, 1)
        return host

    # ------------------------------------------------------------------ 組合清單

    def entries(self) -> list:
        """清單上的每一項：內建（None）、積木組合、這一層的自己寫的規則。"""
        written = [rule for rule in self._dialog._plain if rule.get("level", 2) == self.level]
        return [None] + self._dialog.combos[self.level] + written

    def current_rule(self):
        row = self.combo_list.currentRow()
        entries = self.entries()
        return entries[row] if 0 <= row < len(entries) else None

    def blocks(self, rule=None) -> dict:
        if rule is None:
            return _builtin_blocks(self.level, self._dialog.disabled)
        return rule["blocks"]

    def rebuild_list(self, select: int = 0):
        self._updating = True
        self.combo_list.clear()
        for index, rule in enumerate(self.entries()):
            item = QListWidgetItem()
            row = self._list_row(rule, index)
            item.setSizeHint(row.sizeHint())
            self.combo_list.addItem(item)
            self.combo_list.setItemWidget(item, row)
        self._updating = False
        self.combo_list.setCurrentRow(max(0, min(select, self.combo_list.count() - 1)))
        self.show_current()

    def _rebuild_selecting(self, rule):
        """清單順序是內建＋積木組合＋自己寫的規則：新組合不在最後一列，要照物件找它在哪。"""
        self.rebuild_list(next(i for i, entry in enumerate(self.entries()) if entry is rule))

    def _list_row(self, rule, index: int) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(8, 6, 6, 6)
        layout.setSpacing(6)
        text = QVBoxLayout()
        text.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(6)
        name = QLabel(_BUILTIN_NAMES[self.level] if rule is None else rule["name"])
        if rule is not None:
            i18n.skip(name)
        top.addWidget(name)
        if rule is not None and not is_written_rule(rule):
            badge = QLabel(confidence(rule["blocks"]))
            badge.setObjectName("confidence")
            top.addWidget(badge)
        top.addStretch(1)
        text.addLayout(top)
        info = QLabel("內建" if rule is None else "自己寫" if is_written_rule(rule) else "")
        info.setObjectName("fileLabel")
        text.addWidget(info)
        layout.addLayout(text, 1)
        toggle = ToggleSwitch("", fill=False)
        toggle.setChecked(self._entry_enabled(rule))
        toggle.clicked.connect(lambda checked, index=index: self._on_entry_toggled(index, checked))
        layout.addWidget(toggle)
        row.name_label, row.info_label, row.toggle = name, info, toggle
        return row

    def _entry_enabled(self, rule) -> bool:
        if rule is None:
            return any(unit not in self._dialog.disabled for unit in _BUILTIN_UNITS[self.level])
        return bool(rule.get("enabled", True))

    def _row_widget(self, index: int):
        return self.combo_list.itemWidget(self.combo_list.item(index))

    def _refresh_row(self, index: int):
        widget = self._row_widget(index)
        if widget is None:
            return
        rule = self.entries()[index]
        if rule is not None:
            widget.name_label.setText(rule["name"])
            badge = widget.findChild(QLabel, "confidence")
            if badge is not None and not is_written_rule(rule):
                badge.setText(confidence(rule["blocks"]))
        widget.toggle.blockSignals(True)
        widget.toggle.setChecked(self._entry_enabled(rule))
        widget.toggle.blockSignals(False)

    def _on_entry_toggled(self, index: int, checked: bool):
        rule = self.entries()[index]
        if rule is None:
            units = set(_BUILTIN_UNITS[self.level])
            if checked:
                self._dialog.disabled -= units
            else:
                self._dialog.disabled |= units
        else:
            rule["enabled"] = checked
        if index == self.combo_list.currentRow():
            self.show_current()

    def _add_rule(self, blocks: dict):
        rule = block_rule(blocks, self.level)
        self._dialog.combos[self.level].append(rule)
        self._last = {}
        self._rebuild_selecting(rule)
        self.name_input.setFocus()

    def _add_blank(self):
        self._add_rule({"frame": ["無"], "prefix": ["無"], "number": ["123"], "unit": ["無"], "sep": ["空格"],
                        "title": "要有"})

    def _add_template(self, key: str):
        _level, blocks = template(key)
        self._add_rule(blocks)

    def _add_from_sample(self):
        """貼一行標題：積木組得出來就建積木組合；組不出來（英文、特殊寫法）改建一條自己寫的規則。"""
        sample = self.sample_input.text().strip()
        if not sample:
            return
        blocks = blocks_from_sample(sample, self.level)
        if blocks is not None:
            self.sample_message.clear()
            self.sample_message.hide()
            self.sample_input.clear()
            self._add_rule(blocks)
            return
        rule = rule_from_sample(sample)
        if rule is None:
            i18n.set_text(self.sample_message, "這一行找不到編號")
            self.sample_message.show()
            return
        self.sample_input.clear()
        self._add_written(rule)
        i18n.set_text(self.sample_message, "積木組不出這種寫法，已建成自己寫的規則")
        self.sample_message.show()

    def _add_written(self, rule=None):
        """新增一條自己寫的規則（從範例產生的，或空白的讓使用者自己寫），放在這一層的最後面。"""
        existing = {item["name"] for item in self._dialog._plain}
        number = len(self._dialog._plain) + 1
        while f"自訂規則 {number}" in existing:
            number += 1
        rule = dict(rule or {"pattern": ""}, level=self.level, enabled=True)
        rule["name"] = rule.get("name") or f"自訂規則 {number}"
        self._dialog._plain.append(rule)
        self._rebuild_selecting(rule)
        (self.pattern_input if not rule["pattern"] else self.name_input).setFocus()

    def _written_from_sample(self):
        rule = self.current_rule()
        sample = self.written_sample.text().strip()
        if not is_written_rule(rule) or not sample:
            return
        generated = rule_from_sample(sample)
        if generated is None:
            i18n.set_text(self.written_message, "這一行找不到章號（數字或中文數字），無法自動產生規則")
            return
        rule["pattern"] = generated["pattern"]
        self.pattern_input.setText(rule["pattern"])
        self._on_pattern_edited(rule["pattern"])

    def _insert_snippet(self, name: str, text: str, hint: str):
        rule = self.current_rule()
        if not is_written_rule(rule):
            return
        self.pattern_input.insert(text)
        self.pattern_input.setFocus()
        self._on_pattern_edited(self.pattern_input.text())
        if not self.written_message.property("problem"):
            i18n.set_text(self.written_message, f"{name}：{hint}")

    def _on_pattern_edited(self, text: str):
        """打字就更新規則、重算本文有幾行符合（不用另外按測試）。"""
        rule = self.current_rule()
        if not is_written_rule(rule):
            return
        rule["pattern"] = text.strip()
        problem = pattern_problem(rule["pattern"])
        if not problem and is_risky_pattern(rule["pattern"]):
            problem = RISKY_WARNING
        self.written_message.setProperty("problem", bool(problem))
        i18n.set_text(self.written_message, problem)
        self.count_label.setText("…")
        self._count_timer.start()

    def _delete_current(self):
        row = self.combo_list.currentRow()
        rule = self.current_rule()
        if row <= 0 or rule is None:
            return
        if is_written_rule(rule):
            self._dialog._plain = [item for item in self._dialog._plain if item is not rule]
        else:
            self._dialog.combos[self.level] = [item for item in self._dialog.combos[self.level] if item is not rule]
        self.rebuild_list(row - 1)

    def _on_name_edited(self, text: str):
        rule = self.current_rule()
        if rule is None:
            return
        name = text.strip()
        if is_written_rule(rule):
            rule["name"] = name or rule["name"]
            self._refresh_row(self.combo_list.currentRow())
            return
        rule["name"] = name or auto_name(rule["blocks"])
        if name:
            rule["named"] = True
        else:
            rule.pop("named", None)
        self._refresh_row(self.combo_list.currentRow())

    # ------------------------------------------------------------------ 積木

    def _on_chip(self, column: str, option: str):
        rule = self.current_rule()
        if rule is None:
            if column == "unit" and option in _BUILTIN_UNITS[self.level]:
                self._dialog.disabled ^= {option}
                self._last[column] = option if option not in self._dialog.disabled else None
                self._refresh_row(0)
            self.show_current()
            return
        blocks = rule["blocks"]
        if column == "title":
            blocks["title"] = option
            self._last[column] = option
        else:
            chosen = set(blocks[column])
            if option in chosen:
                if len(chosen) == 1:           # 每一欄至少要選一個
                    self.show_current()
                    return
                chosen.discard(option)
                self._last[column] = None
            else:
                chosen.add(option)
                self._last[column] = option
            blocks[column] = [item for item in options(column, self.level) if item in chosen]
        refreshed = block_rule(blocks, self.level, rule["name"] if rule.get("named") else "", rule.get("enabled", True))
        rule.clear()
        rule.update(refreshed)
        self._refresh_row(self.combo_list.currentRow())
        self.show_current()

    def _shown_option(self, blocks: dict, column: str) -> str:
        chosen = [blocks[column]] if column == "title" else blocks[column]
        last = self._last.get(column)
        if last in chosen:
            return last
        if column == "sep" and "空格" in chosen:
            return "空格"            # 範例用最常見的寫法「第十二章 過河」
        return chosen[0] if chosen else ""

    def _example_parts(self, blocks: dict) -> dict:
        parts = {}
        for column in COLUMNS:
            option = self._shown_option(blocks, column)
            if column == "number":
                parts[column] = _NUMBER_SAMPLES.get(option, "")
            elif column == "title":
                parts[column] = "" if option == "沒有" else "過河"
            elif column == "sep":
                parts[column] = "" if option == "無" else ("␣" if option == "空格" else option)
            elif column == "prefix" and option == "N-":
                parts[column] = "2-"          # 卷號-章號：「2-12 過河」
            else:
                parts[column] = "" if option in ("無", "") else option
        return parts

    def _example_line(self, blocks: dict, parts: dict) -> str:
        frame = parts["frame"].split() if parts["frame"] else ["", ""]
        prefix = parts["prefix"]
        spacer = " " if prefix.isascii() and prefix.isalpha() else ""
        head = f"{frame[0]}{prefix}{spacer}{parts['number']}{parts['unit']}{frame[1]}"
        if not parts["title"]:
            sep = parts["sep"] if parts["sep"] not in ("", "␣") else ""
            return head + sep
        sep_option = self._shown_option(blocks, "sep")
        return head + _SEP_SHOWN.get(sep_option, sep_option) + parts["title"]

    def show_current(self):
        if self._updating:
            return
        rule = self.current_rule()
        builtin = rule is None
        written = is_written_rule(rule)
        self.name_input.setText(_BUILTIN_NAMES[self.level] if builtin else rule["name"])
        self.name_input.setReadOnly(builtin)
        self.delete_button.setVisible(not builtin)
        i18n.set_text(self.delete_button, "刪除規則" if written else "刪除組合")
        self.editor_stack.setCurrentIndex(1 if written else 0)
        self.confidence_label.setVisible(not written)
        if written:
            self.written_sample.clear()
            self.pattern_input.setText(rule["pattern"])
            self._on_pattern_edited(rule["pattern"])
            self.result_label.setText(rule["name"])
            return
        blocks = self.blocks(rule)
        grade = "高" if builtin else confidence(blocks)
        i18n.set_text(self.confidence_label, f"{grade}信心")
        for column, chips in self.chips.items():
            for option, chip in chips.items():
                chip.setChecked(option == blocks[column] if column == "title" else option in blocks[column])
                chip.setEnabled(not builtin or (column == "unit" and option in _BUILTIN_UNITS[self.level]))
        parts = self._example_parts(blocks)
        for column, label in self.examples.items():
            label.setText(parts[column] or "—")
            label.setProperty("empty", not parts[column])
            label.style().unpolish(label)
            label.style().polish(label)
        self.result_label.setText(self._example_line(blocks, parts) if (builtin and blocks["unit"]) or not builtin
                                  else "—")
        self.count_label.setText("…")
        self._count_timer.start()

    # ------------------------------------------------------------------ 本文符合的行

    def _update_counts(self):
        rule = self.current_rule()
        dialog = self._dialog
        rows = []
        if rule is None:
            units = set(self.blocks()["unit"])
            rows = [(row, clean, known) for row, known, clean in dialog.heading_rows
                    if heading_word(clean) in units]
        elif is_written_rule(rule):
            # 自己寫的規則不受章名結尾限制（那只擋自動辨識）；寫錯的正則不算，行數寫「—」
            if pattern_problem(rule["pattern"]):
                self.count_label.setText("—")
                self._line_rows = []
                if self.lines_button.isChecked():
                    self._fill_lines()
                return
            test_rule = dict(rule, enabled=True)
            for row, clean, known in dialog.rule_lines():
                if match_user_chapter_rule(clean, [test_rule]):
                    rows.append((row, clean, known))
        else:
            check = dialog.title_check()
            test_rule = dict(rule, enabled=True)
            for row, clean, known in dialog.rule_lines():
                if match_user_chapter_rule(clean, [test_rule], check):
                    rows.append((row, clean, known))
        collected = sum(1 for _row, _clean, known in rows if known)
        i18n.set_text(self.count_label, _count_text(collected, len(rows) - collected))
        self._line_rows = rows
        if self.lines_button.isChecked():
            self._fill_lines()

    def _toggle_lines(self, shown: bool):
        self.lines_box.setVisible(shown)
        self.lines_button.set_icon_name("chevron-up" if shown else "chevron-down")
        # 收起來時下面只剩結果列：高度固定、分隔線藏起來（沒有東西可以調），跟積木之間留原本的間距
        self._lines_host.layout().setContentsMargins(0, 0 if shown else 10, 0, 0)
        self._lines_host.setMaximumHeight(16777215 if shown else self._lines_host.sizeHint().height())
        self.blocks_splitter.handle(1).setEnabled(shown)
        if shown:
            # 打開時上下大約各一半（本文的行多一點）；之後使用者拖過就照拖過的
            total = sum(self.blocks_splitter.sizes()) or self.blocks_splitter.height()
            self.blocks_splitter.setSizes([total * 9 // 20, total - total * 9 // 20])
            self._fill_lines()

    def _fill_lines(self):
        rows = sorted(self._line_rows, key=lambda item: (item[2], item[0]))[:_LINES_SHOWN]
        table = self.lines_table
        table.setRowCount(len(rows))
        for index, (row, clean, known) in enumerate(rows):
            status = make_item(i18n.T("已收錄" if known else "未收錄"))
            status.setData(Qt.ItemDataRole.UserRole, row)
            table.setItem(index, 0, status)
            table.setItem(index, 1, make_item(clean))

    def _on_line_selected(self):
        items = self.lines_table.selectedItems()
        if not items:
            self.lines_preview.hide()
            return
        row = self.lines_table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self.lines_preview.show_rows(self._dialog._lines, row, row, "accent")
        self._dialog.candidateHighlighted.emit(row, row)


class RecognitionDialog(QDialog):
    """按「保存並重掃」之後結果在 result_rules（整份自訂規則清單：自己寫的正則在前、組合在後）、
    result_disabled_words、result_special_levels、result_max_title_length、result_title_tail、
    result_title_tail_custom；在「可疑章節」勾了行一起加入時，result_lines 是加上 [::] 之後的整份本文、
    result_volume_rows 是其中要當成卷的行（沒有就是 None／空集合）。"""

    candidateHighlighted = Signal(int, int)

    def __init__(self, rules: list, get_document_lines, parent=None, known_rows=None,
                 title_tail_allowed: str = DEFAULT_TITLE_TAIL_ALLOWED, title_tail_custom: str = "",
                 disabled_words=frozenset(), max_title_length: int = MAX_TITLE_LENGTH, special_levels=None):
        super().__init__(parent)
        self.setWindowTitle("辨識章節")
        self._plain = [dict(rule) for rule in rules if not managed_rule(rule)]
        self.combos = {2: [], 1: []}
        self._toggle_presets = dict.fromkeys(_TOGGLE_PRESETS)
        for rule in rules:
            if rule.get("blocks"):
                self.combos[1 if rule.get("level") == 1 else 2].append(dict(rule, blocks=dict(rule["blocks"])))
            elif rule.get("preset") in _TOGGLE_PRESETS:
                self._toggle_presets[rule["preset"]] = dict(rule)
        self.custom_specials = [dict(rule) for rule in rules if rule.get("special")]
        self.disabled = set(disabled_words or ())
        self.special_levels = dict(special_levels or {})
        self._tail_custom = "".join(dict.fromkeys(title_tail_custom or ""))
        self._tail_state = {symbol: any(char in title_tail_allowed for char in chars)
                            for symbol, chars, _name in title_tail_groups(self._tail_custom)}
        self.result_rules = None
        self.result_disabled_words = None
        self.result_special_levels = None
        self.result_max_title_length = None
        self.result_title_tail = None
        self.result_title_tail_custom = None
        self._max_title_length = int(max_title_length)
        self._rule_lines = None
        self.result_lines = None
        self.result_volume_rows: set = set()
        self._analyze(get_document_lines(), known_rows)

        root, footer = dialog_frame(self, intro="設定哪些寫法算章、卷；可疑章節分頁可以把漏掉的行加進目錄。")
        root.setSpacing(12)
        self.tabs = QTabWidget()
        self.pages = {level: _LevelPage(self, level) for level in (2, 1)}
        self.tabs.addTab(_scrolling(self.pages[2]), "章")
        self.tabs.addTab(_scrolling(self.pages[1]), "卷")
        # 章、卷兩頁長得一樣：一頁拖過的分隔（組合清單寬度、積木與本文的行的高度），另一頁跟著一樣
        for source, target in ((self.pages[2], self.pages[1]), (self.pages[1], self.pages[2])):
            for name in ("splitter", "blocks_splitter"):
                getattr(source, name).splitterMoved.connect(
                    lambda _pos, _index, source=source, target=target, name=name:
                    _copy_splitter(getattr(source, name), getattr(target, name)))
        self.tabs.addTab(_scrolling(self._build_special_page()), "特殊標題")
        self.suspects = SuspectsPage(self._lines, self._known_rows, self._max_title_length,
                                     self._save_suspect_format, self._add_suspect_lines)
        self.suspects.candidateHighlighted.connect(self.candidateHighlighted.emit)
        self.suspects.countChanged.connect(
            lambda count: self.tabs.setTabText(_SUSPECTS_TAB, i18n.T(f"可疑章節（{count}）")))
        # 跟其他分頁一樣放進捲動區：不然這一頁（表格＋前後文）會把整個視窗的最小高度撐高，矮螢幕放不下
        self.tabs.addTab(_scrolling(self.suspects), i18n.T("可疑章節"))   # 掃過之後標題寫出幾行（countChanged）
        root.addWidget(self.tabs, 1)
        # 下面兩列是整個視窗共用的設定（每一頁都看得到）：跟按鈕列一樣固定在底部、用滿版的分隔線隔開
        shared = pinned_section(self)

        length_row = QHBoxLayout()
        length_row.setSpacing(10)
        _length_slider, self.title_length_spin = slider_with_spin(
            length_row, "標題最長", (10, 200), self._max_title_length, " 字")
        self.title_length_spin.valueChanged.connect(self._on_check_changed)
        shared.addLayout(length_row)

        tail_row = QHBoxLayout()
        tail_row.setSpacing(8)
        tail_label = QLabel("章名結尾可以是")
        tail_label.setObjectName("fileLabel")
        tail_row.addWidget(tail_label)
        self._tail_box, self._tail_flow = flow_container(h_spacing=6, v_spacing=6)
        tail_row.addWidget(self._tail_box, 1)
        shared.addLayout(tail_row)
        self._tail_toggles: dict = {}
        self.tail_input = QLineEdit()
        self.tail_input.setPlaceholderText(i18n.T("＋ 標點"))
        self.tail_input.setMaxLength(8)
        self.tail_input.setFixedWidth(120)
        self.tail_input.returnPressed.connect(self._add_tail_chars)
        self.tail_message = QLabel("")
        self.tail_message.setObjectName("fileLabel")
        self._rebuild_tail_chips()

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        commit_button = buttons.addButton("保存並重掃", QDialogButtonBox.ButtonRole.AcceptRole)
        commit_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        commit_button.clicked.connect(self._commit)
        footer.addWidget(buttons)

        for page in self.pages.values():
            page.rebuild_list()
        size_dialog(self, 1000, 820)
        self._fit_minimum_width()
        # Custom special counts need the title-length / tail settings, which are built after the special page.
        self._update_custom_counts()

    def _fit_minimum_width(self):
        """The pages sit in scroll areas, which don't report their content's width: without this the
        dialog could be dragged so narrow that the blocks scroll sideways and the layout falls apart."""
        page_width = max(page.minimumSizeHint().width() for page in self.pages.values())
        margins = self.layout().contentsMargins()
        scrollbar = self.style().pixelMetric(self.style().PixelMetric.PM_ScrollBarExtent)
        self.setMinimumWidth(page_width + scrollbar + margins.left() + margins.right() + 40)

    # ------------------------------------------------------------------ 本文

    def _analyze(self, lines, known_rows):
        """目錄裡的行，和本文裡像標題但沒進目錄的行：[(行號, 已收錄?, 文字)]。單位、特殊標題、
        章名結尾的收錄數都從這份清單算，整份本文只看一次。"""
        self._lines = list(lines)
        self._known_rows = set(known_rows or ())
        self._rule_lines = None
        limit = self._max_title_length
        found = []
        for row, line in enumerate(self._lines):
            known = row in self._known_rows
            heading = may_have_heading_word(line)
            if not known and (len(line) > limit + 40 or not heading):
                continue
            clean, marker = strip_persistent_title_marker(line.strip())
            if not clean or marker == "exclude":
                continue
            if known or looks_like_heading(clean, limit):
                found.append((row, known, clean))
        self.heading_rows = found

    def rule_lines(self) -> list:
        """組合要比對的行（不太長、不是空行、沒有被排除）：第一次要算組合的收錄數時才整理。"""
        if self._rule_lines is None:
            rows = []
            for row, line in enumerate(self._lines):
                if not line or len(line) > 220:
                    continue
                clean, marker = strip_persistent_title_marker(line.strip())
                if clean and marker != "exclude":
                    rows.append((row, clean, row in self._known_rows))
            self._rule_lines = rows
        return self._rule_lines

    def reload(self, lines, known_rows=None):
        """對話框開著時本文被改過：換成新的一份重算收錄數與可疑章節。"""
        self._analyze(lines, known_rows)
        self.suspects.reload(lines, known_rows)
        for page in self.pages.values():
            page.show_current()
        self._update_special_counts()
        self._rebuild_custom_specials()

    def title_check(self):
        return build_title_check(self.title_tail_allowed(), self._tail_custom, self.title_length_spin.value())

    def _on_check_changed(self, *_args):
        """標題長度、章名結尾改了：組合的收錄數照新的設定重算。"""
        for page in self.pages.values():
            page.show_current()
        self._update_custom_counts()

    # ------------------------------------------------------------------ 特殊標題

    def _build_special_page(self) -> QWidget:
        """兩欄：左邊內建的特殊標題、右邊自己新增的（一長串內建的排在自訂上面，自訂常常要捲到很下面才看得到）。"""
        page = QWidget()
        columns = QHBoxLayout(page)
        columns.setContentsMargins(4, 16, 4, 0)
        columns.setSpacing(24)
        builtin = QVBoxLayout()
        builtin.setSpacing(10)
        builtin_heading = QLabel("內建")
        builtin_heading.setObjectName("appTitle")
        builtin.addWidget(builtin_heading)
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        builtin.addLayout(grid)
        builtin.addStretch(1)
        columns.addLayout(builtin)
        line = QFrame()
        line.setObjectName("divider")
        line.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        line.setFixedWidth(1)
        columns.addWidget(line)
        self.special_toggles: dict = {}
        self.special_level_chips: dict = {}
        self._special_count_labels: dict = {}
        for row, (key, label) in enumerate(_SPECIAL_ROWS):
            toggle = ToggleSwitch("", fill=False)
            toggle.setChecked(self._special_enabled(key))
            toggle.clicked.connect(lambda checked, key=key: self._on_special_toggled(key, checked))
            self.special_toggles[key] = toggle
            grid.addWidget(toggle, row, 0)
            grid.addWidget(QLabel(label), row, 1)
            chips = {}
            for column, level in enumerate((1, 2)):
                chip = QPushButton(_LEVEL_NAMES[level])
                chip.setObjectName("blockChip")
                chip.setCheckable(True)
                chip.clicked.connect(lambda _checked=False, key=key, level=level: self._on_special_level(key, level))
                chips[level] = chip
                grid.addWidget(chip, row, 2 + column)
            self.special_level_chips[key] = chips
            count = QLabel("")
            count.setObjectName("fileLabel")
            self._special_count_labels[key] = count
            grid.addWidget(count, row, 4)
        self._custom_widgets: list = []

        # 自己新增的特殊標題（「續章」「附錄」…）：照原文顯示，不改寫成第N章
        custom = QVBoxLayout()
        custom.setSpacing(10)
        heading = QLabel("自訂")
        heading.setObjectName("appTitle")
        custom.addWidget(heading)
        self._special_grid = QGridLayout()
        self._special_grid.setContentsMargins(0, 0, 0, 0)
        self._special_grid.setHorizontalSpacing(14)
        self._special_grid.setVerticalSpacing(8)
        self._special_grid.setColumnStretch(6, 1)
        custom.addLayout(self._special_grid)
        custom.addStretch(1)
        columns.addLayout(custom, 1)
        self.special_input = QLineEdit()
        self.special_input.setPlaceholderText(i18n.T("標題開頭的字，例如：續章"))
        self.special_input.setMaxLength(SPECIAL_WORD_MAX)
        self.special_input.returnPressed.connect(self._add_custom_special)
        self.special_add_button = QPushButton("新增")
        self.special_add_button.clicked.connect(self._add_custom_special)
        self.special_message = QLabel("")
        self.special_message.setObjectName("fileLabel")
        self.special_message.setWordWrap(True)      # 右欄比較窄：說明換行，不要撐出橫向捲軸
        self.special_input_row = QWidget()
        input_layout = QHBoxLayout(self.special_input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(8)
        self.special_input.setFixedWidth(220)
        input_layout.addWidget(self.special_input)
        input_layout.addWidget(self.special_add_button)
        input_layout.addStretch(1)
        self.special_add_button.setFixedHeight(self.special_input.sizeHint().height())
        custom.insertWidget(2, self.special_message)       # 輸入框下面（標題、清單＋輸入列之後）
        self._rebuild_custom_specials()
        self._sync_special_levels()
        self._update_special_counts()
        return page

    def _rebuild_custom_specials(self):
        grid = self._special_grid
        for widget in self._custom_widgets:
            grid.removeWidget(widget)
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        self._custom_widgets = []
        self._custom_count_labels = []
        grid.removeWidget(self.special_input_row)
        first = 0
        for offset, rule in enumerate(self.custom_specials):
            row = first + offset
            toggle = ToggleSwitch("", fill=False)
            toggle.setChecked(rule.get("enabled", True))
            toggle.clicked.connect(lambda checked, rule=rule: rule.__setitem__("enabled", checked))
            # show every spelling that is matched (traditional and simplified) so it's clear both count
            word = rule["special"]
            label = QLabel("／".join([word] + [item for item in special_word_variants(word) if item != word]))
            i18n.skip(label)
            chips = []
            for column, level in enumerate((1, 2)):
                chip = QPushButton(_LEVEL_NAMES[level])
                chip.setObjectName("blockChip")
                chip.setCheckable(True)
                chip.setChecked(rule["level"] == level)
                chip.clicked.connect(lambda _checked=False, rule=rule, level=level: self._on_custom_level(rule, level))
                chips.append(chip)
                grid.addWidget(chip, row, 2 + column)
            count = QLabel("")
            count.setObjectName("fileLabel")
            self._custom_count_labels.append((rule, count))
            remove = QPushButton("移除")
            remove.setObjectName("inlineLink")
            remove.clicked.connect(lambda _checked=False, rule=rule: self._remove_custom_special(rule))
            grid.addWidget(toggle, row, 0)
            grid.addWidget(label, row, 1)
            grid.addWidget(count, row, 4)
            grid.addWidget(remove, row, 5)
            self._custom_widgets += [toggle, label, *chips, count, remove]
        input_row = first + len(self.custom_specials)
        grid.addWidget(self.special_input_row, input_row, 0, 1, 7)
        for row in range(grid.rowCount()):
            grid.setRowStretch(row, 0)
        grid.setRowStretch(input_row + 1, 1)
        self._update_custom_counts()

    def _update_custom_counts(self):
        if not hasattr(self, "_tail_toggles"):
            return          # still inside __init__: the settings the counts depend on don't exist yet
        for rule, label in getattr(self, "_custom_count_labels", ()):
            i18n.set_text(label, self._custom_count_text(rule))

    def _custom_count_text(self, rule) -> str:
        check = self.title_check()
        collected = pending = 0
        for _row, clean, known in self.rule_lines():
            if match_user_chapter_rule(clean, [dict(rule, enabled=True)], check) is not None:
                if known:
                    collected += 1
                else:
                    pending += 1
        return _count_text(collected, pending)

    def _add_custom_special(self):
        word = self.special_input.text().strip()
        if not word:
            return
        existing = {variant for rule in self.custom_specials for variant in special_word_variants(rule["special"])}
        if any(char.isspace() for char in word) or not any(char.isalpha() for char in word):
            message = "要是文字，不能有空白"
        elif word_key(word) is not None or any(word == label for _key, label in _SPECIAL_ROWS):
            message = f"「{word}」是內建的，在上面的清單"
        elif word in existing:
            message = f"「{word}」已經加過了"
        else:
            self.custom_specials.append(special_word_rule(word))
            self.special_input.clear()
            self._rebuild_custom_specials()
            message = "預設是章（掛在目前的卷底下）；可以改成卷"
        i18n.set_text(self.special_message, message)

    def _on_custom_level(self, rule, level: int):
        rule["level"] = level
        self._rebuild_custom_specials()

    def _remove_custom_special(self, rule):
        self.custom_specials = [item for item in self.custom_specials if item is not rule]
        i18n.set_text(self.special_message, "")
        self._rebuild_custom_specials()

    def _special_enabled(self, key: str) -> bool:
        if key in _TOGGLE_PRESETS:
            rule = self._toggle_presets[key]
            return rule is not None and rule.get("enabled", True)
        return key not in self.disabled

    def special_level(self, key: str) -> int:
        if key in _TOGGLE_PRESETS:
            rule = self._toggle_presets[key]
            return rule["level"] if rule else 1
        return self.special_levels.get(key, SPECIAL_LEVELS[key])

    def _on_special_toggled(self, key: str, checked: bool):
        if key in _TOGGLE_PRESETS:
            if checked and self._toggle_presets[key] is None:
                self._toggle_presets[key] = preset_rule(key)
            elif self._toggle_presets[key] is not None:
                self._toggle_presets[key]["enabled"] = checked
        elif checked:
            self.disabled.discard(key)
        else:
            self.disabled.add(key)

    def _on_special_level(self, key: str, level: int):
        if key in _TOGGLE_PRESETS:
            if self._toggle_presets[key] is None:
                self._toggle_presets[key] = dict(preset_rule(key), enabled=False)
            self._toggle_presets[key]["level"] = level
        elif level == SPECIAL_LEVELS[key]:
            self.special_levels.pop(key, None)
        else:
            self.special_levels[key] = level
        self._sync_special_levels()

    def _sync_special_levels(self):
        for key, chips in self.special_level_chips.items():
            for level, chip in chips.items():
                chip.setChecked(level == self.special_level(key))

    def _update_special_counts(self):
        if not hasattr(self, "_special_count_labels"):
            return
        counts = {}
        for _row, known, clean in self.heading_rows:
            key = heading_word(clean)
            if key is not None:
                counts.setdefault(key, [0, 0])[0 if known else 1] += 1
        for key, label in self._special_count_labels.items():
            collected, pending = counts.get(key, (0, 0))
            label.setText("" if not (collected or pending) else i18n.T(_count_text(collected, pending)))

    # ------------------------------------------------------------------ 章名結尾

    def _rebuild_tail_chips(self):
        for symbol, chip in self._tail_toggles.items():
            self._tail_state[symbol] = chip.isChecked()
        flow = self._tail_flow
        while flow.count():
            widget = flow.takeAt(0).widget()
            if widget is not None and widget not in (self.tail_input, self.tail_message):
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self._tail_toggles = {}
        for symbol, _chars, name in title_tail_groups(self._tail_custom):
            chip = QPushButton(symbol)
            chip.setObjectName("blockChip")
            chip.setCheckable(True)
            chip.setChecked(self._tail_state.get(symbol, False))
            chip.setMinimumWidth(chip.fontMetrics().horizontalAdvance("＿") + 22)
            chip.clicked.connect(self._on_check_changed)
            if name == "自訂":
                chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                chip.customContextMenuRequested.connect(lambda _pos, char=symbol: self._tail_menu(char))
            self._tail_toggles[symbol] = chip
            flow.addWidget(chip)
        # 同一列的按鈕跟輸入框一樣高（UI_RULES：同一列的控制項同高）
        height = self.tail_input.sizeHint().height()
        for chip in self._tail_toggles.values():
            chip.setFixedHeight(height)
        flow.addWidget(self.tail_input)
        flow.addWidget(self.tail_message)

    def _tail_menu(self, char: str):
        menu = QMenu(self)
        menu.addAction(i18n.T("移除"), lambda: self._remove_tail_char(char))
        menu.exec(self.cursor().pos())

    def _add_tail_chars(self):
        """新增的字：不能是文字、數字、空白，已經在清單上（含全形半形同一組）的不重複加。
        新增的預設關著（清單外的標點本來就不擋，加進來就是為了擋它）。"""
        text = self.tail_input.text()
        listed = "".join(chars for _symbol, chars, _name in title_tail_groups(self._tail_custom))
        added, skipped = [], []
        for char in dict.fromkeys(text):
            if char.isspace():
                continue
            if char.isalnum() or char in listed:
                skipped.append(char)
                continue
            self._tail_custom += char
            listed += char
            self._tail_state[char] = False
            added.append(char)
        self.tail_input.clear()
        if added:
            self._rebuild_tail_chips()
        message = f"「{''.join(skipped)}」已經在清單上或不是標點" if skipped else ("按右鍵移除" if added else "")
        i18n.set_text(self.tail_message, message)

    def _remove_tail_char(self, char: str):
        self._tail_custom = self._tail_custom.replace(char, "")
        self._tail_state.pop(char, None)
        self._tail_toggles.pop(char, None)
        self._rebuild_tail_chips()
        self._on_check_changed()

    def title_tail_allowed(self) -> str:
        return "".join(chars for symbol, chars, _name in title_tail_groups(self._tail_custom)
                       if symbol in self._tail_toggles and self._tail_toggles[symbol].isChecked())

    def title_tail_custom(self) -> str:
        return self._tail_custom

    # ------------------------------------------------------------------

    def managed_rules(self) -> list:
        rules = [dict(rule) for level in (2, 1) for rule in self.combos[level]]
        rules += [dict(rule) for rule in self._toggle_presets.values() if rule is not None]
        # 自訂特殊標題放最前面：「書名 續章3」這種行不要先被別的組合收走
        return [special_word_rule(rule["special"], rule["level"], rule.get("enabled", True))
                for rule in self.custom_specials] + rules

    def show_candidates(self, fmt=None):
        """切到「可疑章節」分頁，只看某一種格式（None＝全部）。"""
        self.suspects.show_format(fmt)
        self.tabs.setCurrentIndex(_SUSPECTS_TAB)

    def _save_suspect_format(self, fmt: str, candidate) -> str:
        """可疑章節的「把這種格式存成規則」：常用寫法加成組合（已經有一樣的就打開它），
        其他寫法加成自己寫的規則。按「保存並重掃」才生效。回傳寫在狀態列的一句話。"""
        if fmt.startswith("preset:"):
            preset_id = fmt.split(":", 1)[1]
            if preset_id in _TOGGLE_PRESETS:
                if self._toggle_presets[preset_id] is None:
                    self._toggle_presets[preset_id] = preset_rule(preset_id)
                self._toggle_presets[preset_id]["enabled"] = True
                self.special_toggles[preset_id].setChecked(True)
                name = dict(_SPECIAL_ROWS)[preset_id].split("（")[0]
                return f"已打開特殊標題的「{name}」，按「保存並重掃」後生效"
            level, blocks = template(preset_id)
            rule = block_rule(blocks, level)
            existing = next((item for item in self.combos[level] if item.get("pattern") == rule["pattern"]), None)
            if existing is None:
                self.combos[level].append(rule)
            else:
                existing["enabled"] = True
                rule = existing
            self.pages[level].rebuild_list(self.pages[level].combo_list.currentRow())
            return f"已加入組合「{rule['name']}」，按「保存並重掃」後生效"
        rule = weak_candidate_to_user_rule(candidate)
        rule["name"] = candidate["label"]
        if any(existing.get("pattern") == rule["pattern"] for existing in self._plain):
            return "相同寫法的規則已經在清單裡了"
        self._plain.append(rule)
        page = self.pages[1 if rule.get("level") == 1 else 2]
        page.rebuild_list(page.combo_list.currentRow())
        return f"已加入自己寫的規則「{rule['name']}」，按「保存並重掃」後生效"

    def _add_suspect_lines(self):
        """可疑章節的「加入已勾選項目」：勾的行加上 [::]，同時保存全部設定並關閉視窗。"""
        self.result_lines, self.result_volume_rows = self.suspects.checked_lines_result()
        self._commit(ask_about_checked=False)

    def _check_written_rules(self) -> bool:
        """保存前檢查自己寫的規則：寫錯、空白的不能存（切到那一條、說明哪裡錯）；有巢狀量詞的問一次。"""
        for rule in self._plain:
            problem = pattern_problem(rule.get("pattern", ""))
            risky = not problem and is_risky_pattern(rule["pattern"])
            if not problem and not risky:
                continue
            page = self.pages[1 if rule.get("level") == 1 else 2]
            self.tabs.setCurrentIndex(0 if page.level == 2 else 1)
            page.combo_list.setCurrentRow(next(index for index, entry in enumerate(page.entries()) if entry is rule))
            if problem:
                dialogs.error(self, "規則寫錯了", f"「{rule['name']}」：{problem}")
                return False
            if not dialogs.confirm(self, "正則可能很慢", f"「{rule['name']}」{RISKY_WARNING}。\n\n仍要使用嗎？"):
                return False
        return True

    def _commit(self, ask_about_checked: bool = True):
        if not self._check_written_rules():
            self.result_lines, self.result_volume_rows = None, set()
            return
        # 在可疑章節勾了行卻直接按「保存並重掃」：多半是想一起加入，先問清楚，不要默默丟掉，也不要默默改本文
        if ask_about_checked and self.suspects.checked_count() and self.result_lines is None:
            box = QMessageBox(QMessageBox.Icon.Question, i18n.T("一起加入目錄？"),
                              i18n.T(f"你在「可疑章節」勾了 {self.suspects.checked_count()} 行，要一起加入目錄嗎？"),
                              parent=self)
            add_button = box.addButton(i18n.T("一起加入"), QMessageBox.ButtonRole.AcceptRole)
            rules_only = box.addButton(i18n.T("只保存設定"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(i18n.T("返回"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(add_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked is add_button:
                self.result_lines, self.result_volume_rows = self.suspects.checked_lines_result()
            elif clicked is not rules_only:
                return
        rules = [dict(rule) for rule in self._plain] + self.managed_rules()
        if not _save_json(RULES_FILE, rules):
            dialogs.error(self, "無法保存", "章節規則無法寫入設定檔。")
            self.result_lines, self.result_volume_rows = None, set()
            return
        self.result_rules = rules
        self.result_disabled_words = frozenset(self.disabled)
        self.result_special_levels = dict(self.special_levels)
        self.result_max_title_length = self.title_length_spin.value()
        self.result_title_tail = self.title_tail_allowed()
        self.result_title_tail_custom = self.title_tail_custom()
        self.accept()
