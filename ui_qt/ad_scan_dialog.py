"""掃描視窗（從「內容檢查」卡片開啟），兩種模式：

- 掃描無關連內容（mode="ads"）：網址、發布頁、QQ、微信、來源署名、網頁字元碼；「重複段落」
  獨立一個分頁，最短長度、至少重複幾次都可以用拉桿或輸入框調整，表格即時更新。
  網頁字元碼不是刪掉整行，是換回原本的字（候選帶著 fix）。
- 作者感言與作品資訊（mode="notes"）：只列這兩類。

非模式：開著的時候可以直接在本文手動刪掉沒被找到的內容。對話框拿一份
raw_lines 快照作業；本文改過之後，主視窗會呼叫 reload() 換成新的一份重新
掃描（勾選狀態照內容對回去）。按「刪除已勾選項目」送出 deletionReady（只刪
目前這個分頁勾選的），由主視窗套用，對話框不關，可以繼續處理剩下的。
"""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.ad_scan import (
    AD_CATEGORY_LABELS, AD_ONLY_CATEGORIES, FIX_CATEGORIES, NOTE_CATEGORIES, REPEAT_MIN_COUNT, REPEAT_MIN_LENGTH,
    scan_ad_candidates,
)
from . import dialogs, i18n
from .theme import active_tokens
from .widgets import (
    ContextPreview, Divider, GroupCheckBox, ScopeToggle, dialog_frame, flow_container, size_dialog, slider_with_spin,
)
from .sortable_table import (
    PreviewTable,
    CONFIDENCE_ORDER, carry_over, data_index, enable_sorting, limit_rows, make_item, resort, setup_columns,
)

_CONFIDENCE_BY_BUTTON = {"勾選高信心": "高", "勾選中信心": "中", "勾選低信心": "低"}

# 只有需要解釋的類型才寫提示；其餘看名字就懂。
_CATEGORY_TIPS = {
    "meta": "作者、字數、發表日期與平台、整行的裝飾分隔線。\n"
            "單獨的日期只給「中」信心：日記體小說每章開頭就是日期，不宜預設勾選。",
    "entity": "網頁轉存時沒轉回來的字元碼（&#29368;、&nbsp;、&amp;）。\n"
              "不會刪掉整行，是換回原本的字（章節標題裡的也會換）。",
}

REPEAT_LENGTH_RANGE = (2, 60)
_WAITING_TEXT = "準備中：第一次打開要先整理本文，整理好就會自動掃描"
REPEAT_COUNT_RANGE = (2, 50)

_INTROS = {
    "ads": "找出網址、QQ／微信、小說來源、論壇轉貼資訊、重複段落這類跟故事無關的內容；勾選後刪除，網頁字元碼換回原字。",
    "notes": "找出作者的話、作者／字數／發表平台、分隔線；勾選後刪除。",
}

_MODES = {
    # 模式: (視窗標題, 偵測類型（勾選框）, 沒找到時的說明)
    "ads": ("掃描無關連內容", tuple(key for key in AD_ONLY_CATEGORIES if key != "repeat"), "內容"),
    "notes": ("作者感言與作品資訊", NOTE_CATEGORIES, "作者感言或作品資訊"),
}


def _changed_span(before: str, after: str):
    """換字的候選（網址片段、網頁字元碼）：原本那一行裡被換掉的那一段（起, 迄）。"""
    left = 0
    while left < min(len(before), len(after)) and before[left] == after[left]:
        left += 1
    right = 0
    while right < min(len(before), len(after)) - left and before[-1 - right] == after[-1 - right]:
        right += 1
    right = max(left, len(before) - right)
    # 「&amp;」換成「&」時最短的差別只有「amp;」：標成整個字元碼
    for match in _ENTITY.finditer(before):
        if match.start() < right and match.end() > left:
            left, right = min(left, match.start()), max(right, match.end())
    return left, right


_ENTITY = re.compile(r"&#?[0-9A-Za-z]{1,10};")


