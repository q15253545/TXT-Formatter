"""「非正文內容」視窗（內容檢查卡片的「掃描非正文內容」），三個分頁：

- 廣告與網頁字元：網址、發布頁、QQ、微信、來源署名、網頁字元碼。
  網頁字元碼不是刪掉整行，是換回原本的字（候選帶著 fix）。
- 作者感言與作品資訊。
- 重複段落：最短長度、至少重複幾次都可以用拉桿或輸入框調整，表格即時更新。

偵測類型收成一顆下拉（widgets.ChoiceMenuButton），跟「依信心勾選」同一列。
「在本文逐筆檢查」打開本文上方的逐筆檢查列（main_window 的 review_bar）。

非模式：開著的時候可以直接在本文手動刪掉沒被找到的內容。對話框拿一份
raw_lines 快照作業；本文改過之後，主視窗會呼叫 reload() 換成新的一份重新
掃描（勾選狀態照內容對回去）。按「刪除已勾選項目」送出 deletionReady（只刪
目前這個分頁勾選的），由主視窗套用，對話框不關，可以繼續處理剩下的。
"""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.ad_scan import (
    AD_CATEGORY_LABELS, AD_ONLY_CATEGORIES, NOTE_CATEGORIES, REPEAT_MIN_COUNT, REPEAT_MIN_LENGTH,
    apply_candidates, scan_ad_candidates,
)
from . import dialogs, i18n
from .theme import active_tokens
from .widgets import (
    ChoiceMenuButton, ContextPreview, ScopeToggle, dialog_frame, size_dialog, slider_with_spin,
)
from .sortable_table import (
    HeaderCheckBox, PreviewTable, confidence_menu_button,
    CONFIDENCE_ORDER, carry_over, data_index, enable_sorting, limit_rows, make_item, resort, setup_columns,
)


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

