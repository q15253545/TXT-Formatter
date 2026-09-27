"""PySide6 主視窗：開檔／編輯／存檔／目錄樹／格式選項／搜尋取代／復原重做。

章節辨識與排版邏輯完全交給 core.structure_builder，這裡只負責畫面與把使用者
的操作轉成呼叫 core 純函式的參數。

復原／重做刻意不用 QPlainTextEdit 內建的 QTextDocument undo：忽略集合、
強制卷／章層級、自動標題快取這些「章節結構」狀態跟正文是綁在一起的，
只復原文字、不復原這些狀態，會讓目錄跟正文對不起來（點右鍵選單的操作
之後按 Ctrl+Z，文字復原了但目錄還停在操作後的樣子）。所以改成
整份文字＋結構狀態一起存成快照（見 _checkpoint_document／
_restore_document_step），輸入文字時用計時器合併成一步，不是每個按鍵
存一份。
"""

import bisect
import dataclasses
import threading
import difflib
import os
import re
from collections import Counter

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QRect, QRegularExpression, QTimer, Qt, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QGuiApplication, QKeySequence, QShortcut,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableWidget, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.cn_numerals import chinese_to_arabic
from core.chapter_parse import (
    CN_NUM_FLOAT_PATTERN, DEFAULT_TITLE_TAIL_ALLOWED, MAX_TITLE_LENGTH, SPECIAL_LEVELS, build_title_check, chapter_unit_signature,
    heading_number,
    compact_number_ranges,
    compact_toc_label, extract_author_from_intro, locate_chapter_number, looks_like_auto_chapter,
    parse_mixed_volume_chapter_header, render_chapter_number_like,
)
from core.ad_scan import (
    AD_CATEGORY_LABELS, AD_ONLY_CATEGORIES, NOTE_CATEGORIES, REPEAT_MIN_COUNT, REPEAT_MIN_LENGTH, scan_ad_candidates,
)
from core.quote_check import QUOTE_PROBLEM_LABELS
from core.user_rules import PRESET_RULES, pop_timed_out_rules, preset_match
from core.collection import chapter_gap_report, group_formal_chapters, missed_tail_chapters, scan_chapter_candidates
from core.encoding import detect_line_ending, smart_detect_encoding, strip_stray_bom
from core.file_io import read_text, read_text_lossy, write_text_atomic
from core.filename_meta import (
    DEFAULT_COMPLETED_TEMPLATE, DEFAULT_ONGOING_TEMPLATE, build_smart_filename, extract_filename_metadata,
    filename_fields, filename_template_for, upgrade_template,
)
from core.format_options import FormatOptions
from core.script_convert import (
    SCRIPT_SIMP, SCRIPT_TRAD, convert_body_text, convert_script, opencc_available,
)
from core.scan_cache import clear_line_caches, freeze_line_caches, warm_line_caches
from core.word_count import chapter_word_counts
from core.structure_builder import BuildContext, build_document_structure
from core.insert_suggestions import get_insert_suggestions
from core.persistence import (
    APP_DATA_DIR, RULES_FILE, _save_json, load_ui_state, load_user_chapter_rules, load_window_state, save_ui_state,
    save_window_state,
)
from core.title_blocks import TEMPLATE_LABELS, TEMPLATES, block_rule, template
from core.title_markers import strip_export_markers, strip_persistent_title_marker

from . import app_log, dialogs, i18n, icons, toc_ops
from .app_log import action, log, native_dialog, timed
from .ad_scan_dialog import AdScanDialog
from .help_dialog import HelpDialog
from .content_panel import ContentPanel
from .duplicate_chapters_dialog import DuplicateChaptersDialog
from .chapter_panel import ChapterPanel
from .find_bar import FindBar
from .filename_dialog import FilenameDialog
from .insert_title_dialog import InsertTitleDialog
from .metadata_bar import ENCODING_CODECS, MetadataBar
from .options_panel import OptionsPanel, describe_options
from .quote_check_dialog import QuoteCheckDialog
from .script_convert_dialog import ScriptConvertDialog
from .recognition_dialog import RecognitionDialog
from .rules_dialog import RulesDialog
from .word_count_dialog import WordCountDialog
from .text_positions import PositionMap
from .theme import DARK, DEFAULT_THEME, THEMES, build_stylesheet, set_active_tokens, theme_tokens
from .widgets import (
    AppWidgetPolisher, Card, ClickableLabel, Editor, IconButton, IconTextButton, LanguageToggle,
    ElidedLabel, ThemeButton, VDivider,
    make_card_header,
)
from . import __version__

MAX_HISTORY_STEPS = 30
# 復原歷史每步都保存一份完整正文，必須設上限才不會把記憶體吃光。
MAX_HISTORY_CHARS = 30_000_000
# 但無論文件多大，至少保留這麼多步，否則「復原」會形同失效。
MIN_HISTORY_STEPS = 3
# 輸入時多久沒有新的按鍵才視為一次「停頓」、存成一個復原步驟；
# 不是每個按鍵都存一份，那樣復原歷史會被打字過程灌爆。
TYPING_CHECKPOINT_DELAY_MS = 450

DEFAULT_STRUCTURE_MODE = "自動判斷"

# 本文字級縮放：基準跟 theme.py 樣式表 * 規則的 font-size 一致。
EDITOR_BASE_FONT_PX = 14
EDITOR_ZOOM_MIN = 50
EDITOR_ZOOM_MAX = 300

# 本文裡最多同時畫幾個搜尋反白；超過就只畫目前這一筆。
MAX_HIGHLIGHT_SPANS = 800

# 四種行尾標記的意義。說明框要列給使用者看，所以文字放在這裡集中管理，
# 不要散在各個提示字串裡（core/title_markers.py 是判讀它們的地方）。
MARKER_GUIDE = [
    ("[::]", "手動加入目錄", "指定該行為章節標題"),
    ("[::X]", "排除於目錄", "保留正文，不列入目錄"),
    ("[::W]", "作品標題", "多作品合集中各作品的標題"),
    ("[::T]", "特殊標題", "序章、後記等無編號的標題"),
]

MIN_WINDOW_WIDTH = 680


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
# 工具列縮成「只有圖示」的門檻。兩個數字不一樣是為了留遲滯：在邊界附近
# 拖動視窗時才不會一直來回切換。
COMPACT_TOOLBAR_WIDTH = 1290
FULL_TOOLBAR_WIDTH = 1350
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
    finished = Signal(int, object, object, object)     # 本文版本、廣告行、作者感言行、目錄（沒重建是 None）


def _tree_depth(item) -> int:
    """目錄節點的深度：最上層是 0。"""
    depth = 0
    while item.parent() is not None:
        item = item.parent()
        depth += 1
    return depth