class _CandidatePane(QWidget):
    """一張候選表格＋選取按鈕＋狀態列。掃描無關連內容的兩個分頁、作者感言視窗共用。"""

    highlighted = Signal(int, int)

    def __init__(self, second_column: str, lines_source=lambda: [], parent=None):
        super().__init__(parent)
        self._lines_source = lines_source          # 目前的本文（前後文預覽用）
        self._candidates: list[dict] = []
        self._shown: list[int] = []            # 表格列出的候選（超過上限時只是其中一部分）
        self._selected: set[int] = set()
        self._second_column = second_column

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        select_row = QHBoxLayout()
        select_row.setSpacing(8)
        for label in ("勾選高信心", "勾選中信心", "勾選低信心", "全選", "全部取消"):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked, m=label: self.select_mode(m))
            select_row.addWidget(button)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self.status_label = QLabel("尚未掃描")
        self.status_label.setObjectName("fileLabel")
        layout.addWidget(self.status_label)

        self.table = PreviewTable(0, 3)
        self.table.setHorizontalHeaderLabels(["信心", second_column, "內容預覽"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: "contents", 1: 200 if second_column == "類型" else 90})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        enable_sorting(self.table)
        # 選到一列：下面顯示那一段加上前後文（拉中間的分隔可以調高度）
        self.preview = ContextPreview()
        layout.addWidget(self.preview.stacked_under(self.table), 1)
        self._empty_text = "沒有找到符合的內容"

    @staticmethod
    def candidate_key(candidate):
        return tuple(sorted(candidate["types"])), candidate["preview"]

    def state(self):
        """（目前的候選, {索引: 有沒有勾}）：重新掃描時交給 set_candidates 把勾選對回去。"""
        return self._candidates, {index: index in self._selected for index in self._shown}

    def set_candidates(self, candidates, empty_text: str, state=None):
        """換一批候選。state 是 state() 的結果：對得上的照舊，其餘照預設（高信心勾選）。"""
        self._candidates = list(candidates)
        self._empty_text = empty_text
        self._shown, _total = limit_rows(
            range(len(self._candidates)),
            lambda index: CONFIDENCE_ORDER.get(self._candidates[index]["confidence"], 9))
        kept = carry_over(state[0], self._candidates, self.candidate_key, state[1]) if state else {}
        self._selected = {index for index in self._shown
                          if kept.get(index, self._candidates[index]["confidence"] == "高")}
        self.refresh()

    def selected_candidates(self) -> list:
        return [self._candidates[index] for index in sorted(self._selected)]

    def refresh(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._shown))
        for row, index in enumerate(self._shown):
            candidate = self._candidates[index]
            confidence_item = make_item(candidate["confidence"], index,
                                        CONFIDENCE_ORDER.get(candidate["confidence"], 9))
            confidence_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            confidence_item.setCheckState(
                Qt.CheckState.Checked if index in self._selected else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, confidence_item)

            if self._second_column == "類型":
                labels = "、".join(AD_CATEGORY_LABELS[key] for key in AD_CATEGORY_LABELS if key in candidate["types"])
                self.table.setItem(row, 1, make_item(i18n.T(labels), index))
            else:
                count = candidate.get("repeat_count", 0)
                self.table.setItem(row, 1, make_item(i18n.T(f"{count} 次"), index, count))

            preview = re.sub(r"\s+", " ", candidate["preview"]).strip()
            if candidate.get("fix") is not None:
                preview = f"{preview} → {candidate['fix'].strip()}"
            # 內容預覽可以橫向捲動，只擋住幾十行串成一行的極端情況
            if len(preview) > 1000:
                preview = preview[:999] + "…"
            self.table.setItem(row, 2, make_item(preview, index))
        self.table.blockSignals(False)
        resort(self.table)
        self.update_status()
        self._update_preview()

    def update_status(self):
        if not self._candidates:
            i18n.set_text(self.status_label, self._empty_text)
            return
        text = f"找到 {len(self._candidates)} 個候選；已勾選 {len(self._selected)} 個"
        if len(self._shown) < len(self._candidates):
            text += f"（太多了，只列出 {len(self._shown)} 個，信心高的優先；刪除後會列出其餘的）"
        i18n.set_text(self.status_label, text)

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        index = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._selected.add(index)
        else:
            self._selected.discard(index)
        self.update_status()

    def _selected_candidate(self):
        rows = self.table.selectionModel().selectedRows()
        return self._candidates[data_index(self.table, rows[0].row())] if rows else None

    def _on_selection_changed(self):
        candidate = self._update_preview()
        if candidate is not None:
            self.highlighted.emit(candidate["start"], candidate["end"])

    def _update_preview(self):
        """前後文預覽跟著目前選的那一列；沒有選取就藏起來。回傳選到的候選。"""
        candidate = self._selected_candidate()
        lines = self._lines_source()
        if candidate is None or not lines:
            self.preview.hide()
            return candidate
        tokens = active_tokens()
        # 跟本文字色一樣：作者感言、作品資訊一種顏色，其餘（廣告、重複段落…）廣告的顏色
        color = (tokens.note_mark_text if set(candidate["types"]) <= set(NOTE_CATEGORIES)
                 else tokens.ad_mark_text)
        spans = {}
        fix = candidate.get("fix")
        if fix is not None and candidate["start"] == candidate["end"] and candidate["start"] < len(lines):
            spans[candidate["start"]] = _changed_span(lines[candidate["start"]], fix)
        self.preview.show_rows(lines, candidate["start"], candidate["end"], color, spans)
        return candidate

    def select_mode(self, button_label: str):
        if button_label == "全選":
            self._selected = set(self._shown)
        elif button_label == "全部取消":
            self._selected = set()
        else:
            # 加勾那一種信心，原本勾的保留（跟自訂章節規則的「勾選高信心」一樣）
            target = _CONFIDENCE_BY_BUTTON[button_label]
            self._selected |= {i for i in self._shown if self._candidates[i]["confidence"] == target}
        self.refresh()


