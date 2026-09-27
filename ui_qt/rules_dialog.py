"""自訂章節規則對話框：管理自己寫的正則規則，並找出本文裡還沒收進目錄的可疑章節。
用積木組出來的寫法、單位與特殊標題、標題長度與章名結尾在「辨識章節」（recognition_dialog.py）。

兩個分頁：

「規則」：自己寫的正則（可以從一行範例產生）。

「本文可疑章節」：看起來像章節、但目前不在目錄裡的行（原本獨立的「檢查未
辨識章節」）。每行都有信心度——同格式前後編號連續、兩章之間有夠多正文就
比較可信；編號連續但中間幾乎沒有正文，通常是正文裡的條列。整種格式都是
章節就「存成規則」；只有其中幾行是章節，就勾那幾行「加入目錄」。

編輯區的互動刻意保持簡單：切換選取列前如果有未儲存的修改，不會自動存成
草稿，必須自己按「儲存變更」，否則切換會直接捨棄。
"""

import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from core.chapter_parse import MAX_TITLE_LENGTH, weak_candidate_to_user_rule
from core.collection import scan_chapter_candidates
from core.persistence import RULES_FILE, _save_json
from core.title_markers import strip_persistent_title_marker
from core import safe_regex
from core.title_blocks import block_rule, template
from core.user_rules import is_risky_pattern, match_user_chapter_rule, preset_rule, rule_from_sample
from . import dialogs, i18n
from .sortable_table import PreviewTable, CONFIDENCE_ORDER, data_index, limit_rows, enable_sorting, make_item, resort, setup_columns
from .recognition_dialog import managed_rule
from .widgets import Divider, dialog_frame, fit_window_to_screen, size_dialog

_LEVEL_LABELS = {1: "卷", 2: "章"}

# 「快速插入」的片段：名稱 → （插入的正則, 說明）。寫給不熟正則的人看，
# 所以說明一律用白話，不解釋語法本身。
_SNIPPETS = [
    ("章號", r"(?P<number>[0-9０-９]{1,8})", "數字章號（001、12…）；缺章檢查與連續編號都靠它"),
    ("中文章號", r"(?P<number>[一二兩两三四五六七八九十百千零〇]{1,8})", "中文數字章號（一、十二…）"),
    ("章名", r"(?P<title>\S.*?)", "章號後面那段標題文字"),
    ("任意文字", ".*", "任何字都可以，長度不限"),
    ("空白", r"\s*", "可有可無的空白"),
    ("行首", "^", "從這一行的開頭開始比對"),
    ("行尾", "$", "比對到這一行結束"),
]

_RULES_TAB, _CANDIDATES_TAB = 0, 1


class _CompactTable(PreviewTable):
    """建議高度就是最小高度（標題列＋兩列）。表格預設建議約 190px，放在會
    捲動的頁面裡時，頁面就照這個高度排，開窗時表格佔掉一大塊。
    視窗拉高時表格照樣會跟著變高（版面的伸展係數不變）。"""

    def sizeHint(self):
        hint = super().sizeHint()
        if self.minimumHeight() > 0:
            hint.setHeight(self.minimumHeight())
        return hint


