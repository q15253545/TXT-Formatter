"""「辨識章節」對話框（章節管理的第一顆按鈕）：用積木組出章、卷的標題寫法，設定特殊標題的層級，
以及所有自動辨識共用的標題長度與章名結尾。

章、卷各一頁：左邊是組合清單（最上面是內建的「第N章」「第N卷」，只能改單位、不能刪），
右邊六欄積木（外框、前綴、數字、單位、分隔、章名），同一欄可以選好幾個。組合存成自訂規則
（多一個 blocks 欄位，見 core/title_blocks.py）；自己寫的正則留在「自訂章節規則」。"""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QScrollArea, QTableWidget, QTabWidget, QVBoxLayout, QWidget,
)

from core.chapter_parse import (
    DEFAULT_TITLE_TAIL_ALLOWED, MAX_TITLE_LENGTH, SPECIAL_LEVELS, SPECIAL_WORDS, build_title_check, heading_word,
    looks_like_heading, may_have_heading_word, title_tail_groups,
)
from core.persistence import RULES_FILE, _save_json
from core.title_blocks import (
    COLUMNS, auto_name, block_rule, blocks_from_sample, confidence, options, template, templates_by_confidence,
)
from core.title_markers import strip_persistent_title_marker
from core.user_rules import match_user_chapter_rule, preset_rule
from . import dialogs, i18n
from .sortable_table import PreviewTable, make_item, setup_columns
from .widgets import ToggleSwitch, dialog_frame, flow_container, size_dialog, slider_with_spin

_COLUMN_NAMES = {"frame": "外框", "prefix": "前綴", "number": "數字", "unit": "單位", "sep": "分隔", "title": "章名"}
_LEVEL_NAMES = {2: "章", 1: "卷"}
# 內建組合＝自動辨識：前綴、數字…都固定，只有單位可以開關（對到「關掉的字」）
_BUILTIN_UNITS = {2: ("章", "回", "節", "折", "幕"), 1: ("卷", "部", "篇", "集")}
_BUILTIN_NAMES = {2: "第N章", 1: "第N卷"}
_NUMBER_SAMPLES = {"一二三": "十二", "123": "12", "全形１２": "１２", "壹貳參": "拾貳"}
_SEP_SHOWN = {"無": "", "空格": " ", ".": ". ", "-": " - "}
_NAMED_VOLUME = "named_volume"
_SPECIAL_ROWS = ([(key, label) for key, label, _variants in SPECIAL_WORDS]
                 + [("番外", "番外"), ("外傳", "外傳"), ("終章", "終章"), (_NAMED_VOLUME, "名稱＋篇（青雲篇、上卷）")])
_LINES_SHOWN = 300


def managed_rule(rule: dict) -> bool:
    """這條規則歸「辨識章節」管（積木組合、名稱＋篇），不在「自訂章節規則」的清單裡。"""
    return bool(rule.get("blocks")) or rule.get("preset") == _NAMED_VOLUME


def _builtin_blocks(level: int, disabled: set) -> dict:
    return {"frame": list(options("frame", level)), "prefix": ["第"], "number": ["一二三", "123", "全形１２"],
            "unit": [unit for unit in _BUILTIN_UNITS[level] if unit not in disabled],
            "sep": list(options("sep", level)), "title": "可有可無"}


