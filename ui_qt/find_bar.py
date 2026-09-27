"""尋找／取代面板：跟排版設定、章節管理、內容檢查共用左側卡片。

幾個刻意的設計：

* 結果清單自己畫、只畫看得到的那幾列，並限制最多保留幾筆：每一筆一個元件的話，
  上萬筆命中會讓程式卡死。
* 預覽一律截斷成一行、關閉橫向捲軸：預覽只是用來確認「是不是這一筆」，
  拖著橫向捲軸看完整行並不實用，而且捲動時會重畫整份清單。
* 正則模式不邊打邊搜尋，要按 Enter 或「搜尋」。使用者寫的正則可能花上
  好幾秒（例如 (a+)+ 這種巢狀量詞），每打一個字就跑一次等於邊打邊當。
* 取代／取代全部刻意用文字按鈕而不是圖示——這兩個動作的圖示很容易長得
  差不多，使用者分不出哪個是「這一筆」哪個是「全部」。
"""

import re
import time

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from core import safe_regex
from core.user_rules import RISKY_REGEX as _RISKY_REGEX
from . import i18n
from .widgets import Divider, IconButton, make_card_header

# 預覽在命中的字前面只留一小段前文：留太多會把命中的字擠出可視範圍。
_PREVIEW_CONTEXT = 10
# 結果清單一次最多列幾筆（一「頁」）。超過時在最後一筆按「下一個」會從那裡
# 接著往下找下一頁，不會跳回第一筆；「全部取代」不受這個上限影響（它直接在
# 整份文字上取代，不靠這份清單）。
MAX_MATCHES = 5000
# 在第一筆按「上一個」要繞到最後一筆時，最多往後載入幾頁（避免寫壞的正則
# 在超大檔案上一路跑下去）。
MAX_WRAP_PAGES = 200
# 搜尋最久跑多久（秒）。純文字搜尋不會碰到，正則寫得不好才會。
SEARCH_DEADLINE = 1.5
_PREVIEW_ROLE = int(Qt.ItemDataRole.UserRole) + 1

# 常用正則：名稱 → （正則, 說明）。
SEARCH_PRESETS = {
    "第…章（最短）": (
        r"(第.*?章)",
        "找出從「第」開始，到同一行最近一個「章」結束的內容。",
    ),
    "阿拉伯數字章號": (
        r"(第\d+章)",
        "找出第1章、第12章等阿拉伯數字章號；不包含中文數字。",
    ),
    "章號＋同行標題": (
        r"(第.*?章\s*.*)",
        "找出第○章及後方文字；其中 \\s* 也可能涵蓋換行，使用前請留意結果範圍。",
    ),
    "章號前文字＋章號": (
        r"(.*)(第.*?章)",
        "找出章號前文字及第○章；前段為貪婪比對，同一行有多個章號時通常取最後一個。",
    ),
    "a 到 b（最短）": (
        r"a.*?b",
        "找出從 a 開始，到同一行最近一個 b 結束的內容；可自行替換 a、b。",
    ),
}
_PRESET_PLACEHOLDER = "請選擇…"
_MARKER_DISPLAY = re.compile(r"[ \t　]*\[::[XxWwTt]?\]")


