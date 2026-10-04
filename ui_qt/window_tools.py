"""主視窗：工具視窗（字數、重複章節、非正文內容、標點校對、繁簡轉換、
辨識章節（含可疑章節）、新增章節）與本文字色標示。

工具視窗是非模式的：開著也能改本文，切回視窗時照文字版本決定要不要重算（_open_tool_dialog）。
MainWindow 的一部分（mixin），只用 MainWindow 的屬性與方法。"""

import bisect
import dataclasses
import os
import threading

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QTableWidget, QTextEdit

from core.ad_scan import (
    AD_CATEGORY_LABELS, AD_ONLY_CATEGORIES, FIX_CATEGORIES, NOTE_CATEGORIES, REPEAT_MIN_COUNT,
    REPEAT_MIN_LENGTH, apply_candidates, scan_ad_candidates,
)
from core.chapter_update import (
    LONGER, MISSING, NEW, append_all, apply_update, dominant_script, plan_update, toc_entries,
)
from core.docx_reader import is_docx
from core.epub_reader import is_epub
from core.encoding import smart_detect_encoding, strip_invisible_chars
from core.quote_check import PROBLEM_LABELS
from core.script_convert import (
    DEFAULT_VOCABULARY, convert_with_word_lists, opencc_available, parse_keep_words, parse_vocabulary,
)
from core.word_count import chapter_word_counts
from core.structure_builder import build_document_structure
from core.insert_suggestions import get_insert_suggestions

from . import dialogs, i18n
from .app_log import action, log, native_dialog
from .ad_scan_dialog import AdScanDialog
from .chapter_update_dialog import APPEND_ALL, OTHER_FILE, ChapterUpdateDialog
from .duplicate_chapters_dialog import DuplicateChaptersDialog
from .insert_title_dialog import InsertTitleDialog
from .quote_check_dialog import QuoteCheckDialog
from .script_convert_dialog import ScriptConvertDialog
from .recognition_dialog import RecognitionDialog
from .word_count_dialog import WordCountDialog
from .window_common import MARK_SCAN_DELAY_MS, OPEN_FILE_FILTER

# 標點校對預設不勾的檢查項目（只能列出、數量常很多）
QUOTE_DEFAULT_OFF = frozenset({"masked"})