class RulesDialog(QDialog):
    """建構時吃現有規則清單與目前的本文。

    規則清單裡歸「辨識章節」管的（積木組合、名稱＋篇）不列出來，保存時原樣接在後面。

    按「保存並重掃」（或第二頁的「加入目錄」）之後：
      result_rules：新的規則清單
      result_lines：有逐行加入目錄時，是加上 [::] 之後的整份本文；否則 None
      result_volume_rows：逐行加入的行裡，哪些要當成卷（卷級格式）
    """

    candidateHighlighted = Signal(int, int)  # start_line, end_line（0 起算，單行）

    def __init__(self, rules: list, get_document_lines, parent=None, known_rows=None,
                 max_title_length: int = MAX_TITLE_LENGTH):
        super().__init__(parent)
        self._max_title_length = int(max_title_length)
        self.setWindowTitle("自訂章節規則")
        self._working = [dict(rule) for rule in rules if not managed_rule(rule)]
        self._managed = [dict(rule) for rule in rules if managed_rule(rule)]
        self._selected_index: int | None = None
        self.result_rules: list | None = None
        self.result_lines: list | None = None
        self.result_volume_rows: set = set()
        # 計數、可疑章節、測試都用同一份本文，不會前後不一致；對話框開著時
        # 本文改了，主視窗會呼叫 reload() 換成新的一份。
        self._analyze_document(get_document_lines(), known_rows)

        root, footer = dialog_frame(self, intro="自己寫正則辨識章節；本文可疑章節可以勾選後加入目錄。")
        root.setSpacing(10)

        self.tabs = QTabWidget()
        # 規則頁東西多（常用格式、表格、編輯區）：螢幕不夠高時整頁捲動，
        # 不要讓對話框的最小高度超出螢幕、下半部被切掉。
        self._rules_scroll = QScrollArea()
        self._rules_scroll.setObjectName("panelScroll")
        self._rules_scroll.setWidgetResizable(True)
        self._rules_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._rules_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rules_page = self._build_rules_tab()
        rules_page.setObjectName("panelScrollContent")
        self._rules_scroll.setWidget(rules_page)
        self._fitted = False
        self.tabs.addTab(self._rules_scroll, "規則")
        self.tabs.addTab(self._build_candidates_tab(), f"本文可疑章節（{len(self._candidates)}）")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox()
        cancel_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        commit_button = buttons.addButton("保存並重掃", QDialogButtonBox.ButtonRole.AcceptRole)
        commit_button.setObjectName("primary")
        cancel_button.clicked.connect(self.reject)
        commit_button.clicked.connect(self._commit)
        footer.addWidget(buttons)

        self._refresh_table()
        self._update_buttons()
        self._refresh_candidates()
        self._size_to_content()

    def _analyze_document(self, lines, known_rows):
        self._lines = list(lines)
        self._known_rows = set(known_rows or ())
        self._candidates = scan_chapter_candidates(self._lines, self._known_rows, self._max_title_length)
        self._checked: set[int] = set()
        self._format_counts: dict[str, int] = {}
        for candidate in self._candidates:
            self._format_counts[candidate["format"]] = self._format_counts.get(candidate["format"], 0) + 1

    def reload(self, lines, known_rows=None):
        """對話框開著時本文被改過（使用者直接在本文編輯、刪行…）：換成新的
        一份重算。規則頁正在編輯的內容不動；第二頁勾選的行號可能已經對不上，
        清掉重來。"""
        self._analyze_document(lines, known_rows)
        self._populate_format_combo()
        self.tabs.setTabText(_CANDIDATES_TAB, i18n.T(f"本文可疑章節（{len(self._candidates)}）"))
        self._refresh_candidates()

    def _size_to_content(self):
        """規則表格看得到兩列就夠了，多的用表格自己的捲軸看。開窗高度在第一次
        顯示時照規則頁實際需要的高度算（見 showEvent），不再把多出來的空間都
        塞給表格；要看更多列，把視窗拉高即可（表格會跟著變高）。"""
        self.ensurePolished()
        row_height = self.table.verticalHeader().defaultSectionSize()
        header_height = self.table.horizontalHeader().sizeHint().height()
        for table in (self.table, self.candidate_table):
            table.setMinimumHeight(header_height + row_height * 2 + 4)
        wanted = 760
        parent = self.parentWidget()
        screen = parent.window().screen() if parent is not None else self.screen()
        if screen is not None:
            wanted = min(wanted, int(screen.availableGeometry().width() * 0.95))
        self.setMinimumWidth(max(self.minimumWidth(), wanted))
        size_dialog(self, 920, 600)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._fitted:
            self._fitted = True
            # 等版面排好（寬度確定、自動換行的區塊算出列數）之後再調高度。
            QTimer.singleShot(0, self._fit_height_to_rules_page)

    def _fit_height_to_rules_page(self):
        """把對話框調成規則頁剛好不用捲動的高度；螢幕不夠高就夾回螢幕內，
        剩下的用捲動。調高度後常用格式的列數不會變，但保險起見重算到穩定。"""
        page = self._rules_scroll.widget()
        layout = page.layout()
        for _attempt in range(3):
            self.layout().activate()
            # 用「沒有捲軸」時的寬度算：調好高度後捲軸就會消失，內容會變寬
            # （常用格式可能從兩欄變三欄、需要的高度變少）。
            width = self._rules_scroll.width() - 2 * self._rules_scroll.frameWidth()
            # 用「建議高度」而不是最小高度：捲動區就是照建議高度排內容的。
            needed = layout.heightForWidth(width) if layout.hasHeightForWidth() else page.sizeHint().height()
            extra = needed - self._rules_scroll.viewport().height()
            if not extra:
                break
            self.resize(self.width(), max(self.minimumHeight(), self.height() + extra))
        fit_window_to_screen(self)

    # ------------------------------------------------------------------
    # 版面

    def _build_rules_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(4, 12, 4, 4)
        root.setSpacing(10)

        self.table = _CompactTable(0, 4)
        self.table.setHorizontalHeaderLabels(["啟用", "名稱", "層級", "正則"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.table, {0: "contents", 1: 160, 2: "contents"})
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._load_selected)
        # 刻意不開放點標題列排序：表格的順序就是規則的套用優先順序（由上往下
        # 比對，第一條符合的規則生效），只能用上移／下移調整。
        self.table.horizontalHeader().setSectionsClickable(False)
        root.addWidget(self.table, 1)

        # 表格的操作（對「選取的那條規則」）緊貼在表格下面；編輯區的按鈕放在
        # 編輯區底下。兩組分開，視窗變窄時也不會混成一排再亂換行。
        table_actions = QHBoxLayout()
        table_actions.setSpacing(8)
        self.move_up_button = QPushButton("上移")
        self.move_up_button.clicked.connect(lambda: self._move_rule(-1))
        self.move_down_button = QPushButton("下移")
        self.move_down_button.clicked.connect(lambda: self._move_rule(1))
        self.delete_button = QPushButton("刪除規則")
        self.delete_button.clicked.connect(self._delete_rule)
        for button in (self.move_up_button, self.move_down_button):
            table_actions.addWidget(button)
        table_actions.addStretch(1)
        table_actions.addWidget(self.delete_button)
        root.addLayout(table_actions)
        root.addWidget(Divider())

        editor_grid = QGridLayout()
        editor_grid.setHorizontalSpacing(12)
        editor_grid.setVerticalSpacing(8)
        editor_grid.addWidget(QLabel("名稱"), 0, 0)
        self.name_input = QLineEdit()
        editor_grid.addWidget(self.name_input, 0, 1)
        editor_grid.addWidget(QLabel("層級"), 0, 2)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["卷", "章"])
        i18n.set_combo_value(self.level_combo, "章")
        editor_grid.addWidget(self.level_combo, 0, 3)
        editor_grid.addWidget(QLabel("從範例產生"), 1, 0)
        sample_row = QHBoxLayout()
        sample_row.setSpacing(8)
        self.sample_input = QLineEdit()
        self.sample_input.setPlaceholderText("貼上一行章節標題，例如：正文 001章：初入江湖")
        self.sample_input.returnPressed.connect(self._build_from_sample)
        sample_row.addWidget(self.sample_input, 1)
        sample_button = QPushButton("產生規則")
        sample_button.clicked.connect(self._build_from_sample)
        sample_row.addWidget(sample_button)
        editor_grid.addLayout(sample_row, 1, 1, 1, 3)

        # 「清空欄位」放在正則欄右邊，跟上一列的「產生規則」同寬、上下對齊。
        editor_grid.addWidget(QLabel("正則"), 2, 0)
        pattern_row = QHBoxLayout()
        pattern_row.setSpacing(8)
        self.pattern_input = QLineEdit()
        self.pattern_input.textChanged.connect(lambda _: self._validate_pattern(show_error=False))
        pattern_row.addWidget(self.pattern_input, 1)
        clear_button = QPushButton("清空欄位")
        clear_button.clicked.connect(self._new_rule)
        pattern_row.addWidget(clear_button)
        editor_grid.addLayout(pattern_row, 2, 1, 1, 3)
        side_width = max(sample_button.sizeHint().width(), clear_button.sizeHint().width())
        sample_button.setFixedWidth(side_width)
        clear_button.setFixedWidth(side_width)

        # 快速插入用下拉選單（一排按鈕在窄視窗會換行）；同一列右邊放編輯區的動作。
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.snippet_button = QPushButton("快速插入")
        self.snippet_button.setObjectName("menuButton")
        snippet_menu = QMenu(self.snippet_button)
        for label, snippet, _explanation in _SNIPPETS:
            action = snippet_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, text=snippet: self._insert_snippet(text))
        self.snippet_button.setMenu(snippet_menu)
        action_row.addWidget(self.snippet_button)
        action_row.addStretch(1)
        test_button = QPushButton("測試目前文件")
        test_button.clicked.connect(self._test_current_document)
        action_row.addWidget(test_button)
        self.save_button = QPushButton("儲存變更")
        self.save_button.clicked.connect(lambda: self._save_editor(refresh=True))
        action_row.addWidget(self.save_button)
        add_button = QPushButton("新增規則")
        add_button.setObjectName("primary")
        add_button.clicked.connect(self._add_rule)
        action_row.addWidget(add_button)
        editor_grid.addLayout(action_row, 3, 1, 1, 3)
        editor_grid.setColumnStretch(1, 1)
        root.addLayout(editor_grid)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        root.addWidget(self.result_label)
        return page

    def _build_candidates_tab(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(4, 12, 4, 4)
        root.setSpacing(10)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_row.addWidget(QLabel("格式"))
        self.format_combo = QComboBox()
        self._populate_format_combo()
        i18n.skip(self.format_combo)   # 內容是算出來的，切換繁簡時由 _format_label 重組
        self.format_combo.currentIndexChanged.connect(lambda _index: self._refresh_candidates())
        filter_row.addWidget(self.format_combo, 1)
        for label, slot in (("勾選高信心", self._check_high_confidence), ("全部取消", self._uncheck_all)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            filter_row.addWidget(button)
        root.addLayout(filter_row)

        self.candidate_status = QLabel("")
        self.candidate_status.setObjectName("fileLabel")
        root.addWidget(self.candidate_status)

        self.candidate_table = _CompactTable(0, 4)
        self.candidate_table.setHorizontalHeaderLabels(["加入", "信心", "格式", "內容"])
        self.candidate_table.verticalHeader().setVisible(False)
        self.candidate_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.candidate_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        setup_columns(self.candidate_table, {0: "contents", 1: "contents", 2: "contents"})
        self.candidate_table.itemChanged.connect(self._on_candidate_changed)
        self.candidate_table.itemSelectionChanged.connect(self._on_candidate_selected)
        enable_sorting(self.candidate_table)
        root.addWidget(self.candidate_table, 1)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        # 存的是上方「格式」選的那一種（不是勾選的行），按鈕上直接寫出格式名稱；
        # 選「全部格式」時沒有對象，整顆藏起來。
        self.save_format_button = QPushButton("")
        i18n.skip(self.save_format_button)
        self.save_format_button.clicked.connect(self._save_format_as_rule)
        action_row.addWidget(self.save_format_button)
        action_row.addStretch(1)
        self.add_lines_button = QPushButton("把已勾選的行加入目錄")
        self.add_lines_button.setObjectName("primary")
        self.add_lines_button.setToolTip("只處理勾選的行（行尾加上 [::]），同時保存規則並關閉視窗")
        self.add_lines_button.clicked.connect(self._add_checked_lines)
        action_row.addWidget(self.add_lines_button)
        root.addLayout(action_row)
        return page

    # ------------------------------------------------------------------
    # 常用格式

    # ------------------------------------------------------------------
    # 標題結尾允許字元

    # ------------------------------------------------------------------
    # 辨識格式：左邊分類、右邊開關（章節單位、特殊標題、常用格式、標題結尾、標題長度）

    # ------------------------------------------------------------------
    # 本文可疑章節

    def _populate_format_combo(self):
        """格式清單（重新分析本文後也用這個重建；原本選的格式還在就留著）。"""
        current = self.format_combo.currentData() if self.format_combo.count() else None
        self.format_combo.blockSignals(True)
        self.format_combo.clear()
        self.format_combo.addItem(i18n.T(f"全部格式（{len(self._candidates)} 行）"), None)
        for fmt, count in self._format_counts.items():
            kind = "常用格式" if fmt.startswith("preset:") else "其他格式"
            self.format_combo.addItem(i18n.T(f"{self._format_label(fmt)}（{count} 行）· {kind}"), fmt)
        index = self.format_combo.findData(current) if current is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))
        self.format_combo.blockSignals(False)

    def _format_label(self, fmt: str) -> str:
        return next((candidate["label"] for candidate in self._candidates if candidate["format"] == fmt), fmt)

    def _current_format(self):
        return self.format_combo.currentData()

    def show_candidates(self, fmt=None):
        """切到「本文可疑章節」分頁，只看某一種格式（None＝全部）。"""
        index = self.format_combo.findData(fmt) if fmt is not None else 0
        self.format_combo.setCurrentIndex(max(index, 0))
        self.tabs.setCurrentIndex(_CANDIDATES_TAB)

    def _matching_candidates(self) -> list:
        fmt = self._current_format()
        return [index for index, candidate in enumerate(self._candidates)
                if fmt is None or candidate["format"] == fmt]

    def _visible_candidates(self) -> list:
        """表格列出的候選：太多時只列一部分（信心高的優先）。"""
        return limit_rows(self._matching_candidates(),
                          lambda index: CONFIDENCE_ORDER.get(self._candidates[index]["confidence"], 9))[0]

    def _refresh_candidates(self):
        visible = self._visible_candidates()
        table = self.candidate_table
        table.blockSignals(True)
        table.setRowCount(len(visible))
        for row, index in enumerate(visible):
            candidate = self._candidates[index]
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
        fmt = self._current_format()
        self.save_format_button.setVisible(fmt is not None and fmt != "weak:odd_number")
        if fmt is not None:
            self.save_format_button.setText(
                i18n.T("把「{name}」這種格式存成規則").format(name=i18n.T(self._format_label(fmt))))
        self._update_candidate_status()

    def _update_candidate_status(self):
        visible = self._matching_candidates()
        fmt = self._current_format()
        checked = len(self._checked)
        if not self._candidates:
            text = "目前沒有可疑章節"
        elif fmt is None:
            text = f"共 {len(visible)} 行；已勾選 {checked} 行"
        else:
            # 分頁標題是全部格式的總數；篩選中只看得到其中一種，要講清楚，
            # 不然會以為標題的數字跟清單對不上。
            text = (f"目前只顯示「{self._format_label(fmt)}」{len(visible)} 行，"
                    f"全部共 {len(self._candidates)} 行（格式選「全部格式」可以看全部）；"
                    f"已勾選 {checked} 行")
        shown = len(self._visible_candidates())
        if shown < len(visible):
            text += f"（太多了，只列出 {shown} 行，信心高的優先）"
        i18n.set_text(self.candidate_status, text)
        self.add_lines_button.setEnabled(bool(self._checked))

    def _on_candidate_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        index = data_index(self.candidate_table, item.row())
        if item.checkState() == Qt.CheckState.Checked:
            self._checked.add(index)
        else:
            self._checked.discard(index)
        self._update_candidate_status()

    def _on_candidate_selected(self):
        rows = self.candidate_table.selectionModel().selectedRows()
        if not rows:
            return
        candidate = self._candidates[data_index(self.candidate_table, rows[0].row())]
        self.candidateHighlighted.emit(candidate["index"], candidate["index"])

    def _check_high_confidence(self):
        self._checked |= {index for index in self._visible_candidates()
                          if self._candidates[index]["confidence"] == "高"}
        self._refresh_candidates()

    def _uncheck_all(self):
        self._checked -= set(self._visible_candidates())
        self._refresh_candidates()

    def _save_format_as_rule(self):
        """目前選的那種格式整個存成規則（常用格式就是把它勾起來）。

        跟規則頁的其他修改一樣，按「保存並重掃」才真的寫檔、套用到本文。"""
        fmt = self._current_format()
        if fmt is None:
            dialogs.info(self, "先選一種格式", "請先在上方「格式」選擇要存成規則的格式。")
            return
        if fmt.startswith("preset:"):
            # 常用寫法：加成「辨識章節」的組合（已經有一樣的就打開它）
            preset_id = fmt.split(":", 1)[1]
            if preset_id == "named_volume":
                rule = preset_rule(preset_id)
            else:
                level, blocks = template(preset_id)
                rule = block_rule(blocks, level)
            existing = next((item for item in self._managed if item.get("pattern") == rule["pattern"]), None)
            if existing is None:
                self._managed.append(rule)
            else:
                existing["enabled"] = True
                rule = existing
            i18n.set_text(self.candidate_status, f"已加入組合「{rule['name']}」，按「保存並重掃」後生效")
            return
        if fmt == "weak:odd_number":
            dialogs.info(self, "不能存成規則", "章號不是數字的標題沒辦法做成規則（沒有章號可以排序）；"
                                              "請勾選要加入的行，按「把勾選的行加入目錄」。")
            return
        candidate = next(c for c in self._candidates if c["format"] == fmt)
        rule = weak_candidate_to_user_rule(candidate)
        rule["name"] = candidate["label"]
        if any(existing.get("pattern") == rule["pattern"] for existing in self._working):
            dialogs.info(self, "規則已存在", "相同格式的章節規則已經在清單裡了。")
            return
        self._working.append(rule)
        self._refresh_table()
        matches = len(self._document_matches(rule))
        i18n.set_text(self.candidate_status,
                      f"已加入規則「{rule['name']}」（本文符合 {matches} 行），按「保存並重掃」後生效")

    def _checked_lines_result(self):
        """把勾選的行加上 [::]，回傳（新的整份本文, 要當成卷的行）。"""
        lines = list(self._lines)
        volume_rows = set()
        for index in sorted(self._checked):
            candidate = self._candidates[index]
            row = candidate["index"]
            clean, _marker = strip_persistent_title_marker(lines[row].rstrip())
            lines[row] = clean + "[::]"
            # [::] 只代表「這一行是標題」，層級照格式本身判斷時一律當成章；
            # 卷級格式（例如「卷一 風起」）要另外記下來設成卷。
            if candidate["level"] == 1:
                volume_rows.add(row)
        return lines, volume_rows

    def _add_checked_lines(self):
        if not self._checked:
            dialogs.info(self, "尚未勾選", "請先勾選要加入目錄的行。")
            return
        self.result_lines, self.result_volume_rows = self._checked_lines_result()
        self._commit(ask_about_checked=False)

    # ------------------------------------------------------------------
    # 規則清單

    def _refresh_table(self, select: int | None = None):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._working))
        for row, rule in enumerate(self._working):
            enabled_item = QTableWidgetItem()
            enabled_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            enabled_item.setCheckState(Qt.CheckState.Checked if rule["enabled"] else Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, enabled_item)
            self.table.setItem(row, 1, QTableWidgetItem(rule["name"]))
            self.table.setItem(row, 2, QTableWidgetItem(i18n.T(_LEVEL_LABELS.get(rule["level"], "章"))))
            self.table.setItem(row, 3, QTableWidgetItem(rule["pattern"]))
        self.table.blockSignals(False)
        if select is not None and 0 <= select < self.table.rowCount():
            self.table.selectRow(select)

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != 0:
            return
        self._working[item.row()]["enabled"] = item.checkState() == Qt.CheckState.Checked

    def _load_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        self._selected_index = index
        self._update_buttons()
        rule = self._working[index]
        self.name_input.setText(rule["name"])
        self.pattern_input.setText(rule["pattern"])
        i18n.set_combo_value(self.level_combo, _LEVEL_LABELS.get(rule["level"], "章"))

    def _insert_snippet(self, snippet: str):
        """把片段插在正則欄游標的位置，插完游標停在片段後面。"""
        self.pattern_input.insert(snippet)
        self.pattern_input.setFocus()

    def _build_from_sample(self):
        """貼一行真正的章節標題，直接推出規則，不用自己寫正則。"""
        sample = self.sample_input.text().strip()
        if not sample:
            return
        rule = rule_from_sample(sample)
        if rule is None:
            i18n.set_text(self.result_label,
                          "這一行找不到章號（數字或中文數字），無法自動產生規則")
            return
        self._selected_index = None
        self.table.clearSelection()
        self.name_input.setText(rule["name"])
        self.pattern_input.setText(rule["pattern"])
        i18n.set_combo_value(self.level_combo, _LEVEL_LABELS.get(rule["level"], "章"))
        self._update_buttons()
        matches = self._document_matches(rule)
        preview = "\n".join(matches[:3])
        i18n.set_text(self.result_label,
                      f"已依範例產生規則，目前本文符合 {len(matches)} 行\n{preview}\n"
                      "確認沒問題就按「新增規則」")

    def _document_matches(self, rule) -> list:
        matches = []
        for number, line in enumerate(self._lines, 1):
            clean, marker = strip_persistent_title_marker(line.strip())
            if marker != "exclude" and match_user_chapter_rule(clean, [rule]):
                matches.append(f"第 {number} 行：{clean}")
        return matches

    def _validate_pattern(self, show_error: bool):
        pattern = self.pattern_input.text().strip()
        if not pattern:
            i18n.set_text(self.result_label, "正則錯誤：不可空白")
            return None
        try:
            safe_regex.compile(pattern, re.IGNORECASE)
        except safe_regex.errors as error:
            i18n.set_text(self.result_label, f"正則錯誤：{error}")
            if show_error:
                dialogs.error(self, "正則錯誤", str(error))
            return None
        # 規則會在每次重建目錄時對「每一行」比對一次；巢狀量詞在長段落上會
        # 指數成長，寫錯一次就是之後每次掃描都卡住。不硬擋，但要講清楚。
        if is_risky_pattern(pattern):
            warning = ("⚠ 這個正則有巢狀量詞（例如 (a+)+、(.*)*），在長段落上可能跑很久；"
                       "它會在每次重建目錄時對每一行執行，可能讓程式卡住")
            i18n.set_text(self.result_label, warning)
            if show_error and not dialogs.confirm(self, "正則可能很慢", warning + "。\n\n仍要使用嗎？"):
                return None
            if not show_error:
                return pattern
        i18n.set_text(self.result_label, "正則有效，按「測試目前文件」可以看本文有幾行符合")
        return pattern

    def _add_rule(self):
        """把目前編輯區的內容新增成一條規則（要從頭填寫改用「清空欄位」）。"""
        self._selected_index = None
        self.table.clearSelection()
        if self._save_editor(refresh=True):
            name = self._working[self._selected_index]["name"]
            i18n.set_text(self.result_label, f"已新增規則「{name}」，按「保存並重掃」才會套用到本文")

    def _default_rule_name(self) -> str:
        existing = {rule["name"] for rule in self._working}
        number = len(self._working) + 1
        while f"自訂規則 {number}" in existing:
            number += 1
        return f"自訂規則 {number}"

    def _save_editor(self, refresh: bool) -> bool:
        name = self.name_input.text().strip()
        pattern = self._validate_pattern(show_error=True)
        if pattern is None:
            return False
        if not name:
            # 沒填名稱就自動給一個，不要因為這樣擋住使用者剛寫好的正則。
            name = self._default_rule_name()
            self.name_input.setText(name)
        index = self._selected_index
        enabled = self._working[index].get("enabled", True) if index is not None else True
        rule = {"name": name, "pattern": pattern,
                "level": 1 if i18n.combo_value(self.level_combo) == "卷" else 2, "enabled": enabled}
        if index is None:
            self._working.append(rule)
            index = len(self._working) - 1
        else:
            self._working[index] = rule
        self._selected_index = index
        self._refresh_table(select=index)
        self._update_buttons()
        return True

    def _new_rule(self):
        self._selected_index = None
        self.table.clearSelection()
        self.name_input.clear()
        self.pattern_input.clear()
        i18n.set_combo_value(self.level_combo, "章")
        i18n.set_text(self.result_label, "")
        self._update_buttons()

    def _update_buttons(self):
        """沒有選取任何一列時，「儲存變更」與表格操作都沒有對象。"""
        index = self._selected_index
        selected = index is not None
        self.save_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.move_up_button.setEnabled(selected and index > 0)
        self.move_down_button.setEnabled(selected and index < len(self._working) - 1)
        self.save_button.setToolTip("" if selected else i18n.T("請先在上方清單選取要修改的規則；新的規則請按「新增規則」"))

    def _delete_rule(self):
        index = self._selected_index
        if index is None:
            return
        del self._working[index]
        self._selected_index = None
        self._new_rule()
        self._refresh_table()

    def _move_rule(self, offset: int):
        index = self._selected_index
        if index is None:
            return
        target = index + offset
        if not 0 <= target < len(self._working):
            return
        self._working[index], self._working[target] = self._working[target], self._working[index]
        self._selected_index = target
        self._refresh_table(select=target)
        self._update_buttons()

    def _test_current_document(self):
        pattern = self._validate_pattern(show_error=True)
        if pattern is None:
            return
        probe = {"name": "測試", "pattern": pattern, "enabled": True,
                 "level": 1 if i18n.combo_value(self.level_combo) == "卷" else 2}
        matches = self._document_matches(probe)
        preview = "\n".join(matches[:3])
        i18n.set_text(self.result_label, f"目前本文符合 {len(matches)} 行\n{preview}")

    # ------------------------------------------------------------------

    def _commit(self, ask_about_checked: bool = True):
        # 在第二頁勾了行卻直接按「保存並重掃」：多半是想一起加入，先問清楚，
        # 不要默默丟掉，也不要默默改本文。
        if ask_about_checked and self._checked and self.result_lines is None:
            box = QMessageBox(QMessageBox.Icon.Question, i18n.T("一起加入目錄？"),
                              i18n.T(f"你在「本文可疑章節」勾了 {len(self._checked)} 行，要一起加入目錄嗎？"),
                              parent=self)
            add_button = box.addButton(i18n.T("一起加入"), QMessageBox.ButtonRole.AcceptRole)
            rules_only = box.addButton(i18n.T("只保存規則"), QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(i18n.T("返回"), QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(add_button)
            box.exec()
            clicked = box.clickedButton()
            if clicked is add_button:
                self.result_lines, self.result_volume_rows = self._checked_lines_result()
            elif clicked is not rules_only:
                return
        rules = [dict(rule) for rule in self._working] + [dict(rule) for rule in self._managed]
        if not _save_json(RULES_FILE, rules):
            dialogs.error(self, "無法保存", "章節規則無法寫入設定檔。")
            self.result_lines, self.result_volume_rows = None, set()
            return
        self.result_rules = rules
        self.accept()
