"""主視窗各部分（main_window 與 window_* mixin）共用的常數與小工具：行號換算、目錄標籤、
版面監看。"""

import bisect
import difflib
import re

from PySide6.QtCore import QEvent, QObject, QRegularExpression, QTimer, Signal
from PySide6.QtWidgets import QWidget

from core.chapter_parse import CN_NUM_FLOAT_PATTERN, compact_toc_label

MAX_HISTORY_STEPS = 30
# 復原歷史每步都保存一份完整正文，必須設上限才不會把記憶體吃光。
MAX_HISTORY_CHARS = 30_000_000
# 但無論文件多大，至少保留這麼多步，否則「復原」會形同失效。
MIN_HISTORY_STEPS = 3
# 輸入時多久沒有新的按鍵才視為一次「停頓」、存成一個復原步驟；
# 不是每個按鍵都存一份，那樣復原歷史會被打字過程灌爆。
TYPING_CHECKPOINT_DELAY_MS = 450

DEFAULT_STRUCTURE_MODE = "自動判斷"

# 能開的檔案：TXT，和 Word（.docx，只取文字）；存檔一律是 TXT
OPENABLE_EXTENSIONS = (".txt", ".docx", ".epub")
OPEN_FILE_FILTER = "文字檔 (*.txt *.docx *.epub);;所有檔案 (*)"
WORD_ENCODING = "docx"        # detected_encoding 的值：Word 檔沒有文字編碼
EPUB_ENCODING = "epub"        # EPUB 也一樣（裡面的 XHTML 自己宣告編碼）


def openable(path: str) -> bool:
    return bool(path) and path.lower().endswith(OPENABLE_EXTENSIONS)

# 本文字級縮放：基準跟 theme.py 樣式表 * 規則的 font-size 一致。
EDITOR_BASE_FONT_PX = 14
EDITOR_ZOOM_MIN = 50
EDITOR_ZOOM_MAX = 300

# 本文裡最多同時畫幾個搜尋反白；超過就只畫目前這一筆。
MAX_HIGHLIGHT_SPANS = 800

# 四種行尾標記的意義。說明框要列給使用者看，所以文字放在這裡集中管理，
# 不要散在各個提示字串裡（core/title_markers.py 是判讀它們的地方）。
MARKER_GUIDE = [
    ("[::]", "手動加入目錄，這一行是章節標題"),
    ("[::X]", "保留正文，手動排除於目錄"),
    ("[::W]", "手動設為作品標題（多作品合集的各部作品）"),
    ("[::T]", "手動設為特殊標題（序章、後記這類沒有編號的標題）"),
]

MIN_WINDOW_WIDTH = 680
MIN_WINDOW_HEIGHT = 420       # 書籍資料收起時；展開時再加上它的高度（_update_minimum_height）
# 章節管理的預覽開關：狀態列說明的結尾
_PREVIEW_NOTE = "（預覽，按「套用到本文」才寫入）"


class _LayoutWatcher(QObject):
    """盯著幾個元件的 LayoutRequest（內容的大小變了），合併成一次呼叫 callback。"""

    def __init__(self, callback, parent):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(callback)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.LayoutRequest:
            self._timer.start()
        return False


def _settle(widget):
    """量最小寬度之前先套好樣式、排好版面：還沒顯示過的卡片（例如開檔時自動打開上次的功能卡片）
    沒套樣式表的字型與內距，量出來會比實際需要的窄，卡片就被壓到切掉。"""
    widget.ensurePolished()
    for child in widget.findChildren(QWidget):
        child.ensurePolished()
    layout = widget.layout()
    if layout is not None:
        layout.invalidate()
        layout.activate()
# 視窗預設大小的上限；實際大小還會被螢幕可用區域夾住（見 _fit_to_screen）。
DEFAULT_WINDOW_SIZE = (1360, 860)
WARM_START_DELAY_MS = 500
WARM_CHUNK_LINES = 600
# Lines still to warm that are simply warmed on the spot when a scan window opens (about 0.3 s)
WARM_NOW_LINES = 20000
WARM_WAITING_SLICE = 0.04
# 工具列縮成「只有圖示」的門檻。兩個數字不一樣是為了留遲滯：在邊界附近
# 拖動視窗時才不會一直來回切換。
COMPACT_TOOLBAR_WIDTH = 1330
FULL_TOOLBAR_WIDTH = 1390
COMPACT_ARROW_WIDTH = 26     # 只顯示圖示時，選擇檔案、匯出 TXT 旁邊的箭頭寬度
_NUMBER_WITHOUT_UNIT = re.compile(r"^第\s*(" + CN_NUM_FLOAT_PATTERN + r")[\s　]+\S")
PENDING_LINE_MAP_LIMIT = 32     # 行號位移累積幾次就折成一張表（見 _push_line_map）


# 行尾持久標記（連同前面的空白），畫面上要隱藏；規則與 core.title_markers 一致。
_MARKER_REGEX = QRegularExpression(r"\s*\[::[XxWwTt]?\]\s*$")