class MainWindow(QMainWindow):
    @property
    def tokens(self):
        """目前主題的配色。"""
        return theme_tokens(self.theme_name)

    @property
    def dark_mode(self) -> bool:
        return self.tokens.is_dark

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TXT 排版工具")
        self.setWindowIcon(icons.make_app_icon())
        # 視窗大小依螢幕決定，不寫死：直立螢幕在 200% 縮放下，
        # 程式看到的可用寬度只有 720，硬塞 1360 會有一半在畫面外。
        self._toolbar_compact = False
        self._restore_window_geometry()

        self.input_file = ""
        self.detected_encoding = "utf-8"
        self.raw_lines: list[str] = []
        self.user_chapter_rules: list = load_user_chapter_rules()
        self.auto_titles: dict = {}
        self.force_lv1_chapters: set = set()
        self.force_lv2_chapters: set = set()
        self.ignored_chapters: set = set()
        self.structure_mode = DEFAULT_STRUCTURE_MODE
        # 標題結尾允許字元（自訂章節規則 → 標題結尾）
        self.title_tail_allowed = DEFAULT_TITLE_TAIL_ALLOWED
        self.title_tail_custom = ""     # 使用者在「標題結尾」分頁自己加的標點
        self.disabled_words = frozenset()           # 「辨識章節」關掉的章節單位、特殊標題
        self.special_levels = {}                    # 「辨識章節」改成卷或章的特殊標題（只記跟預設不同的）
        self._side_width = 0                        # 功能卡片最後的寬度：關掉再打開時照這個寬度
        self.max_title_length = MAX_TITLE_LENGTH    # 「辨識格式 → 標題長度」
        self._skip_duplicate_titles = False         # 「自動合併重複標題」（預覽）
        self.filename_ongoing = DEFAULT_ONGOING_TEMPLATE     # 匯出檔名格式（連載中／未指定）
        self.filename_completed = DEFAULT_COMPLETED_TEMPLATE  # 匯出檔名格式（已完結）
        self.filename_script = SCRIPT_TRAD                    # 匯出檔名轉繁體／簡體；跟著介面繁簡切換
        self.absorbed_titles: dict = {}             # 重複標題的預覽：重複那一行 → 保留的標題行
        self.absorbed_title_items: set = set()
        self._infer_volumes = False     # 章節管理的「自動補齊卷號」開關
        self._infer_volume_names = False  # 「自動補齊卷名」（卷號開著才有作用）
        self._merge_titles = False      # 「自動合併下行標題」（預覽，套用到本文才寫進去）
        # 合併下行標題的預覽：標題行號 → (章名所在行號, 章名)；目錄上對應的項目
        self.merged_titles: dict = {}
        self.merged_title_items: set = set()
        self.format_options = FormatOptions(structure=DEFAULT_STRUCTURE_MODE)

        # 目錄項目（QTreeWidgetItem 物件本身當 key）→ 原始行號（0 起算）／
        # 顯示行號（1 起算）。目錄每次重建都是新的物件，舊的 key 就作廢。
        self.chapter_raw_map: dict = {}
        self.chapter_index_map: dict = {}
        self.chapter_records: dict = {}
        self.toc_boundary_map: dict = {}
        # 開著的工具對話框（標點校對、掃描無關連內容、自訂章節規則）：非模式，
        # 開著時也能編輯本文。同一種只開一個。
        self._tool_dialogs: dict = {}
        self._tool_dialog_watcher = _ToolDialogWatcher(self)

        # 目錄顯示切換：完整標題／簡稱各自快取一份，切換時不必重新計算。
        self.toc_compact_mode = False
        self.toc_full_labels: dict = {}
        self.toc_compact_labels: dict = {}
        self._breadcrumb_rows: list = []
        self._breadcrumb_items: list = []
        self._breadcrumb_current = None
        # 上次畫目錄之後，正文行號經過的每一次位移（換算函式，依序套用）；
        # 重畫目錄時用來把原本選取的章節對到新位置。
        self._pending_line_maps: list = []

        # 復原／重做：整份文字＋結構狀態的快照陣列，見模組開頭說明。
        self._history: list[tuple] = []
        self._history_position = -1
        self._restoring_history = False
        self._typing_checkpoint_timer = QTimer(self)
        self._typing_checkpoint_timer.setSingleShot(True)
        self._typing_checkpoint_timer.timeout.connect(self._checkpoint_document)
        # 連續轉滑鼠滾輪縮放時，標題格式只在停下來之後重套一次。
        self._format_refresh_timer = QTimer(self)
        self._format_refresh_timer.setSingleShot(True)
        self._format_refresh_timer.setInterval(120)
        self._format_refresh_timer.timeout.connect(self._refresh_title_formats)

        # 主題：選過的記在 ui_state.json；第一次開啟用簡約藍，不跟系統設定走。
        self.theme_name = DEFAULT_THEME
        # 快速切換（toggle_theme）在「最近用過的淺色系」與「深色系」之間來回。
        self._last_light_theme = DEFAULT_THEME
        self._last_dark_theme = DARK.name
        self._icon_buttons: list[IconButton] = []
        self._panel_toggle_buttons: list[IconTextButton] = []
        self._primary_buttons: list[IconTextButton] = []
        self._editor_zoom = 100
        # 標題加粗／隱藏標記只改字元格式，Qt 一樣會發 textChanged；這段期間
        # 不能當成「使用者在打字」，否則「上一步」按鈕會亮起來。
        self._applying_formats = False
        # 章節標記平常藏起來，按本文卡片上的切換鈕才顯示（只影響顯示）。
        self._show_title_markers = False
        # 匯出時要不要移除標記：只看章節標記說明裡的勾選，跟「顯示章節標記」無關。
        self._strip_markers_on_export = True
        # 缺章檢查結果：按過一次「檢查缺章」之後，每次目錄重建都自動重算。
        self._missing_report_active = False
        self._missing_groups: list = []
        # 按「重新整理目錄」時，最新卷／最新章無條件重新填入。
        self._force_last_found = False
        # 推定卷（本文沒有卷標題、由卷結尾行推得的卷）：目錄項目 → 資訊
        self.virtual_volume_items: dict = {}
        self.split_volume_items: set = set()   # 從每章標題拆出來、本文還沒有卷標題的卷
        # 已剪下、等著貼上的章節（照檔案總管的做法：按貼上才真的搬動）。
        self._cut_state: dict | None = None
        # 正文的版本號：每次正文真的變動就加一（純顯示的格式變更不算）。
        # 目錄、搜尋結果、raw_lines 各自記下自己是在哪一版算出來的，過期的
        # 位置就不能再拿去改文字。
        self._text_version = 0
        # 字色標示：掃描結果的行號（跟哪一版本文對應）、延遲重掃的計時器、背景掃描
        self._mark_rows = {"ad": set(), "note": set()}
        self._mark_rows_version = None
        self._mark_scan_running = False
        self._mark_scan_pending = False
        self._warm_lines = None
        self._warm_position = 0
        self._warm_timer = QTimer(self)
        self._warm_timer.setSingleShot(True)
        self._warm_timer.timeout.connect(self._warm_step)
        self._mark_timer = QTimer(self)
        self._mark_timer.setSingleShot(True)
        self._mark_timer.setInterval(MARK_SCAN_DELAY_MS)
        self._mark_timer.timeout.connect(self._start_mark_scan)
        self._mark_signals = _MarkScanSignals()
        self._mark_signals.finished.connect(self._on_mark_scan_finished)
        self._toc_text_version = -1
        self._synced_text_version = -1
        self._synced_text = ""
        self._position_map = PositionMap("")
        self._position_map_version = -1
        self._ad_scan_cache: list = []
        self._ad_scan_version = -1
        # 正文改過、還沒匯出：換檔案或關視窗前要先問過使用者。
        # 旗標只代表「可能改過」，真正要問之前會再跟已存檔的內容比對一次，
        # 使用者把所有修改都復原掉時就不該再問。
        self._document_dirty = False
        self._saved_text_hash = None

        app_log.set_error_callback(self._show_unhandled_error)

        self._widget_polisher = AppWidgetPolisher(self)
        QApplication.instance().installEventFilter(self._widget_polisher)

        # 上次關閉時的介面狀態（深色模式、各種勾選…），畫面建好之後還原。
        self._ui_state = load_ui_state()
        self._pending_side_panel = None

        self._build_ui()
        # 右下角顯示版本（只在發布到 GitHub 時進版，見 ui_qt/__init__.py）。
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setObjectName("versionLabel")
        i18n.skip(self.version_label)
        self.statusBar().addPermanentWidget(self.version_label)
        self._apply_theme()
        self._set_document_actions_enabled(False)
        self.setAcceptDrops(True)
        self._restore_ui_state()

    # ------------------------------------------------------------------
    # UI 建構
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        self.metadata_bar = MetadataBar()
        self.metadata_bar.structure_changed.connect(self._on_structure_changed)
        self.metadata_bar.encoding_changed.connect(self._on_encoding_changed)
        self._panel_toggle_buttons.append(self.metadata_bar.toggle_button)
        root.addWidget(self.metadata_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter = splitter
        splitter.setChildrenCollapsible(False)
        # 卡片之間留一小段距離就好：太寬會把畫面切得零碎，完全貼齊又分不出
        # 是三張不同的卡片。
        splitter.setHandleWidth(10)

        # 左側常駐一張卡片，裡面疊放兩個面板（格式選項／章節管理），一次只
        # 顯示一個：格式選項由工具列「排版設定」開關，章節管理從目錄標題列的
        # 「…」開關；預設全部收起，不需要的設定不用一直佔畫面。
        self.side_card = Card()
        side_layout = QVBoxLayout(self.side_card)
        side_layout.setContentsMargins(0, 0, 0, 0)
        self.options_panel = OptionsPanel(self.format_options)
        self.options_panel.apply_requested.connect(self.apply_formatting)
        self.options_panel.apply_selected_requested.connect(self.format_selected_chapters)
        self.options_panel.option_toggled.connect(self._on_format_option_toggled)
        self.options_panel.save_one_click_requested.connect(self.save_one_click_options)
        self.options_panel.closed.connect(lambda: self._set_active_side_panel(None))
        side_layout.addWidget(self.options_panel)
        self.options_panel.hide()

        # 內容檢查：掃描無關連內容、作者感言與作品資訊、標點校對、繁簡轉換＋字色標示開關
        self.content_panel = ContentPanel()
        self.content_panel.closed.connect(lambda: self._set_active_side_panel(None))
        self.content_panel.ad_scan_requested.connect(self.open_ad_scan_dialog)
        self.content_panel.note_scan_requested.connect(self.open_note_scan_dialog)
        self.content_panel.quote_check_requested.connect(self.open_quote_check_dialog)
        self.content_panel.word_count_requested.connect(self.open_word_count_dialog)
        self.content_panel.script_convert_requested.connect(self.open_script_convert_dialog)
        self.content_panel.marking_changed.connect(self._on_marking_changed)
        self.content_panel.show_whitespace_toggle.toggled.connect(self._on_whitespace_toggled)
        self.content_panel.show_whitespace_toggle.clicked.connect(
            lambda on: self._show_status("顯示內文空格：半形 ·、全形 □、Tab →，行尾多餘的空白標紅"
                                         if on else "不顯示內文空格"))
        self.content_panel.mark_ad_toggle.clicked.connect(
            lambda on: self._show_status("本文用顏色標出廣告" if on else "不再標出廣告"))
        self.content_panel.mark_note_toggle.clicked.connect(
            lambda on: self._show_status("本文用顏色標出作者感言與作品資訊" if on else "不再標出作者感言與作品資訊"))
        side_layout.addWidget(self.content_panel)
        self.content_panel.hide()

        self.chapter_panel = ChapterPanel()
        self.chapter_panel.closed.connect(lambda: self._set_active_side_panel(None))
        self.chapter_panel.recognition_requested.connect(self.open_recognition_dialog)
        self.chapter_panel.insert_requested.connect(self.open_insert_title_dialog)
        self.chapter_panel.rules_requested.connect(self.open_rules_dialog)
        self.chapter_panel.merge_duplicates_requested.connect(self.open_duplicate_chapters_dialog)
        self.chapter_panel.check_missing_requested.connect(self.check_missing_chapters)
        self.chapter_panel.missing_mode_changed.connect(self._refresh_missing_report)
        self.chapter_panel.merge_titles_toggled.connect(self._on_merge_titles_toggled)
        self.chapter_panel.skip_duplicates_toggled.connect(self._on_skip_duplicates_toggled)
        self.chapter_panel.infer_volumes_toggled.connect(self._on_infer_volumes_toggled)
        self.chapter_panel.infer_volume_names_toggled.connect(self._on_infer_volume_names_toggled)
        self.chapter_panel.apply_volumes_requested.connect(self.apply_toc_preview)
        self.chapter_panel.report_link_activated.connect(self._on_missing_report_link)
        self.chapter_panel.report_closed.connect(self._on_missing_report_closed)
        side_layout.addWidget(self.chapter_panel)
        self.chapter_panel.hide()

        # 尋找／取代也放進同一張卡片：本文不再被壓縮，搜尋結果也有完整高度。
        self.find_bar = FindBar()
        self.find_bar.closed.connect(self.close_find_bar)
        self.find_bar.bind(
            get_text=lambda: self.editor.toPlainText(),
            on_select=self._find_on_select,
            on_replace_one=self._find_on_replace_one,
            on_replace_all_text=self._find_on_replace_all_text,
            on_matches_changed=self._find_on_matches_changed,
            get_version=self._text_version_now,
            on_message=self._show_status,
        )
        side_layout.addWidget(self.find_bar)
        self.find_bar.hide()

        # 不寫死最小寬度：讓卡片最窄就是「剛好裝得下面板內容」，寫死的數字
        # 一旦比內容窄，拉到最小時下拉框、按鈕就會超出卡片。
        self.side_card.setMaximumWidth(360)
        self.side_card.hide()
        splitter.addWidget(self.side_card)

        self.tree_card = Card()
        tree_layout = QVBoxLayout(self.tree_card)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.itemClicked.connect(self._on_tree_item_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_toc_context_menu)
        i18n.skip(self.tree)   # 目錄是書的內容，不跟著介面切換繁簡

        tree_header, tree_header_layout = make_card_header("目錄")
        for icon_name, tooltip, slot in (
            ("refresh-cw", "重新掃描目錄（F5）", self.rescan_toc),
            ("unfold-vertical", "全部展開", self.tree.expandAll),
            ("fold-vertical", "全部摺疊", self.tree.collapseAll),
        ):
            button = IconButton(icon_name, tooltip, size=16)
            button.clicked.connect(slot)
            self._icon_buttons.append(button)
            tree_header_layout.addWidget(button)
        # 目錄只顯示章號：開關型圖示，開著時用互動色
        self.toc_compact_button = IconButton("list-filter", "", size=16)
        self.toc_compact_button.setCheckable(True)
        self.toc_compact_button.clicked.connect(self._on_toc_compact_clicked)
        tree_header_layout.addWidget(self.toc_compact_button)
        button = IconButton("ellipsis", "章節管理", size=16)
        button.clicked.connect(lambda: self._toggle_side_panel(self.chapter_panel))
        self._icon_buttons.append(button)
        tree_header_layout.addWidget(button)
        tree_layout.addWidget(tree_header)

        tree_body = QVBoxLayout()
        tree_body.setContentsMargins(4, 8, 4, 8)
        # 目錄是空的、本文卻有很多同一種常用寫法（1 標題、#1…）：提示一鍵加成辨識章節的組合
        self.toc_hint = QFrame()
        self.toc_hint.setObjectName("tocHint")
        hint_layout = QVBoxLayout(self.toc_hint)
        hint_layout.setContentsMargins(12, 10, 12, 10)
        hint_layout.setSpacing(8)
        self.toc_hint_label = QLabel("")
        self.toc_hint_label.setWordWrap(True)
        hint_layout.addWidget(self.toc_hint_label)
        self.toc_hint_button = QPushButton("加入辨識章節")
        self.toc_hint_button.clicked.connect(self._accept_toc_hint)
        hint_layout.addWidget(self.toc_hint_button)
        self.toc_hint.hide()
        self._toc_hint_template = None
        self._toc_hint_format = None
        self._toc_hint_timer = QTimer(self)
        self._toc_hint_timer.setSingleShot(True)
        self._toc_hint_timer.setInterval(0)
        self._toc_hint_timer.timeout.connect(self._update_toc_hint)
        tree_body.addWidget(self.toc_hint)
        tree_body.addWidget(self.tree)
        tree_layout.addLayout(tree_body, 1)
        splitter.addWidget(self.tree_card)

        self.editor_card = Card()
        editor_layout = QVBoxLayout(self.editor_card)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        # 麵包屑自己吃掉剩下的寬度（stretch=1），所以標題列不另外放彈簧：
        # 兩個都放的話會平分，麵包屑只剩一半寬度可用。章節名稱靠最右邊。
        editor_header, editor_header_layout = make_card_header("本文", with_stretch=False)
        self.breadcrumb_label = ElidedLabel("")
        self.breadcrumb_label.setObjectName("fileLabel")
        self.breadcrumb_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        i18n.skip(self.breadcrumb_label)
        # 點麵包屑：目錄選到這一章並捲過去（本文不動，還在看的地方）
        self.breadcrumb_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.breadcrumb_label.clicked.connect(self._reveal_current_chapter)
        editor_header_layout.addWidget(self.breadcrumb_label, 1)
        editor_header_layout.addSpacing(6)
        editor_layout.addWidget(editor_header)
        # 章節標記平常藏起來（1px 透明字），但它們是真的寫在檔案裡的：顯示與否、
        # 匯出時要不要拿掉，兩個開關放在「內容檢查」卡片；說明按鈕在檔名列。
        self.marker_button = self.content_panel.show_markers_toggle
        self.marker_button.toggled.connect(self._on_markers_toggled)
        self.content_panel.strip_markers_toggled.connect(self._on_strip_markers_toggled)
        self.marker_help_button = self.metadata_bar.help_button
        self.marker_help_button.clicked.connect(self._show_marker_help)
        self._icon_buttons.append(self.marker_help_button)

        self.editor = Editor()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setPlaceholderText("開啟或把 TXT 檔案拖曳到這裡開始使用（Ctrl+O）")
        self.editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._show_editor_context_menu)
        self.editor.undo_requested.connect(self._undo)
        self.editor.redo_requested.connect(self._redo)
        self.editor.zoom_requested.connect(self._on_editor_zoom)
        self.editor.file_dropped.connect(self._open_dropped_file)
        self.editor.textChanged.connect(self._on_editor_text_changed)
        editor_layout.addWidget(self.editor, 1)

        # 游標位置與編碼放在本文卡片自己的底部列，而不是視窗最下面的狀態列：
        # 它們講的是「這份本文」的狀態，貼著本文看比較直覺，也讓狀態列專心
        # 顯示操作結果訊息。
        editor_footer = QWidget()
        editor_footer.setObjectName("cardFooter")
        footer_layout = QHBoxLayout(editor_footer)
        footer_layout.setContentsMargins(18, 8, 18, 8)
        self.cursor_position_label = QLabel("第 1 行 · 第 1 欄")
        self.cursor_position_label.setObjectName("footerLabel")
        # 每次移動游標都會重寫；切換繁簡時由 _update_cursor_position_label 自己重畫。
        i18n.skip(self.cursor_position_label)
        footer_layout.addWidget(self.cursor_position_label)
        footer_layout.addStretch(1)
        self.encoding_footer_label = QLabel("")
        self.encoding_footer_label.setObjectName("footerLabel")
        footer_layout.addWidget(self.encoding_footer_label)
        footer_layout.addSpacing(6)
        footer_separator = QLabel("／")
        footer_separator.setObjectName("footerLabel")
        footer_layout.addWidget(footer_separator)
        footer_layout.addSpacing(6)
        self.zoom_label = ClickableLabel("100%")
        self.zoom_label.setObjectName("footerLabel")
        self.zoom_label.double_clicked.connect(lambda: self._on_editor_zoom(0))
        footer_layout.addWidget(self.zoom_label)
        editor_layout.addWidget(editor_footer)
        splitter.addWidget(self.editor_card)
        # 卡片內容變寬（本文底部的編碼文字在開檔後才填上、換主題換字型…）就重算視窗最小寬度：
        # 只在開關卡片那一刻算，開檔時自動打開上次的功能卡片會量到還沒填字的本文卡片而偏窄。
        self._card_watcher = _LayoutWatcher(self._update_minimum_width, self)
        for card in (self.side_card, self.tree_card, self.editor_card):
            card.installEventFilter(self._card_watcher)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 3)

        splitter_wrap = QWidget()
        splitter_wrap_layout = QHBoxLayout(splitter_wrap)
        splitter_wrap_layout.setContentsMargins(14, 8, 14, 14)
        splitter_wrap_layout.addWidget(splitter)
        root.addWidget(splitter_wrap, 1)

        self.setCentralWidget(central)
        # 工具列縮成只有圖示時，版面本身的最小寬度是 677；設成 680 剛好塞得進
        # 直立螢幕（200% 縮放下只有 720）。開著功能卡片時卡片要更寬，見 _update_minimum_width。
        self.setMinimumSize(MIN_WINDOW_WIDTH, 420)
        self._show_status("準備就緒")
        self.editor.cursorPositionChanged.connect(self._update_cursor_position_label)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.toggle_find_bar)
        QShortcut(QKeySequence("F5"), self, activated=self.rescan_toc)
        # 視窗層級的快捷鍵：焦點在目錄樹（剛做完右鍵選單操作）時按 Ctrl+Z 也要
        # 能復原。焦點在本文時，本文編輯器會自己攔下這兩組按鍵（見
        # widgets.Editor），不會重複觸發。
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self._undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, activated=self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+L"), self, activated=self.open_log_folder)
        QShortcut(QKeySequence("Esc"), self, activated=self._on_escape)

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("headerBar")
        header.setFixedHeight(64)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 0, 20, 0)
        # 所有按鈕之間用同一個間距，不再分組各自加空白；左右兩群之間只靠
        # 中間的伸縮空間隔開。
        layout.setSpacing(8)

        self.open_button = self._add_text_button(
            layout, "folder-open", "選擇檔案", "", self.open_file, "Ctrl+O", primary=True)
        self.one_click_button = self._add_text_button(
            layout, "wand-sparkles", "一鍵排版", "", self.one_click_format, None, primary=True)
        # 左邊是動作（開檔、一鍵排版），右邊是開關側邊卡片：中間用一條直線分開
        layout.addSpacing(4)
        layout.addWidget(VDivider())
        layout.addSpacing(4)
        # 工具列的按鈕不放滑鼠提示：圖示＋文字已經說明用途
        self.format_toggle_button = self._add_text_button(
            layout, "sliders-horizontal", "排版設定", "",
            lambda: self._toggle_side_panel(self.options_panel), None, checkable=True)
        self.chapter_toggle_button = self._add_text_button(
            layout, "list-tree", "章節管理", "",
            lambda: self._toggle_side_panel(self.chapter_panel), None, checkable=True)
        self.content_toggle_button = self._add_text_button(
            layout, "file-check", "內容檢查", "",
            lambda: self._toggle_side_panel(self.content_panel), None, checkable=True)
        # 尋找／取代（Ctrl+F）也是左側卡片之一；圖示跟內容檢查分開（放大鏡＋文字行）
        self.find_toggle_button = self._add_text_button(
            layout, "text-search", "尋找取代", "", self.toggle_find_bar, None, checkable=True)
        layout.addStretch(1)

        self.undo_button = self._add_header_button(layout, "undo-2", "", self._undo, None)
        self.redo_button = self._add_header_button(layout, "redo-2", "", self._redo, None)
        self.clear_button = self._add_header_button(layout, "eraser", "", self.clear_all, None)
        # 編輯動作（上一步、下一步、清空）跟外觀設定（主題、繁簡）之間一條直線
        layout.addSpacing(4)
        layout.addWidget(VDivider())
        layout.addSpacing(4)
        self.theme_button = ThemeButton(THEMES.values(), "")
        self.theme_button.themeSelected.connect(self.set_theme)
        layout.addWidget(self.theme_button)
        self.language_toggle = LanguageToggle()
        if not i18n.available():
            self.language_toggle.setEnabled(False)
            self.language_toggle.setToolTip("需要安裝 OpenCC 才能切換簡體介面")
        self.language_toggle.toggled.connect(self._on_language_toggled)
        layout.addWidget(self.language_toggle)
        # 匯出 TXT 旁邊的箭頭打開匯出檔名設定；兩顆靠在一起，像同一顆按鈕分成兩半
        save_group = QHBoxLayout()
        save_group.setSpacing(2)
        self.save_button = self._add_text_button(
            save_group, "download", "匯出 TXT", "", self.save_file_as, "Ctrl+S", primary=True)
        self.save_button.setProperty("split", "left")
        self.filename_button = self._add_text_button(
            save_group, "chevron-down", "", "", self.open_filename_dialog, None, primary=True)
        self.filename_button.setProperty("split", "right")
        layout.addLayout(save_group)
        return header

    def _add_header_button(self, layout, icon_name, tooltip, slot, shortcut) -> IconButton:
        button = IconButton(icon_name, tooltip)
        button.setObjectName("toolbarButton")
        if slot is not None:
            button.clicked.connect(slot)
        if shortcut is not None:
            action = QAction(self)
            action.setShortcut(shortcut)
            if slot is not None:
                action.triggered.connect(slot)
            self.addAction(action)
        self._icon_buttons.append(button)
        layout.addWidget(button)
        return button

    def _add_text_button(self, layout, icon_name, text, tooltip, slot, shortcut,
                          *, primary: bool = False, checkable: bool = False) -> IconTextButton:
        button = IconTextButton(icon_name, text, checkable=checkable)
        button.setToolTip(tooltip)
        if primary:
            button.setObjectName("primary")
            self._primary_buttons.append(button)
        else:
            self._panel_toggle_buttons.append(button)
        if slot is not None:
            button.clicked.connect(slot)
        if shortcut is not None:
            action = QAction(self)
            action.setShortcut(shortcut)
            if slot is not None:
                action.triggered.connect(slot)
            self.addAction(action)
        layout.addWidget(button)
        return button

    def _set_document_actions_enabled(self, enabled: bool):
        for button in (self.one_click_button, self.save_button, self.filename_button, self.clear_button,
                       self.format_toggle_button, self.chapter_toggle_button, self.content_toggle_button,
                       self.find_toggle_button):
            button.setEnabled(enabled)
        self.options_panel.set_apply_enabled(enabled)
        self.content_panel.set_actions_enabled(enabled)
        if not enabled:
            self._set_active_side_panel(None)
        self._update_history_buttons()

    def _update_history_buttons(self):
        """上一步／下一步按鈕：沒有可以退回或重做的步驟時變灰。打字到一半、
        還沒被計時器存成一步時，也算有「上一步」可退。"""
        typing = self._typing_checkpoint_timer.isActive()
        self.undo_button.setEnabled(self._history_position > 0 or typing)
        self.redo_button.setEnabled(not typing and self._history_position < len(self._history) - 1)

    # ------------------------------------------------------------------
    # 主題
    # ------------------------------------------------------------------

    @action
    def set_theme(self, name: str):
        """換成指定的主題（主題選單）。"""
        if name not in THEMES:
            return
        self.theme_name = name
        if self.tokens.is_dark:
            self._last_dark_theme = name
        else:
            self._last_light_theme = name
        self._apply_theme()

    @action
    def toggle_theme(self):
        """在最近用過的淺色系與深色系主題之間切換。"""
        self.set_theme(self._last_light_theme if self.tokens.is_dark else self._last_dark_theme)

    def _apply_theme(self):
        tokens = self.tokens
        set_active_tokens(tokens)
        icons.clear_icon_cache()
        chevron_closed = icons.icon_file_path("chevron-right", tokens.icon, 12)
        chevron_open = icons.icon_file_path("chevron-down", tokens.icon, 12)
        check_mark = icons.icon_file_path("check", tokens.accent_text, 13)
        chevron_up = icons.icon_file_path("chevron-up", tokens.icon, 12)
        QApplication.instance().setStyleSheet(
            build_stylesheet(tokens, chevron_closed, chevron_open, check_mark, chevron_up))
        for button in self._icon_buttons:
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        # 滑鼠移上去時圖示跟文字（樣式表）一起變成 icon_hover；不能按的時候
        # 圖示跟文字一樣淡。
        for button in self._panel_toggle_buttons:
            button.set_colors(tokens.icon, tokens.icon_hover, tokens.checked_text, tokens.text_faint)
        # 主要按鈕是常駐彩色底，圖示固定用按鈕文字色，不跟著 hover 變色。
        for button in self._primary_buttons:
            button.set_colors(tokens.primary_text, tokens.primary_text, tokens.primary_text, tokens.text_faint)
        self.chapter_panel.set_icon_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        self.toc_compact_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint, tokens.checked_text)
        self.chapter_panel.set_report_theme(tokens)
        self.options_panel.set_icon_colors(tokens.icon, tokens.primary_text, tokens.checked_text, tokens.icon_hover,
                                           tokens.text_faint)
        self.content_panel.set_colors(tokens)
        self.find_bar.set_theme(tokens)
        self.find_bar.close_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        self._apply_editor_style()
        trailing = QColor(tokens.warn_text)
        trailing.setAlpha(60 if tokens.is_dark else 38)
        self.editor.set_whitespace_colors(tokens.text_faint, trailing)
        self.editor.set_preview_color(tokens.marker_text)
        self.language_toggle.set_colors(tokens.toggle_track, tokens.toggle_knob, tokens.toggle_text,
                                        tokens.toggle_text_inactive, tokens.toggle_shadow, tokens.icon_hover)
        self.theme_button.set_current(self.theme_name)
        # 目錄裡自己上色的項目（推定卷、已剪下）要照新主題重算。
        self._style_virtual_volumes()
        self._style_cut_items()
        # 章名顏色與顯示中的章節標記是畫在本文上的格式，也要照新主題重畫；
        # 目錄不是最新的話，下次重建目錄時就會畫，這裡不必為此重建。
        if hasattr(self, "_toc_text_version") and self._toc_text_version == self._text_version:
            self._apply_title_formats()
        # 繁簡切換鈕跟旁邊的圖示按鈕一樣高（要等樣式表套上 padding 後才量得
        # 準），寬度沿用設計稿的比例（118×52）。
        # 工具列上所有控制項同一個高度（UI_RULES.md）：純圖示按鈕的 sizeHint 比有文字的
        # 按鈕高，「匯出 TXT」夾在圖示按鈕和繁簡切換旁邊就顯得矮一截。
        header_buttons = ([self.open_button, self.one_click_button, self.format_toggle_button,
                           self.chapter_toggle_button, self.content_toggle_button, self.find_toggle_button,
                           self.save_button, self.filename_button,
                           self.undo_button, self.redo_button, self.clear_button, self.theme_button])
        for button in header_buttons:
            button.setMinimumHeight(0)
            button.setMaximumHeight(16777215)
            button.ensurePolished()
        button_height = max(button.sizeHint().height() for button in header_buttons)
        for button in header_buttons:
            button.setFixedHeight(button_height)
        self.language_toggle.setFixedSize(round(button_height * 118 / 52), button_height)
        self._sync_side_panel_widths()
        # 只有尋找列開著時才重搜（反白顏色要跟著主題換）；關著時重搜會把
        # 已經清掉的搜尋反白又畫回本文上。
        if self.find_bar.isVisible():
            self.find_bar.refresh()

    # ------------------------------------------------------------------
    # 開檔／存檔
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 視窗大小、多螢幕與顯示比例
    # ------------------------------------------------------------------

    def _restore_window_geometry(self):
        """還原上次的大小與位置；沒有紀錄就依螢幕可用區域決定。"""
        state = load_window_state()
        if state is not None:
            self.setGeometry(state["x"], state["y"], state["width"], state["height"])
            self._start_maximized = state["maximized"]
        else:
            screen = QGuiApplication.primaryScreen()
            available = screen.availableGeometry() if screen else QRect(0, 0, *DEFAULT_WINDOW_SIZE)
            width = min(DEFAULT_WINDOW_SIZE[0], int(available.width() * 0.9))
            height = min(DEFAULT_WINDOW_SIZE[1], int(available.height() * 0.9))
            self.resize(width, height)
            self.move(available.center().x() - width // 2, available.center().y() - height // 2)
            self._start_maximized = False

    def _current_screen(self):
        handle = self.windowHandle()
        return (handle.screen() if handle is not None else None) or QGuiApplication.primaryScreen()

    def _fit_to_screen(self):
        """把視窗夾回目前螢幕的可用範圍。

        直立螢幕、換螢幕、改顯示比例之後，原本的大小可能比整個桌面還大，
        視窗就會有一部分在畫面外而且拉不回來。"""
        screen = self._current_screen()
        if screen is None or self.isMaximized() or self.isFullScreen():
            return
        available = screen.availableGeometry()
        width = min(self.width(), available.width())
        height = min(self.height(), available.height())
        x = min(max(self.x(), available.left()), available.right() - width + 1)
        y = min(max(self.y(), available.top()), available.bottom() - height + 1)
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _on_screen_changed(self, _screen=None):
        """換螢幕或顯示比例改變：圖示要照新的比例重畫，視窗要夾回可用範圍。"""
        screen = self._current_screen()
        icons.set_device_scale(screen.devicePixelRatio() if screen else None)
        self._connect_screen_signals(screen)
        self._apply_theme()
        self._fit_to_screen()
        self._update_toolbar_compact()

    def _connect_screen_signals(self, screen):
        """只接目前這一台螢幕的訊號，換螢幕時把舊的斷掉。"""
        previous = getattr(self, "_watched_screen", None)
        if previous is screen:
            return
        if previous is not None:
            for signal in (previous.geometryChanged, previous.availableGeometryChanged,
                           previous.logicalDotsPerInchChanged, previous.physicalDotsPerInchChanged):
                try:
                    signal.disconnect(self._on_screen_metrics_changed)
                except (RuntimeError, TypeError):
                    pass
        self._watched_screen = screen
        if screen is not None:
            for signal in (screen.geometryChanged, screen.availableGeometryChanged,
                           screen.logicalDotsPerInchChanged, screen.physicalDotsPerInchChanged):
                signal.connect(self._on_screen_metrics_changed)

    def _on_screen_metrics_changed(self, *_args):
        self._on_screen_changed()

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_screen_watch_ready", False):
            return
        self._screen_watch_ready = True
        handle = self.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._on_screen_changed)
        self._on_screen_changed()
        if getattr(self, "_start_maximized", False):
            self.showMaximized()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_toolbar_compact()

    def _update_toolbar_compact(self):
        """視窗太窄時，工具列上有文字的按鈕改成只顯示圖示。

        整排按鈕不會換行也不會縮，有文字時最小寬度約 1250；只顯示圖示時約 780，
        縮放比較大的小螢幕（800 寬）也放得下。"""
        width = self.width()
        compact = self._toolbar_compact
        if not compact and width < COMPACT_TOOLBAR_WIDTH:
            compact = True
        elif compact and width > FULL_TOOLBAR_WIDTH:
            compact = False
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        # 只顯示圖示時間距、左右留白也收一點：要能塞進 800 寬的視窗
        layout = self.open_button.parentWidget().layout()
        layout.setSpacing(4 if compact else 8)
        margin = 8 if compact else 20
        layout.setContentsMargins(margin, 0, margin, 0)
        for button in (self.open_button, self.one_click_button, self.format_toggle_button,
                       self.chapter_toggle_button, self.content_toggle_button, self.find_toggle_button,
                       self.save_button):
            button.set_compact(compact)

    def _confirm_discard_changes(self) -> bool:
        """換掉目前這份正文之前先問過：可以先匯出、直接捨棄或取消。

        回傳 True 代表可以繼續（已匯出或使用者選擇捨棄）。只看正文有沒有
        改過；標題粗體這種純顯示的格式不算。"""
        if not self._document_dirty:
            return True
        if self._saved_text_hash is not None and hash(self.editor.toPlainText()) == self._saved_text_hash:
            self._document_dirty = False     # 改過又全部復原，等於沒改
            return True
        box = QMessageBox(QMessageBox.Icon.Warning, i18n.T("尚未匯出"),
                          i18n.T("目前的修改還沒有匯出，繼續下去會遺失。要先匯出嗎？"), parent=self)
        save_button = box.addButton(i18n.T("匯出 TXT"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(i18n.T("不匯出，直接繼續"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(i18n.T("取消"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            if not self.save_file_as():
                return False                # 沒匯出（取消或失敗）：留在原地
            if not self._document_dirty:
                return True
            return self._confirm_stripped_export()
        return clicked is discard_button

    def _confirm_stripped_export(self) -> bool:
        """匯出時移除了章節標記：寫出去的檔案重新開啟時沒有手動調整過的目錄，
        工作狀態其實沒有保存下來。不能直接放行（會把這些調整一起丟掉），
        讓使用者決定要不要另存一份保留標記的檔案。"""
        box = QMessageBox(QMessageBox.Icon.Warning, i18n.T("章節標記沒有保存"),
                          i18n.T("剛才匯出的檔案已移除章節標記，重新開啟時不會保留手動調整過的目錄。\n\n"
                                 "要另外存一份保留標記的檔案嗎？"), parent=self)
        keep_button = box.addButton(i18n.T("另存保留標記的檔案"), QMessageBox.ButtonRole.AcceptRole)
        discard_button = box.addButton(i18n.T("不保存，直接繼續"), QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(i18n.T("取消"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(keep_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is keep_button:
            return bool(self.save_file_as(strip_markers=False)) and not self._document_dirty
        return clicked is discard_button

    def closeEvent(self, event):
        # 繁簡轉換為了更新進度會讓 Qt 處理事件，這時按右上角關閉會在轉換到
        # 一半跳出「尚未匯出」詢問；轉換期間直接擋掉，完成後再關。
        if getattr(self, "_long_task_running", False):
            event.ignore()
            self._show_status("轉換進行中，完成後再關閉")
            return
        if not self._confirm_discard_changes():
            event.ignore()
            return
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        save_window_state(geometry.x(), geometry.y(), geometry.width(), geometry.height(),
                          self.isMaximized())
        save_ui_state(self._collect_ui_state())
        event.accept()

    # ------------------------------------------------------------------
    # 記住上次的介面狀態
    # ------------------------------------------------------------------

    def _collect_ui_state(self) -> dict:
        """關閉前的介面狀態。只記「怎麼用這個程式」的偏好，不記跟某個檔案
        綁在一起的東西（編碼、結構、選取的章節…）。"""
        side_panel = ("options" if self.options_panel.isVisible() else
                      "chapter" if self.chapter_panel.isVisible() else
                      "content" if self.content_panel.isVisible() else None)
        state = dict(self._ui_state)      # 各對話框的勾選已經記在這裡
        state.update({
            "theme": self.theme_name,
            "simplified": self.language_toggle.is_simplified(),
            "editor_zoom": self._editor_zoom,
            "show_title_markers": self.marker_button.isChecked(),
            "strip_markers_on_export": self._strip_markers_on_export,
            "show_whitespace": self.content_panel.show_whitespace_toggle.isChecked(),
            "metadata_expanded": self.metadata_bar.toggle_button.isChecked(),
            "side_panel": side_panel if self.raw_lines and any(self.raw_lines) else
            (self._pending_side_panel or side_panel),
            "splitter": bytes(self.splitter.saveState().toHex()).decode("ascii"),
            "side_width": self.side_card.width() if self.side_card.isVisible() else self._side_width,
            "toc_compact_mode": self.toc_compact_mode,
            "format_options": self.options_panel.options_state(),
            "missing_mode": self.chapter_panel.missing_mode(),
            "title_tail_allowed": self.title_tail_allowed,
            "title_tail_custom": self.title_tail_custom,
            "find_regex": self.find_bar.regex_button.isChecked(),
            "mark_colors": sorted(self.content_panel.marking()),
            "infer_volumes": self._infer_volumes,
            "infer_volume_names": self._infer_volume_names,
            "merge_titles": self._merge_titles,
            "skip_duplicate_titles": self._skip_duplicate_titles,
            "disabled_words": sorted(self.disabled_words),
            "special_levels": dict(self.special_levels),
            "max_title_length": self.max_title_length,
            "filename_ongoing": self.filename_ongoing,
            "filename_completed": self.filename_completed,
        })
        return state

    def _restore_ui_state(self):
        """把上次的介面狀態套回來。每一項都獨立檢查，存檔裡缺的或壞的就用預設值。"""
        state = self._ui_state
        # 更早的設定只記「dark_mode」：深色就對應到深色主題。
        name = state.get("theme")
        if name not in THEMES:
            name = DARK.name if state.get("dark_mode") else None
        if name and name != self.theme_name:
            self.set_theme(name)
        if state.get("simplified") and i18n.available():
            self.language_toggle.set_simplified(True)
            self._on_language_toggled(True)
        zoom = state.get("editor_zoom")
        if isinstance(zoom, int) and EDITOR_ZOOM_MIN <= zoom <= EDITOR_ZOOM_MAX and zoom != 100:
            self._editor_zoom = zoom
            self._apply_editor_style()
            self.zoom_label.setText(f"{zoom}%")
        self._strip_markers_on_export = bool(state.get("strip_markers_on_export", True))
        self.content_panel.strip_markers_toggle.blockSignals(True)
        self.content_panel.strip_markers_toggle.setChecked(self._strip_markers_on_export)
        self.content_panel.strip_markers_toggle.blockSignals(False)
        if state.get("show_title_markers"):
            self.marker_button.setChecked(True)
        if state.get("show_whitespace"):
            self.content_panel.show_whitespace_toggle.setChecked(True)
        if state.get("metadata_expanded"):
            self.metadata_bar.toggle_button.setChecked(True)
        self.toc_compact_mode = bool(state.get("toc_compact_mode"))
        self.toc_compact_button.setChecked(self.toc_compact_mode)
        if isinstance(state.get("format_options"), dict):
            self.options_panel.restore_options_state(state["format_options"])
        mode = state.get("missing_mode")
        if isinstance(mode, str) and self.chapter_panel.missing_mode_combo.findText(mode) >= 0:
            i18n.set_combo_value(self.chapter_panel.missing_mode_combo, mode)
        if isinstance(state.get("title_tail_custom"), str):
            self.title_tail_custom = state["title_tail_custom"]
        tail = state.get("title_tail_allowed")
        if isinstance(tail, str):
            self.title_tail_allowed = tail
        elif isinstance(state.get("allowed_tail_chars"), str):
            # 舊設定「標題結尾例外字元」：在預設之外多放行的字
            self.title_tail_allowed = DEFAULT_TITLE_TAIL_ALLOWED + state["allowed_tail_chars"]
        splitter = state.get("splitter")
        if isinstance(splitter, str) and splitter:
            try:
                self.splitter.restoreState(QByteArray.fromHex(splitter.encode("ascii")))
            except (ValueError, UnicodeError):
                pass
        if isinstance(state.get("side_width"), int):
            self._side_width = max(0, state["side_width"])
        if state.get("find_regex"):
            self.find_bar.regex_button.setChecked(True)
        # 左側面板要等有檔案才能開（沒有檔案時那些按鈕是停用的）。
        if state.get("side_panel") in ("options", "chapter", "content"):
            self._pending_side_panel = state["side_panel"]
        self._infer_volumes = bool(state.get("infer_volumes"))
        self._infer_volume_names = bool(state.get("infer_volume_names"))
        self.chapter_panel.set_infer_volumes(self._infer_volumes, self._infer_volume_names)
        self._merge_titles = bool(state.get("merge_titles"))
        self.chapter_panel.set_merge_titles(self._merge_titles)
        self._skip_duplicate_titles = bool(state.get("skip_duplicate_titles"))
        self.chapter_panel.set_skip_duplicates(self._skip_duplicate_titles)
        if isinstance(state.get("disabled_words"), list):
            self.disabled_words = frozenset(str(word) for word in state["disabled_words"])
        if isinstance(state.get("special_levels"), dict):
            self.special_levels = {key: level for key, level in state["special_levels"].items()
                                   if key in SPECIAL_LEVELS and level in (1, 2) and level != SPECIAL_LEVELS[key]}
        if isinstance(state.get("max_title_length"), int) and 10 <= state["max_title_length"] <= 200:
            self.max_title_length = state["max_title_length"]
        for key in ("filename_ongoing", "filename_completed"):
            if isinstance(state.get(key), str) and state[key].strip():
                setattr(self, key, upgrade_template(state[key]))
        marks = state.get("mark_colors")
        if marks is True:
            marks = ["ad", "note"]                   # 第一版只有一個開關
        if isinstance(marks, list):
            # 有檔案之後才會真的掃描、上色
            self.content_panel.set_marking({kind for kind in marks if kind in ("ad", "note")})
        # 還原過程中各項會在狀態列留下訊息，最後統一改回來。
        self._show_status("準備就緒")

    def _open_pending_side_panel(self):
        """第一次開檔後，把上次開著的左側面板打開。"""
        panel = {"options": self.options_panel, "chapter": self.chapter_panel,
                 "content": self.content_panel}.get(self._pending_side_panel)
        self._pending_side_panel = None
        if panel is not None and not self.side_card.isVisible():
            self._set_active_side_panel(panel)

    @action
    def open_file(self):
        if not self._confirm_discard_changes():
            return
        with native_dialog():
            path, _ = QFileDialog.getOpenFileName(
                self, i18n.T("開啟 TXT 檔案"), "", i18n.T("文字檔 (*.txt);;所有檔案 (*)"))
        if not path:
            return
        self.load_file_path(path)

    @action
    def _open_dropped_file(self, path: str):
        """拖曳到本文卡片：跟拖到視窗其他地方一樣，只接受 TXT。"""
        if not path.lower().endswith(".txt"):
            dialogs.error(self, "無法載入", "請拖曳一個有效的 TXT 檔案。")
            return
        if self._confirm_discard_changes():
            self.load_file_path(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(".txt"):
                event.acceptProposedAction()
                if self._confirm_discard_changes():
                    self.load_file_path(path)
                return
        dialogs.error(self, "無法載入", "請拖曳一個有效的 TXT 檔案。")

    @action
    def load_file_path(self, path: str, encoding: str | None = None):
        encoding = encoding or smart_detect_encoding(path)
        try:
            content, damaged, encoding = self._read_document(path, encoding)
        except OSError as error:
            dialogs.error(self, "讀取失敗", f"無法開啟檔案：\n{path}\n\n{error}")
            return
        if content is None:
            return
        content, removed_boms = strip_stray_bom(content)

        self.close_find_bar()
        self._drop_line_caches()
        self.input_file = path
        self.detected_encoding = encoding
        self.raw_lines = content.split("\n")
        self.auto_titles = {}
        self.force_lv1_chapters = set()
        self.force_lv2_chapters = set()
        self.ignored_chapters = set()
        self.structure_mode = DEFAULT_STRUCTURE_MODE
        self.format_options.structure = DEFAULT_STRUCTURE_MODE
        self.toc_full_labels = {}
        self.chapter_raw_map = {}
        self._pending_line_maps = []
        self._missing_report_active = False
        self._cut_state = None
        self.chapter_panel.clear_report()

        self.metadata_bar.reset()
        self.metadata_bar.set_structure(DEFAULT_STRUCTURE_MODE)
        encoding_choice = next((label for label, codec in ENCODING_CODECS.items() if codec == encoding), "自動")
        self.metadata_bar.set_encoding_choice(encoding_choice)

        self._history = []
        self._history_position = -1
        self._set_editor_text(content)
        # 換過整份內容之後游標會留在最後一行，剛開檔卻應該從頭看起。
        self.editor.moveCursor(QTextCursor.MoveOperation.Start)

        self._mark_synced(content)
        self._rebuild_toc()
        self._autofill_book_metadata()
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:      # 讀完之後檔案被移走或改名
            size_mb = len(content.encode(encoding, "replace")) / (1024 * 1024)
        self.metadata_bar.set_filename(os.path.basename(path), f"{size_mb:.2f} MB")
        self.metadata_bar.set_encoding_badge(self.detected_encoding.upper())
        self.encoding_footer_label.setText(
            f"{self.detected_encoding.upper()} · {detect_line_ending(path)}")
        status = i18n.T("已載入：") + os.path.basename(path)
        if damaged:
            status += i18n.T(f"；有 {damaged} 個字元無法以 {encoding.upper()} 解碼（顯示為 �），存檔會永久遺失")
        if removed_boms:
            status += i18n.T(f"；已移除 {removed_boms} 個夾在行首的 BOM 字元（多檔串接留下的，會讓章節辨識失敗）")
        self._show_status(status, translated=True)
        log.info("載入 %s：%.2f MB、編碼 %s、%d 行、目錄 %d 項（推定卷 %d）、移除 BOM %d 個",
                 os.path.basename(path), size_mb, encoding, len(self.raw_lines), len(self.chapter_raw_map),
                 len(self.virtual_volume_items), removed_boms)
        self._set_document_actions_enabled(True)
        if self._pending_side_panel:
            self._open_pending_side_panel()
        self._checkpoint_document()   # 建立復原歷史的第一步（載入後的初始狀態）
        self._document_dirty = False
        self._saved_text_hash = hash(content)
        self._warm_scan_caches()

    def _warm_scan_caches(self):
        """利用空檔分批先算好各種掃描的逐行判斷（core/scan_cache.py），第一次開掃描視窗才不會卡。
        不用背景執行緒：跟畫面搶 Python 的執行權，開檔後第一次畫面反而會慢一秒多。
        每批幾毫秒、開檔後等一下才開始；換了檔案就從新的檔案重來。"""
        self._warm_lines = list(self.raw_lines)
        self._warm_position = 0
        self._warm_timer.start(WARM_START_DELAY_MS)

    def _drop_line_caches(self):
        self._warm_timer.stop()
        self._warm_lines = None
        clear_line_caches()

    def _warm_step(self):
        lines = self._warm_lines
        if lines is None:
            return
        if self._warm_position >= len(lines):
            self._warm_lines = None
            freeze_line_caches()
            return
        end = self._warm_position + WARM_CHUNK_LINES
        warm_line_caches(lines[self._warm_position:end])
        self._warm_position = end
        self._warm_timer.start(0)

    def _read_document(self, path: str, encoding: str):
        """先嚴格解碼；編碼不符時問過使用者才容錯開啟。

        不預設容錯：解不開的位元組會變成「�」，一旦照這樣編輯、匯出，原本
        的字就真的沒了，而且使用者不一定看得出來。回傳（內容, 壞掉的字元數），
        內容為 None 代表使用者選擇不開啟。回傳（內容, 壞字數, 實際用的編碼）：
        解碼失敗時可以直接換一種編碼重讀同一個檔。"""
        while True:
            try:
                return read_text(path, encoding), 0, encoding
            except (UnicodeError, LookupError) as error:
                log.warning("以 %s 嚴格解碼失敗：%s", encoding, error)
            choices = [label for label, codec in ENCODING_CODECS.items() if codec != encoding]
            choice, label = dialogs.decode_failure(self, os.path.basename(path), encoding, choices)
            if choice == "retry" and label in ENCODING_CODECS:
                encoding = ENCODING_CODECS[label]
                continue
            if choice != "lossy":
                self._show_status("已取消開啟：編碼可能不符")
                return None, 0, encoding
            try:
                content, damaged = read_text_lossy(path, encoding)
                return content, damaged, encoding
            except (UnicodeError, LookupError) as error:
                dialogs.error(self, "讀取失敗", f"無法以 {encoding.upper()} 讀取檔案：\n{error}")
                return None, 0, encoding

    def _autofill_book_metadata(self):
        """書名／作者／狀態只在還沒填過時才自動帶入，不覆蓋使用者已輸入的值。"""
        if self.metadata_bar.book_title():
            return
        filename_title, filename_author, filename_status = extract_filename_metadata(self.input_file)
        self.metadata_bar.set_title_if_empty(filename_title)
        self.metadata_bar.set_author_if_empty(filename_author)
        if not self.metadata_bar.author():
            intro_author = extract_author_from_intro(self.raw_lines)
            self.metadata_bar.set_author_if_empty(intro_author)
        self.metadata_bar.set_status(filename_status)

    @action
    def _on_structure_changed(self, value: str):
        if not self.input_file:
            return
        self.structure_mode = value
        self.format_options.structure = value
        self.rescan_toc()

    @action
    def _on_encoding_changed(self, choice: str):
        """換編碼＝整份重新讀檔，會丟掉目前的修改，所以要先問過。"""
        if not self.input_file:
            return
        if not self._confirm_discard_changes():
            # 使用者取消：下拉選單要轉回原本的編碼，不然顯示的跟實際讀的不一致。
            current = next((label for label, codec in ENCODING_CODECS.items()
                            if codec == self.detected_encoding), "自動")
            self.metadata_bar.set_encoding_choice(current)
            return
        encoding = smart_detect_encoding(self.input_file) if choice == "自動" else ENCODING_CODECS[choice]
        self.load_file_path(self.input_file, encoding=encoding)

    def _set_editor_text(self, text: str, map_row=None):
        """換掉整份內容（行距一起套，避免分兩階段變化）。

        map_row：舊行號 → 新行號。有給就把游標與畫面最上面那一行放回原本的內容上，
        不然整份替換後游標會跑到最後一行；"diff" 表示比對新舊兩版自己算。"""
        if map_row is not None and self._typing_checkpoint_timer.isActive() and not self._restoring_history:
            # 剛打的字還沒存成一步：先存起來，這次整份替換（全部取代、工具視窗…）才是獨立的一步，
            # 復原時不會連打的字一起退掉。
            self._checkpoint_document()
        view = old_lines = None
        if map_row is not None and self.editor.document().characterCount() > 1:
            view = self._capture_view()
            if map_row == "diff":
                old_lines = self.editor.toPlainText().split("\n")
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.select(QTextCursor.SelectionType.Document)
        block_format = QTextBlockFormat()
        block_format.setLineHeight(150, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(block_format)
        cursor.endEditBlock()
        if view is not None:
            if map_row == "diff":
                map_row = _diff_line_mapper(_line_opcodes(old_lines, text.split("\n")))
            self._restore_view(view, map_row)

    def _capture_view(self) -> tuple:
        cursor = self.editor.textCursor()
        return cursor.blockNumber(), cursor.positionInBlock(), self.editor.firstVisibleBlock().blockNumber()

    def _restore_view(self, view: tuple, map_row):
        """游標放回對應的那一行（同一欄，行變短就放行尾），畫面最上面也對回原本那一行。"""
        row, column, top = view
        document = self.editor.document()
        last = document.blockCount() - 1
        top_block = document.findBlockByNumber(max(0, min(last, map_row(top))))
        block = document.findBlockByNumber(max(0, min(last, map_row(row))))
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + max(0, min(column, block.length() - 1)))
        self.editor.setTextCursor(cursor)
        self.editor.verticalScrollBar().setValue(top_block.firstLineNumber())

    # ------------------------------------------------------------------
    # 復原／重做：整份文字＋章節結構狀態的快照
    # ------------------------------------------------------------------

    def _on_editor_text_changed(self):
        """打字時每個按鍵都會觸發；用計時器合併成「停頓 450ms 才存一步」，
        不然復原歷史會被逐字灌爆。結構性操作（合併／整理／連續編號…）自己
        會在動作結束時立刻呼叫 _checkpoint_document，不用等這個計時器。"""
        if self._applying_formats:
            return          # 只是套標題粗體／隱藏標記，正文一個字都沒變
        self._text_version += 1
        self._document_dirty = True
        if self.find_bar.isVisible():
            # 搜尋結果記的是字元位置，正文一變就全部作廢，不能再拿去取代。
            self.find_bar.invalidate()
        if self.content_panel.marking():
            self._mark_timer.start(MARK_SCAN_DELAY_MS)   # 停一下才在背景重掃，不是每打一個字就掃
        if self._restoring_history:
            return
        self._typing_checkpoint_timer.start(TYPING_CHECKPOINT_DELAY_MS)
        self._update_history_buttons()

    @timed
    def _checkpoint_document(self):
        """把目前的正文與章節結構存成一步；跟最新一步完全相同就不重複存。"""
        if self._restoring_history:
            return
        # 這一步已經包含還沒被計時器存起來的輸入，計時器不用再跑。
        self._typing_checkpoint_timer.stop()
        # 先把「記在第幾行」的章節狀態對到目前的文字，否則快照會是「新的文字
        # 配舊的行號」，復原之後強制層級、忽略標記會落在別行。
        self._sync_raw_lines()
        text = (self._synced_text if self._synced_text_version == self._text_version
                and self._synced_text is not None else self.editor.toPlainText())
        state = (
            text,
            frozenset(self.ignored_chapters),
            frozenset(self.force_lv1_chapters),
            frozenset(self.force_lv2_chapters),
            dict(self.auto_titles),
        )
        if self._history_position >= 0 and self._history[self._history_position] == state:
            self._update_history_buttons()
            return
        del self._history[self._history_position + 1:]
        self._history.append(state)
        self._trim_history()
        self._history_position = len(self._history) - 1
        self._update_history_buttons()

    def _trim_history(self):
        """同時套用步數上限與記憶體預算，從最舊的步驟開始丟棄。"""
        if len(self._history) > MAX_HISTORY_STEPS:
            del self._history[:len(self._history) - MAX_HISTORY_STEPS]
        while len(self._history) > MIN_HISTORY_STEPS:
            total = sum(len(step[0]) for step in self._history)
            if total <= MAX_HISTORY_CHARS:
                break
            del self._history[0]

    @action
    def _undo(self):
        self._restore_document_step(-1)

    @action
    def _redo(self):
        self._restore_document_step(1)

    @timed
    def _restore_document_step(self, direction: int):
        self._typing_checkpoint_timer.stop()
        # 復原／重做前先把「正在輸入到一半、還沒被計時器存檔」的內容存起來，
        # 不然這段修改會直接消失，也跳不回來。
        self._checkpoint_document()
        target = self._history_position + direction
        if not 0 <= target < len(self._history):
            self._show_status("沒有上一步了" if direction < 0 else "沒有下一步了")
            return
        state = self._history[target]
        self._restoring_history = True
        try:
            self._set_editor_text(state[0], "diff")
            # 先記下行號位移（目錄選取要用），再用快照整組覆蓋章節狀態。
            self._adopt_lines(state[0].split("\n"), remap_state=False)
            self.ignored_chapters = set(state[1])
            self.force_lv1_chapters = set(state[2])
            self.force_lv2_chapters = set(state[3])
            self.auto_titles = dict(state[4])
            self._history_position = target
            self._rebuild_toc()
        finally:
            self._restoring_history = False
        self._update_history_buttons()
        self._show_status("已回到上一步" if direction < 0 else "已重做下一步")

    def _on_editor_zoom(self, step: int):
        """Ctrl＋滾輪：本文字級每格 10%，範圍 50%～300%；只影響顯示。"""
        zoom = 100 if step == 0 else min(EDITOR_ZOOM_MAX, max(EDITOR_ZOOM_MIN, self._editor_zoom + step * 10))
        if zoom == self._editor_zoom:
            return
        self._editor_zoom = zoom
        self._apply_editor_style()
        self.zoom_label.setText(f"{zoom}%")
        self._format_refresh_timer.start()

    def _apply_editor_style(self):
        """本文的字級與文字選取色。

        字級是整份樣式表的 * 規則給的，對元件呼叫 setFont／zoomIn 會被樣式表
        蓋掉；只有元件自己的樣式表比全域的優先，所以用這個方式改。

        文字反白一律是搜尋「目前這一筆」的橘色：搜尋、從對話框跳到某一行、
        平常用滑鼠選字，看起來都是同一種反白。"""
        tokens = self.tokens
        rules = [f"font-size: {round(EDITOR_BASE_FONT_PX * self._editor_zoom / 100)}px;",
                 f"selection-background-color: {tokens.find_current_bg};",
                 f"selection-color: {tokens.text};"]
        self.editor.setStyleSheet(" ".join(rules))

    def _positions(self) -> PositionMap:
        """目前正文的「Python 位置 ↔ Qt 位置」換算表（依文字版本快取）。"""
        if self._position_map_version != self._text_version:
            self._position_map = PositionMap(self.editor.toPlainText())
            self._position_map_version = self._text_version
        return self._position_map

    def _text_version_now(self) -> int:
        return self._text_version

    def _ensure_toc_current(self):
        """正文行數變過之後，目錄記的行號就指到別行了；先重建目錄再動手。

        重建時會依行號位移把原本選取的章節對到新節點（見 _capture_tree_view），
        所以使用者的選取不會跑掉。"""
        if self._toc_text_version != self._text_version:
            self._rebuild_toc()

    def _update_cursor_position_label(self):
        cursor = self.editor.textCursor()
        self.cursor_position_label.setText(i18n.T("第 {line} 行 · 第 {column} 欄").format(
            line=cursor.blockNumber() + 1, column=cursor.positionInBlock() + 1))
        self._update_breadcrumb(cursor.blockNumber())

    @action
    def _on_whitespace_toggled(self, enabled: bool):
        """內容檢查卡片的「顯示內文空格」；狀態列說明只在使用者自己切換時顯示（clicked）。"""
        self.editor.set_show_whitespace(enabled)

    def _on_format_option_toggled(self, label: str, on: bool, description: str):
        """排版設定的開關：切換之後說一句它會做什麼（開關本身不放滑鼠提示）。"""
        if on:
            message = i18n.T(f"已開啟「{label}」：{description}（按「套用格式」才會動到本文）")
        else:
            message = i18n.T(f"已關閉「{label}」")
        self._show_status(message, translated=True)

    # ------------------------------------------------------------------
    # 介面繁／簡
    # ------------------------------------------------------------------

    @action
    def _on_language_toggled(self, simplified: bool):
        """只換介面文字，不動本文與目錄；匯出檔名的繁簡預設跟著換，仍可在
        「書籍資料」裡另外指定。"""
        i18n.set_simplified(simplified)
        i18n.install_qt_translation(QApplication.instance())
        for widget in QApplication.topLevelWidgets():
            if widget.isVisible() or widget is self:
                i18n.retranslate(widget)
        self.filename_script = SCRIPT_SIMP if simplified else SCRIPT_TRAD
        self.metadata_bar.refresh_language()
        self.chapter_panel.refresh_language()
        self._update_cursor_position_label()
        self._style_virtual_volumes()
        self._show_status("介面已切換為簡體" if simplified else "介面已切換為繁體")

    def open_log_folder(self):
        """Ctrl+Shift+L：打開記錄檔資料夾（回報問題時附上 app.log、faults.log）。"""
        app_log.LOG_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_log.LOG_DIR)))

    def _show_unhandled_error(self, summary: str):
        """程式內部錯誤：記錄檔已經寫好了，這裡只告訴使用者去哪裡找。"""
        box = QMessageBox(QMessageBox.Icon.Critical, i18n.T("發生未預期的錯誤"),
                          i18n.T("剛才的操作沒有完成，詳細內容已寫入記錄檔：\n")
                          + f"{app_log.LOG_FILE}\n\n{summary}", parent=self)
        open_button = box.addButton(i18n.T("開啟記錄檔資料夾"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(i18n.T("確定"), QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is open_button:
            self.open_log_folder()

    def _show_status(self, message: str, *, translated: bool = False):
        """狀態列訊息一律經過這裡，才會跟著介面切換繁簡。"""
        self.statusBar().showMessage(message if translated else i18n.T(message))

    def _update_breadcrumb(self, line_number: int):
        """本文卡片標題列最右邊顯示游標目前落在哪一卷／哪一章。"""
        position = bisect.bisect_right(self._breadcrumb_rows, line_number + 1) - 1
        current = self._breadcrumb_items[position] if position >= 0 else None
        if current is self._breadcrumb_current:
            return          # 還在同一章，連文字都不用重設
        self._breadcrumb_current = current
        if current is None:
            self.breadcrumb_label.setText("")
            return
        names = []
        node = current
        while node is not None:
            names.insert(0, self.toc_full_labels.get(node, node.text(0)))
            node = node.parent()
        self.breadcrumb_label.setText(" / ".join(names))

    def _reveal_current_chapter(self):
        """目錄裡選到游標所在的那一章、捲到看得見；目錄過期就先重建。"""
        self._ensure_toc_current()
        self._breadcrumb_current = None
        self._update_breadcrumb(self.editor.textCursor().blockNumber())
        item = self._breadcrumb_current
        if item is None:
            return
        self.tree.clearSelection()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item, QTreeWidget.ScrollHint.PositionAtCenter)
        self._show_status(i18n.T("目錄已選到：") + self.toc_full_labels.get(item, item.text(0)), translated=True)

    @action
    def save_file_as(self, *, strip_markers: bool | None = None):
        """匯出 TXT。strip_markers 為 None 時照「匯出時移除章節標記」的設定。
        只能用名稱傳：按鈕的 clicked 會帶一個 checked=False 進來，當成位置參數就蓋掉設定了。"""
        content = self.editor.toPlainText()
        if not content.strip():
            return False
        default_name = self._suggest_export_filename()
        with native_dialog():
            path, _ = QFileDialog.getSaveFileName(self, i18n.T("另存新檔"), default_name, i18n.T("文字檔 (*.txt)"))
        if not path:
            return False
        stripped = 0
        if self._strip_markers_on_export if strip_markers is None else strip_markers:
            content, stripped = strip_export_markers(content)
        try:
            # 寫暫存檔、成功才取代目標檔：中途失敗時原本的檔案不會被清空。
            write_text_atomic(path, content)
        except (OSError, UnicodeError) as error:
            dialogs.error(self, "存檔失敗", f"無法寫入檔案：\n{path}\n\n{error}\n\n原本的檔案沒有被更動。")
            return False
        if stripped:
            # 寫出去的內容跟編輯器裡的不一樣（少了標記），所以這次不算「已存檔」：
            # 關閉前還是會提醒一次，要留住目錄狀態的話可以再存一份沒移除的。
            note = i18n.T("（已移除 %d 個章節標記，這份檔案重新開啟時不會保留手動調整過的"
                          "目錄；本文仍算未存檔）")
            self._show_status(i18n.T("已匯出：") + path + note % stripped, translated=True)
            return True
        self._document_dirty = False
        self._saved_text_hash = hash(content)
        self._show_status(i18n.T("已匯出：") + path, translated=True)
        return True

    def _filename_fields(self) -> dict:
        """匯出檔名的變數：書籍資料的欄位，加上目錄裡的卷數與番外章數
        （章名有「番外」、或放在名稱有「番外」的卷底下的章都算）。"""
        extra = volumes = 0
        for item, record in self.chapter_records.items():
            if record.get("kind") == "volume":
                volumes += 1
            elif record.get("kind") == "chapter":
                parent = item.parent()
                parent_title = self.chapter_records.get(parent, {}).get("title", "") if parent is not None else ""
                if record.get("prefix") == "番外" or "番外" in record.get("title", "") or "番外" in parent_title:
                    extra += 1
        bar = self.metadata_bar
        return filename_fields(bar.book_title(), bar.author(), bar.status(), bar.last_vol_text(),
                               bar.last_ch_text(), extra, volumes)

    def _suggest_export_filename(self) -> str:
        if not self.metadata_bar.book_title():
            return os.path.basename(self.input_file) if self.input_file else "未命名.txt"
        template = filename_template_for(self.metadata_bar.status(), self.filename_ongoing, self.filename_completed)
        return convert_script(build_smart_filename(self._filename_fields(), template), self.filename_script)

    @action
    def open_filename_dialog(self):
        dialog = FilenameDialog(self.filename_ongoing, self.filename_completed, self.filename_script,
                                self._filename_fields(), self.metadata_bar.status(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.filename_ongoing, self.filename_completed = dialog.result_ongoing, dialog.result_completed
        self.filename_script = dialog.result_script
        self._show_status(i18n.T("匯出檔名：") + self._suggest_export_filename(), translated=True)

    # ------------------------------------------------------------------
    # 目錄樹
    # ------------------------------------------------------------------

    @action
    def rescan_toc(self):
        """依目前編輯器內容重新辨識章節（含重新學習文件規律）。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._force_last_found = True
        try:
            self._rebuild_toc()
        finally:
            self._force_last_found = False
        self._show_status("已重新掃描目錄")

    @action
    def clear_all(self):
        """清空目前檔案與所有章節標記狀態，回到剛啟動時的樣子。

        復原歷史整個重設（不是疊加一筆「清空後」的快照）：
        清空之後沒有「上一步」可以復原，這是刻意的——清空前的內容已經
        用「另存新檔」或原始檔案保住了，不需要靠復原堆疊撐著。
        """
        if not self.input_file and not self.editor.toPlainText().strip():
            return
        if self._document_dirty:
            if not self._confirm_discard_changes():
                return
        elif not dialogs.confirm(self, "清空重來", "確定要清空目前的內容與所有章節標記狀態嗎？"):
            return
        self.close_find_bar()
        self._drop_line_caches()
        self.input_file = ""
        self.raw_lines = [""]
        self.chapter_index_map = {}
        self.chapter_raw_map = {}
        self.chapter_records = {}
        self.toc_boundary_map = {}
        self.auto_titles = {}
        self.force_lv1_chapters = set()
        self.force_lv2_chapters = set()
        self.ignored_chapters = set()
        self.tree.clear()
        self._cut_state = None
        self._breadcrumb_rows = []
        self._breadcrumb_items = []
        self._breadcrumb_current = None
        self._mark_synced("")
        self.virtual_volume_items = {}
        self.split_volume_items = set()
        self.merged_titles = {}
        self.merged_title_items = set()
        self.absorbed_titles = {}
        self.absorbed_title_items = set()
        self._missing_report_active = False
        self.chapter_panel.clear_report()
        self._set_editor_text("")
        self.toc_full_labels = {}
        self.toc_compact_labels = {}
        self._pending_line_maps = []
        self.metadata_bar.reset()
        self.breadcrumb_label.setText("")
        self.encoding_footer_label.setText("")
        self._history = []
        self._history_position = -1
        self._checkpoint_document()
        self._document_dirty = False
        self._saved_text_hash = hash("")
        self._set_document_actions_enabled(False)
        self._show_status("已清空，可以重新選擇檔案")

    def _title_check(self):
        return build_title_check(self.title_tail_allowed, self.title_tail_custom, self.max_title_length)

    def _build_context(self) -> BuildContext:
        return BuildContext(
            raw_lines=self.raw_lines,
            options=self.format_options,
            user_chapter_rules=self.user_chapter_rules,
            auto_titles=self.auto_titles,
            force_lv1_chapters=self.force_lv1_chapters,
            force_lv2_chapters=self.force_lv2_chapters,
            ignored_chapters=self.ignored_chapters,
            invalid_tail_regex=self._title_check(),
            infer_volumes=self._infer_volumes,
            infer_volume_names=self._infer_volumes and self._infer_volume_names,
            merge_titles=self._merge_titles,
            disabled_words=self.disabled_words,
            special_levels=self.special_levels,
            skip_duplicate_titles=self._skip_duplicate_titles,
        )

    @timed
    def _rebuild_toc(self):
        ctx = self._build_context()
        result = build_document_structure(ctx, apply_format=False, write_text=False)
        self._populate_tree(result)
        self._warn_timed_out_rules()

    def _warn_timed_out_rules(self):
        """自訂規則的正則在期限內跑不完：這次開啟期間已停用，告訴使用者是哪一條。"""
        names = pop_timed_out_rules()
        if names:
            self._show_status(f"自訂章節規則「{'、'.join(names)}」執行太久，已暫停使用；"
                              "請到「自訂章節規則」修改寫法")

    def _label_path(self, item) -> tuple:
        path = []
        while item is not None:
            path.append(self.toc_full_labels.get(item, item.text(0)))
            item = item.parent()
        return tuple(reversed(path))

    def _capture_tree_view(self):
        """重畫目錄前記下畫面狀態：哪些節點展開、選了哪些章節、捲到哪裡。

        選取用「正文行號」記（再經過這段期間的行號位移換算），不是用節點
        物件——重畫後節點全是新的。展開狀態用「標題路徑」記，行號位移不影響。
        """
        expanded = {self._label_path(item): item.isExpanded()
                    for item in self.toc_full_labels if item.childCount()}
        # 同一行可能同時是卷和章（「第一卷 山河 第1章 開始」），只記行號的話
        # 還原時分不出是哪一個。另外記節點在目錄裡的深度。
        def key(item):
            return self._map_tree_line(self.chapter_raw_map[item]), _tree_depth(item)

        current = self.tree.currentItem()
        return {
            "expanded": expanded,
            "current": key(current) if current in self.chapter_raw_map else None,
            "selected": [key(item) for item in self.tree.selectedItems() if item in self.chapter_raw_map],
            "scroll": self.tree.verticalScrollBar().value(),
        }

    def _restore_tree_view(self, view):
        for item in self.toc_full_labels:
            if item.childCount():
                state = view["expanded"].get(self._label_path(item))
                if state is not None:
                    item.setExpanded(state)
        if not view["selected"] and view["current"] is None:
            return
        ordered = sorted(self.chapter_raw_map.items(), key=lambda pair: pair[1])
        rows = [row for _item, row in ordered]
        by_row: dict = {}
        for item, row in self.chapter_raw_map.items():
            by_row.setdefault(row, []).append(item)

        def nearest(key):
            # 章節還在就選回同一個（同一行有卷有章時選深度一樣的）；
            # 被刪掉了就選它前面最近的那一個。
            line, level = key
            same_row = by_row.get(line)
            if same_row:
                return min(same_row, key=lambda item: abs(_tree_depth(item) - level))
            position = bisect.bisect_right(rows, line) - 1
            return ordered[max(position, 0)][0] if ordered else None

        current = nearest(view["current"]) if view["current"] is not None else None
        # 設定目前項目時 Qt 會自動捲過去，捲的時候順便把收合的上層展開——
        # 使用者收起的卷就被打開了。還原期間先關掉自動捲動。
        self.tree.setAutoScroll(False)
        try:
            if current is not None:
                self.tree.setCurrentItem(current)
            for line in view["selected"]:
                item = nearest(line)
                if item is not None:
                    item.setSelected(True)
        finally:
            self.tree.setAutoScroll(True)
        self.tree.verticalScrollBar().setValue(view["scroll"])
        # scrollToItem 會把收合的上層自動展開，使用者收起的卷就被打開了；
        # 選到的章節藏在收合的卷底下時，只還原捲動位置就好。
        parent = current.parent() if current is not None else None
        while parent is not None and parent.isExpanded():
            parent = parent.parent()
        if current is not None and parent is None:
            self.tree.scrollToItem(current, QTreeWidget.ScrollHint.EnsureVisible)

    @timed
    def _populate_tree(self, result):
        view = self._capture_tree_view() if self.toc_full_labels else None
        self.tree.clear()
        node_map = {}

        def populate(src_parent, dst_parent):
            for child in result.tree.get_children(src_parent):
                item = QTreeWidgetItem([result.tree.item(child, "text")])
                if dst_parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    dst_parent.addChild(item)
                item.setExpanded(bool(result.tree.item(child, "open")))
                node_map[child] = item
                populate(child, item)

        populate("", None)
        self.virtual_volume_items = {node_map[n]: dict(info) for n, info in result.virtual_volumes.items()}
        # 拆出來的卷：目錄上照常是卷，但本文還沒有卷標題 → 跟推定卷一樣用非原文色
        self.split_volume_items = {node_map[n] for n in result.split_volumes if n in node_map}
        self.chapter_raw_map = {node_map[n]: row for n, row in result.chapter_raw_map.items()}
        # 合併下行標題（預覽）：目錄上的標題已經接上章名，本文還沒有 → 也用非原文色
        self.merged_titles = dict(result.merged_titles)
        self.merged_title_items = {item for item, row in self.chapter_raw_map.items() if row in self.merged_titles}
        self.absorbed_titles = dict(result.absorbed_titles)
        kept_rows = set(self.absorbed_titles.values())
        self.absorbed_title_items = {item for item, row in self.chapter_raw_map.items() if row in kept_rows}
        self.chapter_index_map = {node_map[n]: row for n, row in result.chapter_index_map.items()}
        self.chapter_records = {node_map[n]: dict(record) for n, record in result.chapter_records.items()}
        self.toc_boundary_map = dict(self.chapter_index_map)

        self._toc_text_version = self._text_version
        # 麵包屑要靠「行號 → 章節」查表；先排好序，游標移動時就只要二分搜尋，
        # 不必每次掃過全部章節。
        self._breadcrumb_rows = sorted(self.chapter_index_map.values())
        rows_to_item: dict = {}
        for item, row in self.chapter_index_map.items():
            # 同一行有卷有章時用最深的那個（章）：麵包屑本來就會帶出上層的卷。
            current = rows_to_item.get(row)
            if current is None or _tree_depth(item) > _tree_depth(current):
                rows_to_item[row] = item
        self._breadcrumb_items = [rows_to_item[row] for row in self._breadcrumb_rows]
        self._breadcrumb_current = None
        self.breadcrumb_label.setText("")
        self.toc_full_labels = {item: item.text(0) for item in node_map.values()}
        self.toc_compact_labels = {item: short_toc_label(label) for item, label in self.toc_full_labels.items()}
        self._apply_toc_label_mode()
        self._pending_line_maps = []
        if view is not None:
            self._restore_tree_view(view)
        self._apply_title_formats()
        self._apply_merge_preview()
        self._style_virtual_volumes()
        self._style_cut_items()
        self.metadata_bar.set_last_found(result.last_found_vol, result.last_found_ch,
                                         force=self._force_last_found)
        if self._missing_report_active:
            self._refresh_missing_report()
        # 目錄剛重建，行號跟本文一致；麵包屑照游標現在的位置重新顯示一次，
        # 不然改了章名、刪了章節之後還是舊的字，要移動游標才會更新。
        self._update_breadcrumb(self.editor.textCursor().blockNumber())
        self._toc_hint_timer.start()

    def _update_toc_hint(self):
        """目錄卡片上面的提示，按一下把漏掉的章節收進來：
        目錄是空的或只有幾項——本文裡最多的一種常用寫法（高信心的可疑章節至少 3 行、目錄項數的五倍），
        加成辨識章節的組合；
        後半本換了寫法——最後一章之後的常用寫法章號接著往下數，一樣加成組合；接不上號、但後面還有
        一大段、裡面有同一種寫法的可疑章節（「2-1」這種沒有內建組合的），打開可疑章節。"""
        self._toc_hint_template = None
        self._toc_hint_format = None
        if not any(line.strip() for line in self.raw_lines):
            self.toc_hint.hide()
            return
        text = None
        # 目錄只有幾項（整本其實是另一種寫法，只認到零星幾個「第N章」）：看整本最多的常用寫法
        if len(self.chapter_raw_map) <= 5:
            counts = Counter(candidate["format"] for candidate in
                             scan_chapter_candidates(self.raw_lines, set(self.chapter_raw_map.values()),
                                                     self.max_title_length)
                             if candidate["confidence"] == "高" and candidate["format"].startswith("preset:"))
            template_ids = {key for key, _level, _blocks in TEMPLATES}
            found = next(((fmt.split(":", 1)[1], count) for fmt, count in counts.most_common()
                          if fmt.split(":", 1)[1] in template_ids
                          and count >= max(3, 5 * len(self.chapter_raw_map))), None)
            if found is not None:
                self._toc_hint_template = found[0]
                text = f"本文有 {found[1]} 行是「{TEMPLATE_LABELS[found[0]]}」這種寫法"
        if text is None and self.chapter_raw_map:
            text = self._toc_tail_hint()
        if text is None:
            self.toc_hint.hide()
            return
        i18n.set_text(self.toc_hint_label, text)
        i18n.set_text(self.toc_hint_button, "查看可疑章節" if self._toc_hint_format else "加入辨識章節")
        self.toc_hint.show()

    def _toc_tail_hint(self):
        """最後一個目錄項目之後漏掉的章節（core.collection.missed_tail_chapters）。"""
        last_row = max(self.chapter_raw_map.values())
        chapters = sorted((row, self.chapter_records.get(item, {}).get("number"))
                          for item, row in self.chapter_raw_map.items()
                          if self.chapter_records.get(item, {}).get("kind") == "chapter")
        missed = missed_tail_chapters(self.raw_lines, chapters, last_row, self.max_title_length)
        if missed is None:
            return None
        last_number = chapters[-1][1]
        name = (f"第{int(last_number) if float(last_number).is_integer() else last_number}章"
                if isinstance(last_number, (int, float)) else "最後一章")
        if missed["kind"] == "template":
            self._toc_hint_template = missed["key"]
            return f"目錄到{name}為止，後面有 {missed['count']} 行是「{TEMPLATE_LABELS[missed['key']]}」這種寫法"
        self._toc_hint_format = missed["format"]
        return f"目錄到{name}為止，後面還有 {missed['count']} 行像章節標題"

    @action
    def _accept_toc_hint(self):
        if self._toc_hint_format:
            fmt = self._toc_hint_format
            self.open_rules_dialog()
            dialog = self._tool_dialogs.get("rules")
            if dialog is not None:
                dialog.show_candidates(fmt)
            return
        if not self._toc_hint_template:
            return
        level, blocks = template(self._toc_hint_template)
        rule = block_rule(blocks, level)
        if all(existing.get("pattern") != rule["pattern"] for existing in self.user_chapter_rules):
            self.user_chapter_rules = list(self.user_chapter_rules) + [rule]
            _save_json(RULES_FILE, self.user_chapter_rules)
        # 辨識的設定變了：排版時記下的自動標題照新的設定重新辨識（作品名稱照舊）
        self.auto_titles = {row: record for row, record in self.auto_titles.items() if record.get("kind") == "work"}
        self.toc_hint.hide()
        self.rescan_toc()
        self._show_status(f"已加入辨識章節的組合「{rule['name']}」，目錄收進 {len(self.chapter_raw_map)} 項")

    def _style_virtual_volumes(self):
        """推定卷用斜體＋「非原文色」（跟顯示中的章節標記同色）：本文裡沒有這個卷標題，
        是推算出來的（UI_RULES.md）。"""
        tokens = self.tokens
        tooltip = i18n.T("推算出來的卷：本文沒有這個卷標題。\n"
                         "確認沒問題後，在「章節管理」按「套用到本文」寫進本文。")
        for item in list(self.virtual_volume_items) + list(self.split_volume_items):
            font = item.font(0)
            font.setItalic(True)
            item.setFont(0, font)
            item.setForeground(0, QColor(tokens.marker_text))
            item.setToolTip(0, tooltip)
        merged_tip = i18n.T("接上了下一行的章名（預覽）：本文還沒改。\n"
                            "確認沒問題後，在「章節管理」按「套用到本文」寫進本文。")
        for item in self.absorbed_title_items:
            font = item.font(0)
            font.setItalic(True)
            item.setFont(0, font)
            item.setForeground(0, QColor(tokens.marker_text))
            item.setToolTip(0, i18n.T("後面重複的標題（預覽）會併進這一章；按「套用到本文」才刪掉那一行。"))
        for item in self.merged_title_items:
            font = item.font(0)
            font.setItalic(True)
            item.setFont(0, font)
            item.setForeground(0, QColor(tokens.marker_text))
            item.setToolTip(0, merged_tip)

    def _apply_merge_preview(self):
        """本文上的合併預覽：章名用非原文色畫在標題後面，原本的章名行（和中間的空行）先藏起來。"""
        appended, hidden = {}, set()
        for row, (subtitle_row, subtitle) in self.merged_titles.items():
            appended[row] = subtitle
            hidden.update(range(row + 1, subtitle_row + 1))
        self.editor.set_title_preview(appended, hidden, self.tokens.marker_text)

    def _refresh_title_formats(self):
        """重畫標題粗體與標記樣式；正文行數變過就先重建目錄（重建時會順便重畫）。

        粗體與隱藏標記都是照「目錄記的行號」畫的。使用者打字多了幾行之後
        直接重畫，粗體會落在錯的行——實測在開頭多打兩行再縮放字級，粗體跑到
        新打的第一行和一行正文上，真正的章節標題反而變細；隱藏的 [::] 也會
        因此露出來。"""
        if self._toc_text_version != self._text_version:
            self._sync_raw_lines()
            self._rebuild_toc()
        else:
            self._apply_title_formats()

    @timed
    def _apply_title_formats(self):
        """把章節標題那幾行加粗放大，本文區塊才看得出層次。

        純粹是顯示用的字元格式，不動到任何一個字：復原快照比對的是文字與
        章節狀態，格式改變不會多存一步歷史。先把整份格式清乾淨再重畫，
        否則使用者把標題改成內文後，那行還會繼續粗體。
        （150,000 行、1,000 章的檔案實測：清除 0.03 秒、重畫 0.01 秒。）
        """
        document = self.editor.document()
        title_format = QTextCharFormat()
        title_format.setFontWeight(QFont.Weight.Bold)
        # 章名不另外配色（所有主題統一）：跟主要文字同色，純黑主題稍微亮一點。
        title_format.setForeground(QColor(self.tokens.title_text))
        # 樣式表用 px 指定字級，這時 pointSizeF() 是 -1；要照字型實際用的單位放大。
        base_font = self.editor.font()
        if base_font.pixelSize() > 0:
            title_format.setProperty(QTextFormat.Property.FontPixelSize, round(base_font.pixelSize() * 1.25))
        else:
            title_format.setFontPointSize(base_font.pointSizeF() * 1.25)

        plain_format = QTextCharFormat()
        cursor = QTextCursor(document)
        # 用 try/finally：套格式中途若出錯，旗標留在 True 的話之後打字都不會
        # 再建立復原快照，而且完全沒有跡象。
        previous_flag = self._applying_formats
        self._applying_formats = True
        cursor.beginEditBlock()
        try:
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.setCharFormat(plain_format)
            for raw_index in sorted(set(self.chapter_raw_map.values())):
                block = document.findBlockByNumber(raw_index)
                if not block.isValid():
                    continue
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.setCharFormat(title_format)
                # 標題的下一行通常是空行，空行沒有文字、格式是跟著前一行繼承的：
                # 不把它壓回一般字重，使用者在標題底下打字會打出一整行粗體。
                following = block.next()
                if following.isValid():
                    cursor.setPosition(following.position())
                    cursor.setBlockCharFormat(plain_format)
            self._apply_mark_colors(cursor)
            self._strike_absorbed_titles(cursor)
            self._hide_title_markers(cursor)
        finally:
            cursor.endEditBlock()
            self._applying_formats = previous_flag

    def _strike_absorbed_titles(self, cursor: QTextCursor):
        """自動合併重複標題的預覽：會被刪掉的那一行畫刪除線、用非原文色（本文不改）。"""
        if not self.absorbed_titles:
            return
        strike = QTextCharFormat()
        strike.setFontStrikeOut(True)
        strike.setForeground(QColor(self.tokens.marker_text))
        document = self.editor.document()
        for row in self.absorbed_titles:
            block = document.findBlockByNumber(row)
            if block.isValid():
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(strike)

    def _on_strip_markers_toggled(self, on: bool):
        self._strip_markers_on_export = on
        self._show_status("匯出時會移除章節標記" if on else "匯出時保留章節標記")

    def _on_markers_toggled(self, shown: bool):
        """切換只影響顯示，一個字都不會動到。"""
        self._show_title_markers = shown
        self._refresh_title_formats()
        self._show_status("已顯示章節標記" if shown else "已隱藏章節標記")

    def _show_marker_help(self):
        """檔名列的問號（說明）：章節標記、本文字色、自動補齊卷號／卷名、設定檔位置。"""
        dialog = HelpDialog(self.tokens, [(mark, i18n.T(name), i18n.T(detail)) for mark, name, detail in MARKER_GUIDE],
                            APP_DATA_DIR, self)
        dialog.exec()
        dialog.deleteLater()

    def _hide_title_markers(self, cursor: QTextCursor):
        """行尾的 [::]／[::X]／[::W]／[::T] 標記只給程式辨識用，閱讀時是雜訊：
        預設縮成 1px 並設成透明，畫面上等於看不到，但字還在文字裡——存檔、
        復原、章節辨識都照舊。

        按下「顯示章節標記」之後改成用強調色加淡底畫出來，一眼就看得出
        「這是工具加的，不是作者寫的」。

        先用 raw_lines 找出「哪幾行有標記」（純字串比對，很快），再只在那幾
        行上比對正則；原本是讓 QTextDocument 掃過整份文件，檔案愈大愈慢，
        而有標記的通常只有幾十行。"""
        hidden = QTextCharFormat()
        if self._show_title_markers:
            tokens = self.tokens
            hidden.setForeground(QColor(tokens.marker_text))
            hidden.setBackground(QColor(tokens.marker_bg))
        else:
            hidden.setForeground(QColor(0, 0, 0, 0))
            hidden.setProperty(QTextFormat.Property.FontPixelSize, 1)
        document = self.editor.document()
        for row, line in enumerate(self.raw_lines):
            if "[::" not in line:
                continue
            block = document.findBlockByNumber(row)
            if not block.isValid():
                continue
            match = _MARKER_REGEX.match(block.text())
            if not match.hasMatch():
                continue
            cursor.setPosition(block.position() + match.capturedStart())
            cursor.setPosition(block.position() + match.capturedEnd(), QTextCursor.MoveMode.KeepAnchor)
            # 顯示時用 merge：保留該行原本的字級與字重，標記才會跟著標題
            # 一起放大，不會變成擠在標題旁邊的一小截。
            if self._show_title_markers:
                cursor.mergeCharFormat(hidden)
            else:
                cursor.setCharFormat(hidden)

    def _on_tree_item_clicked(self, item, _column):
        if item in self.virtual_volume_items and item.childCount():
            item = item.child(0)   # 推定卷本身沒有標題行，跳到卷內第一章
        row = self.chapter_index_map.get(item)
        if not row:
            return
        if self._toc_text_version != self._text_version:
            # 目錄建好之後正文行數變過：換算成目前的行號，不然會跳到別章。
            self._sync_raw_lines()
            row = self._map_tree_line(row - 1) + 1
        block = self.editor.document().findBlockByNumber(row - 1)
        if not block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(block.position())
        self.editor.setTextCursor(cursor)
        self.editor.centerCursor()
        self.editor.setFocus()

    def _apply_toc_label_mode(self):
        labels = self.toc_compact_labels if self.toc_compact_mode else self.toc_full_labels
        for item, label in labels.items():
            item.setText(0, label)

    def _on_toc_compact_clicked(self, on: bool):
        """目錄卡片標題列的開關：目錄只顯示章號／顯示完整標題。"""
        self.toc_compact_mode = on
        self._apply_toc_label_mode()
        self._show_status("目錄只顯示章號" if on else "目錄顯示完整標題")

    # ------------------------------------------------------------------
    # 缺章檢查
    # ------------------------------------------------------------------

    def _find_collection_missing_from_toc(self):
        """單本與合集共用已確認章節資料，作品間不互相延續編號。"""
        mode = self.chapter_panel.missing_mode()
        groups = group_formal_chapters(
            self.chapter_records, lambda node: node.parent(),
            lambda node: self.toc_full_labels.get(node, node.text(0)))
        results, previous = [], {}
        for group in groups:
            report = chapter_gap_report(group["numbers"], group["label"], mode, previous.get(group["work"]),
                                        group["titles"])
            report["nodes"] = sorted(
                ((self.chapter_raw_map.get(node, 0), int(self.chapter_records[node]["number"]), node)
                 for node in group["nodes"]), key=lambda entry: entry[0])
            report["anomaly_rows"] = [(self.chapter_raw_map.get(group["nodes"][index], 0), number, kind, guess)
                                      for index, number, kind, guess in report["anomalies"]]
            results.append(report)
            previous[group["work"]] = report["last"]
        return results

    def _missing_problems_from_structure(self, result) -> list:
        """直接用 core 的結構結果算缺章，不需要先把目錄畫出來。"""
        tree = result.tree
        groups = group_formal_chapters(
            result.chapter_records, lambda node: tree.parent(node) or None,
            lambda node: tree.item(node, "text"))
        mode = self.chapter_panel.missing_mode()
        results, previous = [], {}
        for group in groups:
            report = chapter_gap_report(group["numbers"], group["label"], mode, previous.get(group["work"]),
                                        group["titles"])
            results.append(report)
            previous[group["work"]] = report["last"]
        return self._missing_chapter_problems(results)

    @staticmethod
    def _missing_chapter_problems(results) -> list:
        problems = []
        for result in results:
            details = []
            if result["missing_ranges"]:
                details.append("章號缺口 " + "、".join(
                    str(a) if a == b else f"{a}–{b}" for a, b in result["missing_ranges"]))
            if result["duplicates"]:
                details.append("重複 " + compact_number_ranges(result["duplicates"]))
            if details:
                prefix = "" if result["label"] == "全書" else f"{result['label']}："
                problems.append(prefix + "、".join(details))
        return problems

    @action
    def check_missing_chapters(self):
        """結果顯示在章節管理的結果區；之後目錄每次重建都會自動重算。"""
        if not self.editor.toPlainText().strip():
            return
        self._missing_report_active = True
        self._refresh_missing_report()

    @timed
    def _refresh_missing_report(self):
        if not self._missing_report_active:
            return
        results = self._find_collection_missing_from_toc()
        self._missing_groups = [result["nodes"] for result in results]
        uncollected = self._uncollected_headings() if any(r["missing_ranges"] for r in results) else {}
        self.chapter_panel.show_missing_report({
            "mode": self.chapter_panel.missing_mode(),
            "total": sum(result["count"] for result in results),
            "start_unverified": any(result["start_unverified"] for result in results),
            "groups": [{"label": result["label"], "entries": self._report_entries(result, uncollected)}
                       for result in results],
        })

    def _report_entries(self, result, uncollected) -> list:
        """缺口與重複照本文順序排：缺口放在缺口後面那一章的位置，重複放在第二次出現的位置。
        缺的章在原文找得到（沒被認成章節）時，附上那幾行。"""
        nodes = result["nodes"]
        entries = []
        first_row = nodes[0][0] if nodes else 0
        last_row = self._group_end_row(nodes)
        for start, end in result["missing_ranges"]:
            # 缺口接在「號碼最接近的前一章」後面；錯放在別處的章號不影響位置
            previous = max(((number, row) for row, number, _node in nodes if number < start), default=(0, first_row))
            found = sorted((number, row, reason) for number in range(start, end + 1)
                           for row, reason in uncollected.get(number, ()) if first_row <= row <= last_row)
            entries.append({"kind": "gap", "start": start, "end": end, "row": previous[1] + 0.5, "found": found})
        for number in result["duplicates"]:
            rows = [row for row, value, _node in nodes if value == number]
            entries.append({"kind": "dup", "start": number, "end": number, "row": rows[1] if len(rows) > 1 else 0,
                            "found": []})
        for row, number, kind, guess in result.get("anomaly_rows", []):
            entries.append({"kind": kind, "start": int(number), "end": guess, "row": row, "found": []})
        return sorted(entries, key=lambda entry: entry["row"])

    def _group_end_row(self, nodes) -> int:
        """這一組章節（同一卷）在本文裡延伸到哪一行：最後一章之後、下一個目錄項目之前。"""
        if not nodes:
            return 0
        last = nodes[-1][0]
        later = [row for row in self.chapter_raw_map.values() if row > last]
        return (min(later) - 1) if later else len(self.raw_lines) - 1

    def _uncollected_headings(self) -> dict:
        """原文裡有章號、但不在目錄的行：{章號: [(行號, 沒收錄的原因)]}。

        一般只看正式章號（第N章）。目錄裡有常用格式、自訂規則認出來的章（「72 標題」這種書）時，
        也看常用格式的寫法：同一本書常混著「71. 標題」「35 標題。」，沒收錄的原因多半是寫法或結尾標點。"""
        known = set(self.chapter_raw_map.values())
        tail = self._title_check()
        by_rule = any(record.get("source") == "rule" for record in self.chapter_records.values())
        enabled = {rule["preset"] for rule in self.user_chapter_rules
                   if rule.get("preset") and rule.get("enabled", True)}
        names = {preset["preset"]: preset["name"] for preset in PRESET_RULES}
        found: dict = {}
        for row, line in enumerate(self.raw_lines):
            if row in known or (not by_rule and "第" not in line):
                continue
            text, marker = strip_persistent_title_marker(line.strip())
            number = heading_number(text) if text and "第" in text else None
            preset = None
            if number is None and by_rule and text:
                matched = preset_match(text)
                if matched is not None:
                    preset, number = matched[0], matched[1]["number"]
            if number is None and text and "第" in text:
                # 「第一百五十七　標題」少了「章」字
                bare = _NUMBER_WITHOUT_UNIT.match(text)
                if bare:
                    value = chinese_to_arabic(bare.group(1))
                    if value and float(value).is_integer():
                        found.setdefault(int(value), []).append((row, "少了「章」字"))
                continue
            if not number:
                continue
            if preset is not None and preset not in enabled and marker != "exclude":
                found.setdefault(number, []).append((row, f"寫法是「{names[preset]}」，這種常用格式沒打開"))
                continue
            if marker == "exclude":
                reason = "標註為非章節"
            elif row in self.ignored_chapters:
                reason = "標註為非章節"
            elif tail is not None and tail.search(text):
                reason = f"標題以「{text[-1]}」結尾"
            else:
                reason = "沒被認成章節"
            found.setdefault(number, []).append((row, reason))
        return found

    def _on_missing_report_closed(self):
        self._missing_report_active = False

    def _on_missing_report_link(self, link: str):
        """點結果裡的缺口：跳到缺口前最後一章；點重複：跳到第二次出現的那一章。"""
        if link.startswith("line|"):
            self._jump_to_line(int(link.split("|")[1]) + 1)
            return
        try:
            group_index, number, kind = link.split("|")
            nodes = self._missing_groups[int(group_index)]
            number = int(number)
        except (ValueError, IndexError):
            return
        if kind == "dup":
            matches = [node for _row, value, node in nodes if value == number]
            target = matches[1] if len(matches) > 1 else (matches[0] if matches else None)
        else:
            before = [node for _row, value, node in nodes if value < number]
            target = before[-1] if before else (nodes[0][2] if nodes else None)
        if target is None:
            return
        self.tree.setCurrentItem(target)
        self.tree.scrollToItem(target)
        self._on_tree_item_clicked(target, 0)

    # ------------------------------------------------------------------
    # 側邊面板切換（格式選項／章節管理／內容清理）
    # ------------------------------------------------------------------

    def _set_active_side_panel(self, panel: QWidget | None):
        was_open = self.side_card.isVisible()
        sizes = self.splitter.sizes()
        for widget in (self.options_panel, self.chapter_panel, self.content_panel, self.find_bar):
            widget.setVisible(widget is panel)
        if panel is not self.find_bar:
            self.editor.setExtraSelections([])      # 關掉搜尋面板就把反白收掉
        self.format_toggle_button.setChecked(panel is self.options_panel)
        self.chapter_toggle_button.setChecked(panel is self.chapter_panel)
        self.content_toggle_button.setChecked(panel is self.content_panel)
        self.find_toggle_button.setChecked(panel is self.find_bar)
        self.side_card.setVisible(panel is not None)
        if was_open != (panel is not None):
            self._resize_cards_for_side_panel(sizes, opening=panel is not None)

    def _resize_cards_for_side_panel(self, sizes, opening: bool):
        """開、關功能卡片時目錄卡片維持原本的寬度，只由本文卡片讓出或拿回：
        關掉時目錄移到左邊那個位置，不是目錄變寬。"""
        side, tree, editor = sizes
        handle = self.splitter.handleWidth()
        if not opening:
            self._side_width = side
            self._update_minimum_width(settle=True)
            self.splitter.setSizes([0, tree, editor + side + handle])
            return
        self._update_minimum_width(settle=True)
        side = max(self._side_width, self.side_card.minimumSizeHint().width())
        side = min(side, self.side_card.maximumWidth())
        margins = self.splitter.parentWidget().layout().contentsMargins()
        available = max(self.splitter.width(), self.minimumWidth() - margins.left() - margins.right())
        editor = max(self.editor_card.minimumSizeHint().width(), available - side - tree - 2 * handle)
        tree = max(self.tree_card.minimumSizeHint().width(), available - side - editor - 2 * handle)
        self.splitter.setSizes([side, tree, editor])

    def _update_minimum_width(self, settle: bool = False):
        """視窗最窄：工具列只剩圖示的寬度，跟目前顯示的卡片都完整放得下的寬度，取大的
        （拉窄視窗時卡片不會被壓到比內容窄、按鈕被切掉）。螢幕放不下時以螢幕為準。
        settle：剛打開卡片時先套好樣式再量（平常內容變了由 _card_watcher 呼叫，不必再整張檢查）。"""
        # 自己加總：剛打開的卡片，分隔條的最小寬度要等版面重排後才會算進去
        cards = [card for card in (self.side_card, self.tree_card, self.editor_card) if not card.isHidden()]
        if settle:
            for card in cards:
                _settle(card)
        margins = self.splitter.parentWidget().layout().contentsMargins()
        needed = (sum(max(card.minimumSizeHint().width(), card.minimumWidth()) for card in cards)
                  + self.splitter.handleWidth() * (len(cards) - 1) + margins.left() + margins.right())
        screen = self.screen()
        if screen is not None:
            needed = min(needed, screen.availableGeometry().width())
        self.setMinimumWidth(max(MIN_WINDOW_WIDTH, needed))

    def _sync_side_panel_widths(self):
        """左側卡片裡的三個面板用同一個最小寬度（以最寬的那個為準）：
        不然切換「排版設定」「章節管理」時，卡片寬度會跟著跳。
        字型與樣式表會影響寬度，所以每次套用主題後重算。"""
        panels = (self.options_panel, self.chapter_panel, self.content_panel, self.find_bar)
        for panel in panels:
            panel.setMinimumWidth(0)
            _settle(panel)
        width = max(panel.minimumSizeHint().width() for panel in panels)
        for panel in panels:
            panel.setMinimumWidth(width)
        self._update_minimum_width(settle=True)

    def _toggle_side_panel(self, panel: QWidget):
        self._set_active_side_panel(None if panel.isVisible() else panel)

    # ------------------------------------------------------------------
    # 格式選項／一鍵排版
    # ------------------------------------------------------------------

    @action
    def apply_formatting(self):
        if not self.raw_lines:
            return
        self._sync_raw_lines()
        options = self.options_panel.current_options(self.structure_mode)
        self._apply_format_options(options, "已套用格式到全文，可以按 Ctrl+Z 復原")

    @action
    def one_click_format(self):
        """一鍵排版：不管面板目前勾了什麼，直接套用一組固定的常用組合——
        刪除所有空行、章節插入空行、增加縮排、標題前後空行、編號與標題間隔
        用半形空格（使用者按過「套用到一鍵排版」就用存下來的組合）。不做合併下行標題：
        那是猜測，要在章節管理預覽過再套用。

        排版前先檢查缺章與高信心廣告：排版會重排整份文字，事後比較難回頭
        確認原本的問題，所以有狀況時先問過再動手。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        options = self._one_click_options()
        # 排版前的檢查直接用排版那一次的辨識結果，不另外再建一次結構（大檔每次要好幾秒）。
        applied = self._apply_format_options(
            options, "已套用一鍵排版，可以按 Ctrl+Z 復原",
            confirm=self._confirm_one_click_warnings)
        if not applied:
            self._show_status("已取消一鍵排版")
            return
        self.options_panel.reset_to_defaults()

    def _one_click_options(self) -> FormatOptions:
        """一鍵排版的組合：使用者按過「套用到一鍵排版」就用存下來的，否則用內建的。"""
        saved = self._ui_state.get("one_click_options")
        if isinstance(saved, dict):
            # 合併下行標題不在排版做（章節管理預覽＋套用），舊存檔裡的 merge_title 忽略
            known = {field.name for field in dataclasses.fields(FormatOptions)} - {"structure", "merge_title"}
            values = {key: value for key, value in saved.items() if key in known}
            try:
                return FormatOptions(**values, structure=self.structure_mode)
            except TypeError:
                pass
        return FormatOptions(
            remove_extra_empty=True,
            add_empty=True,
            auto_indent=True,
            format_title=True,
            sep_style="半形空格",
            structure=self.structure_mode,
        )

    @action
    def save_one_click_options(self):
        """把排版設定目前的開關與下拉存成一鍵排版的組合。"""
        options = self.options_panel.current_options(self.structure_mode)
        saved = dataclasses.asdict(options)
        saved.pop("structure", None)
        self._ui_state["one_click_options"] = saved
        items = describe_options(options)
        self._show_status(i18n.T("一鍵排版改用目前的設定：") + ("、".join(i18n.T(item) for item in items)
                                                           if items else i18n.T("（沒有勾選任何項目）")),
                          translated=True)

    def _confirm_one_click_warnings(self, result) -> bool:
        """有缺章或高信心廣告時彈窗確認；沒有狀況就直接放行。

        result 是排版那一次的辨識結果，直接拿來算缺章。"""
        warnings = []
        problems = self._missing_problems_from_structure(result)
        if problems:
            warnings.append("缺章：" + "；".join(problems))
        ads = self._high_confidence_ads()
        if ads:
            warnings.append(f"高信心廣告：{len(ads)} 處（可先到「內容檢查 → 掃描無關連內容」刪除）")
        if not warnings:
            return True
        return dialogs.confirm(
            self, "一鍵排版前確認",
            "排版前發現以下狀況：\n\n" + "\n".join(f"• {w}" for w in warnings)
            + "\n\n仍要繼續一鍵排版嗎？")

    def _high_confidence_ads(self) -> list:
        """高信心廣告候選；同一份文字只掃一次（掃描本身在大檔要好幾秒）。"""
        if self._ad_scan_version != self._text_version:
            self._ad_scan_cache = [candidate for candidate in scan_ad_candidates(
                self.raw_lines, set(AD_CATEGORY_LABELS) - {"entity"},
                title_rows=set(self.chapter_raw_map.values()))
                                   if candidate["confidence"] == "高"]
            self._ad_scan_version = self._text_version
        return self._ad_scan_cache

    @timed
    def _apply_format_options(self, options: FormatOptions, status_message: str, confirm=None) -> bool:
        """套用格式；confirm 會拿到這一次的辨識結果，回傳 False 就整個取消。"""
        self.format_options = options
        ctx = self._build_context()
        result = build_document_structure(ctx, apply_format=True, write_text=True)
        if confirm is not None and not confirm(result):
            return False

        generated = "\n".join(result.processed_render_lines)
        auto_titles = {
            result.chapter_index_map[node] - 1: dict(record)
            for node, record in result.chapter_records.items()
        }
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            old = getattr(self, attribute)
            setattr(self, attribute, {
                result.chapter_index_map[node] - 1
                for node, row in result.chapter_raw_map.items() if row in old
            })
        self.auto_titles = auto_titles
        moved_titles = {row: result.chapter_index_map[node] - 1 for node, row in result.chapter_raw_map.items()}
        self._push_line_map(_chapter_line_mapper(moved_titles))
        old_lines = self.raw_lines
        self.raw_lines = generated.split("\n")

        self._set_editor_text(generated, _format_line_mapper(old_lines, self.raw_lines, moved_titles))
        self._mark_synced(generated)
        # 排版後的行號跟排版前完全不同：不能直接拿排版結果畫目錄（那份結果
        # 的原始行號指的是排版「前」的文字，標題粗體會落在錯的行），要用
        # 新文字重新辨識一次。
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(status_message)
        return True

    @action
    def format_selected_chapters(self):
        """只對目錄選取的章節套用目前的格式選項。

        每一段自己跑一次 build_document_structure，再把結果接回原文；段落
        邊界一律切在章節標題上，所以不會排版到沒選的章。行號對照用每個
        章節標題「從哪一行搬到哪一行」精確算出來，不靠 difflib 猜，
        強制層級與忽略標記才不會跑掉（跟剪下／貼上同一套作法）。"""
        if not self.raw_lines:
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        if not spans:
            dialogs.info(self, "尚未選取章節", "請先在目錄選取要排版的章節。")
            return
        options = self.options_panel.current_options(self.structure_mode)
        items = describe_options(options)
        if not items:
            dialogs.info(self, "沒有開啟任何項目",
                         "排版設定目前沒有開啟任何項目，先打開要套用的開關或選擇下拉選項。")
            return
        chapter_count = self._selected_chapter_count()
        lines_count = sum(end - start for start, end in spans)
        if not dialogs.confirm(
            self, "套用格式到選取章節",
            f"將對選取的 {chapter_count} 章（共 {lines_count} 行）套用：\n\n"
            + "\n".join(f"• {item}" for item in items)
            + "\n\n沒有選到的章節維持原樣，所以整本書可能看起來不一致"
              "（章節編號樣式、編號與標題間隔這類設定尤其明顯）。\n"
              "此操作算一步，可以用「上一步」完整復原。\n\n是否繼續？",
        ):
            return

        self.format_options = options
        new_lines, moved_titles = self._format_spans(spans, options)
        generated = "\n".join(new_lines)
        self._push_line_map(_chapter_line_mapper(moved_titles))
        self._remap_chapter_state(moved_titles)
        old_lines = self.raw_lines
        self.raw_lines = list(new_lines)
        self._set_editor_text(generated, _format_line_mapper(old_lines, self.raw_lines, moved_titles))
        self._mark_synced(generated)
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(f"已套用格式到 {chapter_count} 章，可以按 Ctrl+Z 復原"), translated=True)

    def _format_spans(self, spans, options):
        """逐段排版並接回原文，回傳（新的整份行, 章節標題的新舊行號對照）。"""
        new_lines: list = []
        moved_titles: dict = {}
        cursor_row = 0
        # 先算好一次：它是每次呼叫都重建的集合，放進迴圈裡等於每一行都掃一遍全部章節。
        title_rows = self._title_rows
        for start, end in spans:
            # 沒選到的部分原樣搬過去，順便記下這段裡的章節標題位移。
            for old_row in range(cursor_row, start):
                if old_row in title_rows:
                    moved_titles[old_row] = len(new_lines) + (old_row - cursor_row)
            new_lines.extend(self.raw_lines[cursor_row:start])

            segment_start = len(new_lines)
            result = build_document_structure(
                self._segment_context(start, end, options), apply_format=True, write_text=True)
            new_lines.extend(result.processed_render_lines)
            for node, local_old in result.chapter_raw_map.items():
                moved_titles[start + local_old] = segment_start + result.chapter_index_map[node] - 1
            cursor_row = end

        for old_row in range(cursor_row, len(self.raw_lines)):
            if old_row in title_rows:
                moved_titles[old_row] = len(new_lines) + (old_row - cursor_row)
        new_lines.extend(self.raw_lines[cursor_row:])
        return new_lines, moved_titles

    def _segment_context(self, start: int, end: int, options) -> BuildContext:
        """把整份文件的章節狀態裁成這一段的區域座標。"""
        def shift(rows):
            return {row - start for row in rows if start <= row < end}

        return BuildContext(
            raw_lines=self.raw_lines[start:end],
            options=options,
            user_chapter_rules=self.user_chapter_rules,
            auto_titles={row - start: record for row, record in self.auto_titles.items()
                         if start <= row < end},
            force_lv1_chapters=shift(self.force_lv1_chapters),
            force_lv2_chapters=shift(self.force_lv2_chapters),
            ignored_chapters=shift(self.ignored_chapters),
            invalid_tail_regex=self._title_check(),
            disabled_words=self.disabled_words,
            special_levels=self.special_levels,
        )

    @property
    def _title_rows(self) -> set:
        """目前所有章節標題所在的行（0 起算）。"""
        return {row - 1 for row in self.chapter_index_map.values()}

    def _remap_chapter_state(self, moved_titles: dict):
        """排版後行號全變了，把以行號為鍵的章節狀態搬到新行號。"""
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            old = getattr(self, attribute)
            setattr(self, attribute, {moved_titles[row] for row in old if row in moved_titles})
        self.auto_titles = {moved_titles[row]: record
                            for row, record in self.auto_titles.items() if row in moved_titles}

    # ------------------------------------------------------------------
    # 尋找／取代
    # ------------------------------------------------------------------

    def _on_escape(self):
        """Esc：先取消剪下狀態，沒有的話才收起尋找面板。"""
        if self._cut_state is not None:
            self.cancel_cut()
            return
        self.close_find_bar()

    def toggle_find_bar(self):
        if not self.raw_lines:
            return
        if self.find_bar.isVisible():
            self.close_find_bar()
        else:
            self._set_active_side_panel(self.find_bar)
            self.find_bar.refresh()
            self.find_bar.focus_input()

    def close_find_bar(self):
        if self.find_bar.isVisible():
            self._set_active_side_panel(None)
        self.editor.setExtraSelections([])

    # 尋找列算出來的是 Python 字元位置，游標吃的是 Qt（UTF-16）位置：
    # 本文只要出現過一個 emoji 或擴充漢字，後面每個位置就會差一格，取代會
    # 改到前一個字。所有進出游標的位置都經過這裡換算。

    def _find_on_select(self, start: int, end: int):
        positions = self._positions()
        cursor = self.editor.textCursor()
        cursor.setPosition(positions.to_qt(start))
        cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def _find_on_replace_one(self, start: int, end: int, new_text: str):
        positions = self._positions()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.setPosition(positions.to_qt(start))
        cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)
        cursor.endEditBlock()

    @action
    def _find_on_replace_all_text(self, new_content: str, count: int):
        """全部取代直接換掉整份文字：結果清單有筆數上限，逐筆套用會變成
        「只取代前面幾筆」，而且上萬次游標操作也慢。"""
        self._set_editor_text(new_content, "diff")
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(f"已取代 {count} 處，可以按 Ctrl+Z 復原"))

    def _find_on_matches_changed(self, spans: list[tuple[int, int]], current_index: int):
        tokens = self.tokens
        document = self.editor.document()
        positions = self._positions()
        # 命中太多時只畫目前這一筆：每一筆都是一個 ExtraSelection，上萬筆
        # 會讓每次重繪都很慢。
        if len(spans) > MAX_HIGHLIGHT_SPANS:
            spans = [spans[current_index]] if 0 <= current_index < len(spans) else []
            current_index = 0
        selections = []
        for index, (start, end) in enumerate(spans):
            selection = QTextEdit.ExtraSelection()
            cursor = QTextCursor(document)
            cursor.setPosition(positions.to_qt(start))
            cursor.setPosition(positions.to_qt(end), QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            char_format = selection.format
            char_format.setBackground(QColor(
                tokens.find_current_bg if index == current_index else tokens.find_match_bg))
            char_format.setForeground(QColor(tokens.text))
            selection.format = char_format
            selections.append(selection)
        self.editor.setExtraSelections(selections)

    # ------------------------------------------------------------------
    # 插入章節標題
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 廣告掃描
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 工具對話框（非模式）
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # 合併重複章節
    # ------------------------------------------------------------------

    @action
    def open_duplicate_chapters_dialog(self):
        """相鄰、章號相同的章節列成清單，勾選後合併（判斷規則見 core/duplicate_chapters.py）。"""
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
        self._show_status(f"已合併重複章節（刪除 {removed} 行標題與空行），可以按 Ctrl+Z 復原")

    # ------------------------------------------------------------------
    # 廣告掃描
    # ------------------------------------------------------------------

    def _saved_ad_categories(self) -> set:
        """掃描無關連內容視窗記住的偵測類型（不含重複段落，那在自己的分頁）。

        記住的是「上次勾了哪些」；之後才新增的類型上次根本還沒有，不能當成
        使用者取消了它——那些照預設勾起來。"""
        own = {key for key in AD_ONLY_CATEGORIES if key != "repeat"}
        saved = self._ui_state.get("ad_categories")
        if not isinstance(saved, list):
            return own
        known = self._ui_state.get("ad_categories_known")
        if not isinstance(known, list):
            known = [key for key in AD_CATEGORY_LABELS if key != "author_note"]   # 還沒有「作者感言」類型時存的設定
        return (set(saved) | (set(AD_CATEGORY_LABELS) - set(known))) & own

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
        self._open_scan_dialog("ads")

    @action
    def open_note_scan_dialog(self):
        self._open_scan_dialog("notes")

    def _open_scan_dialog(self, mode: str):
        """mode＝"ads"：掃描無關連內容（含重複段落分頁）；"notes"：作者感言與作品資訊。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        enabled = self._saved_ad_categories() if mode == "ads" else self._saved_note_categories()

        def create():
            dialog = AdScanDialog(self.raw_lines, self, selected_ranges=spans,
                                  selected_count=self._selected_chapter_count(),
                                  enabled_categories=enabled,
                                  title_rows=set(self.chapter_raw_map.values()),
                                  mode=mode, repeat_settings=self._saved_repeat_settings())
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            dialog.deletionReady.connect(lambda lines, d=dialog: self._apply_ad_deletion(d, lines))
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count, title_rows=set(self.chapter_raw_map.values()))

        def on_closed(dialog, _accepted):
            if mode == "ads":
                self._ui_state["ad_categories"] = sorted(dialog.enabled_categories())
                self._ui_state["ad_categories_known"] = sorted(AD_CATEGORY_LABELS)
                self._ui_state["repeat_settings"] = list(dialog.repeat_settings())
            else:
                self._ui_state["note_categories"] = sorted(dialog.enabled_categories())
            if self.content_panel.marking():
                self._schedule_mark_scan(0)      # 勾選的類型可能變了，照新的重標

        self._open_tool_dialog("ad_scan" if mode == "ads" else "note_scan", create, reload, on_closed)

    # ------------------------------------------------------------------
    # 字色標示（內容檢查卡片底下的開關）
    # ------------------------------------------------------------------

    def _on_merge_titles_toggled(self, on: bool):
        self._merge_titles = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動合併下行標題：只有章號的標題接上下一行的章名（預覽，"
                          "按「套用到本文」才寫進去）" if on else "已關閉自動合併下行標題")

    def _on_skip_duplicates_toggled(self, on: bool):
        self._skip_duplicate_titles = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動合併重複標題：同一章的標題重複出現、中間不到 100 字時只留第一個（預覽，"
                          "按「套用到本文」才刪掉重複的那一行）" if on else "已關閉自動合併重複標題")

    def _rebuild_preview_toc(self):
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._sync_raw_lines()
            self._rebuild_toc()

    def _on_infer_volumes_toggled(self, on: bool):
        self._infer_volumes = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動補齊卷號：目錄補上推算出來的卷（預覽，按「套用到本文」才寫進去）"
                          if on else "已關閉自動補齊卷號")

    def _on_infer_volume_names_toggled(self, on: bool):
        self._infer_volume_names = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動補齊卷名：補上的卷帶卷名，每章前面的卷拆成卷標題（預覽）"
                          if on else "已關閉自動補齊卷名")

    def _on_marking_changed(self):
        """廣告、作者感言兩個開關任一個變了：先把關掉的那一種清掉，還有開著的就重掃。"""
        kinds = self.content_panel.marking()
        for kind in ("ad", "note"):
            if kind not in kinds:
                self._mark_rows[kind] = set()
        self._refresh_title_formats()
        if kinds:
            self._schedule_mark_scan(0)
        else:
            self._mark_timer.stop()
            self._mark_rows_version = None

    def _schedule_mark_scan(self, delay_ms: int = MARK_SCAN_DELAY_MS):
        if not self.raw_lines or not any(line.strip() for line in self.raw_lines):
            return
        self._mark_timer.start(delay_ms)

    def _start_mark_scan(self):
        """在背景執行緒掃描（大檔要將近一秒），掃完才回到主執行緒上色。
        掃描期間本文又改了：結果作廢，等這一輪結束再掃一次。"""
        kinds = self.content_panel.marking()
        if not kinds:
            return
        if self._mark_scan_running:
            self._mark_scan_pending = True
            return
        self._sync_raw_lines()
        lines = list(self.raw_lines)
        version = self._text_version
        # 目錄過期（剛改過本文）：掃完回來上色前本來要在畫面上重建目錄（大檔半秒以上），
        # 改成在同一個背景執行緒裡一起辨識，畫面只負責把結果畫上去。
        toc_ctx = None
        title_rows = set(self.chapter_raw_map.values())
        if self._toc_text_version != version:
            toc_ctx = dataclasses.replace(
                self._build_context(), raw_lines=lines, user_chapter_rules=list(self.user_chapter_rules),
                auto_titles=dict(self.auto_titles), force_lv1_chapters=set(self.force_lv1_chapters),
                force_lv2_chapters=set(self.force_lv2_chapters), ignored_chapters=set(self.ignored_chapters))
        # 網頁字元碼只是換字，不是廣告：不標廣告色
        ad_categories = (self._saved_ad_categories() - {"entity"}) | {"repeat"} if "ad" in kinds else set()
        note_categories = self._saved_note_categories() if "note" in kinds else set()
        min_length, min_count = self._saved_repeat_settings()
        self._mark_scan_running = True

        def work():
            try:
                structure = None
                rows = title_rows
                if toc_ctx is not None:
                    structure = build_document_structure(toc_ctx, apply_format=False, write_text=False)
                    rows = set(structure.chapter_raw_map.values())
                ad_rows, note_rows = set(), set()
                if ad_categories:
                    for candidate in scan_ad_candidates(lines, ad_categories, None, rows,
                                                        repeat_min_length=min_length, repeat_min_count=min_count):
                        ad_rows.update(range(candidate["start"], candidate["end"] + 1))
                if note_categories:
                    for candidate in scan_ad_candidates(lines, note_categories, None, rows):
                        note_rows.update(range(candidate["start"], candidate["end"] + 1))
                self._mark_signals.finished.emit(version, ad_rows, note_rows, structure)
            except Exception:          # 背景執行緒的例外不會出現在畫面上：記下來、結束這一輪
                log.exception("字色標示掃描失敗")
                self._mark_signals.finished.emit(-1, set(), set(), None)

        threading.Thread(target=work, name="mark-scan", daemon=True).start()

    def _on_mark_scan_finished(self, version: int, ad_rows, note_rows, structure=None):
        self._mark_scan_running = False
        if self._mark_scan_pending or version != self._text_version:
            self._mark_scan_pending = False
            if self.content_panel.marking() and version != -1:
                self._schedule_mark_scan()
            return
        kinds = self.content_panel.marking()
        if not kinds:
            return
        # 同一行兩種都是：用廣告的顏色。開關在掃描期間被關掉的那一種不畫。
        ad_rows = set(ad_rows) if "ad" in kinds else set()
        note_rows = set(note_rows) - ad_rows if "note" in kinds else set()
        self._mark_rows = {"ad": ad_rows, "note": note_rows}
        self._mark_rows_version = version
        if structure is not None and self._toc_text_version != version:
            self._populate_tree(structure)
            self._warn_timed_out_rules()
        self._refresh_title_formats()

    def _apply_mark_colors(self, cursor: QTextCursor):
        """把掃描到的廣告／作者感言那幾行換成對應的字色（章節標題不動）。
        只在掃描結果對應的就是目前這一版本文時才畫：行號過期會標錯行。"""
        if not self.content_panel.marking() or self._mark_rows_version != self._text_version:
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
        # 記的是「關掉了哪些」：之後新增的檢查項目預設是開的。
        # 舊版記的是「開著哪些」（quote_kinds），只認得當時那四種。
        disabled = self._ui_state.get("quote_disabled_kinds")
        if not isinstance(disabled, list):
            saved = self._ui_state.get("quote_kinds")
            disabled = [kind for kind in ("unclosed", "unpaired", "leading_punct", "missing_separator")
                        if kind not in saved] if isinstance(saved, list) else []
        enabled = set(QUOTE_PROBLEM_LABELS) - set(disabled)

        def create():
            dialog = QuoteCheckDialog(self.raw_lines, self, selected_ranges=spans,
                                      selected_count=self._selected_chapter_count(),
                                      enabled_kinds=enabled,
                                      title_rows=set(self.chapter_raw_map.values()))
            dialog.problemSelected.connect(self._jump_to_line)
            dialog.fixesReady.connect(lambda lines, count, d=dialog: self._apply_quote_fixes(d, lines, count))
            return dialog

        def reload(dialog):
            ranges, count = self._tool_scope()
            dialog.reload(self.raw_lines, ranges, count, title_rows=set(self.chapter_raw_map.values()))

        def on_closed(dialog, _accepted):
            # 「分隔線不一致」不是勾選框（由視窗裡的下拉決定），不記
            self._ui_state["quote_disabled_kinds"] = sorted(
                set(QUOTE_PROBLEM_LABELS) - dialog.enabled_kinds() - {"separator_style"})
            self._ui_state.pop("quote_kinds", None)

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
        dialog = ScriptConvertDialog(self, selected_count=self._selected_chapter_count() if spans else 0,
                                     mode=self._ui_state.get("script_mode"),
                                     convert_metadata=bool(self._ui_state.get("script_metadata", True)))
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            mode = dialog.mode()
            selected_only = dialog.selected_only()
            with_metadata = dialog.convert_metadata()
            self._ui_state["script_mode"] = mode
            self._ui_state["script_metadata"] = with_metadata
        finally:
            dialog.deleteLater()

        lines = list(self.raw_lines)
        if selected_only and spans:
            rows = [row for start, end in spans for row in range(start, min(end, len(lines)))]
            scope_text = f"{len(self._selected_toc_items())} 個章節"
        else:
            rows = range(len(lines))
            scope_text = "全文"
        generated = "\n".join(self._convert_lines_with_progress(lines, rows, mode))

        if with_metadata:
            self.metadata_bar.title_input.setText(
                convert_body_text(self.metadata_bar.book_title(), mode))
            self.metadata_bar.author_input.setText(
                convert_body_text(self.metadata_bar.author(), mode))

        # 自動辨識的作品／標題快取記著舊文字，重建目錄時會把舊名稱寫回去
        # ：轉換範圍內的一起轉，建新的 dict，不改到復原快照共用的。
        converted_rows = set(rows)
        self.auto_titles = {
            row: ({**record, "title": convert_body_text(record.get("title", ""), mode)}
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

    def _convert_lines_with_progress(self, lines: list, rows, mode: str) -> list:
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
                converted = convert_body_text("\n".join(lines[row] for row in block), mode)
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

    def _jump_to_line(self, line_number: int):
        """跳到某一行並整行反白（檢查清單點選用，1 起算）。"""
        self._highlight_ad_candidate(line_number - 1, line_number - 1)

    # ------------------------------------------------------------------
    # 自訂章節規則
    # ------------------------------------------------------------------

    @action
    def open_recognition_dialog(self):
        """辨識章節：積木組合、單位與特殊標題、標題長度與章名結尾。非模式：開著時可以從本文複製一行貼上。"""
        self._sync_raw_lines()
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()

        def create():
            dialog = RecognitionDialog(self.user_chapter_rules, lambda: list(self.raw_lines), self,
                                       known_rows=set(self.chapter_raw_map.values()),
                                       title_tail_allowed=self.title_tail_allowed,
                                       title_tail_custom=self.title_tail_custom,
                                       disabled_words=self.disabled_words, max_title_length=self.max_title_length,
                                       special_levels=self.special_levels)
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            return dialog

        def reload(dialog):
            dialog.reload(self.raw_lines, set(self.chapter_raw_map.values()))

        self._open_tool_dialog("recognition", create, reload, self._on_recognition_dialog_closed)

    @action
    def _on_recognition_dialog_closed(self, dialog, accepted: bool):
        if not accepted or dialog.result_rules is None:
            return
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
        self._show_status("已保存辨識章節的設定")

    def open_rules_dialog(self):
        """自訂章節規則（自己寫的正則、本文可疑章節）。非模式：開著時可以從本文複製一行貼到「從範例產生」。"""
        self._sync_raw_lines()
        # 目錄在打字之後可能還沒重建，行號會對不上：已經是章節的行被當成
        # 「可疑章節」再列一次，或真正沒辨識到的反而被跳過。
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._ensure_toc_current()

        def create():
            dialog = RulesDialog(self.user_chapter_rules, lambda: list(self.raw_lines), self,
                                 known_rows=set(self.chapter_raw_map.values()),
                                 max_title_length=self.max_title_length)
            dialog.candidateHighlighted.connect(self._highlight_ad_candidate)
            return dialog

        def reload(dialog):
            dialog.reload(self.raw_lines, set(self.chapter_raw_map.values()))

        self._open_tool_dialog("rules", create, reload, self._on_rules_dialog_closed)

    @action
    def _on_rules_dialog_closed(self, dialog, accepted: bool):
        result_rules = dialog.result_rules
        result_lines = dialog.result_lines
        volume_rows = set(dialog.result_volume_rows)
        if not accepted or result_rules is None:
            return
        if result_lines is not None and dialog._tool_version != self._text_version:
            # 按下按鈕前本文又改了（理論上切回對話框時就會重算，這裡保險）：
            # 勾選的行號已經對不上，只存規則，不動本文。
            result_lines = None
            self._show_status("本文在勾選之後改過了，只保存規則；要加入的行請重新勾選")
        added = 0
        if result_lines is not None:
            # 只在行尾加 [::]，行數不變，章節狀態的行號也不用搬。
            added = sum(1 for old, new in zip(self.raw_lines, result_lines) if old != new)
            generated = "\n".join(result_lines)
            self.raw_lines = list(result_lines)
            self._set_editor_text(generated, lambda row: row)
            self._mark_synced(generated)
            # 卷級格式逐行加入時要設成卷；[::] 本身只代表「這一行是標題」。
            self.force_lv1_chapters |= volume_rows
            self.force_lv2_chapters -= volume_rows
        detection_changed = result_rules != self.user_chapter_rules
        self.user_chapter_rules = result_rules
        if detection_changed:
            # 排版時會把當時目錄裡的標題都記成自動標題，重掃時優先採用；規則、標題結尾改了，
            # 就要照新的設定重新辨識，不然停用的規則、關掉的標點都改不動目錄。作品名稱照舊。
            self.auto_titles = {row: record for row, record in self.auto_titles.items()
                                if record.get("kind") == "work"}
        self.rescan_toc()
        if result_lines is not None:
            self._checkpoint_document()
            self._show_status(f"已把 {added} 行加入目錄，並保存 {len(self.user_chapter_rules)} 條自訂章節規則")
        elif dialog._tool_version == self._text_version:
            self._show_status(f"已保存 {len(self.user_chapter_rules)} 條自訂章節規則")

    # ------------------------------------------------------------------
    # 目錄右鍵選單：整理／合併／連續編號／設層級／忽略／刪除
    # ------------------------------------------------------------------

    def _sync_raw_lines(self):
        """把編輯器目前的內容設為新的工作版本（raw_lines）。

        同一個版本只做一次：比對整份文字並不便宜，而點目錄、右鍵操作可能
        連續呼叫好幾次。"""
        if self._synced_text_version == self._text_version:
            return
        if self._typing_checkpoint_timer.isActive() and not self._restoring_history:
            # 剛打的字還沒被計時器存成一步，就要開始做別的操作（排版、設層級…）：
            # 先把打字存成獨立的一步，復原時才不會連打的字一起退掉。
            # _checkpoint_document 會停掉計時器並同步，不會再回到這裡。
            self._checkpoint_document()
            return
        # 留著這份字串：快照要用同一份，不必再把整份文字複製一次。
        text = self.editor.toPlainText()
        self._adopt_lines(text.split("\n"))
        self._mark_synced(text)

    def _mark_synced(self, text: str | None):
        """記下「raw_lines 對應的是哪一版文字」，以及那份文字本身。

        text 傳 None 代表「已經同步，但手上沒有現成的字串」，之後要用的時候
        再跟編輯器要一次——不能把舊的字串留著，快照會存到錯的內容。"""
        self._synced_text = text
        self._synced_text_version = self._text_version

    @timed
    def _adopt_lines(self, new_lines: list, *, remap_state: bool = True):
        """換成新的 raw_lines，並把所有「以行號為 key」的章節狀態搬到新行號。

        忽略／強制層級集合、自動標題快取，全都是記「第幾行」。正文只要多一行
        或少一行（手動編輯、刪廣告、插入標題、合併章節…），舊行號就會指到
        別的行：本來是正文的行被當成標題、真正的標題反而消失，而且重新掃描
        也救不回來，因為重掃用的還是這些過期行號。所以每次接手新內容都先
        比對新舊兩版，把行號搬過去。

        remap_state=False 只記錄行號位移（重畫目錄時對回原本的選取用），不動
        任何章節狀態：復原／重做時這些狀態會直接用快照整組覆蓋。
        """
        old_lines = self.raw_lines
        self.raw_lines = new_lines
        self._synced_text = None
        self._synced_text_version = self._text_version
        if new_lines == old_lines:
            return
        opcodes = _line_opcodes(old_lines, new_lines)
        mapping = {}
        for tag, a, b, c, d in opcodes:
            # 整段改寫即使行數相同，也不能假定標題還在同一行；只有單行改寫才對應。
            if tag == "equal":
                mapping.update((a + k, c + k) for k in range(b - a))
            elif tag == "replace" and b - a == d - c == 1:
                # 只改了一行（例如章名改一個字）：位置明確，強制層級、移出目錄
                # 這些人工設定跟著這一行走。改成空行就不算了。
                if new_lines[c].strip():
                    mapping[a] = c
            elif tag == "replace" and b - a == d - c:
                # 等長改寫：只有「內容完全一樣」的行才對應得起來。重複行多的
                # 文件（大量空行、重複台詞）只改一行也會被整段判成 replace，
                # 中間沒動過的章節標記就這樣被丟掉。
                mapping.update((a + k, c + k) for k in range(b - a)
                               if old_lines[a + k] == new_lines[c + k])
        self._push_line_map(_diff_line_mapper(opcodes))
        if not remap_state:
            return
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            setattr(self, attribute, {mapping[i] for i in getattr(self, attribute) if i in mapping})
        self.auto_titles = {mapping[i]: value for i, value in self.auto_titles.items()
                            if i in mapping and old_lines[i] == new_lines[mapping[i]]}

    def _map_tree_line(self, index: int) -> int:
        """目錄節點記的行號（chapter_raw_map 的值）換算到目前的正文；只給這些行號用。"""
        for map_line in self._pending_line_maps:
            index = map_line(index)
        return index

    def _push_line_map(self, map_line):
        """記下一次行號位移。一直打字、沒重建目錄時會一直累積：超過一定數量就先把目錄節點的
        行號算好、折成一張表，跳章時才不用一次跑幾千個換算。"""
        self._pending_line_maps.append(map_line)
        if len(self._pending_line_maps) > PENDING_LINE_MAP_LIMIT:
            rows = {row: self._map_tree_line(row) for row in set(self.chapter_raw_map.values())}
            self._pending_line_maps = [lambda index, rows=rows: rows.get(index, index)]

    def _replace_line(self, cursor: QTextCursor, raw_idx: int, new_text: str):
        """把第 raw_idx 行（0-based）整行內容換成 new_text；呼叫端負責用同一個
        cursor 把多次替換包進同一個 beginEditBlock/endEditBlock，變成一次復原。"""
        block = self.editor.document().findBlockByNumber(raw_idx)
        if not block.isValid():
            return
        cursor.setPosition(block.position())
        # 用 block.length()-1 而不是 len(block.text())：長度是 Qt 的單位，
        # 行裡有 emoji 或擴充漢字時兩者不一樣，行尾會切在字的中間。
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)

    def _chapter_nodes_share_unit(self, nodes: list) -> bool:
        """判斷這些節點是不是同一種章節類型（例如都是「第…章」，
        不能混到「第…集」）。任一節點判斷不出類型，就保守視為不一致。"""
        if len(nodes) < 2:
            return True
        signatures = set()
        for node in nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                return False
            clean, _marker = strip_persistent_title_marker(self.raw_lines[raw_idx].strip())
            signature = chapter_unit_signature(clean)
            if signature is None:
                return False
            signatures.add(signature)
        return len(signatures) == 1

    def _build_toc_context_menu(self, pos):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        只放「現在按得下去、而且有意義」的項目：灰掉的項目只會讓人猜為什麼
        不能按。剪下之後就不再顯示「剪下」，改成貼上與取消；已經是章的不顯示
        「設為章標題」，卷也一樣。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return None
        if item not in self.tree.selectedItems():
            self.tree.setCurrentItem(item)
        selected = self._selected_toc_items()
        # 用詞統一：選到卷也照「章」描述——一章叫「這章」，多章叫「這 N 章」；
        # 排版設定卡片的按鈕叫「套用格式到選取章節」，跟這裡同一組詞。
        chapters = self._selected_chapter_count()
        these = f"這 {chapters} 章" if chapters > 1 else "這章"

        groups = [[(f"選取{these}的全部內容", self.select_chapter_text)]]

        pending = self._cut_state
        if pending is None:
            groups.append([(f"剪下{these}", self.cut_selected_chapters)])
        else:
            group = []
            if self._paste_target(item) is not None:
                group.append((f"貼到這章之前（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=True)))
                group.append((f"貼到這章之後（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=False)))
            group.append(("取消剪下（Esc）", self.cancel_cut))
            groups.append(group)

        group = [(f"套用格式到{these}", self.format_selected_chapters)]
        leaves = [node for node in selected if node.childCount() == 0]
        if len(selected) > 1 and len(leaves) == len(selected):
            group.append((f"合併{these}", self.merge_selected_chapters))
        chapter_nodes = [node for node in selected
                         if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(chapter_nodes) > 1 and self._chapter_nodes_share_unit(chapter_nodes):
            group.append(("連續編號", self.renumber_selected_chapters))
        groups.append(group)

        kinds = {self.chapter_records.get(node, {}).get("kind") for node in selected}
        group = []
        if kinds - {"volume"}:
            group.append(("設為卷標題", lambda: self.set_chapter_level(1)))
        if kinds - {"chapter"}:
            group.append(("設為章標題", lambda: self.set_chapter_level(2)))
        groups.append(group)

        groups.append([("標註為非章節（保留正文，從目錄移除）", self.ignore_selected_chapter),
                       (f"刪除{these}（含正文）", self.delete_selected_chapter)])

        menu = QMenu(self)
        for group in groups:
            if not group:
                continue
            if menu.actions():
                menu.addSeparator()
            for text, slot in group:
                menu.addAction(text).triggered.connect(slot)
        return menu

    def _selected_chapter_count(self) -> int:
        """選取範圍有幾章：選到卷就算卷底下的章。"""
        chapters = set()

        def collect(item):
            if item.childCount() == 0:
                chapters.add(id(item))
            for index in range(item.childCount()):
                collect(item.child(index))

        for item in self._selected_toc_items():
            collect(item)
        return len(chapters)

    def _selected_toc_items(self) -> list:
        """目前選取的目錄項目；推定卷沒有對應的標題行，換成它底下的章節。"""
        items = []
        for item in self.tree.selectedItems():
            if item in self.virtual_volume_items:
                items.extend(item.child(index) for index in range(item.childCount()))
            else:
                items.append(item)
        return list(dict.fromkeys(items))

    @action
    def apply_toc_preview(self):
        """「章節管理 → 套用到本文」：把開關預覽的結果真的寫進本文。

        - 自動合併下行標題：章名接到標題行後面，原本放章名的行（和中間的空行）拿掉。

        - 推算出來的卷（斜體）：卷標題插在卷內第一個項目前面，前後留空行。
        - 開了「自動補齊卷名」、每章都帶著卷的寫法（「卷一 山路 第一章 出發」）：換卷的地方
          插一行卷標題，章節行只留「第一章 出發」。卷標題寫成正式的「第一卷 山路」——
          單獨一行的「卷一」預設不算卷（避免誤判），寫成「第…卷」重新整理後才認得出來。
        寫進去之後就是一般的卷標題，排版、匯出都會帶著它；可以按 Ctrl+Z 復原。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        lines = list(self.raw_lines)
        inserts = {}          # 行號 → 要插在這一行前面的卷標題
        replaces = {}         # 行號 → 換成這一行
        for info in self.virtual_volume_items.values():
            inserts[info["row"]] = info["title"]
        if self._infer_volumes and self._infer_volume_names:
            previous = None
            for row in sorted(set(self.chapter_raw_map.values())):
                clean, marker = strip_persistent_title_marker(lines[row].strip())
                mixed = parse_mixed_volume_chapter_header(clean, True)
                if not mixed or mixed["volume_raw"].startswith("第"):
                    # 「第一卷 卷名 第N章」原本就會分卷，排版時處理；這裡只拆「卷一」這種
                    if not mixed:
                        previous = None
                    continue
                number = mixed["volume_raw"][1:].strip()        # 「篇一」→「一」，數字寫法、單位照原文
                volume = f"第{number}{mixed['volume_unit']}{mixed['volume_note']} {mixed['volume_body']}".strip()
                indent = lines[row][:len(lines[row]) - len(lines[row].lstrip())]
                suffix = {"include": "[::]", "auto_title": "[::T]"}.get(marker, "")    # 手動收錄的標記照留
                replaces[row] = f"{indent}{mixed['chapter_raw'].strip()}{suffix}"
                if volume != previous:
                    inserts[row] = volume
                previous = volume
        removed = set()
        for row, (subtitle_row, subtitle) in self.merged_titles.items():
            line = replaces.get(row, lines[row])
            clean, marker = strip_persistent_title_marker(line.strip())
            indent = line[:len(line) - len(line.lstrip())]
            suffix = {"include": "[::]", "auto_title": "[::T]"}.get(marker, "")
            replaces[row] = f"{indent}{clean} {subtitle}{suffix}"
            removed.update(range(row + 1, subtitle_row + 1))
        removed.update(self.absorbed_titles)
        if not inserts and not replaces and not removed:
            self._show_status("沒有可以套用的內容：先打開章節管理的預覽開關（合併下行標題、合併重複標題、"
                              "補齊卷號），目錄上會先顯示預覽")
            return
        done = []           # 寫進去之前先數好：寫完目錄就重建了，預覽資料會清掉
        if self.merged_titles:
            done.append(f"合併 {len(self.merged_titles)} 個標題")
        if self.absorbed_titles:
            done.append(f"刪掉 {len(self.absorbed_titles)} 行重複標題")
        if inserts:
            done.append(f"寫入 {len(inserts)} 個卷標題")
        result = []
        for row, line in enumerate(lines):
            if row in removed:
                continue
            if row in inserts:
                if result and result[-1].strip():
                    result.append("")
                result.extend([inserts[row], ""])
            result.append(replaces.get(row, line))
        self._replace_text_from_tool(result)
        self._show_status(i18n.T("已套用到本文：" + "、".join(done) + "，可以按 Ctrl+Z 復原"), translated=True)

    def _show_toc_context_menu(self, pos):
        menu = self._build_toc_context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    @action
    def select_chapter_text(self):
        """把選取章節的整段內容（標題＋正文）在本文裡選起來，方便直接複製。

        多選時選取涵蓋範圍的頭到尾——編輯器一次只能有一段選取，硬要分段
        反而看不出選到哪裡。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        total_lines = len(self.raw_lines)
        spans = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None:
                spans.append(span)
        if not spans:
            return
        document = self.editor.document()
        first_row = min(start for start, _end in spans)
        last_row = min(max(end for _start, end in spans), document.blockCount()) - 1
        start_block = document.findBlockByNumber(first_row)
        end_block = document.findBlockByNumber(max(first_row, last_row))
        if not start_block.isValid() or not end_block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(start_block.position())
        cursor.setPosition(end_block.position() + end_block.length() - 1,
                           QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self._show_status(i18n.T(f"已選取 {end_block.blockNumber() - first_row + 1} 行，可以直接複製（Ctrl+C）"))

    # ------------------------------------------------------------------
    # 剪下／貼上章節（調整章節順序）
    # ------------------------------------------------------------------

    def _selected_section_spans(self) -> list:
        """選取章節涵蓋的行範圍（0-based 半開區間），重疊的合併起來。"""
        total_lines = len(self.raw_lines)
        spans = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None and span[0] < span[1]:
                spans.append(span)
        spans.sort()
        merged: list = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @action
    def cut_selected_chapters(self):
        """剪下＝先做記號，按貼上才真的搬動（跟檔案總管一樣）。

        沒有貼上就不會少任何東西；按 Esc 或重新剪下就取消。內容同時放進系統
        剪貼簿，也可以直接貼到其他程式。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        if not spans:
            return
        labels = [self.toc_full_labels.get(node, node.text(0)) for node in self.tree.selectedItems()]
        summary = "、".join(label[:12] for label in labels[:2])
        if len(labels) > 2:
            summary += f" 等 {len(labels)} 項"
        moved_lines = [line for start, end in spans for line in self.raw_lines[start:end]]
        QApplication.clipboard().setText("\n".join(moved_lines))
        self._cut_state = {
            "spans": spans,
            "text_version": self._text_version,
            "summary": summary,
            "count": len(labels),
        }
        self._style_cut_items()
        self._show_status(i18n.T(f"已剪下 {len(labels)} 章（共 {len(moved_lines)} 行）："
                                 "在目錄對著要放的位置按右鍵貼上，按 Esc 取消"))

    def cancel_cut(self):
        if self._cut_state is None:
            return
        self._cut_state = None
        self._style_cut_items()
        self._show_status("已取消剪下")

    def _style_cut_items(self):
        """已剪下的章節在目錄裡變淡，一眼看得出「等著被搬走」。"""
        tokens = self.tokens
        spans = self._cut_state["spans"] if self._cut_state else []
        # 推算／拆出來的卷（預覽）自己有斜體＋非原文色，不能在這裡被重設
        preview = (set(self.virtual_volume_items) | self.split_volume_items | self.merged_title_items
                   | self.absorbed_title_items)
        for item, row in self.chapter_raw_map.items():
            inside = any(start <= row < end for start, end in spans)
            font = item.font(0)
            if font.italic() != (inside or item in preview) or inside:
                font.setItalic(inside or item in preview)
                item.setFont(0, font)
            if inside:
                item.setForeground(0, QColor(tokens.text_faint))
                item.setToolTip(0, i18n.T("已剪下，貼上後才會真的移動；按 Esc 取消"))
            elif item not in preview:
                item.setData(0, Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip(0, "")

    def _paste_target(self, item):
        """可以貼到這個項目旁邊嗎？回傳它的行範圍，不行則回傳 None。"""
        if self._cut_state is None or item is None:
            return None
        if self._cut_state["text_version"] != self._text_version:
            return None
        if item in self.virtual_volume_items:
            item = item.child(0) if item.childCount() else None
            if item is None:
                return None
        span = toc_ops.toc_section_lines(
            self.tree, item, self.chapter_index_map, self.toc_boundary_map, len(self.raw_lines))
        if span is None:
            return None
        # 不能貼到自己（或自己底下）：那等於把整段搬進它自己裡面。
        for start, end in self._cut_state["spans"]:
            if start <= span[0] < end:
                return None
        return span

    @action
    def paste_cut_chapters(self, item, before: bool):
        """把剪下的章節搬到指定章節的前面或後面，整個算一步。"""
        if self._cut_state is None:
            return
        if self._cut_state["text_version"] != self._text_version:
            self.cancel_cut()
            dialogs.info(self, "無法貼上", "剪下之後本文有變動，原本的範圍已經對不上，請重新剪下。")
            return
        target = self._paste_target(item)
        if target is None:
            dialogs.info(self, "無法貼上", "不能貼到剪下的章節自己（或它底下的章節）旁邊。")
            return
        spans = self._cut_state["spans"]
        insert_at = target[0] if before else target[1]
        # 目標節點在重建目錄時會被銷毀，標題要先留下來。
        target_label = self.toc_full_labels.get(item, item.text(0))[:16]
        new_lines, mapping = self._move_line_blocks(self.raw_lines, spans, insert_at)
        summary, count = self._cut_state["summary"], self._cut_state["count"]
        self._cut_state = None

        text = "\n".join(new_lines)
        self._set_editor_text(text, lambda row: mapping.get(row, row))
        # 行號是自己算出來的，不必也不能靠 difflib 猜：搬移在比對時會變成
        # 「一處刪掉、一處新增」，被搬走章節的強制層級、忽略標記都會遺失。
        for attribute in ("ignored_chapters", "force_lv1_chapters", "force_lv2_chapters"):
            setattr(self, attribute, {mapping[row] for row in getattr(self, attribute) if row in mapping})
        self.auto_titles = {mapping[row]: value for row, value in self.auto_titles.items() if row in mapping}
        self.raw_lines = new_lines
        self._mark_synced(text)
        self._push_line_map(_chapter_line_mapper(mapping))
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(
            f"已把 {count} 章（{summary}）移到「{target_label}」"
            f"{'之前' if before else '之後'}，可以按 Ctrl+Z 復原"))

    @staticmethod
    def _move_line_blocks(lines: list, spans: list, insert_at: int):
        """把 spans 這幾段行搬到 insert_at 之前，回傳（新的行, 舊行號→新行號）。

        接縫處順便整理空行：搬走的位置不留下連續空行，貼上的地方前後各留
        一個空行，這樣章節標題不會黏在上一段的最後一行。只整理接縫：其他
        章節裡刻意留的多個空行、檔尾的空行都照原樣。"""
        moved_rows = [row for start, end in spans for row in range(start, end)]
        # 搬動的內容去掉前後空行，貼上時再統一補。
        while moved_rows and not lines[moved_rows[0]].strip():
            moved_rows.pop(0)
        while moved_rows and not lines[moved_rows[-1]].strip():
            moved_rows.pop()
        cut_rows = {row for start, end in spans for row in range(start, end)}

        result: list = []
        mapping: dict = {}
        at_seam = False           # 剛經過剪下的位置或貼上的位置，還沒遇到下一行文字

        def emit(row):
            mapping[row] = len(result)
            result.append(lines[row])

        def emit_moved():
            if not moved_rows:
                return
            if result and result[-1].strip():
                result.append("")
            for row in moved_rows:
                emit(row)
            result.append("")

        for index, line in enumerate(lines):
            if index == insert_at:
                emit_moved()
                at_seam = True
            if index in cut_rows:
                at_seam = True
                continue
            if at_seam and not line.strip() and result and not result[-1].strip():
                continue          # 接縫：搬走之後不要留下連續空行
            if line.strip():
                at_seam = False
            emit(index)
        if insert_at >= len(lines):
            emit_moved()
        if insert_at >= len(lines) or at_seam:
            # 檔尾是接縫（貼在檔尾，或剪下的是最後一章）：檔尾的空行照原本的樣子
            original_blank_tail = 0
            for line in reversed(lines):
                if line.strip():
                    break
                original_blank_tail += 1
            while result and not result[-1].strip():
                result.pop()
            result.extend([""] * original_blank_tail)
        return result, mapping

    @action
    def merge_selected_chapters(self):
        """保留第一個選取章節的標題，把其餘選取章節的正文接到它後面。
        支援不連續多選：例如選取第 2、3、7 章後，第 3 與第 7 章的正文
        會依序搬到第 2 章之後，兩者的標題行一併移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        nodes = [node for node in self._selected_toc_items() if node in self.chapter_index_map]
        if len(nodes) < 2:
            dialogs.info(self, "合併章節", "請先用 Ctrl 點選或 Shift 連續選取兩個以上的章節。")
            return

        lines = self.editor.toPlainText().split("\n")
        sections = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in nodes:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, len(lines), ordered)
            if span is not None:
                sections.append((span[0], span[1], node))
        if len(sections) < 2:
            dialogs.info(self, "合併章節", "選取的章節無法定位到正文，請先按「重新掃描目錄」再試一次。")
            return
        sections.sort()

        for (_, previous_end, _), (next_start, _, _) in zip(sections, sections[1:]):
            if next_start < previous_end:
                dialogs.info(
                    self, "合併章節",
                    "選取範圍互相重疊（可能同時選到某一卷與它底下的章節）。\n"
                    "請只選取同一層級、彼此獨立的章節。",
                )
                return

        keep_start, keep_end, keep_node = sections[0]
        keep_title = keep_node.text(0)
        others = sections[1:]
        preview_text = "、".join(node.text(0)[:20] for _, _, node in others[:4])
        if len(others) > 4:
            preview_text += f" 等 {len(others)} 章"
        if not dialogs.confirm(
            self, "合併章節",
            f"將把以下章節的正文併入「{keep_title[:30]}」：\n\n{preview_text}\n\n"
            "這些章節的標題行會被移除，正文依原順序接續在後。是否繼續？",
        ):
            return

        moved = []
        for start, end, _ in others:
            body = lines[start + 1:end]           # 跳過標題行，只取正文
            while body and not body[0].strip():    # 去掉正文前後的空行，稍後統一補
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            if body:
                moved.append(body)

        new_lines = list(lines)
        for start, end, _ in reversed(others):     # 由後往前刪除，避免行號位移
            del new_lines[start:end]

        block = []
        for body in moved:
            if block:
                block.append("")
            block.extend(body)
        if block:
            if keep_end > 0 and new_lines[keep_end - 1].strip():
                block.insert(0, "")
            if keep_end < len(new_lines) and new_lines[keep_end].strip():
                block.append("")
            new_lines[keep_end:keep_end] = block

        self._set_editor_text("\n".join(new_lines), "diff")
        self._sync_raw_lines()
        self._rebuild_toc()

        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == keep_start:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._checkpoint_document()
        self._show_status(f"已把 {len(others)} 章的正文併入「{keep_title[:20]}」，可以按 Ctrl+Z 復原")

    @action
    def renumber_selected_chapters(self):
        """把選取的章節（或卷／集／篇）改成連續編號：保留第一個（依文件順序）
        的原編號當起點，其餘依序遞增。同一次只能對同一種單位操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected_nodes = [node for node in self._selected_toc_items()
                          if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(selected_nodes) < 2:
            dialogs.info(self, "連續編號", "請選取兩個以上的章節或卷節點（不含作品層級）。")
            return
        if not self._chapter_nodes_share_unit(selected_nodes):
            dialogs.info(
                self, "連續編號",
                "選取的項目類型不一致（例如同時選到「第…卷」和「第…集」，"
                "或「第…集」和「第…章」），這些是各自獨立的編號系統，"
                "混在一起重排沒有意義，請分開執行。",
            )
            return

        selected_nodes.sort(key=lambda node: self.chapter_raw_map.get(node, 0))

        plans, skipped = [], []
        for node in selected_nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                skipped.append((node, "找不到對應行"))
                continue
            raw_line = self.raw_lines[raw_idx]
            leading = raw_line[:len(raw_line) - len(raw_line.lstrip())]
            clean, marker = strip_persistent_title_marker(raw_line.strip())
            located = locate_chapter_number(clean)
            if located is None:
                skipped.append((node, "無法自動判斷編號位置"))
                continue
            start, end, number_text = located
            current_number = int(round(chinese_to_arabic(number_text)))
            plans.append({
                "node": node, "raw_idx": raw_idx, "leading": leading,
                "clean": clean, "marker": marker, "start": start, "end": end,
                "number_text": number_text, "current_number": current_number,
            })

        if len(plans) < 2:
            detail = "\n".join(f"• {node.text(0)[:24]}：{reason}" for node, reason in skipped)
            dialogs.info(
                self, "連續編號",
                "選取範圍內能自動判斷編號位置的項目不足兩個，無法執行。\n\n" + detail,
            )
            return

        anchor_number = plans[0]["current_number"]
        targets = [anchor_number + offset for offset in range(len(plans))]

        selected_node_set = {plan["node"] for plan in plans}
        reference_signature = chapter_unit_signature(plans[0]["clean"])
        sibling_index = {}
        for parent in {plan["node"].parent() for plan in plans}:
            for sibling in toc_ops.tree_children(self.tree, parent):
                if sibling in selected_node_set:
                    continue
                sibling_row = self.chapter_raw_map.get(sibling)
                if sibling_row is None or sibling_row >= len(self.raw_lines):
                    continue
                sibling_clean, _m = strip_persistent_title_marker(self.raw_lines[sibling_row].strip())
                if chapter_unit_signature(sibling_clean) != reference_signature:
                    continue
                sibling_located = locate_chapter_number(sibling_clean)
                if sibling_located is None:
                    continue
                _s, _e, sibling_number_text = sibling_located
                key = (parent, int(round(chinese_to_arabic(sibling_number_text))))
                sibling_index.setdefault(key, sibling)

        conflicts = []
        for plan, target in zip(plans, targets):
            sibling = sibling_index.get((plan["node"].parent(), target))
            if sibling is not None:
                conflicts.append((plan, target, sibling))

        changes = [(plan, target) for plan, target in zip(plans, targets)
                  if plan["current_number"] != target]
        if not changes:
            dialogs.info(self, "連續編號", "選取的項目編號本來就已經連續，不需要調整。")
            return

        preview_lines = [f"「{plan['node'].text(0)[:22]}」：{plan['current_number']} → {target}"
                         for plan, target in changes[:8]]
        if len(changes) > 8:
            preview_lines.append(f"……等共 {len(changes)} 處")
        message = "將調整以下編號：\n\n" + "\n".join(preview_lines)
        if skipped:
            message += "\n\n以下項目無法判斷編號位置，將維持不變：\n" + "\n".join(
                f"• {node.text(0)[:22]}：{reason}" for node, reason in skipped)
        if conflicts:
            conflict_lines = "\n".join(
                f"• 目標編號 {target} 與「{sibling.text(0)[:22]}」重複"
                for _plan, target, sibling in conflicts[:5])
            message += "\n\n⚠️ 以下目標編號會與未選取的項目重複，繼續的話會出現重複編號：\n" + conflict_lines
        message += "\n\n此操作可用「復原」撤銷。是否繼續？"

        if not dialogs.confirm(self, "連續編號", message):
            return

        marker_suffix = {"exclude": "[::X]", "include": "[::]",
                         "auto_work": "[::W]", "auto_title": "[::T]", "": ""}
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for plan, target in changes:
            new_number_text = render_chapter_number_like(target, plan["number_text"])
            new_clean = plan["clean"][:plan["start"]] + new_number_text + plan["clean"][plan["end"]:]
            new_line = plan["leading"] + new_clean + marker_suffix[plan["marker"]]
            self._replace_line(cursor, plan["raw_idx"], new_line)
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已把 {len(changes)} 章改成連續編號，可以按 Ctrl+Z 復原")

    def _exclude_marker_for(self, raw_idx: int, line: str) -> str:
        """把某一行移出目錄時，行尾該留什麼？

        只有「使用者自己用 [::] 加進目錄、而且拿掉標記之後不會被自動認成
        章節」的行，才直接把標記清掉——那段正文本來就不是章節，留一個 [::X]
        只是在檔案裡多一個看不懂的符號。

        其他情況一律寫 [::X]：沒有標記、或是 [::W]／[::T] 這類自動標記的行，
        它會在目錄裡是因為別的機制（合集結構、自動標題快取、強制層級…），
        只清標記的話文字根本沒變，重新掃描它又會回來，使用者卻看到「已移除」。"""
        clean, marker = strip_persistent_title_marker(line.strip())
        if marker != "include":
            return "[::X]"
        if raw_idx in self.force_lv1_chapters or raw_idx in self.force_lv2_chapters:
            return "[::X]"
        return "[::X]" if looks_like_auto_chapter(clean, self.user_chapter_rules) else ""

    @action
    def ignore_selected_chapter(self):
        """章節保留在正文中，只是行尾加上 [::X]，從目錄移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = sorted({raw_idx for node in selected
                          if (raw_idx := self.chapter_raw_map.get(node)) is not None}, reverse=True)
        if not targets:
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        marked_count, last_clean, marked_any = 0, "", False
        for raw_idx in targets:
            raw_str = self.raw_lines[raw_idx]
            clean, marker = strip_persistent_title_marker(raw_str.strip())
            if marker == "exclude":
                continue
            leading = raw_str[:len(raw_str) - len(raw_str.lstrip())]
            suffix = self._exclude_marker_for(raw_idx, raw_str)
            self._replace_line(cursor, raw_idx, leading + clean + suffix)
            marked_any = marked_any or bool(suffix)
            marked_count += 1
            last_clean = clean
        cursor.endEditBlock()
        if not marked_count:
            return
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        note = "（行尾已加上 [::X]）" if marked_any else "（原本就不是章節，已直接清掉標記）"
        if marked_count == 1:
            self._show_status(f"已標註為非章節：{last_clean[:18]}{note}")
        else:
            self._show_status(f"已把 {marked_count} 章標註為非章節{note}")

    @action
    def set_chapter_level(self, level: int):
        """章節層級（force_lv1/2_chapters）是跟著文件走的結構狀態，不是文字，
        所以就算沒有改到任何一個字，也要單獨存一次復原步驟，不然 Ctrl+Z
        永遠碰不到這個操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = {raw_idx for node in selected if (raw_idx := self.chapter_raw_map.get(node)) is not None}
        if not targets:
            return
        for raw_idx in targets:
            if level == 1:
                self.force_lv1_chapters.add(raw_idx)
                self.force_lv2_chapters.discard(raw_idx)
            elif level == 2:
                self.force_lv2_chapters.add(raw_idx)
                self.force_lv1_chapters.discard(raw_idx)
        self._rebuild_toc()
        self._checkpoint_document()
        if len(targets) == 1:
            raw_str = self.raw_lines[next(iter(targets))].strip()
            self._show_status(f"已設為{'卷' if level == 1 else '章'}標題：{raw_str[:15]}")
        else:
            self._show_status(f"已把 {len(targets)} 項設為{'卷' if level == 1 else '章'}標題")

    @action
    def delete_selected_chapter(self):
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        lines = self.editor.toPlainText().split("\n")
        total_lines = len(lines)
        sections = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in selected:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None:
                sections.append((span[0], span[1], node))
        if not sections:
            return
        sections.sort()
        merged = [sections[0]]
        for start, end, node in sections[1:]:
            last_start, last_end, last_node = merged[-1]
            if start < last_end:
                merged[-1] = (last_start, max(last_end, end), last_node)
            else:
                merged.append((start, end, node))
        sections = merged

        if len(sections) == 1:
            ch_name = sections[0][2].text(0)
            question = f"確定要刪除「{ch_name}」及其所有正文內容嗎？"
            done_text = f"已刪除：{ch_name}，可以按 Ctrl+Z 復原"
        else:
            preview_text = "、".join(node.text(0)[:20] for _, _, node in sections[:4])
            if len(sections) > 4:
                preview_text += f" 等 {len(sections)} 章"
            question = f"確定要刪除以下章節及其所有正文內容嗎？\n\n{preview_text}"
            done_text = f"已刪除 {len(sections)} 章，可以按 Ctrl+Z 復原"
        if not dialogs.confirm(self, "刪除章節", question):
            return

        document = self.editor.document()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for start, end, _ in reversed(sections):    # 由後往前刪除，讓前面區段的行號保持有效
            start_block = document.findBlockByNumber(start)
            end_block = document.findBlockByNumber(end)
            end_of_document = document.characterCount() - 1     # Qt 單位，不能用 len(文字)
            start_pos = start_block.position() if start_block.isValid() else end_of_document
            end_pos = end_block.position() if end_block.isValid() else end_of_document
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(done_text)

    # ------------------------------------------------------------------
    # 本文右鍵選單：加入目錄／取消「非章節」標記
    # ------------------------------------------------------------------

    def _build_editor_context_menu(self):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        刻意不用 QPlainTextEdit.createStandardContextMenu()：那組復原／剪下／
        複製／貼上／全選是 Qt 內建、沒套用中文翻譯，混在自己中文選單裡顯得
        突兀，而且復原／剪貼本來就有全域快捷鍵可用，選單裡不需要重複。

        「加入目錄」「移除目錄」一次只會有一個能按：看目標那一行現在是不是
        已經在目錄裡。"""
        menu = QMenu(self)
        insert_action = menu.addAction("新增章節")
        insert_action.triggered.connect(self.open_insert_title_dialog)
        menu.addSeparator()

        target = self._editor_target_line()
        in_toc = target is not None and target[0] in self._current_title_lines()
        add_action = menu.addAction("將所選文字加入目錄")
        add_action.setEnabled(target is not None and not in_toc)
        add_action.triggered.connect(self.mark_selected_as_title)
        remove_action = menu.addAction("將所選文字移除目錄")
        remove_action.setEnabled(target is not None and in_toc)
        remove_action.triggered.connect(self.exclude_selected_line)

        # 下方接上 Qt 自己的剪下／複製／貼上／全選：這些動作的文字由 Qt 的
        # 翻譯檔提供（見 i18n.install_qt_translation），會跟著繁簡切換。
        standard = self.editor.createStandardContextMenu()
        # 去掉 Qt 自己的復原／重做：Qt 的復原已經關掉（復原走自訂快照系統），
        # 留著只會是兩個永遠灰掉的項目。Ctrl+Z／Ctrl+Y 與工具列按鈕照常可用。
        undo_keys = {QKeySequence(QKeySequence.StandardKey.Undo).toString(),
                     QKeySequence(QKeySequence.StandardKey.Redo).toString()}
        # Qt 把快捷鍵直接寫在動作文字裡（「復原(&U)	Ctrl+Z」），shortcut() 是空的，
        # 所以從文字尾端取快捷鍵來比對。
        editing_actions = [action for action in standard.actions()
                           if action.text() and action.text().rpartition("	")[2] not in undo_keys]
        if editing_actions:
            menu.addSeparator()
            # 只把「動作」接過來，改由這個選單持有；標準選單本身要丟掉，
            # 不能拿它當子元件——QMenu 是一個真的視窗元件，掛成子元件會被
            # 整個畫在自訂選單上面，變成兩層選單疊在一起。
            for standard_action in editing_actions:
                standard_action.setParent(menu)
            menu.addActions(editing_actions)
        standard.deleteLater()
        return menu

    def _show_editor_context_menu(self, pos):
        # 在選取範圍以外按右鍵時，先把游標移到按的位置：選單裡的動作（新增
        # 章節、加入／移除目錄）都是針對「游標所在那一行」。
        clicked = self.editor.cursorForPosition(pos)
        current = self.editor.textCursor()
        if not (current.hasSelection()
                and current.selectionStart() <= clicked.position() <= current.selectionEnd()):
            self.editor.setTextCursor(clicked)
        menu = self._build_editor_context_menu()
        menu.exec(self.editor.viewport().mapToGlobal(pos))

    def _current_title_lines(self) -> set:
        """目前目錄裡每個節點在正文的行號（已換算到編輯器現在的內容）。"""
        self._sync_raw_lines()
        return {self._map_tree_line(row) for row in self.chapter_raw_map.values()}

    def _editor_target_line(self):
        """右鍵動作的目標行：有選取時是選取範圍內唯一的非空白行，沒選取時是
        游標所在行。多行或空白行回傳 None——避免把整段正文誤加進目錄。"""
        cursor = self.editor.textCursor()
        document = self.editor.document()
        if not cursor.hasSelection():
            block = cursor.block()
            return (block.blockNumber(), block.text()) if block.text().strip() else None
        start_block = document.findBlock(cursor.selectionStart())
        end_block = document.findBlock(cursor.selectionEnd())
        end_number = end_block.blockNumber()
        if cursor.selectionEnd() == end_block.position() and end_number > start_block.blockNumber():
            end_number -= 1
        lines = []
        for number in range(start_block.blockNumber(), end_number + 1):
            text = document.findBlockByNumber(number).text()
            if text.strip():
                lines.append((number, text))
        return lines[0] if len(lines) == 1 else None

    @action
    def mark_selected_as_title(self):
        """把目標行標成目錄章節（行尾加 [::]；原本若是 [::X] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一個標題", "請選取（或把游標放在）單獨一行章節標題，避免將正文誤加到目錄。")
            return
        clean_title = self._set_line_marker(target[0], "[::]")
        if clean_title is None:
            return
        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == target[0]:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._show_status(f"已加入目錄：{clean_title}")

    @action
    def exclude_selected_line(self):
        """把目標行移出目錄（行尾加 [::X]；原本若是 [::] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一行", "請選取（或把游標放在）單獨一行章節標題。")
            return
        suffix = self._exclude_marker_for(target[0], target[1])
        clean_title = self._set_line_marker(target[0], suffix)
        if clean_title is None:
            return
        self._show_status(f"已移出目錄：{clean_title}" if suffix
                          else f"已移除章節標記：{clean_title}")

    def _set_line_marker(self, line_number: int, marker: str):
        block = self.editor.document().findBlockByNumber(line_number)
        original = block.text()
        clean, _old_marker = strip_persistent_title_marker(original.strip())
        if not clean:
            return None
        leading = original[:len(original) - len(original.lstrip())]
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        self._replace_line(cursor, line_number, leading + clean + marker)
        cursor.endEditBlock()
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        return clean