ADS_TAB, NOTES_TAB, REPEAT_TAB = range(3)
# 有偵測類型的兩個分頁：(分頁名稱, 偵測類型, 沒找到時的說明)。重複段落優先度最低，放最後。
_CATEGORY_TABS = {
    ADS_TAB: ("廣告與網頁字元", tuple(key for key in AD_ONLY_CATEGORIES if key != "repeat"), "內容"),
    NOTES_TAB: ("作者感言與作品資訊", NOTE_CATEGORIES, "作者感言或作品資訊"),
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
    """一張候選表格＋選取按鈕＋狀態列。非正文內容的三個分頁共用。"""

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
        # 依信心勾選跟找到幾個放同一列；全選／全部取消是表格左上角的總勾選框
        select_row = QHBoxLayout()
        select_row.setSpacing(12)
        select_row.addWidget(confidence_menu_button(self.check_confidence))
        self.status_label = QLabel("尚未掃描")
        self.status_label.setObjectName("fileLabel")
        select_row.addWidget(self.status_label, 1)
        layout.addLayout(select_row)
        self.select_row = select_row        # 分頁可以在最前面加自己的設定（偵測類型）

        self.table = PreviewTable(0, 3)
        self.table.setHorizontalHeaderLabels(["信心", second_column, "內容預覽"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.header_check = HeaderCheckBox(self.table, lambda: (len(self._selected), len(self._shown)),
                                           self._set_all_checked)
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
                # 整行只有遺失的字（??）時換成空行：箭頭後面什麼都沒有看不懂，寫出來
                preview = f"{preview} → {candidate['fix'].strip() or i18n.T('（換成空行）')}"
            # 內容預覽可以橫向捲動，只擋住幾十行串成一行的極端情況
            if len(preview) > 1000:
                preview = preview[:999] + "…"
            self.table.setItem(row, 2, make_item(preview, index))
        self.table.blockSignals(False)
        resort(self.table)
        self.update_status()
        self._update_preview()

    def update_status(self):
        self.header_check.refresh()
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

    def check_confidence(self, levels: set):
        """依信心勾選：列出來的候選只勾那幾種信心的，其餘取消。"""
        self._selected = {index for index in self._shown if self._candidates[index]["confidence"] in levels}
        self.refresh()

    def _set_all_checked(self, checked: bool):
        self._selected = set(self._shown) if checked else set()
        self.refresh()


class AdScanDialog(QDialog):
    """非正文內容：三個分頁各自一張候選表格，偵測類型、勾選各自記。
    按「刪除已勾選項目」只處理目前這個分頁勾選的。"""

    candidateHighlighted = Signal(int, int)  # start_line, end_line（0-indexed，含首尾）
    deletionReady = Signal(list)             # 刪除後的整份本文
    reviewRequested = Signal(str, int)       # 「在本文逐筆檢查」：從哪一類（ad／note／repeat）、選到的那一筆從哪一行開始（沒選 -1）

    def __init__(self, raw_lines: list, parent=None, selected_ranges=None, selected_count: int = 0,
                 ad_categories=None, note_categories=None, title_rows=None, repeat_settings=None,
                 defer_scan: bool = False, start_tab: int = ADS_TAB):
        """defer_scan: show the dialog first and scan when start_scan() is called (the main window does that once
        its idle-time cache warming is done — scanning a large file cold would freeze the window for seconds)."""
        super().__init__(parent)
        self._waiting = defer_scan
        self.setWindowTitle("非正文內容")
        size_dialog(self, 900, 660)
        self._raw_lines = list(raw_lines)
        self._title_rows = set(title_rows) if title_rows is not None else None
        self._selected_ranges = list(selected_ranges or [])
        self.result_lines: list | None = None
        self.result_summary = (0, 0)          # （刪掉幾行, 換回網頁字元碼的行數）

        root, footer = dialog_frame(self, intro="找出廣告、作者感言、重複段落這類不屬於正文的內容，勾選後刪除。")
        root.setSpacing(12)

        # 「只掃描選取的章節」是範圍，三個分頁共用，放在最上面（預設關著，見 ScopeToggle）。
        self.scope_check = ScopeToggle("掃描", selected_count if self._selected_ranges else 0)
        self.scope_check.toggled.connect(self._run_scan)
        root.addWidget(self.scope_check)

        self.tabs = QTabWidget()
        self._panes = {}
        self._category_buttons = {}
        for key, (label, categories, empty_name) in _CATEGORY_TABS.items():
            chosen = ad_categories if key == ADS_TAB else note_categories
            self.tabs.addTab(self._build_category_page(key, categories, chosen), label)
        self.tabs.addTab(self._build_repeat_page(repeat_settings), "重複段落")
        self.tabs.setCurrentIndex(start_tab)
        self.tabs.currentChanged.connect(self._update_action_label)
        # 只掃看得到的那一頁：其他分頁切過去才掃（大檔每一頁要零點幾秒）
        self._stale: set = set()
        self.tabs.currentChanged.connect(self._scan_if_stale)
        root.addWidget(self.tabs, 1)

        # 「在本文逐筆檢查」是換一種方式看（到本文一筆一筆跳），不是這個視窗的處理：放在左邊，跟處理／關閉分開
        review_button = QPushButton("在本文逐筆檢查")
        review_button.clicked.connect(self._request_review)
        footer.addWidget(review_button)
        footer.addStretch(1)
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
            for pane in self._panes.values():
                pane.set_candidates([], _WAITING_TEXT)
        else:
            self._run_scan()

    def _build_category_page(self, key: int, categories, chosen) -> QWidget:
        """廣告與網頁字元、作者感言與作品資訊：偵測類型收成一顆下拉（跟依信心勾選同一列）＋候選表格。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(12)
        pane = _CandidatePane("類型", lambda: self._raw_lines)
        pane.highlighted.connect(self.candidateHighlighted.emit)
        label = QLabel("偵測類型")
        label.setObjectName("fileLabel")
        button = ChoiceMenuButton([(item, AD_CATEGORY_LABELS[item], _CATEGORY_TIPS.get(item, ""))
                                   for item in categories], chosen)
        button.changed.connect(lambda key=key: self._scan_categories(key))
        pane.select_row.insertWidget(0, label)
        pane.select_row.insertWidget(1, button)
        pane.select_row.insertSpacing(2, 8)
        layout.addWidget(pane, 1)
        self._panes[key] = pane
        self._category_buttons[key] = button
        return page

    def start_scan(self):
        """The deferred first scan (see defer_scan), with whatever settings the user picked meanwhile."""
        if not self._waiting or not self.isVisible():
            return                  # already scanned, or closed while waiting
        self._waiting = False
        self.delete_button.setEnabled(True)
        self._run_scan()

    def _update_action_label(self, *_args):
        replaces = self.tabs.currentIndex() == ADS_TAB
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
        self.repeat_length_slider, _length_input = slider_with_spin(
            controls, "最短長度", REPEAT_LENGTH_RANGE, min_length, " 字")
        controls.addSpacing(16)
        self.repeat_count_slider, _count_input = slider_with_spin(
            controls, "至少重複", REPEAT_COUNT_RANGE, min_count, " 次")
        layout.addLayout(controls)

        pane = _CandidatePane("次數", lambda: self._raw_lines)
        pane.highlighted.connect(self.candidateHighlighted.emit)
        layout.addWidget(pane, 1)
        self._panes[REPEAT_TAB] = pane

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
        return self._panes[ADS_TAB].table

    @property
    def status_label(self):
        return self._panes[ADS_TAB].status_label

    def enabled_categories(self) -> set:
        """廣告與網頁字元分頁勾的偵測類型。"""
        return self._category_buttons[ADS_TAB].checked()

    def note_categories(self) -> set:
        """作者感言與作品資訊分頁勾的偵測類型。"""
        return self._category_buttons[NOTES_TAB].checked()

    def repeat_settings(self):
        """（最短長度, 至少幾次）。"""
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

    def _ranges(self):
        return self._selected_ranges if self.scope_check.isChecked() else None

    def _run_scan(self, *_args, keep_state: bool = False):
        """範圍、本文變了：目前這一頁馬上重掃，其他頁記成過期，切過去時才掃。"""
        if self._waiting:
            return          # start_scan() picks up the current settings
        self._stale = {key for key in self._panes if key != self.tabs.currentIndex()}
        self._stale_keep = keep_state
        self._scan_tab(self.tabs.currentIndex(), keep_state)

    def _scan_tab(self, key: int, keep_state: bool):
        if key == REPEAT_TAB:
            self._scan_repeats(keep_state=keep_state)
        else:
            self._scan_categories(key, keep_state=keep_state)

    def _scan_if_stale(self, key: int):
        if key in self._stale:
            self._stale.discard(key)
            self._scan_tab(key, self._stale_keep)

    def _scan_categories(self, key: int, keep_state: bool = False):
        if self._waiting:
            return
        if key != self.tabs.currentIndex():
            self._stale.add(key)        # 偵測類型改了但不在這一頁（理論上不會）：切過去再掃
            return
        pane = self._panes[key]
        categories = self._category_buttons[key].checked()
        empty_name = _CATEGORY_TABS[key][2]
        state = pane.state() if keep_state else None
        if not categories:
            pane.set_candidates([], "尚未選擇偵測類型")
            return
        candidates = scan_ad_candidates(self._raw_lines, categories, self._ranges(), self._title_rows)
        empty = (f"選取的章節裡沒有符合的{empty_name}" if self.scope_check.isChecked()
                 else f"沒有找到符合所選類型的{empty_name}")
        pane.set_candidates(candidates, empty, state)

    def _on_repeat_setting_changed(self, *_args):
        self._repeat_timer.start()

    def _scan_repeats(self, *_args, keep_state: bool = True):
        if self._waiting:
            return
        min_length, min_count = self.repeat_settings()
        pane = self._panes[REPEAT_TAB]
        state = pane.state() if keep_state else None
        candidates = scan_ad_candidates(self._raw_lines, {"repeat"}, self._ranges(), self._title_rows,
                                        repeat_min_length=min_length, repeat_min_count=min_count)
        pane.set_candidates(candidates, "沒有重複出現的段落", state)

    def _request_review(self):
        tab = self.tabs.currentIndex()
        candidate = self._current_pane()._selected_candidate()
        self.reviewRequested.emit({ADS_TAB: "ad", NOTES_TAB: "note"}.get(tab, "repeat"),
                                  candidate["start"] if candidate else -1)
        # 改到本文逐筆看：視窗留著會擋住本文，內容也會隨著在本文刪改而過期；要一次處理很多筆再開
        self.reject()

    def _current_pane(self) -> _CandidatePane:
        return self._panes[self.tabs.currentIndex()]

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

        lines, _removed, replaced = apply_candidates(self._raw_lines, selected)
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()

        self.result_lines = lines
        self.result_summary = (len(self._raw_lines) - len(lines), replaced)
        self.deletionReady.emit(lines)