class ToolWindowsMixin:
    @action
    def open_insert_title_dialog(self):
        if not self.editor.toPlainText().strip():
            return
        insert_index = self.editor.textCursor().blockNumber()
        self._sync_raw_lines()
        insert_index = min(insert_index, len(self.raw_lines))
        recognized_indices = sorted(set(self.chapter_raw_map.values()))
        suggestions, default_kind = get_insert_suggestions(self.raw_lines, recognized_indices, insert_index)
        self._close_tool_dialogs()

        dialog = InsertTitleDialog(suggestions, default_kind, self)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            generated = dialog.result_text
        finally:
            dialog.deleteLater()
        if not accepted:
            return

        lines = self.raw_lines
        previous_is_blank = insert_index == 0 or not lines[insert_index - 1].strip()
        current_is_blank = insert_index >= len(lines) or not lines[insert_index].strip()
        leading = "" if previous_is_blank else "\n"
        trailing = "\n" if current_is_blank else "\n\n"
        generated_line = insert_index + (0 if previous_is_blank else 1)

        block = self.editor.document().findBlockByNumber(insert_index)
        position = block.position() if block.isValid() else len(self.editor.toPlainText())
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(position)
        cursor.insertText(leading + generated + trailing)
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == generated_line:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._checkpoint_document()
        self._show_status(f"已新增章節：{generated}")

    def _open_tool_dialog(self, key: str, create, reload, on_closed=None):
        """開一個「開著也能編輯本文」的工具對話框。

        非模式，本文隨時可以改，所以對話框手上的本文快照可能過期：每次切回對話框時
        （_ToolDialogWatcher）比對文字版本，改過就呼叫 reload 用新的本文重算。

        create()：建立對話框；reload(dialog)：用目前的本文重算；
        on_closed(dialog, accepted)：關閉時要做的事（記住設定、套用結果）。"""
        existing = self._tool_dialogs.get(key)
        if existing is not None:
            existing.showNormal()
            existing.raise_()
            existing.activateWindow()
            return None
        # 一次只開一個工具視窗：開新的之前先把其他開著的關掉（照常記住它們的設定）。
        self._close_tool_dialogs()
        dialog = create()
        dialog._tool_version = self._text_version
        dialog._tool_reload = reload
        dialog.installEventFilter(self._tool_dialog_watcher)
        dialog.finished.connect(
            lambda result, d=dialog: self._on_tool_dialog_finished(key, d, result, on_closed))
        # 在表格上點兩下：跳到本文那一行，並把焦點交給本文，可以直接改。
        for table in dialog.findChildren(QTableWidget):
            table.doubleClicked.connect(self._focus_editor_from_tool)
        self._tool_dialogs[key] = dialog
        dialog.show()
        return dialog

    def _close_tool_dialogs(self):
        for dialog in list(self._tool_dialogs.values()):
            dialog.reject()

    def _focus_editor_from_tool(self, *_args):
        self.activateWindow()
        self.raise_()
        self.editor.setFocus()

    def _refresh_tool_dialog(self, dialog) -> bool:
        """本文在對話框上次分析之後改過，就讓它重算；有重算回傳 True。"""
        if getattr(dialog, "_tool_version", None) is None or dialog._tool_version == self._text_version:
            return False
        self._sync_raw_lines()
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()
        dialog._tool_reload(dialog)
        dialog._tool_version = self._text_version
        return True

    def _on_tool_dialog_finished(self, key, dialog, result, on_closed):
        self._tool_dialogs.pop(key, None)
        self.editor.setExtraSelections([])
        try:
            if on_closed is not None:
                on_closed(dialog, result == QDialog.DialogCode.Accepted)
        finally:
            # 對話框留著整份 raw_lines 與整張表格；明確釋放，不然一直開一直
            # 累積到主視窗關閉為止。
            dialog.deleteLater()

    def _replace_text_from_tool(self, lines: list):
        """工具對話框改完的整份本文：拆行、接行、刪行會改變行數，交給
        _sync_raw_lines 比對新舊兩版搬章節狀態。"""
        self._set_editor_text("\n".join(lines), "diff")
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()

    @action
    def open_word_count_dialog(self):
        """章節字數：每一章正文的字數，標出特別短、特別長的章（core/word_count.py）。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()

        def create():
            dialog = WordCountDialog(*self._word_counts(), self)
            dialog.chapterSelected.connect(lambda row: self._jump_to_line(row + 1))
            return dialog

        def reload(dialog):
            dialog.reload(*self._word_counts())

        self._open_tool_dialog("word_count", create, reload)

    def _word_counts(self):
        """目錄裡沒有子項目的（章、序章、番外…）各算一段：從標題下一行到下一個目錄項目之前。"""
        title_rows = sorted(set(self.chapter_raw_map.values()))
        sections = []
        for item, row in sorted(self.chapter_raw_map.items(), key=lambda pair: pair[1]):
            if item.childCount():
                continue
            position = bisect.bisect_right(title_rows, row)
            end = title_rows[position] if position < len(title_rows) else len(self.raw_lines)
            parent = item.parent()
            volume = self.toc_full_labels.get(parent, parent.text(0)) if parent is not None else ""
            sections.append((row, end, self.toc_full_labels.get(item, item.text(0)), volume))
        return chapter_word_counts(self.raw_lines, sections)

    def _tool_scope(self):
        spans = self._selected_section_spans()
        return spans, len(self._selected_toc_items()) if spans else 0

    @action
    def open_duplicate_chapters_dialog(self):
        """相鄰、章號相同的章節一列一章，勾選要保留的（判斷規則見 core/duplicate_chapters.py）。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()

        def create():
            dialog = DuplicateChaptersDialog(self.raw_lines, set(self.chapter_raw_map.values()), self)
            dialog.groupHighlighted.connect(self._highlight_ad_candidate)
            dialog.mergeReady.connect(lambda lines, d=dialog: self._apply_duplicate_merge(d, lines))
            return dialog

        def reload(dialog):
            dialog.reload(self.raw_lines, set(self.chapter_raw_map.values()))

        self._open_tool_dialog("duplicate_chapters", create, reload)

    @action
    def _apply_duplicate_merge(self, dialog, lines: list):
        if self._refresh_tool_dialog(dialog):
            dialogs.info(dialog, "本文已修改", "本文在檢查之後改過了，已經重新檢查，請確認勾選的項目後再按一次。")
            return
        removed = len(self.raw_lines) - len(lines)
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        self._refresh_tool_dialog(dialog)
        self._show_status(f"已刪除未保留的章節（共 {removed} 行），可以按 Ctrl+Z 復原")

    def _saved_ad_categories(self) -> set:
        """非正文內容視窗「廣告與網頁字元」分頁記住的偵測類型（不含重複段落，那在自己的分頁）；沒記過就全部。"""
        own = {key for key in AD_ONLY_CATEGORIES if key != "repeat"}
        saved = self._ui_state.get("ad_categories")
        return set(saved) & own if isinstance(saved, list) else own

    def _saved_note_categories(self) -> set:
        saved = self._ui_state.get("note_categories")
        return set(saved) & set(NOTE_CATEGORIES) if isinstance(saved, list) else set(NOTE_CATEGORIES)

    def _saved_repeat_settings(self):
        saved = self._ui_state.get("repeat_settings")
        if (isinstance(saved, list) and len(saved) == 2
                and all(isinstance(value, int) and value > 0 for value in saved)):
            return saved[0], saved[1]
        return REPEAT_MIN_LENGTH, REPEAT_MIN_COUNT

    @action
    def open_ad_scan_dialog(self):
        """非正文內容視窗：廣告與網頁字元、作者感言與作品資訊、重複段落三個分頁。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()

        def create():
            # Caches still warming (a large file opened moments ago): show the dialog now and scan when they're done
            cold = self._caches_cold()
            dialog = AdScanDialog(self.raw_lines, self, selected_ranges=spans,
                                  selected_count=self._selected_chapter_count(),
                                  ad_categories=self._saved_ad_categories(),
                                  note_categories=self._saved_note_categories(),
                                  title_rows=set(self.chapter_raw_map.values()),
                                  repeat_settings=self._saved_repeat_settings(), defer_scan=cold)
            if cold:
                # the dialog may have been closed (and deleted) by the time the caches are warm
                self._when_warm(lambda: dialog.start_scan() if shiboken6.isValid(dialog) else None)
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            dialog.deletionReady.connect(lambda lines, d=dialog: self._apply_ad_deletion(d, lines))
            dialog.reviewRequested.connect(self._review_from_scan)
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count, title_rows=set(self.chapter_raw_map.values()))

        def on_closed(dialog, _accepted):
            self._ui_state["ad_categories"] = sorted(dialog.enabled_categories())
            self._ui_state["note_categories"] = sorted(dialog.note_categories())
            self._ui_state["repeat_settings"] = list(dialog.repeat_settings())
            if self.review_bar.marking():
                self._schedule_mark_scan(0)      # 勾選的類型可能變了，照新的重標

        self._open_tool_dialog("ad_scan", create, reload, on_closed)

    @action
    def start_review(self):
        """開始逐筆檢查：本文上方出現逐筆檢查列、用字色標出非正文內容，掃好就跳到游標後面的第一筆。
        已經在檢查時就跳到下一筆。"""
        if not self.raw_lines or not any(line.strip() for line in self.raw_lines):
            return
        if self.review_bar.is_active():
            self.goto_mark(True)
            return
        self.review_bar.set_active(True)
        self._mark_advance_pending = True
        self._on_marking_changed()
        self._show_status("逐筆檢查：F8 下一筆、Shift+F8 上一筆，Esc 結束；要看哪幾類在「篩選」裡勾（顏色定義見說明）")

    def _review_from_scan(self, kind: str, row: int):
        """非正文內容視窗的「在本文逐筆檢查」：篩選確定有勾那一類（從重複段落分頁按，預設不看重複段落就什麼都找不到），
        表格選到一筆時從那一筆開始（游標放到那一行，逐筆檢查從游標接著找）。"""
        types = self.review_bar.review_types()
        if kind not in types:
            self.review_bar.set_review_types(types | {kind})
            self._on_review_types_changed()
        self._focus_editor_from_tool()
        if row >= 0:
            self._jump_to_line(row + 1)
        if self.review_bar.is_active():
            self._mark_current = -1
            self.goto_mark(True)
        else:
            self.start_review()

    def stop_review(self):
        """結束逐筆檢查：逐筆檢查列收起來，本文的字色一起收掉。"""
        if not self.review_bar.is_active():
            return
        self.review_bar.set_active(False)
        self._mark_advance_pending = False
        self._mark_pending_step = None
        self.editor.setExtraSelections([])
        self._on_marking_changed()
        self._show_status("已結束逐筆檢查")

    def _on_review_types_changed(self):
        """逐筆檢查的「篩選」換了類型：記下來，照新的類型重掃（信心只要重新篩，見 _on_mark_confidence_changed）。"""
        self._ui_state["review_types"] = sorted(self.review_bar.review_types())
        self._mark_current = -1
        self._on_marking_changed()

    def _on_marking_changed(self):
        """逐筆檢查開始、結束或換了類型：沒有要標的就清掉字色，否則重掃。"""
        kinds = self.review_bar.marking()
        if not kinds:
            self._mark_candidates = {"ad": [], "note": []}
            self._mark_rows = {"ad": set(), "note": set()}
            self._mark_timer.stop()
            self._mark_rows_version = None
            self._mark_current = -1
        self._refresh_title_formats()
        self._update_mark_position()
        if kinds:
            self._schedule_mark_scan(0)

    def _on_mark_confidence_changed(self):
        """逐筆檢查的信心篩選：掃描結果不變，重新篩出要上色、要跳轉的那幾筆就好。"""
        self._ui_state["mark_confidence"] = sorted(self.review_bar.mark_confidence())
        self._mark_current = -1
        self._refresh_mark_rows()
        self._refresh_title_formats()
        self._update_mark_position()

    def _mark_targets(self) -> list:
        """本文字色標出來、逐筆檢查可以一筆一筆跳過去的候選（照信心篩選），照位置排好。
        掃描結果不是這一版本文的（剛改過、還在重掃）就沒有。"""
        if not self._mark_results_current():
            return []
        levels = self.review_bar.mark_confidence()
        found = [candidate for kind in ("ad", "note") for candidate in self._mark_candidates[kind]
                 if candidate["confidence"] in levels]
        return sorted(found, key=lambda candidate: (candidate["start"], candidate["end"]))

    def _mark_results_current(self) -> bool:
        """掃描結果對應的是目前這一版本文、目前目錄認出的標題行。"""
        return (self._mark_rows_version == self._text_version
                and self._mark_rows_generation == self._outline_generation)

    def _refresh_mark_rows(self):
        """照信心篩選把候選換成要上色的行；同一行兩種都是用廣告的顏色。"""
        levels = self.review_bar.mark_confidence()
        rows = {}
        for kind in ("ad", "note"):
            rows[kind] = {row for candidate in self._mark_candidates[kind] if candidate["confidence"] in levels
                          for row in range(candidate["start"], candidate["end"] + 1)}
        self._mark_rows = {"ad": rows["ad"], "note": rows["note"] - rows["ad"]}

    def _update_mark_position(self):
        targets = self._mark_targets()
        if not 0 <= self._mark_current < len(targets):
            self._mark_current = -1
        info = ""
        if self._mark_current >= 0:
            candidate = targets[self._mark_current]
            labels = "、".join(i18n.T(AD_CATEGORY_LABELS[key]) for key in AD_CATEGORY_LABELS if key in candidate["types"])
            info = f"{labels} · {i18n.T(candidate['confidence'] + '信心')}"
        self.review_bar.set_position(self._mark_current, len(targets), info)

    @action
    def goto_mark(self, forward: bool):
        """逐筆檢查的上一筆／下一筆：從游標的位置接著找（跟尋找取代一樣），到底了繞回另一頭。"""
        targets = self._mark_targets()
        if not targets:
            self._mark_current = -1
            self._update_mark_position()
            if self._mark_scan_running or self._mark_timer.isActive():
                # 刪掉一筆、改了本文之後會重掃（大檔要零點幾秒）：這時按的記下來，掃好就跳，不用再按一次
                self._mark_pending_step = forward
                self._show_status("正在重新找要檢查的內容，找好就跳到" + ("下一筆" if forward else "上一筆"))
            else:
                self._show_status("沒有要檢查的內容（逐筆檢查的篩選、非正文內容視窗勾的偵測類型都會影響）")
            return
        row = self.editor.textCursor().blockNumber()
        current = targets[self._mark_current] if 0 <= self._mark_current < len(targets) else None
        if current is not None and current["start"] <= row <= current["end"]:
            index = (self._mark_current + (1 if forward else -1)) % len(targets)
        elif forward:
            index = next((i for i, candidate in enumerate(targets) if candidate["start"] > row
                          or (candidate["start"] == row and current is None)), 0)
        else:
            index = next((i for i in range(len(targets) - 1, -1, -1) if targets[i]["start"] < row), len(targets) - 1)
        self._mark_current = index
        candidate = targets[index]
        self._jump_to_line(candidate["start"] + 1)
        self._highlight_ad_candidate(candidate["start"], candidate["end"])
        self._update_mark_position()

    @action
    def delete_current_mark(self):
        """逐筆檢查的「刪除這筆」：跟掃描視窗的刪除一樣處理（夾在正文裡的網址只刪那一段），重掃完自動跳到下一筆。"""
        targets = self._mark_targets()
        if not 0 <= self._mark_current < len(targets):
            self._show_status("先按上一筆／下一筆選一筆")
            return
        candidate = targets[self._mark_current]
        lines, removed, _replaced = apply_candidates(self.raw_lines, [candidate])
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        cursor = self.editor.textCursor()
        block = self.editor.document().findBlockByNumber(min(candidate["start"], self.editor.document().blockCount() - 1))
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self._mark_current = -1
        self._mark_advance_pending = True
        self._update_mark_position()
        what = f"刪除 {removed} 行" if removed else "刪除夾在正文裡的那一段"
        self._show_status(i18n.T(f"已{what}，可以按 Ctrl+Z 復原"), translated=True)

    def _schedule_mark_scan(self, delay_ms: int = MARK_SCAN_DELAY_MS):
        if not self.raw_lines or not any(line.strip() for line in self.raw_lines):
            return
        self._mark_timer.start(delay_ms)

    def _start_mark_scan(self):
        """在背景執行緒掃描（大檔要將近一秒），掃完才回到主執行緒上色。
        掃描期間本文又改了：結果作廢，等這一輪結束再掃一次。"""
        kinds = self.review_bar.marking()
        if not kinds:
            return
        if self._mark_scan_running:
            self._mark_scan_pending = True
            return
        self._sync_raw_lines()
        lines = list(self.raw_lines)
        version = self._text_version
        self._mark_scan_generation = self._outline_generation
        # 目錄過期（剛改過本文）：在同一個背景執行緒裡連目錄一起辨識，畫面只負責把結果畫上去
        # （在畫面上重建目錄，大檔要半秒以上）。
        toc_ctx = None
        title_rows = set(self.chapter_raw_map.values())
        if self._toc_text_version != version:
            toc_ctx = dataclasses.replace(
                self._build_context(), raw_lines=lines, user_chapter_rules=list(self.user_chapter_rules),
                auto_titles=dict(self.auto_titles), force_lv1_chapters=set(self.force_lv1_chapters),
                force_lv2_chapters=set(self.force_lv2_chapters))
        # 網頁字元碼只是換字，不是廣告：不標廣告色。重複段落要在逐筆檢查的篩選勾了才標（多半是作者慣用的句子）
        types = self.review_bar.review_types()
        ad_categories = ((self._saved_ad_categories() - FIX_CATEGORIES if "ad" in types else set())
                         | ({"repeat"} if "repeat" in types else set()))
        note_categories = self._saved_note_categories() if "note" in types else set()
        min_length, min_count = self._saved_repeat_settings()
        self._mark_scan_running = True

        def work():
            try:
                structure = None
                rows = title_rows
                if toc_ctx is not None:
                    structure = build_document_structure(toc_ctx, apply_format=False, write_text=False)
                    rows = set(structure.chapter_raw_map.values())
                ad_found = scan_ad_candidates(lines, ad_categories, None, rows, repeat_min_length=min_length,
                                              repeat_min_count=min_count) if ad_categories else []
                note_found = scan_ad_candidates(lines, note_categories, None, rows) if note_categories else []
                result = (version, ad_found, note_found, structure)
            except Exception:          # 背景執行緒的例外不會出現在畫面上：記下來、結束這一輪
                log.exception("字色標示掃描失敗")
                result = (-1, [], [], None)
            # the window may have been closed while scanning (the app is quitting): nothing to report to then
            if shiboken6.isValid(self._mark_signals):
                self._mark_signals.finished.emit(*result)

        threading.Thread(target=work, name="mark-scan", daemon=True).start()

    def _on_mark_scan_finished(self, version: int, ad_found, note_found, structure=None):
        self._mark_scan_running = False
        if (self._mark_scan_pending or version != self._text_version
                or self._mark_scan_generation != self._outline_generation):
            self._mark_scan_pending = False
            if self.review_bar.marking() and version != -1:
                self._schedule_mark_scan()
            return
        kinds = self.review_bar.marking()
        if not kinds:
            return
        self._mark_candidates = {"ad": list(ad_found), "note": list(note_found)}
        self._refresh_mark_rows()
        if structure is not None and self._toc_text_version != version:
            self._populate_tree(structure, rescan_marks=False)
            self._warn_timed_out_rules()
        self._mark_rows_version = version
        self._mark_rows_generation = self._outline_generation
        self._refresh_title_formats()
        self._update_mark_position()
        # 剛用「刪除這筆」刪掉一筆（接著停在下一筆），或重掃時按了上一筆／下一筆：重掃好了，照按的方向跳。
        # 刪掉之後又按了下一筆也只跳一次：刪掉那筆的下一筆就是使用者要的那一筆。
        step = self._mark_pending_step if self._mark_pending_step is not None else (
            True if self._mark_advance_pending else None)
        self._mark_advance_pending = False
        self._mark_pending_step = None
        if step is not None and self._mark_targets():
            self.goto_mark(step)

    def _apply_mark_colors(self, cursor: QTextCursor):
        """把掃描到的廣告／作者感言那幾行換成對應的字色（章節標題不動）。
        只在掃描結果對應的就是目前這一版本文時才畫：行號過期會標錯行。"""
        if not self.review_bar.marking() or not self._mark_results_current():
            return
        document = self.editor.document()
        title_rows = set(self.chapter_raw_map.values())
        for key, color in (("note", self.tokens.note_mark_text), ("ad", self.tokens.ad_mark_text)):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            for row in sorted(self._mark_rows[key] - title_rows):
                block = document.findBlockByNumber(row)
                if not block.isValid() or block.length() <= 1:
                    continue
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(fmt)

    @action
    def _apply_ad_deletion(self, dialog, lines: list):
        if self._refresh_tool_dialog(dialog):
            dialogs.info(dialog, "本文已修改", "本文在掃描之後改過了，已經重新掃描，請確認勾選的項目後再按一次。")
            return
        removed, replaced = dialog.result_summary
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        self._refresh_tool_dialog(dialog)
        done = ([f"刪除 {removed} 行"] if removed else []) + ([f"換回 {replaced} 行的網頁字元碼"] if replaced else [])
        self._show_status(i18n.T("已" + "、".join(done or ["處理完成"]) + "，可以按 Ctrl+Z 復原"), translated=True)

    def _highlight_ad_candidate(self, start_line: int, end_line: int):
        document = self.editor.document()
        start_block = document.findBlockByNumber(start_line)
        end_block = document.findBlockByNumber(end_line)
        if not start_block.isValid() or not end_block.isValid():
            return
        tokens = self.tokens
        # 整行（含行尾空白處）用較淡的底色，跟文字選取同色系但淺一階：
        # 使用者在這幾行裡選字複製時，選取範圍才看得出來。
        selections = []
        block = start_block
        while block.isValid() and block.blockNumber() <= end_block.blockNumber():
            selection = QTextEdit.ExtraSelection()
            selection.cursor = QTextCursor(block)
            char_format = selection.format
            char_format.setBackground(QColor(tokens.jump_bg))
            char_format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.format = char_format
            selections.append(selection)
            block = block.next()
        self.editor.setExtraSelections(selections)

        jump_cursor = self.editor.textCursor()
        jump_cursor.setPosition(start_block.position())
        self.editor.setTextCursor(jump_cursor)
        self.editor.centerCursor()

    @action
    def open_quote_check_dialog(self):
        """標點校對；勾選的項目可以自動修正，其餘開著對話框直接在本文改。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        # 記住勾了哪些；沒記過時星號遮字關著（只能列出、書裡常有上百處），其餘都開。
        # 「分隔線不一致」不是勾選框（由視窗裡的下拉決定），一律開著。
        saved = self._ui_state.get("quote_kinds")
        enabled = ((set(saved) & set(PROBLEM_LABELS) if isinstance(saved, list)
                    else set(PROBLEM_LABELS) - QUOTE_DEFAULT_OFF) | {"separator_style"})

        def create():
            # 逐行判斷還沒暖好（大檔剛開）：視窗先出來，它自己分批算好再檢查
            dialog = QuoteCheckDialog(self.raw_lines, self, selected_ranges=spans,
                                      selected_count=self._selected_chapter_count(),
                                      enabled_kinds=enabled,
                                      title_rows=set(self.chapter_raw_map.values()),
                                      defer_scan=self._caches_cold(1))
            dialog.problemSelected.connect(self._jump_to_line)
            dialog.fixesReady.connect(lambda lines, count, d=dialog: self._apply_quote_fixes(d, lines, count))
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count, title_rows=set(self.chapter_raw_map.values()))

        def on_closed(dialog, _accepted):
            self._ui_state["quote_kinds"] = sorted(dialog.enabled_kinds())

        self._open_tool_dialog("quote_check", create, reload, on_closed)

    @action
    def _apply_quote_fixes(self, dialog, lines: list, count: int):
        if self._refresh_tool_dialog(dialog):
            dialogs.info(dialog, "本文已修改", "本文在檢查之後改過了，已經重新檢查，請確認勾選的項目後再按一次。")
            return
        self.editor.setExtraSelections([])
        self._replace_text_from_tool(lines)
        # 對話框不關：用修正後的本文重新檢查，剩下要手動處理的繼續列著。
        self._refresh_tool_dialog(dialog)
        self._show_status(f"已修正 {count} 處標點問題，可以按 Ctrl+Z 復原")

    @action
    def open_script_convert_dialog(self):
        """全文（或選取章節）繁簡轉換。"""
        if not self.editor.toPlainText().strip():
            return
        if not opencc_available():
            dialogs.info(self, "需要 OpenCC",
                         "繁簡轉換需要 OpenCC 套件，目前的執行環境沒有安裝。\n\n"
                         "安裝指令：pip install opencc-python-reimplemented")
            return
        self._close_tool_dialogs()
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        keep_text = self._ui_state.get("script_keep_words")
        vocabulary_text = self._ui_state.get("script_vocabulary")
        dialog = ScriptConvertDialog(self, selected_count=self._selected_chapter_count() if spans else 0,
                                     mode=self._ui_state.get("script_mode"),
                                     keep_words=keep_text if isinstance(keep_text, str) else "",
                                     vocabulary=vocabulary_text if isinstance(vocabulary_text, str)
                                     else DEFAULT_VOCABULARY)
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            # 詞表按取消也記住（改到一半關掉不會不見）
            self._ui_state["script_keep_words"] = dialog.keep_words_text()
            self._ui_state["script_vocabulary"] = dialog.vocabulary_text()
            if not accepted:
                return
            mode = dialog.mode()
            selected_only = dialog.selected_only()
            self._ui_state["script_mode"] = mode
            keep_words = parse_keep_words(dialog.keep_words_text())
            vocabulary = parse_vocabulary(dialog.vocabulary_text())
        finally:
            dialog.deleteLater()

        lines = list(self.raw_lines)
        if selected_only and spans:
            rows = [row for start, end in spans for row in range(start, min(end, len(lines)))]
            scope_text = f"{len(self._selected_toc_items())} 個章節"
        else:
            rows = range(len(lines))
            scope_text = "全文"
        generated = "\n".join(self._convert_lines_with_progress(lines, rows, mode, keep_words, vocabulary))

        # 自動辨識的作品／標題快取記著舊文字，重建目錄時會把舊名稱寫回去
        # ：轉換範圍內的一起轉，建新的 dict，不改到復原快照共用的。
        converted_rows = set(rows)
        self.auto_titles = {
            row: ({**record, "title": convert_with_word_lists(record.get("title", ""), mode, keep_words, vocabulary)}
                  if row in converted_rows else dict(record))
            for row, record in self.auto_titles.items()
        }

        # 逐字轉換，行數與行的順序都沒變，所以 raw_lines 直接換掉就好，
        # 不用跑 _adopt_lines 的 difflib 比對，章節狀態的行號也原封不動。
        self.raw_lines = generated.split("\n")
        self._set_editor_text(generated, lambda row: row)
        self._mark_synced(generated)
        # 章節標題的字變了，目錄要重新辨識。
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已將{scope_text}做「{mode}」轉換，可以按 Ctrl+Z 復原")

    def _convert_lines_with_progress(self, lines: list, rows, mode: str, keep_words=(),
                                     vocabulary=()) -> list:
        """逐塊做繁簡轉換，中間更新狀態列。

        OpenCC 是逐字轉換，2.6 MB 實測要 6 秒、5.8 MB 的檔案十幾秒；
        一口氣轉完的話視窗整個沒反應，看起來就像當掉（卡死監看也會記一筆）。
        分塊之間讓 Qt 重畫一次，並在轉換期間停用視窗，避免中途又按了別的功能。

        OpenCC 不會增減換行，所以把一塊接起來轉、再切回去，行數仍然 1:1。
        """
        rows = list(rows)
        total = len(rows)
        chunk_size = 2000
        self.setEnabled(False)
        self._long_task_running = True
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for index in range(0, total, chunk_size):
                block = rows[index:index + chunk_size]
                converted = convert_with_word_lists("\n".join(lines[row] for row in block), mode,
                                                    keep_words, vocabulary)
                for row, text in zip(block, converted.split("\n")):
                    lines[row] = text
                if total > chunk_size:
                    self._show_status(
                        i18n.T("繁簡轉換中…") + f" {min(index + chunk_size, total)} / {total}",
                        translated=True)
                    QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()
            self._long_task_running = False
            self.setEnabled(True)
        return lines

    def _jump_to_document_end(self, end: bool):
        """目錄右下角的到最前面／到最後面：目錄捲到頭並選到第一個／最後一個項目，
        本文的游標也到開頭／最後一個字。"""
        target = self.tree.topLevelItem(0)
        if end and target is not None:
            target = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
            while target.childCount() and target.isExpanded():
                target = target.child(target.childCount() - 1)
        if target is not None:
            self.tree.setCurrentItem(target)
        if end:
            self.tree.scrollToBottom()
        else:
            self.tree.scrollToTop()
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End if end else QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        self.editor.setFocus()

    # ------------------------------------------------------------------ 接續更新章節

    @action
    def import_chapter_update(self, path: str | None = None):
        """把新下載的同一本書跟本文比對，勾選的章節加入本文（core/chapter_update.py）。
        path 是 None 時先開選檔視窗；視窗裡按「換一個檔案」也回到選檔。"""
        if not self._has_document():
            return
        while True:
            if path is None:
                with native_dialog():
                    path, _ = QFileDialog.getOpenFileName(
                        self, i18n.T("接續更新章節"), self._remembered_dir("last_open_dir"),
                        i18n.T(OPEN_FILE_FILTER))
                if not path:
                    return
                self._ui_state["last_open_dir"] = os.path.dirname(path)
            loaded = self._load_update_source(path)
            if loaded is None:
                return
            new_lines, new_entries, plan, convert_mode = loaded
            convert_label = None
            if convert_mode:
                convert_label = "加入時轉成繁體" if convert_mode != "繁體轉簡體" else "加入時轉成簡體"
            dialog = ChapterUpdateDialog(os.path.basename(path), plan, new_lines,
                                         [entry["row"] for entry in new_entries], convert_label, self)
            dialog.exec()
            if dialog.choice == OTHER_FILE:
                path = None
                continue
            if dialog.choice is None:
                return
            mode = convert_mode if dialog.convert_enabled() else None
            if dialog.choice == APPEND_ALL:
                self._append_whole_update(new_lines, mode)
            else:
                self._apply_chapter_update(new_lines, new_entries, plan, dialog.checked(), mode)
            return

    def _load_update_source(self, path: str):
        """讀新檔、辨識章節（照目前的辨識設定）、跟本文比對：回傳（新檔的行, 目錄項目, 比對結果, 繁簡轉換）。"""
        content, _damaged, _encoding = self._read_document(
            path, None if is_docx(path) or is_epub(path) else smart_detect_encoding(path))
        if content is None:
            return None
        content, _boms, _zero_width = strip_invisible_chars(content)
        new_lines = content.split("\n")
        self._sync_raw_lines()
        self._ensure_toc_current()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            body_entries = toc_entries(self.raw_lines, self.chapter_raw_map, self.chapter_records)
            # 新檔沒有本文的人工調整（強制層級、自動標題記錄），其他辨識設定一樣
            ctx = dataclasses.replace(self._build_context(), raw_lines=new_lines, auto_titles={},
                                      force_lv1_chapters=set(), force_lv2_chapters=set())
            result = build_document_structure(ctx, apply_format=False, write_text=False)
            new_entries = toc_entries(new_lines, result.chapter_raw_map, result.chapter_records)
            plan = plan_update(self.raw_lines, body_entries, new_lines, new_entries)
            body_script, new_script = dominant_script(self.raw_lines), dominant_script(new_lines)
        finally:
            QApplication.restoreOverrideCursor()
        convert_mode = None
        if body_script == "trad" and new_script == "simp":
            # 照繁簡轉換視窗上次選的方式（有沒有換成台灣用語）
            saved = self._ui_state.get("script_mode")
            convert_mode = saved if saved in ("簡體轉繁體", "簡體轉繁體（台灣用語）") else "簡體轉繁體"
        elif body_script == "simp" and new_script == "trad":
            convert_mode = "繁體轉簡體"
        return new_lines, new_entries, plan, convert_mode

    def _apply_chapter_update(self, new_lines, new_entries, plan, checked, convert_mode):
        body_entries = toc_entries(self.raw_lines, self.chapter_raw_map, self.chapter_records)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            lines, added_rows = apply_update(self.raw_lines, body_entries, new_lines, new_entries, plan, checked,
                                             convert_mode)
        finally:
            QApplication.restoreOverrideCursor()
        self._replace_text_from_tool(lines)
        # 目錄選好加入的章：直接按右鍵「套用格式到這幾章」
        items = [item for item, row in self.chapter_raw_map.items() if row in set(added_rows)]
        self.tree.clearSelection()
        for item in items:
            item.setSelected(True)
        if added_rows:
            self._jump_to_line(added_rows[0] + 1)
            if items:
                self.tree.scrollToItem(min(items, key=lambda item: self.chapter_raw_map[item]))
        counts = {status: sum(1 for item in plan if item["index"] in checked and item["status"] == status)
                  for status in (MISSING, NEW, LONGER)}
        parts = []
        if counts[MISSING]:
            parts.append(f"補上本文缺少的 {counts[MISSING]} 章")
        if counts[NEW]:
            parts.append(f"加入 {counts[NEW]} 章新章節")
        if counts[LONGER]:
            parts.append(f"換掉 {counts[LONGER]} 章的正文")
        self._show_status("、".join(parts) + "；目錄已選好這幾章，可以按右鍵「套用格式到這幾章」；"
                          "可以按 Ctrl+Z 復原")

    def _append_whole_update(self, new_lines, convert_mode):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            lines, start = append_all(self.raw_lines, new_lines, convert_mode)
        finally:
            QApplication.restoreOverrideCursor()
        self._replace_text_from_tool(lines)
        self._jump_to_line(start + 1)
        self._show_status(f"已把新檔整份接到最後（從第 {start + 1} 行起）；可以按 Ctrl+Z 復原")

    def _jump_to_line(self, line_number: int):
        """跳到某一行並整行反白（檢查清單點選用，1 起算）。"""
        self._highlight_ad_candidate(line_number - 1, line_number - 1)

    @action
    def open_recognition_dialog(self):
        """辨識章節：積木組合、單位與特殊標題、標題長度與章名結尾。非模式：開著時可以從本文複製一行貼上。"""
        self._sync_raw_lines()
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()

        def create():
            dialog = RecognitionDialog(self.user_chapter_rules, lambda: list(self.raw_lines), self,
                                       known_rows=self._handled_title_rows(),
                                       title_tail_allowed=self.title_tail_allowed,
                                       title_tail_custom=self.title_tail_custom,
                                       disabled_words=self.disabled_words, max_title_length=self.max_title_length,
                                       special_levels=self.special_levels)
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            return dialog

        def reload(dialog):
            dialog.reload(self.raw_lines, self._handled_title_rows())

        self._open_tool_dialog("recognition", create, reload, self._on_recognition_dialog_closed)

    def open_suspect_chapters(self, fmt=None):
        """打開辨識章節的「可疑章節」分頁（目錄上方的提示「查看可疑章節」），只看某一種格式。"""
        self.open_recognition_dialog()
        dialog = self._tool_dialogs.get("recognition")
        if dialog is not None:
            dialog.show_candidates(fmt)

    @action
    def _on_recognition_dialog_closed(self, dialog, accepted: bool):
        if not accepted or dialog.result_rules is None:
            return
        added = self._add_suspect_lines(dialog)
        new = (dialog.result_rules, dialog.result_title_tail, dialog.result_title_tail_custom or "",
               dialog.result_disabled_words, dialog.result_max_title_length, dialog.result_special_levels)
        old = (self.user_chapter_rules, self.title_tail_allowed, self.title_tail_custom, self.disabled_words,
               self.max_title_length, self.special_levels)
        (self.user_chapter_rules, self.title_tail_allowed, self.title_tail_custom, self.disabled_words,
         self.max_title_length, self.special_levels) = new
        if new != old:
            # 排版時記下的自動標題會優先採用：辨識的設定改了就照新的設定重新辨識（作品名稱照舊）
            self.auto_titles = {row: record for row, record in self.auto_titles.items()
                                if record.get("kind") == "work"}
        self.rescan_toc()
        if added is not None:
            self._checkpoint_document()
            self._show_status(f"已把 {added} 行加入目錄，並保存辨識章節的設定")
        elif dialog.result_lines is None or dialog._tool_version == self._text_version:
            self._show_status("已保存辨識章節的設定")

    def _add_suspect_lines(self, dialog):
        """可疑章節勾選加入的行：行尾加 [::]、卷級格式設成卷。回傳加了幾行；沒有要加或本文已經改過時回傳 None。"""
        result_lines = dialog.result_lines
        if result_lines is None:
            return None
        if dialog._tool_version != self._text_version:
            # 按下按鈕前本文又改了（理論上切回對話框時就會重算，這裡保險）：
            # 勾選的行號已經對不上，只存設定，不動本文。
            self._show_status("本文在勾選之後改過了，只保存設定；要加入的行請重新勾選")
            return None
        volume_rows = set(dialog.result_volume_rows)
        # 只在行尾加 [::]，行數不變，章節狀態的行號也不用搬。
        added = sum(1 for old, new in zip(self.raw_lines, result_lines) if old != new)
        generated = "\n".join(result_lines)
        self.raw_lines = list(result_lines)
        self._set_editor_text(generated, lambda row: row)
        self._mark_synced(generated)
        # 卷級格式逐行加入時要設成卷；[::] 本身只代表「這一行是標題」。
        self.force_lv1_chapters |= volume_rows
        self.force_lv2_chapters -= volume_rows
        return added
