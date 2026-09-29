"""「辨識章節」視窗的「可疑章節」分頁：看起來像章節、但目前不在目錄裡的行。

每行都有信心度——同格式前後編號連續、兩章之間有夠多正文就比較可信；編號連續但中間幾乎沒有正文，
通常是正文裡的條列。整種格式都是章節就「存成規則」（常用寫法加成組合，其他寫法加成自己寫的規則）；
只有其中幾行是章節，就勾那幾行「加入已勾選項目」（行尾加上 [::]）。

存規則、加入目錄都交給所在的視窗（RecognitionDialog）：save_format(fmt) 回傳一句結果，
add_lines() 保存全部設定並關閉視窗。
"""

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget

from core.collection import scan_chapter_candidates
from core.title_markers import strip_persistent_title_marker
from . import dialogs, i18n
from .sortable_table import (
    CONFIDENCE_ORDER, HeaderCheckBox, PreviewTable, confidence_menu_button, data_index, enable_sorting, limit_rows,
    make_item, resort, setup_columns,
)
from .theme import active_tokens
from .widgets import ContextPreview


class SuspectsPage(QWidget):
    candidateHighlighted = Signal(int, int)  # start_line, end_line（0 起算，單行）
    countChanged = Signal(int)               # 可疑章節的行數（分頁標題寫出來）

    def __init__(self, lines, known_rows, max_title_length: int, save_format, add_lines, parent=None):
        super().__init__(parent)
        self._max_title_length = int(max_title_length)
        self._save_format = save_format
        self._add_lines = add_lines
        # 掃描本文（大檔要零點幾秒）等第一次看這一頁才做：打開辨識章節多半是改組合，不一定會看可疑章節
        self._pending = (list(lines), set(known_rows or ()))
        self.candidates: list = []
        self._checked: set[int] = set()
        self._format_counts: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 12, 4, 4)
        root.setSpacing(10)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(QLabel("格式"))
        self.format_combo = QComboBox()
        self._populate_format_combo()
        i18n.skip(self.format_combo)   # 內容是算出來的，切換繁簡時由 _format_label 重組
        self.format_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        filter_row.addWidget(self.format_combo, 1)
        # 勾選方式跟掃描視窗同一組（表格有「信心」欄的都一樣）：依信心勾選＋表格左上角的總勾選框
        filter_row.addWidget(confidence_menu_button(self._check_confidence))
        root.addLayout(filter_row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("fileLabel")
        root.addWidget(self.status_label)

        self.table = PreviewTable(0, 4)
        self.table.setHorizontalHeaderLabels(["加入", "信心", "格式", "內容"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.header_check = HeaderCheckBox(
            self.table, lambda: (len(self._checked & set(self._visible())), len(self._visible())),
            self._set_all_checked)
        setup_columns(self.table, {0: "contents", 1: "contents", 2: "contents"})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._on_selected)
        enable_sorting(self.table)
        # 選到一列：下面顯示那一行加上前後文，比較好判斷是不是標題（跟掃描視窗一樣）
        self.preview = ContextPreview()
        root.addWidget(self.preview.stacked_under(self.table), 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        # 存的是上方「格式」選的那一種（不是勾選的行），按鈕上直接寫出格式名稱；
        # 選「全部格式」時沒有對象，整顆藏起來。
        self.save_format_button = QPushButton("")
        i18n.skip(self.save_format_button)
        self.save_format_button.clicked.connect(self._on_save_format)
        action_row.addWidget(self.save_format_button)
        action_row.addStretch(1)
        self.add_lines_button = QPushButton("加入已勾選項目")
        self.add_lines_button.setObjectName("primary")
        self.add_lines_button.clicked.connect(self._on_add_lines)
        action_row.addWidget(self.add_lines_button)
        root.addLayout(action_row)
        self.status_label.setText("")

    def ensure_scanned(self):
        """還沒掃過就現在掃（第一次顯示、要切到某種格式、要勾選的結果時）。"""
        if self._pending is None:
            return
        lines, known_rows = self._pending
        self._pending = None
        self._analyze(lines, known_rows)
        self._populate_format_combo()
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure_scanned()

    # ------------------------------------------------------------------ 本文

    def _analyze(self, lines, known_rows):
        self._lines = list(lines)
        self._known_rows = set(known_rows or ())
        self.candidates = scan_chapter_candidates(self._lines, self._known_rows, self._max_title_length)
        self._checked: set[int] = set()
        self._format_counts: dict[str, int] = {}
        for candidate in self.candidates:
            self._format_counts[candidate["format"]] = self._format_counts.get(candidate["format"], 0) + 1

    def reload(self, lines, known_rows=None):
        """視窗開著時本文被改過：換成新的一份重算；勾選的行號可能已經對不上，清掉重來。還沒看過這一頁時只記下來。"""
        self._pending = (list(lines), set(known_rows or ()))
        self._checked = set()
        if self.isVisible():
            self.ensure_scanned()

    def checked_count(self) -> int:
        return len(self._checked)

    def checked_lines_result(self):
        """把勾選的行加上 [::]，回傳（新的整份本文, 要當成卷的行）。"""
        lines = list(self._lines)
        volume_rows = set()
        for index in sorted(self._checked):
            candidate = self.candidates[index]
            row = candidate["index"]
            clean, _marker = strip_persistent_title_marker(lines[row].rstrip())
            lines[row] = clean + "[::]"
            # [::] 只代表「這一行是標題」，層級照格式本身判斷時一律當成章；
            # 卷級格式（例如「卷一 風起」）要另外記下來設成卷。
            if candidate["level"] == 1:
                volume_rows.add(row)
        return lines, volume_rows

    # ------------------------------------------------------------------ 格式篩選

    def _populate_format_combo(self):
        """格式清單（重新分析本文後也用這個重建；原本選的格式還在就留著）。"""
        current = self.format_combo.currentData() if self.format_combo.count() else None
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self.format_combo.addItem(i18n.T(f"全部格式（{len(self.candidates)} 行）"), None)
        for fmt, count in self._format_counts.items():
            kind = "常用格式" if fmt.startswith("preset:") else "其他格式"
            self.format_combo.addItem(i18n.T(f"{self.format_label(fmt)}（{count} 行）· {kind}"), fmt)
        index = self.format_combo.findData(current) if current is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))
        self.format_combo.blockSignals(False)

    def format_label(self, fmt: str) -> str:
        return next((candidate["label"] for candidate in self.candidates if candidate["format"] == fmt), fmt)

    def current_format(self):
        return self.format_combo.currentData()

    def show_format(self, fmt=None):
        """只看某一種格式（None＝全部）。"""
        self.ensure_scanned()
        index = self.format_combo.findData(fmt) if fmt is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))

    def _matching(self) -> list:
        fmt = self.current_format()
        return [index for index, candidate in enumerate(self.candidates) if fmt is None or candidate["format"] == fmt]

    def _visible(self) -> list:
        """表格列出的候選：太多時只列一部分（信心高的優先）。"""
        return limit_rows(self._matching(),
                          lambda index: CONFIDENCE_ORDER.get(self.candidates[index]["confidence"], 9))[0]

    # ------------------------------------------------------------------ 表格

    def refresh(self):
        visible = self._visible()
        table = self.table
        table.blockSignals(True)
        table.setRowCount(len(visible))
        for row, index in enumerate(visible):
            candidate = self.candidates[index]
            check_item = make_item("", index)
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check_item.setCheckState(Qt.CheckState.Checked if index in self._checked else Qt.CheckState.Unchecked)
            table.setItem(row, 0, check_item)
            table.setItem(row, 1, make_item(candidate["confidence"], index,
                                            CONFIDENCE_ORDER.get(candidate["confidence"], 9)))
            level_note = "（卷）" if candidate["level"] == 1 else ""
            table.setItem(row, 2, make_item(i18n.T(candidate["label"]) + level_note, index))
            preview = re.sub(r"\s+", " ", candidate["text"]).strip()
            if len(preview) > 160:
                preview = preview[:157] + "…"
            table.setItem(row, 3, make_item(preview, index, candidate["index"]))
        table.blockSignals(False)
        resort(table)
        fmt = self.current_format()
        self.save_format_button.setVisible(fmt is not None and fmt != "weak:odd_number")
        if fmt is not None:
            self.save_format_button.setText(
                i18n.T("把「{name}」這種格式存成規則").format(name=i18n.T(self.format_label(fmt))))
        self._update_status()
        self.countChanged.emit(len(self.candidates))

    def _update_status(self, text: str | None = None):
        matching = self._matching()
        fmt = self.current_format()
        checked = len(self._checked)
        if text is not None:
            pass
        elif not self.candidates:
            text = "目前沒有可疑章節"
        elif fmt is None:
            text = f"共 {len(matching)} 行；已勾選 {checked} 行"
        else:
            # 分頁標題是全部格式的總數；篩選中只看得到其中一種，要講清楚，
            # 不然會以為標題的數字跟清單對不上。
            text = (f"目前只顯示「{self.format_label(fmt)}」{len(matching)} 行，"
                    f"全部共 {len(self.candidates)} 行（格式選「全部格式」可以看全部）；"
                    f"已勾選 {checked} 行")
        shown = len(self._visible())
        if shown < len(matching):
            text += f"（太多了，只列出 {shown} 行，信心高的優先）"
        i18n.set_text(self.status_label, text)
        self.add_lines_button.setEnabled(bool(self._checked))
        self.header_check.refresh()

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        index = data_index(self.table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_status()

    def _on_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.preview.hide()
            return
        candidate = self.candidates[data_index(self.table, rows[0].row())]
        self.preview.show_rows(self._lines, candidate["index"], candidate["index"], active_tokens().accent)
        self.candidateHighlighted.emit(candidate["index"], candidate["index"])

    def _check_confidence(self, levels: set):
        """依信心勾選：列出來的候選只勾那幾種信心的（其餘取消）；其他格式的勾選不動。"""
        visible = set(self._visible())
        self._checked = (self._checked - visible) | {index for index in visible
                                                     if self.candidates[index]["confidence"] in levels}
        self.refresh()

    def _set_all_checked(self, checked: bool):
        visible = set(self._visible())
        self._checked = self._checked | visible if checked else self._checked - visible
        self.refresh()

    # ------------------------------------------------------------------ 動作

    def _on_save_format(self):
        fmt = self.current_format()
        if fmt is None:
            return
        if fmt == "weak:odd_number":
            dialogs.info(self, "不能存成規則", "章號不是數字的標題沒辦法做成規則（沒有章號可以排序）；"
                                              "請勾選要加入的行，按「加入已勾選項目」。")
            return
        candidate = next(c for c in self.candidates if c["format"] == fmt)
        message = self._save_format(fmt, candidate)
        if message:
            self._update_status(message)

    def _on_add_lines(self):
        if not self._checked:
            dialogs.info(self, "尚未勾選", "請先勾選要加入目錄的行。")
            return
        self._add_lines()