def _scrolling(page: QWidget) -> QScrollArea:
    """分頁內容放進捲動區：直立螢幕、視窗很窄或很矮時捲動，不會把視窗的最小尺寸撐到螢幕外。"""
    scroll = QScrollArea()
    scroll.setObjectName("panelScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    page.setObjectName("panelScrollContent")
    scroll.setWidget(page)
    return scroll


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
        layout.setSpacing(14)

        left_host = QFrame()
        left_host.setObjectName("comboPane")
        left_host.setFixedWidth(250)
        left = QVBoxLayout(left_host)
        left.setContentsMargins(0, 0, 14, 0)
        left.setSpacing(8)
        self.add_button = QPushButton("＋ 新增組合")
        self.add_button.setObjectName("menuButton")
        menu = QMenu(self.add_button)
        menu.addAction(i18n.T("空白組合"), self._add_blank)
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
        layout.addWidget(left_host)

        right = QVBoxLayout()
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
        right.addWidget(self._build_blocks(), 1)

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
        self.lines_button = QPushButton("看本文的行")
        self.lines_button.setObjectName("inlineLink")
        self.lines_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lines_button.setCheckable(True)
        self.lines_button.toggled.connect(self._toggle_lines)
        bar_layout.addWidget(self.lines_button)
        right.addWidget(bar)
        self.lines_table = PreviewTable(0, 2)
        self.lines_table.setHorizontalHeaderLabels(["狀態", "內容"])
        self.lines_table.verticalHeader().setVisible(False)
        self.lines_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.lines_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.lines_table, {0: 80})
        self.lines_table.setFixedHeight(180)
        self.lines_table.itemSelectionChanged.connect(self._on_line_selected)
        self.lines_table.hide()
        right.addWidget(self.lines_table)
        layout.addLayout(right, 1)

        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(120)
        self._count_timer.timeout.connect(self._update_counts)
        self._line_rows: list = []

    # ------------------------------------------------------------------ 版面

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
        return [None] + self._dialog.combos[self.level]

    def current_rule(self):
        row = self.combo_list.currentRow()
        entries = self.entries()
        return entries[row] if 0 <= row < len(entries) else None

    def is_builtin(self) -> bool:
        return self.combo_list.currentRow() <= 0

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
        if rule is not None:
            badge = QLabel(confidence(rule["blocks"]))
            badge.setObjectName("confidence")
            top.addWidget(badge)
        top.addStretch(1)
        text.addLayout(top)
        info = QLabel("內建" if rule is None else "")
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
            if badge is not None:
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
        self.rebuild_list(len(self.entries()) - 1)
        self.name_input.setFocus()

    def _add_blank(self):
        self._add_rule({"frame": ["無"], "prefix": ["無"], "number": ["123"], "unit": ["無"], "sep": ["空格"],
                        "title": "要有"})

    def _add_template(self, key: str):
        _level, blocks = template(key)
        self._add_rule(blocks)

    def _add_from_sample(self):
        blocks = blocks_from_sample(self.sample_input.text(), self.level)
        if blocks is None:
            i18n.set_text(self.sample_message, "這一行找不到編號")
            self.sample_message.show()
            return
        self.sample_message.clear()
        self.sample_message.hide()
        self.sample_input.clear()
        self._add_rule(blocks)

    def _delete_current(self):
        row = self.combo_list.currentRow()
        if row <= 0:
            return
        del self._dialog.combos[self.level][row - 1]
        self.rebuild_list(row - 1)

    def _on_name_edited(self, text: str):
        rule = self.current_rule()
        if rule is None:
            return
        name = text.strip()
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
        blocks = self.blocks(rule)
        self.name_input.setText(_BUILTIN_NAMES[self.level] if builtin else rule["name"])
        self.name_input.setReadOnly(builtin)
        self.delete_button.setVisible(not builtin)
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
        self.lines_table.setVisible(shown)
        i18n.set_text(self.lines_button, "收起" if shown else "看本文的行")
        if shown:
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
        if items:
            row = self.lines_table.item(items[0].row(), 0).data(Qt.ItemDataRole.UserRole)
            self._dialog.candidateHighlighted.emit(row, row)


