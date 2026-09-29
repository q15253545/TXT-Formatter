"""標點校對結果，以及自動修正。

有明確答案的問題（兩段對話黏在一起、標點在行首、對話中途斷行、引號方向
顛倒、少了開引號、分隔線太長、重複標點）可以勾起來一次修正，「修正後」欄先顯示改完的樣子；看不出
正確寫法的只列出來、點一下跳到那一行，由使用者自己在本文裡改。
"""

import difflib
import re

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem,
)

from core.quote_check import (
    QUOTE_PROBLEM_LABELS, SEPARATOR_LENGTH, WORD_PROBLEM_LABELS, apply_fixes, scan_quote_problems, separator_styles,
)
from . import dialogs, i18n
from .sortable_table import PreviewTable, carry_over, data_index, enable_sorting, limit_rows, make_item, resort, setup_columns
from .theme import active_tokens
from .widgets import ContextPreview, Divider, GroupCheckBox, ScopeToggle, dialog_frame, flow_container, size_dialog

# 每種問題該怎麼看待，寫在勾選框的提示裡。
_KIND_TIPS = {
    "unclosed": "這一行有開引號，但到行尾都沒有收尾——通常是一段對話被硬生生斷成兩行。",
    "unpaired": "收尾引號找不到對應的開引號，或巢狀順序錯亂。",
    "leading_punct": "行首就是逗號、句號這類標點，代表上一行被截斷了。",
    "missing_separator": "上一句的收尾引號後面直接接下一句的開引號，兩個人的對話黏在同一行。",
    "separator_line": "整行都是同一個符號的分隔線（----、*****、====…），保留原本的符號、縮成三個。",
    "repeated_punct": "連續的句號（。。。）、太長的刪節號（…………）、中文裡的半形點（...）改成標準的「……」。"
                      "網址、英文裡的點不會動。",
    "dash_run": "段落裡太長的破折號（——————、——-、中文裡的 ----）改成兩格「——」；"
                "~~~~、～～～～ 改成一個「～」。整行的分隔線、英文與網址裡的不會動。",
    "masked": "中文字旁邊的一到四個星號（**），多半是被遮掉的字；只列出來。",
    "homoglyph": "英文字裡混著長得像英文字母的西里爾、希臘字母，改回英文字母。",
    "noise_dot": "中文字之間、章號裡的句點（大.走一步、第.1808章），拿掉。",
}

# 「分隔線不一致」不是勾選框：由下拉選單決定要不要統一、統一成哪一種
_SEPARATOR_KIND = "separator_style"
_NO_UNIFY = "不統一"

_FIX_COLUMN, _KIND_COLUMN, _TEXT_COLUMN, _AFTER_COLUMN = range(4)


def _one_line(text: str) -> str:
    # 不在這裡截斷：改動可能在很後面，截掉就看不到了；太長的由 _DiffDelegate
    # 照欄寬省略，而且會捲到改動的地方。
    return re.sub(r"\s+", " ", text).strip()


_CHANGED_ROLE = Qt.ItemDataRole.UserRole + 20   # 要標紅的字元位置
_FOCUS_ROLE = Qt.ItemDataRole.UserRole + 21     # 第一個改動的位置（太長時從這附近開始顯示）


_DIFF_LIMIT = 400        # 前後相同的部分去掉後，中間超過這麼多字就不細比


def _diff_marks(before: str, after: str):
    """比對修正前後，回傳（after 裡要標紅的字元位置, before 的第一個改動位置,
    after 的第一個改動位置）。純刪除（例如兩行接回一行）沒有新字，標在接起來
    的那個字上，才看得出是在哪裡接的。"""
    # 先去掉前後相同的部分（線性），只對中間改動的那一小段做 difflib。
    # 整段 diff 在上萬個重複字的段落要好幾秒；中間那段
    # 還是太長就整段標紅，不再細比。
    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < limit - prefix
           and before[len(before) - 1 - suffix] == after[len(after) - 1 - suffix]):
        suffix += 1
    middle_before = before[prefix:len(before) - suffix]
    middle_after = after[prefix:len(after) - suffix]
    if not middle_before and not middle_after:
        return set(), None, None
    changed = set()
    first_before = first_after = None
    if len(middle_before) > _DIFF_LIMIT or len(middle_after) > _DIFF_LIMIT:
        opcodes = [("replace", 0, len(middle_before), 0, len(middle_after))]
    else:
        opcodes = difflib.SequenceMatcher(None, middle_before, middle_after, autojunk=False).get_opcodes()
    for tag, i1, _i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        i1, j1, j2 = i1 + prefix, j1 + prefix, j2 + prefix
        if first_before is None:
            first_before, first_after = i1, j1
        if j2 > j1:
            changed.update(range(j1, j2))
        elif j1 < len(after):
            changed.add(j1)
    return changed, first_before, first_after