class AdScanDialog(QDialog):
    candidateHighlighted = Signal(int, int)  # start_line, end_line（0-indexed，含首尾）
    deletionReady = Signal(list)             # 刪除後的整份本文

    def __init__(self, raw_lines: list, parent=None, selected_ranges=None, selected_count: int = 0,
                 enabled_categories=None, title_rows=None, mode: str = "ads", repeat_settings=None,
                 defer_scan: bool = False):
        """defer_scan: show the dialog first and scan when start_scan() is called (the main window does that once
        its idle-time cache warming is done — scanning a large file cold would freeze the window for seconds)."""
        super().__init__(parent)
        self._waiting = defer_scan
        self._mode = mode
        title_text, self._categories, self._empty_name = _MODES[mode]
        self.setWindowTitle(title_text)
        size_dialog(self, 900, 660)
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows) if title_rows is not None else None
        self._selected_ranges = list(selected_ranges or [])
        self._selected_count = selected_count
        self.result_lines: list | None = None
        self.result_summary = (0, 0)          # （刪掉幾行, 換回網頁字元碼的行數）

        root, footer = dialog_frame(self, intro=_INTROS[mode])
        root.setSpacing(12)

        # 「只掃描選取的章節」是範圍，兩個分頁共用，放在最上面（預設關著，見 ScopeToggle）。
        self.scope_check = ScopeToggle("掃描", selected_count if self._selected_ranges else 0)
        self.scope_check.toggled.connect(self._run_scan)
        root.addWidget(self.scope_check)

        # 偵測類型＋候選表格
        main_page = QWidget()
        main_layout = QVBoxLayout(main_page)
        main_layout.setContentsMargins(0, 8 if mode == "ads" else 0, 0, 0)
        main_layout.setSpacing(12)
        self.category_group = GroupCheckBox("偵測類型")
        self.category_group.members_changed.connect(self._run_scan)
        main_layout.addWidget(self.category_group)
        # 類型照視窗寬度自動換行：寬的時候一列排完，不會在右邊留一大塊空白。
        category_box, category_flow = flow_container(uniform=True)
        self._category_checks = {}
        for key in self._categories:
            checkbox = QCheckBox(AD_CATEGORY_LABELS[key])
            checkbox.setChecked(enabled_categories is None or key in enabled_categories)
            checkbox.setToolTip(_CATEGORY_TIPS.get(key, ""))
            checkbox.toggled.connect(self._run_scan)
            self._category_checks[key] = checkbox
            self.category_group.add_member(checkbox)
            category_flow.addWidget(checkbox)
        main_layout.addWidget(category_box)
        main_layout.addWidget(Divider())
        self._main_pane = _CandidatePane("類型", lambda: self._raw_lines)
        self._main_pane.highlighted.connect(self.candidateHighlighted.emit)
        main_layout.addWidget(self._main_pane, 1)

        self._repeat_pane = None
        self.tabs = None
        if mode == "ads":
            self.tabs = QTabWidget()
            self.tabs.addTab(main_page, "廣告與網頁字元")
            self.tabs.addTab(self._build_repeat_page(repeat_settings), "重複段落")
            self.tabs.currentChanged.connect(self._update_action_label)
            root.addWidget(self.tabs, 1)
        else:
            root.addWidget(main_page, 1)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.RejectRole)
        # 網頁字元碼、文中的廣告片段是換字，其他都是刪除：會換字的分頁叫「處理」，只會刪除的叫「刪除」
        self.delete_button = buttons.addButton("刪除已勾選項目", QDialogButtonBox.ButtonRole.AcceptRole)
        self.delete_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        self.delete_button.clicked.connect(self._delete_selected)
        footer.addWidget(buttons)

        self._update_action_label()
        if defer_scan:
            self.delete_button.setEnabled(False)
            for pane in (self._main_pane, self._repeat_pane):
                if pane is not None:
                    pane.set_candidates([], _WAITING_TEXT)
        else:
            self._run_scan()

    def start_scan(self):
        """The deferred first scan (see defer_scan), with whatever settings the user picked meanwhile."""
        if not self._waiting or not self.isVisible():
            return                  # already scanned, or closed while waiting
        self._waiting = False
        self.delete_button.setEnabled(True)
        self._run_scan()

    def _update_action_label(self, *_args):
        replaces = self._current_pane() is self._main_pane and bool((FIX_CATEGORIES | {"url"}) & set(self._categories))
        i18n.set_text(self.delete_button, "處理已勾選項目" if replaces else "刪除已勾選項目")

    def _build_repeat_page(self, repeat_settings) -> QWidget:
        """重複段落：整本出現好幾次的同一段文字（廣告常這樣重複貼）。太短的分段符號
        （「……」「＊＊＊」）也會重複很多次，所以最短長度跟次數都可以調。"""
        min_length, min_count = repeat_settings or (REPEAT_MIN_LENGTH, REPEAT_MIN_COUNT)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)

        # 兩個設定同一種樣子：標籤、拉桿、可以直接輸入也有上下箭頭的數字框（單位寫在框裡）。
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.repeat_length_slider, self.repeat_length_input = slider_with_spin(
            controls, "最短長度", REPEAT_LENGTH_RANGE, min_length, " 字")
        controls.addSpacing(16)
        self.repeat_count_slider, self.repeat_count_spin = slider_with_spin(
            controls, "至少重複", REPEAT_COUNT_RANGE, min_count, " 次")
        layout.addLayout(controls)

        self._repeat_pane = _CandidatePane("次數", lambda: self._raw_lines)
        self._repeat_pane.highlighted.connect(self.candidateHighlighted.emit)
        layout.addWidget(self._repeat_pane, 1)

        # 拉桿拖動時不要每一格都重掃：停一下再掃
        self._repeat_timer = QTimer(self)
        self._repeat_timer.setSingleShot(True)
        self._repeat_timer.setInterval(150)
        self._repeat_timer.timeout.connect(self._scan_repeats)
        self.repeat_length_slider.valueChanged.connect(self._on_repeat_setting_changed)
        self.repeat_count_slider.valueChanged.connect(self._on_repeat_setting_changed)
        return page

    @property
    def table(self):
        return self._main_pane.table

    @property
    def status_label(self):
        return self._main_pane.status_label

    def enabled_categories(self) -> set:
        return self._enabled_categories()

    def repeat_settings(self):
        """（最短長度, 至少幾次）；作者感言視窗沒有這個分頁，回傳 None。"""
        if self._repeat_pane is None:
            return None
        return self.repeat_length_slider.value(), self.repeat_count_slider.value()

    # ------------------------------------------------------------------
    def _set_scope(self, selected_ranges, selected_count: int):
        self._selected_ranges = list(selected_ranges or [])
        self.scope_check.set_count(selected_count if self._selected_ranges else 0)

    def reload(self, raw_lines, selected_ranges=None, selected_count: int = 0, title_rows=None):
        """本文改過了：重新掃描。使用者勾過／取消過的項目照內容對回去；
        新出現的項目照預設（高信心勾選）。"""
        self._raw_lines = list(raw_lines)
        if title_rows is not None:
            self._title_rows = set(title_rows)
        self.scope_check.blockSignals(True)
        self._set_scope(selected_ranges, selected_count)
        self.scope_check.blockSignals(False)
        self._run_scan(keep_state=True)

    def _enabled_categories(self):
        return {key for key, box in self._category_checks.items() if box.isChecked()}

    def _ranges(self):
        return self._selected_ranges if self.scope_check.isChecked() else None

    def _run_scan(self, *_args, keep_state: bool = False):
        if self._waiting:
            return          # start_scan() picks up the current settings
        categories = self._enabled_categories()
        state = self._main_pane.state() if keep_state else None
        if not categories:
            self._main_pane.set_candidates([], "尚未選擇偵測類型")
        else:
            candidates = scan_ad_candidates(self._raw_lines, categories, self._ranges(), self._title_rows)
            empty = (f"選取的章節裡沒有符合的{self._empty_name}" if self.scope_check.isChecked()
                     else f"沒有找到符合所選類型的{self._empty_name}")
            self._main_pane.set_candidates(candidates, empty, state)
        if self._repeat_pane is not None:
            self._scan_repeats(keep_state=keep_state)

    def _on_repeat_setting_changed(self, *_args):
        self._repeat_timer.start()

    def _scan_repeats(self, *_args, keep_state: bool = True):
        if self._waiting:
            return
        min_length, min_count = self.repeat_settings()
        state = self._repeat_pane.state() if keep_state else None
        candidates = scan_ad_candidates(self._raw_lines, {"repeat"}, self._ranges(), self._title_rows,
                                        repeat_min_length=min_length, repeat_min_count=min_count)
        self._repeat_pane.set_candidates(candidates, "沒有重複出現的段落", state)

    def _current_pane(self) -> _CandidatePane:
        if self.tabs is not None and self.tabs.currentIndex() == 1:
            return self._repeat_pane
        return self._main_pane

    def _delete_selected(self):
        selected = self._current_pane().selected_candidates()
        if not selected:
            dialogs.info(self, "尚未勾選", "請先勾選要處理的候選。")
            return
        # 網頁字元碼是換字、其餘是刪掉整段
        replacements = [candidate for candidate in selected if candidate.get("fix") is not None]
        deletions = [candidate for candidate in selected if candidate.get("fix") is None]
        parts = []
        if deletions:
            parts.append(f"刪除 {len(deletions)} 個候選段落")
        if replacements:
            parts.append(f"把 {len(replacements)} 行的網頁字元碼換回原本的字")
        if not dialogs.confirm(
            self, "確認處理",
            f"確定{'、'.join(parts)}嗎？\n\n處理後會重新建立目錄。",
        ):
            return

        lines = list(self._raw_lines)
        replaced = 0
        for candidate in replacements:
            if lines[candidate["start"]] == candidate["preview"]:
                lines[candidate["start"]] = candidate["fix"]
                replaced += 1
        ranges = sorted((candidate["start"], candidate["end"]) for candidate in deletions)
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))

        for start, end in reversed(merged):
            del lines[start:end + 1]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        self.result_lines = lines
        self.result_summary = (len(self._raw_lines) - len(lines), replaced)
        self.deletionReady.emit(lines)