class RecognitionDialog(QDialog):
    """按「保存並重掃」之後結果在 result_rules（整份自訂規則清單：自己寫的正則在前、組合在後）、
    result_disabled_words、result_special_levels、result_max_title_length、result_title_tail、
    result_title_tail_custom。"""

    candidateHighlighted = Signal(int, int)

    def __init__(self, rules: list, get_document_lines, parent=None, known_rows=None,
                 title_tail_allowed: str = DEFAULT_TITLE_TAIL_ALLOWED, title_tail_custom: str = "",
                 disabled_words=frozenset(), max_title_length: int = MAX_TITLE_LENGTH, special_levels=None):
        super().__init__(parent)
        self.setWindowTitle("辨識章節")
        self._plain = [dict(rule) for rule in rules if not managed_rule(rule)]
        self.combos = {2: [], 1: []}
        self._named_volume = None
        for rule in rules:
            if rule.get("blocks"):
                self.combos[1 if rule.get("level") == 1 else 2].append(dict(rule, blocks=dict(rule["blocks"])))
            elif rule.get("preset") == _NAMED_VOLUME:
                self._named_volume = dict(rule)
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
        self._analyze(get_document_lines(), known_rows)

        root, footer = dialog_frame(self)
        root.setSpacing(12)
        self.tabs = QTabWidget()
        self.pages = {level: _LevelPage(self, level) for level in (2, 1)}
        self.tabs.addTab(_scrolling(self.pages[2]), "章")
        self.tabs.addTab(_scrolling(self.pages[1]), "卷")
        self.tabs.addTab(_scrolling(self._build_special_page()), "特殊標題")
        root.addWidget(self.tabs, 1)

        length_row = QHBoxLayout()
        length_row.setSpacing(10)
        self.title_length_slider, self.title_length_spin = slider_with_spin(
            length_row, "標題最長", (10, 200), self._max_title_length, " 字")
        length_row.addStretch(1)
        self.title_length_spin.valueChanged.connect(self._on_check_changed)
        root.addLayout(length_row)

        tail_row = QHBoxLayout()
        tail_row.setSpacing(8)
        tail_label = QLabel("章名結尾可以是")
        tail_label.setObjectName("fileLabel")
        tail_row.addWidget(tail_label)
        self._tail_box, self._tail_flow = flow_container(h_spacing=6, v_spacing=6)
        tail_row.addWidget(self._tail_box, 1)
        root.addLayout(tail_row)
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
        """對話框開著時本文被改過：換成新的一份重算收錄數。"""
        self._analyze(lines, known_rows)
        for page in self.pages.values():
            page.show_current()
        self._update_special_counts()

    def title_check(self):
        return build_title_check(self.title_tail_allowed(), self._tail_custom, self.title_length_spin.value())

    def _on_check_changed(self, *_args):
        """標題長度、章名結尾改了：組合的收錄數照新的設定重算。"""
        for page in self.pages.values():
            page.show_current()

    # ------------------------------------------------------------------ 特殊標題

    def _build_special_page(self) -> QWidget:
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(4, 16, 4, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
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
        grid.setColumnStretch(5, 1)
        grid.setRowStretch(len(_SPECIAL_ROWS), 1)
        self._sync_special_levels()
        self._update_special_counts()
        return page

    def _special_enabled(self, key: str) -> bool:
        if key == _NAMED_VOLUME:
            return self._named_volume is not None and self._named_volume.get("enabled", True)
        return key not in self.disabled

    def special_level(self, key: str) -> int:
        if key == _NAMED_VOLUME:
            return self._named_volume["level"] if self._named_volume else 1
        return self.special_levels.get(key, SPECIAL_LEVELS[key])

    def _on_special_toggled(self, key: str, checked: bool):
        if key == _NAMED_VOLUME:
            if checked and self._named_volume is None:
                self._named_volume = preset_rule(_NAMED_VOLUME)
            elif self._named_volume is not None:
                self._named_volume["enabled"] = checked
        elif checked:
            self.disabled.discard(key)
        else:
            self.disabled.add(key)

    def _on_special_level(self, key: str, level: int):
        if key == _NAMED_VOLUME:
            if self._named_volume is None:
                self._named_volume = dict(preset_rule(_NAMED_VOLUME), enabled=False)
            self._named_volume["level"] = level
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
        if self._named_volume is not None:
            rules.append(dict(self._named_volume))
        return rules

    def _commit(self):
        rules = [dict(rule) for rule in self._plain] + self.managed_rules()
        if not _save_json(RULES_FILE, rules):
            dialogs.error(self, "無法保存", "章節規則無法寫入設定檔。")
            return
        self.result_rules = rules
        self.result_disabled_words = frozenset(self.disabled)
        self.result_special_levels = dict(self.special_levels)
        self.result_max_title_length = self.title_length_spin.value()
        self.result_title_tail = self.title_tail_allowed()
        self.result_title_tail_custom = self.title_tail_custom()
        self.accept()