class _DiffDelegate(QStyledItemDelegate):
    """「內容」「修正後」兩欄：改動的字用紅色粗體；一行太長時，不是從頭顯示到
    欄寬為止，而是從改動前一點開始顯示（前面用「…」代替），改動一定看得到。

    “ 和 ” 在小字下幾乎一模一樣，不標出來的話，修正前後看起來完全相同。"""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if not text:
            return
        rect = QRectF(style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget))
        rect.adjust(8, 0, -8, 0)          # 跟樣式表的儲存格左右 padding 一致
        changed = set(index.data(_CHANGED_ROLE) or ())     # 每個字都要查一次，用 set 才不會變成平方時間
        focus = index.data(_FOCUS_ROLE)
        tokens = active_tokens()
        normal_font = QFont(opt.font)
        bold_font = QFont(opt.font)
        bold_font.setBold(True)
        normal_metrics, bold_metrics = QFontMetricsF(normal_font), QFontMetricsF(bold_font)

        def advance(position):
            metrics = bold_metrics if position in changed else normal_metrics
            return metrics.horizontalAdvance(text[position])

        ellipsis_width = normal_metrics.horizontalAdvance("…")
        available = rect.width()
        start = 0
        def overflows():
            # 量到超過欄寬就停，不必量完整段（段落很長時每次重畫都量全部會很慢）
            width = 0.0
            for position in range(len(text)):
                width += advance(position)
                if width > available:
                    return True
            return False

        if focus is not None and overflows():
            lead = sum(advance(i) for i in range(focus))
            if lead > available * 0.35:
                # 改動前面留大約三成欄寬的上下文。
                start, budget = focus, available * 0.3 - ellipsis_width
                while start > 0 and budget - advance(start - 1) > 0:
                    budget -= advance(start - 1)
                    start -= 1

        painter.save()
        x = rect.left()
        right = rect.right()
        normal_color, diff_color = QColor(tokens.text), QColor(tokens.diff_text)
        if start > 0:
            painter.setFont(normal_font)
            painter.setPen(normal_color)
            painter.drawText(QRectF(x, rect.top(), ellipsis_width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "…")
            x += ellipsis_width
        position = start
        while position < len(text):
            marked = position in changed
            end = position
            while end < len(text) and (end in changed) == marked:
                end += 1
            font, metrics = (bold_font, bold_metrics) if marked else (normal_font, normal_metrics)
            run = text[position:end]
            width = metrics.horizontalAdvance(run)
            last_run = end >= len(text)
            limit = right - x - (0 if last_run else ellipsis_width)
            truncated = width > limit
            if truncated:
                # 放不下：剩下的空間塞幾個字，後面接「…」。
                cut = 0
                while cut < len(run) and metrics.horizontalAdvance(run[:cut + 1]) <= right - x - ellipsis_width:
                    cut += 1
                run = run[:cut]
                width = metrics.horizontalAdvance(run)
            painter.setFont(font)
            painter.setPen(diff_color if marked else normal_color)
            painter.drawText(QRectF(x, rect.top(), width + 1, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, run)
            x += width
            if truncated:
                painter.setFont(normal_font)
                painter.setPen(normal_color)
                painter.drawText(QRectF(x, rect.top(), ellipsis_width + 1, rect.height()),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "…")
                break
            position = end
        painter.restore()


class QuoteCheckDialog(QDialog):
    """非模式對話框：開著時可以直接在本文手動修改。

    按「修正已勾選項目」時送出 fixesReady（修正後的整份本文, 修了幾處），由主
    視窗套用；對話框不關，主視窗再用 reload() 換成新的本文重新檢查，剩下要
    手動處理的項目繼續留在清單上。"""

    problemSelected = Signal(int)   # 1 起算的行號
    fixesReady = Signal(list, int)

    def __init__(self, raw_lines: list, parent=None, selected_ranges=None, selected_count: int = 0,
                 enabled_kinds=None, title_rows=None):
        super().__init__(parent)
        self.setWindowTitle("標點校對")
        size_dialog(self, 960, 640)
        self._raw_lines = list(raw_lines)
        # 目錄辨識出的章節標題行：自動修正不能跨過它們（None＝自己判斷）
        self._title_rows = set(title_rows) if title_rows is not None else None
        self._selected_ranges = list(selected_ranges or [])
        self._all_problems: list = []
        self._visible_total = 0
        self._visible: list[int] = []          # 目前表格顯示的是 _all_problems 的哪幾筆
        self._checked: set[int] = set()
        self.result_lines: list | None = None
        self.applied_count = 0

        root, footer = dialog_frame(self, intro="找出引號沒成對、對話斷行、重複標點與可疑字詞；有正確寫法的可以勾選後一次修正。")
        root.setSpacing(12)

        # 「只檢查選取的章節」是範圍，所有工具視窗都放在最上面（預設關著，見 ScopeToggle）。
        self.scope_check = ScopeToggle("檢查", selected_count if self._selected_ranges else 0)
        self.scope_check.toggled.connect(self._run_scan)
        root.addWidget(self.scope_check)

        self.kind_group = GroupCheckBox("檢查項目")
        self.kind_group.members_changed.connect(self._refresh)
        root.addWidget(self.kind_group)

        kind_box, kind_flow = flow_container(uniform=True)
        self._kind_checks = {}
        for key, label in QUOTE_PROBLEM_LABELS.items():
            if key == _SEPARATOR_KIND:
                continue
            checkbox = QCheckBox(label)
            checkbox.setChecked(enabled_kinds is None or key in enabled_kinds)
            checkbox.setToolTip(_KIND_TIPS.get(key, ""))
            checkbox.toggled.connect(self._refresh)
            self._kind_checks[key] = checkbox
            self.kind_group.add_member(checkbox)
            kind_flow.addWidget(checkbox)
        root.addWidget(kind_box)
        # 分隔線統一：同一本書裡 --- 和 === 混用時，由使用者決定要不要統一、統一成哪一種。
        # 只列這本書出現過的樣式（附行數，多的在前）；選了之後，其他樣式的分隔線才列成「分隔線不一致」。
        separator_row = QHBoxLayout()
        separator_row.setSpacing(10)
        separator_label = QLabel("分隔線統一成")
        separator_label.setObjectName("fileLabel")
        separator_row.addWidget(separator_label)
        self.separator_combo = QComboBox()
        self.separator_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.separator_combo.currentIndexChanged.connect(self._run_scan)
        separator_row.addWidget(self.separator_combo)
        separator_row.addStretch(1)
        root.addLayout(separator_row)
        # 可疑字詞不是標點，另成一組；一樣逐行找、有正確寫法的可以修
        self.word_group = GroupCheckBox("可疑字詞")
        self.word_group.members_changed.connect(self._refresh)
        root.addWidget(self.word_group)
        word_box, word_flow = flow_container(uniform=True)
        for key, label in WORD_PROBLEM_LABELS.items():
            checkbox = QCheckBox(label)
            checkbox.setChecked(enabled_kinds is None or key in enabled_kinds)
            checkbox.setToolTip(_KIND_TIPS.get(key, ""))
            checkbox.toggled.connect(self._refresh)
            self._kind_checks[key] = checkbox
            self.word_group.add_member(checkbox)
            word_flow.addWidget(checkbox)
        root.addWidget(word_box)
        root.addWidget(Divider())

        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for label, slot in (("勾選可自動修正的項目", self._check_fixable), ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            select_row.addWidget(button)
        select_row.addStretch(1)
        root.addLayout(select_row)

        self.status_label = QLabel("尚未檢查")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        # 不放行號：點一下就會跳到本文那一行，行號本身沒有用處；
        # 「還原原本順序」就是照行號排（見 sortable_table 的三段排序）。
        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["修正", "類型", "內容", "修正後"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # 「內容」與「修正後」都是長文字：內容先給表格寬度的四成，修正後吃剩下的。
        setup_columns(self.table, {_FIX_COLUMN: "contents", _KIND_COLUMN: "contents", _TEXT_COLUMN: 0.45})
        self._diff_delegate = _DiffDelegate(self.table)
        self.table.setItemDelegateForColumn(_TEXT_COLUMN, self._diff_delegate)
        self.table.setItemDelegateForColumn(_AFTER_COLUMN, self._diff_delegate)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        # 選到一列：下面顯示那一行加上前後文（拉中間的分隔可以調高度）
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table), 1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        self.fix_button = buttons.addButton("修正已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.fix_button.setObjectName("primary")
        close_button.clicked.connect(self.reject)
        self.fix_button.clicked.connect(self._apply_fixes)
        footer.addWidget(buttons)

        self._fill_separator_styles()
        self._run_scan()

    # ------------------------------------------------------------------

    def enabled_kinds(self) -> set:
        return {key for key, box in self._kind_checks.items() if box.isChecked()}

    def separator_target(self):
        """使用者選的分隔線符號；不統一時是 None。"""
        return self.separator_combo.currentData()

    def _fill_separator_styles(self):
        """照目前的本文重新列出分隔線樣式；之前選的樣式還在就維持。"""
        current = self.separator_target()
        ranges = self._selected_ranges if self.scope_check.isChecked() else None
        styles = separator_styles(self._raw_lines, ranges, self._title_rows)
        self.separator_combo.blockSignals(True)
        self.separator_combo.clear()
        self.separator_combo.addItem(i18n.T(_NO_UNIFY), None)
        for char, count in styles.items():
            self.separator_combo.addItem(i18n.T(f"{char * SEPARATOR_LENGTH}（{count} 行）"), char)
        index = self.separator_combo.findData(current) if current is not None else 0
        self.separator_combo.setCurrentIndex(max(0, index))
        self.separator_combo.blockSignals(False)
        # 樣式表的 padding 讓 AdjustToContents 算得太窄：照最長的選項自己算
        metrics = self.separator_combo.fontMetrics()
        longest = max(metrics.horizontalAdvance(self.separator_combo.itemText(i))
                      for i in range(self.separator_combo.count()))
        self.separator_combo.setMinimumWidth(longest + 64)
        # 只有一種（或沒有）分隔線時沒什麼好統一的
        self.separator_combo.setEnabled(len(styles) > 1)

    def _set_scope(self, selected_ranges, selected_count: int):
        self._selected_ranges = list(selected_ranges or [])
        self.scope_check.set_count(selected_count if self._selected_ranges else 0)

    @staticmethod
    def _problem_key(problem):
        return problem["kind"], problem["preview"]

    def reload(self, raw_lines, selected_ranges=None, selected_count: int = 0, title_rows=None):
        """本文改過了（使用者手動修改，或剛套用完自動修正）：用新的本文重新檢查。
        勾選狀態與目前選取的那一筆照內容對回去（sortable_table.carry_over），不會因為行號位移而跑掉。"""
        old_problems, old_checked = self._all_problems, {index: True for index in self._checked}
        rows = self.table.selectionModel().selectedRows()
        current = {data_index(self.table, rows[0].row()): True} if rows else {}
        self._raw_lines = list(raw_lines)
        if title_rows is not None:
            self._title_rows = set(title_rows)
        self.scope_check.blockSignals(True)
        self._set_scope(selected_ranges, selected_count)
        self.scope_check.blockSignals(False)
        self._fill_separator_styles()
        self._run_scan()
        self._checked = {index for index in carry_over(old_problems, self._all_problems, self._problem_key, old_checked)
                         if self._all_problems[index]["fix"]}
        self._refresh()
        current = next(iter(carry_over(old_problems, self._all_problems, self._problem_key, current)), None)
        if current is not None:
            for row in range(self.table.rowCount()):
                if data_index(self.table, row) == current:
                    self.table.blockSignals(True)
                    self.table.selectRow(row)
                    self.table.blockSignals(False)
                    break

    def _run_scan(self, *_args):
        ranges = self._selected_ranges if self.scope_check.isChecked() else None
        self._all_problems = scan_quote_problems(self._raw_lines, ranges, self._title_rows,
                                                 separator_target=self.separator_target())
        self._checked = set()
        self._refresh()

    def _refresh(self, *_args):
        kinds = self.enabled_kinds() | {_SEPARATOR_KIND}     # 由下拉決定，選了才會有這種問題
        matching = [index for index, problem in enumerate(self._all_problems) if problem["kind"] in kinds]
        # 太多時只列出一部分（可以自動修正的優先），勾選也只留列出來的。
        self._visible, self._visible_total = limit_rows(
            matching, lambda index: 0 if self._all_problems[index]["fix"] else 1)
        self._checked &= set(self._visible)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._visible))
        for row, index in enumerate(self._visible):
            problem = self._all_problems[index]
            fix = problem["fix"]
            check_item = make_item("", index, 0 if fix else 1)
            if fix:
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                check_item.setCheckState(Qt.CheckState.Checked if index in self._checked else Qt.CheckState.Unchecked)
            else:
                check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, _FIX_COLUMN, check_item)
            self.table.setItem(row, _KIND_COLUMN, make_item(i18n.T(problem["label"]), index))
            text_item = make_item(_one_line(problem["preview"]), index)
            self.table.setItem(row, _TEXT_COLUMN, text_item)
            if fix:
                after = _one_line(" ⏎ ".join(fix["after"]))
                after_item = make_item(after, index)
                before = _one_line(" ⏎ ".join(self._raw_lines[fix["start"]:fix["end"]]))
                changed, _first_before, first_after = _diff_marks(before, after)
                after_item.setData(_CHANGED_ROLE, sorted(changed))
                after_item.setData(_FOCUS_ROLE, first_after)
                # 「內容」只有問題那一行，另外比一次，才知道要捲到哪裡。
                _changed, first_text, _first = _diff_marks(text_item.text(), after)
                text_item.setData(_FOCUS_ROLE, first_text)
            else:
                after_item = make_item(i18n.T("需手動處理"), index)
            self.table.setItem(row, _AFTER_COLUMN, after_item)
        self.table.blockSignals(False)
        resort(self.table)
        self._update_status()
        self._update_preview()

    def _update_status(self):
        wrapped = getattr(self._all_problems, "wrapped", 0)
        if not self._all_problems:
            text = "沒有找到標點問題"
        else:
            fixable = sum(1 for index in self._visible if self._all_problems[index]["fix"])
            text = (f"共 {len(self._visible)} 處，其中 {fixable} 處可以自動修正；"
                    f"已勾選 {len(self._checked)} 處")
            if len(self._visible) < self._visible_total:
                text = (f"共 {self._visible_total} 處，太多了只列出 {len(self._visible)} 處"
                        f"（可修正的優先，修正後會列出其餘的），其中 {fixable} 處可以自動修正；"
                        f"已勾選 {len(self._checked)} 處")
        if wrapped and getattr(self._all_problems, "hard_wrapped", True):
            text += (f"。這本是硬換行（句子被切成好幾行），有 {wrapped} 段跨行的引號沒有列出；"
                     "建議先用排版設定的「整理段落換行」接回去")
        elif wrapped:
            text += f"。有 {wrapped} 段話分成好幾行、引號到最後一行才關（每行開頭沒有補引號），這些沒有列出"
        i18n.set_text(self.status_label, text)
        self.fix_button.setEnabled(bool(self._checked))

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != _FIX_COLUMN:
            return
        index = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_status()

    def _on_selection_changed(self):
        problem = self._update_preview()
        if problem is not None:
            self.problemSelected.emit(problem["line"])

    def _update_preview(self):
        """前後文預覽跟著目前選的那一列；沒有選取就藏起來。"""
        rows = self.table.selectionModel().selectedRows()
        if not rows or not self._raw_lines:
            self.preview.hide()
            return None
        problem = self._all_problems[data_index(self.table, rows[0].row())]
        fix = problem["fix"]
        tokens = active_tokens()
        if fix:
            # 可以修正的：直接顯示修正後的樣子，同一行標出改動（fix 的範圍是 raw_lines 的索引，不含 end）
            self.preview.show_fix(self._raw_lines, fix["start"], fix["end"] - 1, fix["after"], tokens.diff_text)
        else:
            # 只是提醒的：那一行加底色，字不換色（problem["line"] 從 1 起算）
            row = problem["line"] - 1
            self.preview.show_rows(self._raw_lines, row, row, tokens.text)
        return problem

    def _check_fixable(self):
        self._checked |= {index for index in self._visible if self._all_problems[index]["fix"]}
        self._refresh()

    def _uncheck_all(self):
        self._checked = set()
        self._refresh()

    def _apply_fixes(self):
        plans = [self._all_problems[index]["fix"] for index in sorted(self._checked)
                 if self._all_problems[index]["fix"]]
        if not plans:
            dialogs.info(self, "尚未勾選", "請先勾選要自動修正的項目。")
            return
        self.result_lines, self.applied_count = apply_fixes(self._raw_lines, plans)
        self.fixesReady.emit(self.result_lines, self.applied_count)