class _ResultDelegate(QStyledItemDelegate):
    """自己畫結果列：一行、超出寬度就截斷，命中的字加底色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.match_color = QColor("#FFE9A8")
        self.text_color = QColor("#243044")

    def sizeHint(self, option, index):
        metrics = QFontMetrics(option.font)
        return QSize(0, metrics.height() + 12)

    def paint(self, painter, option, index):
        """命中的字一定要看得到：前文從左邊截斷、最多佔三分之一寬，剩下的
        寬度留給命中的字與後文。整行只畫一行，不換行也不橫向捲動。"""
        painter.save()
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)
        before, matched, after = index.data(_PREVIEW_ROLE) or ("", index.data() or "", "")
        metrics = QFontMetrics(option.font)
        rect = option.rect.adjusted(8, 0, -8, 0)
        painter.setFont(option.font)

        before_width = min(metrics.horizontalAdvance(before), int(rect.width() * 0.34))
        before_text = metrics.elidedText(before, Qt.TextElideMode.ElideLeft, before_width) if before else ""
        offset = metrics.horizontalAdvance(before_text)
        rest_text = metrics.elidedText(matched + after, Qt.TextElideMode.ElideRight,
                                       max(0, rect.width() - offset))

        match_width = min(metrics.horizontalAdvance(matched), max(0, rect.width() - offset))
        if match_width > 0:
            painter.fillRect(rect.left() + offset, rect.top() + 3, match_width, rect.height() - 6,
                             self.match_color)
        painter.setPen(self.text_color)
        flags = int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        if before_text:
            painter.drawText(rect, flags, before_text)
        painter.drawText(rect.adjusted(offset, 0, 0, 0), flags, rest_text)
        painter.restore()


class FindBar(QWidget):
    """不直接碰文件內容——每次搜尋都在呼叫端提供的純文字上重新比對，
    取代／取代全部才透過回呼請 MainWindow 動手修改編輯器內容。"""

    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("findPanel")

        self._matches: list[re.Match] = []
        self._current = -1
        # _truncated：這一頁之後還有命中（清單只列了前 MAX_MATCHES 筆）。
        self._truncated = False
        self._timed_out = False
        # 每一頁從正文的哪個位置開始找、前面已經有幾筆：編號才能接著算
        # （第 2 頁的第一筆是 5001），往回翻頁時也知道要從哪裡重找。
        self._page_starts: list[int] = [0]
        self._page_skips: list = [None]      # 每一頁開頭要略過的那一筆（見 _after）
        self._page_bases: list[int] = [0]
        # 搜尋結果記的是字元位置，正文一改就全部失效：記下當時的正文版本，
        # 取代前再比對一次，才不會把「改之前算出來的位置」套到新的正文上。
        self._source_version: object = None
        # 目前這批結果是用哪個搜尋字串、哪種模式找出來的：字串改了但計時器還沒
        # 重新搜尋的那一小段時間，按取代不能拿舊結果去改。
        self._source_query: object = None
        self._research_timer = QTimer(self)
        self._research_timer.setSingleShot(True)
        self._research_timer.setInterval(250)
        self._research_timer.timeout.connect(self._run_search)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, header_layout = make_card_header("尋找／取代")
        self.regex_button = IconButton("regex", "正則表達式（開啟後按 Enter 或「搜尋」才執行）", size=16)
        self.regex_button.setCheckable(True)
        self.regex_button.toggled.connect(self._on_regex_toggled)
        header_layout.addWidget(self.regex_button)
        self.close_button = IconButton("panel-left-close", "收起尋找／取代（Esc）", size=16)
        self.close_button.clicked.connect(self.closed.emit)
        header_layout.addWidget(self.close_button)
        outer.addWidget(header)

        root = QVBoxLayout()
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)
        outer.addLayout(root, 1)

        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("尋找…")
        self.find_input.textChanged.connect(self._on_text_changed)
        self.find_input.returnPressed.connect(self._on_return_pressed)
        root.addWidget(self.find_input)

        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        self.match_label = QLabel("0/0")
        self.match_label.setObjectName("fileLabel")
        i18n.skip(self.match_label)
        nav_row.addWidget(self.match_label)
        nav_row.addStretch(1)
        self.search_button = QPushButton("搜尋")
        self.search_button.clicked.connect(self._run_search)
        nav_row.addWidget(self.search_button)
        self.prev_button = IconButton("chevron-left", "上一個（Shift+Enter）", size=16)
        self.prev_button.clicked.connect(self.find_previous)
        self.next_button = IconButton("chevron-right", "下一個（Enter）", size=16)
        self.next_button.clicked.connect(self.find_next)
        nav_row.addWidget(self.prev_button)
        nav_row.addWidget(self.next_button)
        root.addLayout(nav_row)

        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("取代為…")
        self.replace_input.setToolTip("正則模式可以用 \\1、\\2 代入搜尋時擷取的群組")
        root.addWidget(self.replace_input)

        replace_row = QHBoxLayout()
        replace_row.setSpacing(8)
        self.replace_button = QPushButton("取代")
        self.replace_button.clicked.connect(self._on_replace_clicked)
        self.replace_all_button = QPushButton("全部取代")
        self.replace_all_button.clicked.connect(self._on_replace_all_clicked)
        replace_row.addWidget(self.replace_button)
        replace_row.addWidget(self.replace_all_button)
        root.addLayout(replace_row)

        root.addWidget(Divider())

        preset_label = QLabel("常用正則")
        preset_label.setObjectName("fileLabel")
        root.addWidget(preset_label)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem(_PRESET_PLACEHOLDER)
        # 說明放在滑鼠停在選項上時的提示。
        for name, (pattern, explanation) in SEARCH_PRESETS.items():
            self.preset_combo.addItem(name)
            self.preset_combo.setItemData(self.preset_combo.count() - 1, f"{pattern}\n{explanation}",
                                          Qt.ItemDataRole.ToolTipRole)
        self.preset_combo.activated.connect(self._apply_preset)
        root.addWidget(self.preset_combo)

        self.results_list = QListWidget()
        self.results_list.setObjectName("findResults")
        i18n.skip(self.results_list)   # 搜尋結果是本文內容，不轉換
        self._delegate = _ResultDelegate(self.results_list)
        self.results_list.setItemDelegate(self._delegate)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results_list.setUniformItemSizes(True)
        self.results_list.setWordWrap(False)
        self.results_list.itemClicked.connect(self._on_result_clicked)
        root.addWidget(self.results_list, 1)

        self.hint_label = QLabel("")
        self.hint_label.setObjectName("fileLabel")
        self.hint_label.setWordWrap(True)
        self.hint_label.hide()
        root.addWidget(self.hint_label)

        self._icon_buttons = [self.prev_button, self.next_button, self.close_button]
        self._tokens = None
        self._get_text = lambda: ""
        self._on_select = lambda start, end: None
        self._on_replace_one = lambda start, end, new_text: None
        self._on_replace_all_text = lambda new_text, count: None
        self._on_matches_changed = lambda spans, current: None
        self._get_version = lambda: None
        self._on_message = lambda text: None

    # ------------------------------------------------------------------

    def bind(self, get_text, on_select, on_replace_one, on_replace_all_text, on_matches_changed,
             get_version=None, on_message=None):
        """由 MainWindow 注入：怎麼取得目前文字與它的版本、選取一段範圍、
        取代單筆、用新的整份文字做全部取代、命中清單改變時怎麼畫反白。"""
        self._get_text = get_text
        self._on_select = on_select
        self._on_replace_one = on_replace_one
        self._on_replace_all_text = on_replace_all_text
        self._on_matches_changed = on_matches_changed
        if get_version is not None:
            self._get_version = get_version
        if on_message is not None:
            self._on_message = on_message

    def set_theme(self, tokens):
        self._tokens = tokens
        for button in self._icon_buttons:
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        self.regex_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint, tokens.checked_text)
        self._delegate.match_color = QColor(tokens.find_match_bg)
        self._delegate.text_color = QColor(tokens.text)
        self.results_list.viewport().update()

    def focus_input(self):
        self.find_input.setFocus()
        self.find_input.selectAll()

    def refresh(self):
        self._run_search()

    def spans(self) -> list[tuple[int, int]]:
        return [(match.start(), match.end()) for match in self._matches]

    # ------------------------------------------------------------------
    # 搜尋

    def _pattern(self):
        text = self.find_input.text()
        if not text:
            return None
        if self.regex_button.isChecked():
            # 使用者寫的正則交給 safe_regex：寫法不好時能在期限內停下來，不會卡死介面。
            try:
                return safe_regex.compile(text)
            except safe_regex.errors as error:
                self._set_hint(f"正則錯誤：{error}")
                return None
        return re.compile(re.escape(text))

    def _on_regex_toggled(self, checked: bool):
        hint = "正則模式：按 Enter 或「搜尋」才執行"
        if not safe_regex.HAS_TIMEOUT:
            # 沒有 regex 套件就沒有逾時保護（打包版有附）：先講清楚，寫法不好的正則可能讓程式停住
            hint += "。沒有安裝 regex 套件，寫法不好的正則可能讓程式停住很久（pip install regex）"
        self._set_hint(hint if checked else "")
        self._run_search()

    def _on_text_changed(self, text):
        # 使用者自己改了搜尋字串，就不再是剛才選的那個常用正則。
        preset = SEARCH_PRESETS.get(i18n.combo_value(self.preset_combo))
        if preset is not None and preset[0] != text:
            self.preset_combo.setCurrentIndex(0)
        if self.regex_button.isChecked():
            # 正則可能很慢，不邊打邊跑；先把舊結果清掉免得誤會。
            self._clear_results()
            self._set_hint("按 Enter 或「搜尋」執行這個正則")
            return
        self._research_timer.start()

    def _on_return_pressed(self):
        if self._matches and not self._is_stale():
            self.find_next()
        else:
            self._run_search()

    def _apply_preset(self, index: int):
        preset = SEARCH_PRESETS.get(i18n.combo_value(self.preset_combo, index))
        if preset is None:
            return
        pattern, _explanation = preset
        self.regex_button.blockSignals(True)
        self.regex_button.setChecked(True)
        self.regex_button.blockSignals(False)
        self.find_input.setText(pattern)
        self.find_input.setFocus()
        self._run_search()

    def invalidate(self):
        """正文變了：先清掉現在的結果與反白，停一下再重新搜尋。"""
        self._clear_results()
        self._source_version = None
        if self.isVisible() and self.find_input.text() and not self.regex_button.isChecked():
            self._research_timer.start()

    def _clear_results(self):
        self._matches = []
        self._current = -1
        self._truncated = False
        self._page_starts, self._page_bases = [0], [0]
        self._page_skips = [None]
        self._update_label()
        self.results_list.clear()
        self._on_matches_changed([], -1)

    def _query_key(self):
        return self.find_input.text(), self.regex_button.isChecked()

    def _is_stale(self) -> bool:
        return self._source_version != self._get_version() or self._source_query != self._query_key()

    def _stale_message(self, action: str) -> str:
        if self._source_version != self._get_version():
            return f"本文已變更，搜尋結果已更新，請確認後再{action}"
        return f"搜尋字串已變更，已重新搜尋，請確認後再{action}"

    def _collect(self, pattern, content, start=0, skip=None):
        """從 start 開始逐筆取出命中，超過上限或時間就停下來。

        finditer 從中間開始找時，^ 仍然只認真正的行首，所以接著找下一頁的
        結果跟一次找完是一樣的。

        正則寫得不好時 finditer 本身可能就很慢，這裡擋得住「命中太多」與
        「整體太久」；單一次比對就卡住的極端情況由 safe_regex 的逾時中止
        （有裝 regex 套件時），已經找到的照樣列出。

        skip：上一頁最後一筆是空字串命中時，這一頁從同一個位置重找，第一筆
        會是它自己，略過。"""
        matches, deadline = [], time.monotonic() + SEARCH_DEADLINE
        try:
            for match in safe_regex.finditer(pattern, content, start, timeout=SEARCH_DEADLINE + 1.0):
                if skip is not None and not matches and match.span() == skip:
                    skip = None
                    continue
                matches.append(match)
                if len(matches) >= MAX_MATCHES:
                    return matches, True
                if not len(matches) % 512 and time.monotonic() > deadline:
                    return matches, True
        except safe_regex.RegexTimeout:
            self._timed_out = True
            return matches, True
        return matches, False

    @staticmethod
    def _after(match):
        """下一頁從哪裡接著找：（起點, 要略過的那一筆）。

        空字串的命中不能直接往後挪一格：同一個位置可能先有空字串命中、再有一筆
        非空的命中（例如 (?=a)|a），跳過就漏掉了。改成從同一個
        位置重找、略過重複的空字串那一筆，結果跟一次找完完全一樣。"""
        if match.end() > match.start():
            return match.end(), None
        return match.start(), match.span()

    def _load_first_page(self, pattern, content):
        self._page_starts, self._page_bases = [0], [0]
        self._page_skips = [None]
        self._matches, self._truncated = self._collect(pattern, content, 0)

    def _load_next_page(self, pattern, content) -> bool:
        """這一頁之後還有命中就載入下一頁；已經是最後一頁回傳 False。"""
        if not (self._truncated and self._matches):
            return False
        start, skip = self._after(self._matches[-1])
        if start > len(content):
            self._truncated = False
            return False
        matches, truncated = self._collect(pattern, content, start, skip)
        if not matches:
            self._truncated = False      # 剛好是 5000 的倍數，其實已經到底
            return False
        self._page_bases.append(self._page_bases[-1] + len(self._matches))
        self._page_starts.append(start)
        self._page_skips.append(skip)
        self._matches, self._truncated = matches, truncated
        return True

    def _load_previous_page(self, pattern, content) -> bool:
        if len(self._page_starts) < 2:
            return False
        self._page_starts.pop()
        self._page_bases.pop()
        self._page_skips.pop()
        self._matches, self._truncated = self._collect(pattern, content, self._page_starts[-1],
                                                       self._page_skips[-1])
        return True

    def _show_page(self, content: str):
        """換頁之後：編號、結果清單、本文反白、選取目前這一筆全部重畫。"""
        self._update_label()
        self._update_results_list(content)
        self._on_matches_changed(self.spans(), self._current)
        if self._current >= 0:
            self._select_current()

    def _run_search(self, *, resume_at: int | None = None):
        """resume_at：取代一筆之後從這個位置接著找下一筆（只接受關鍵字參數，
        免得按鈕的 clicked(bool) 被當成位置傳進來）。"""
        self._research_timer.stop()
        pattern = self._pattern()
        content = self._get_text()
        self._source_version = self._get_version()
        self._source_query = self._query_key()
        self._truncated = False
        self._timed_out = False
        if pattern is None or not content:
            self._matches = []
            self._current = -1
            self._page_starts, self._page_bases = [0], [0]
            self._page_skips = [None]
        else:
            if self.regex_button.isChecked() and _RISKY_REGEX.search(self.find_input.text()):
                self._set_hint("這個正則有巢狀量詞（例如 (a+)+），在長文字上可能要跑很久")
            started = time.monotonic()
            self._load_first_page(pattern, content)
            self._current = 0 if self._matches else -1
            if resume_at is not None and self._matches:
                # 取代完第 5 筆應該停在下一筆，不是跳回整份文字的第一筆——
                # 使用者刻意跳過的那幾筆又會被選到，一不注意就取代錯了。
                # 那一筆在後面的頁（例如第 7,000 筆）就一路載到那一頁。
                while True:
                    index = next((i for i, match in enumerate(self._matches)
                                  if match.start() >= resume_at), None)
                    if index is not None:
                        self._current = index
                        break
                    if not self._load_next_page(pattern, content):
                        if len(self._page_starts) > 1:     # 後面沒有了：繞回第一筆
                            self._load_first_page(pattern, content)
                        self._current = 0
                        break
            elapsed = time.monotonic() - started
            if self._timed_out:
                self._set_hint(f"正則執行太久，已停止搜尋；只列出已找到的 {len(self._matches)} 筆")
            elif elapsed > 0.5:
                self._set_hint(f"這次搜尋花了 {elapsed:.1f} 秒")
            elif not _RISKY_REGEX.search(self.find_input.text()):
                self._set_hint("")
        self._show_page(content)

    def _set_hint(self, text: str):
        if text:
            i18n.set_text(self.hint_label, text)
        self.hint_label.setVisible(bool(text))

    def _update_label(self):
        base = self._page_bases[-1] if self._page_bases else 0
        total = base + len(self._matches)
        shown = base + self._current + 1 if self._current >= 0 else 0
        self.match_label.setText(f"{shown}/{total}{'+' if self._truncated else ''}")

    def _update_results_list(self, content: str):
        self.results_list.clear()
        if not self._matches:
            return
        for match in self._matches:
            start, end = match.start(), match.end()
            # 預覽跟本文一樣隱藏 [::] 這類行尾標記（只是顯示，不影響搜尋與取代）。
            before = _MARKER_DISPLAY.sub("", content[max(0, start - _PREVIEW_CONTEXT):start]).replace("\n", " ")
            matched = _MARKER_DISPLAY.sub("", content[start:end]).replace("\n", " ")
            after = _MARKER_DISPLAY.sub("", content[end:end + _PREVIEW_CONTEXT * 8]).replace("\n", " ")
            prefix = "…" if start - _PREVIEW_CONTEXT > 0 else ""
            item = QListWidgetItem()
            item.setData(_PREVIEW_ROLE, (prefix + before, matched, after))
            item.setText(prefix + before + matched + after)   # 給無障礙工具與搜尋用
            self.results_list.addItem(item)

    def _select_current(self):
        if 0 <= self._current < len(self._matches):
            match = self._matches[self._current]
            self._on_select(match.start(), match.end())
            self.results_list.setCurrentRow(self._current)

    def _move_within_page(self):
        self._update_label()
        self._on_matches_changed(self.spans(), self._current)
        self._select_current()

    def find_next(self):
        if not self._matches:
            return
        if self._current + 1 < len(self._matches):
            self._current += 1
            self._move_within_page()
            return
        # 這一頁的最後一筆：後面還有就載入下一頁，真的到底了才繞回第一筆。
        pattern, content = self._pattern(), self._get_text()
        if pattern is None:
            return
        if not self._load_next_page(pattern, content) and len(self._page_starts) > 1:
            self._load_first_page(pattern, content)
        self._current = 0 if self._matches else -1
        self._show_page(content)

    def find_previous(self):
        if not self._matches:
            return
        if self._current > 0:
            self._current -= 1
            self._move_within_page()
            return
        pattern, content = self._pattern(), self._get_text()
        if pattern is None:
            return
        if not self._load_previous_page(pattern, content):
            # 第一筆再往前：繞到整份文字的最後一筆，中間的頁一路載過去。
            for _ in range(MAX_WRAP_PAGES):
                if not self._load_next_page(pattern, content):
                    break
        self._current = len(self._matches) - 1 if self._matches else -1
        self._show_page(content)

    def _on_result_clicked(self, item: QListWidgetItem):
        self._current = self.results_list.row(item)
        self._update_label()
        self._on_matches_changed(self.spans(), self._current)
        self._select_current()

    # ------------------------------------------------------------------
    # 取代

    def _expand(self, match) -> str | None:
        """算出這一筆要換成什麼。正則模式支援 \\1、\\g<name> 這類群組代入；
        純文字模式原樣取代，反斜線不特別處理。"""
        replacement = self.replace_input.text()
        if not self.regex_button.isChecked():
            return replacement
        try:
            return match.expand(replacement)
        except (IndexError, *safe_regex.errors) as error:
            self._set_hint(f"取代字串錯誤：{error}")
            return None

    def _on_replace_clicked(self):
        if self._is_stale():
            message = self._stale_message("取代")
            self._run_search()
            self._on_message(message)
            return
        if not (0 <= self._current < len(self._matches)):
            return
        match = self._matches[self._current]
        replacement = self._expand(match)
        if replacement is None:
            return
        # 這一筆前面的文字沒動過，所以取代後的位置直接用舊位置加上新字串長度。
        resume_at = match.start() + len(replacement)
        self._on_replace_one(match.start(), match.end(), replacement)
        self._run_search(resume_at=resume_at)

    def _on_replace_all_clicked(self):
        if self._is_stale():
            message = self._stale_message("全部取代")
            self._run_search()
            self._on_message(message)
            return
        pattern = self._pattern()
        content = self._get_text()
        if pattern is None or not content:
            return
        replacement = self.replace_input.text()
        try:
            # 直接在整份文字上取代：結果清單有筆數上限，不能拿來當取代依據，
            # 否則「全部取代」會變成「取代前面幾筆」。
            if self.regex_button.isChecked():
                new_content, count = safe_regex.subn(pattern, replacement, content)
            else:
                new_content, count = pattern.subn(lambda _m: replacement, content)
        except safe_regex.RegexTimeout:
            self._set_hint("正則執行太久，已停止；本文沒有被修改")
            return
        except (IndexError, *safe_regex.errors) as error:
            self._set_hint(f"取代字串錯誤：{error}")
            return
        if not count:
            return
        self._on_replace_all_text(new_content, count)
        self._run_search()