# 「第十二章」「第3.5回」「第二卷」這類編號開頭。
_HEADING_NUMBER_REGEX = re.compile(r"^第\s*" + CN_NUM_FLOAT_PATTERN + r"\s*[章回節节折幕卷集篇部]")


def _line_opcodes(old: list, new: list) -> list:
    """difflib 的 opcodes，但先跳過頭尾相同的行：打字、刪幾行廣告通常只動到中間一小段，
    整份十幾萬行交給 SequenceMatcher 要 0.15 秒，停下來存一步復原時會頓一下。
    autojunk 要開著（預設）：小說大量空行會讓比對退化成平方時間。"""
    limit = min(len(old), len(new))
    start = 0
    while start < limit and old[start] == new[start]:
        start += 1
    end_old, end_new = len(old), len(new)
    while end_old > start and end_new > start and old[end_old - 1] == new[end_new - 1]:
        end_old -= 1
        end_new -= 1
    opcodes = [("equal", 0, start, 0, start)] if start else []
    middle = difflib.SequenceMatcher(None, old[start:end_old], new[start:end_new]).get_opcodes()
    opcodes += [(tag, a + start, b + start, c + start, d + start) for tag, a, b, c, d in middle]
    if end_old < len(old):
        opcodes.append(("equal", end_old, len(old), end_new, len(new)))
    return opcodes


def _diff_line_mapper(opcodes):
    """由 difflib 的比對結果產生「舊行號 → 新行號」的換算函式。

    沒變的行照位移換算；被刪掉或整段改寫的行，對應到那段改動之前的最後
    一行——目錄選取回復時就會自然落在前一個章節。"""
    def map_line(index: int) -> int:
        for tag, a, b, c, d in opcodes:
            if a <= index < b:
                if tag == "equal" or (tag == "replace" and b - a == d - c):
                    return c + (index - a)
                return c - 1
        return index
    return map_line


def _chapter_line_mapper(old_to_new: dict):
    """排版後整份文字重排，只能靠「每個章節標題從哪一行搬到哪一行」換算；
    其他行對應到它前面最近的章節標題。"""
    keys = sorted(old_to_new)

    def map_line(index: int) -> int:
        if index in old_to_new:
            return old_to_new[index]
        position = bisect.bisect_right(keys, index) - 1
        return old_to_new[keys[position]] if position >= 0 else index
    return map_line


def _format_line_mapper(old_lines: list, new_lines: list, old_to_new: dict):
    """排版前後的行號換算（游標、畫面用）：先對到同一章的標題，再數「這一章裡第幾個
    非空行」——排版主要是增減空行與縮排，非空行的順序不變（整理段落換行會接行，就停在
    接過去的那一行附近）。"""
    keys = sorted(old_to_new)
    new_titles = sorted(old_to_new.values())

    def map_line(index: int) -> int:
        position = bisect.bisect_right(keys, index) - 1
        if position < 0:
            return min(index, len(new_lines) - 1)
        old_title = keys[position]
        target = sum(1 for row in range(old_title + 1, min(index, len(old_lines) - 1) + 1) if old_lines[row].strip())
        row = old_to_new[old_title]
        following = bisect.bisect_right(new_titles, row)
        end = new_titles[following] if following < len(new_titles) else len(new_lines)
        while target and row + 1 < end:
            row += 1
            if new_lines[row].strip():
                target -= 1
        return row
    return map_line


def short_toc_label(full_label: str) -> str:
    """目錄「簡稱」模式的顯示文字：只留章號，例如「第7章 收获的季节」→「第7章」。

    core.compact_toc_label 只會拿掉章號前面重複的書名／卷名；一般單本小說
    的目錄本來就沒有那段前綴，切換後什麼都不會變。所以簡稱模式再進一步
    只保留章號——一眼看出編號是否連續、有沒有重複的章節。抓不到章號的
    標題（序章、番外、後記…）維持原樣。
    """
    compact = compact_toc_label(full_label)
    match = _HEADING_NUMBER_REGEX.match(compact)
    return re.sub(r"\s+", "", match.group(0)) if match else compact


class _ToolDialogWatcher(QObject):
    """工具對話框被切回來（重新成為作用中視窗）時，請主視窗檢查本文有沒有
    改過；改過就讓對話框用新的本文重算。"""

    def __init__(self, window):
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.WindowActivate:
            self._window._refresh_tool_dialog(watched)
        return False


# 字色標示：本文停止變動這麼久之後才重掃
MARK_SCAN_DELAY_MS = 800


class _MarkScanSignals(QObject):
    """背景執行緒掃完後，透過這個訊號回到主執行緒（跨執行緒會自動排隊）。"""
    finished = Signal(int, object, object, object)     # 本文版本、廣告候選、作者感言候選、目錄（沒重建是 None）


def _tree_depth(item) -> int:
    """目錄節點的深度：最上層是 0。"""
    depth = 0
    while item.parent() is not None:
        item = item.parent()
        depth += 1
    return depth
