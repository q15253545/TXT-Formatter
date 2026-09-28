"""PySide6 主視窗：畫面、開檔／存檔、目錄樹、排版、搜尋取代、復原重做。工具視窗、右鍵操作、介面狀態
在 window_tools／window_toc_edit／window_state（mixin）。

章節辨識與排版邏輯完全交給 core.structure_builder，這裡只負責畫面與把使用者
的操作轉成呼叫 core 純函式的參數。

復原／重做刻意不用 QPlainTextEdit 內建的 QTextDocument undo：忽略集合、
強制卷／章層級、自動標題快取這些「章節結構」狀態跟正文是綁在一起的，
只復原文字、不復原這些狀態，會讓目錄跟正文對不起來（點右鍵選單的操作
之後按 Ctrl+Z，文字復原了但目錄還停在操作後的樣子）。所以是
整份文字＋結構狀態一起存成快照（見 _checkpoint_document／
_restore_document_step），輸入文字時用計時器合併成一步，不是每個按鍵
存一份。
"""

import bisect
import dataclasses
import os
import sys
import time
from collections import Counter

from PySide6.QtCore import QProcess, QTimer, Qt, QUrl
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QFont, QKeySequence, QShortcut, QTextBlockFormat, QTextCharFormat,
    QTextCursor, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.cn_numerals import chinese_to_arabic
from core.chapter_parse import (
    DEFAULT_TITLE_TAIL_ALLOWED, MAX_TITLE_LENGTH, build_title_check, heading_number, compact_number_ranges,
    extract_author_from_intro,
)
from core.ad_scan import AD_CATEGORY_LABELS, FIX_CATEGORIES, scan_ad_candidates
from core.user_rules import PRESET_RULES, pop_timed_out_rules, preset_match
from core.collection import (
    chapter_gap_report, group_formal_chapters, missed_middle_chapters, missed_tail_chapters, missed_volumes,
    scan_chapter_candidates,
)
from core.encoding import detect_line_ending, smart_detect_encoding, strip_stray_bom
from core.file_io import read_text, read_text_lossy, write_text_atomic
from core.filename_meta import (
    DEFAULT_COMPLETED_TEMPLATE, DEFAULT_ONGOING_TEMPLATE, build_smart_filename, extract_filename_metadata,
    filename_fields, filename_template_for,
)
from core.format_options import FormatOptions
from core.script_convert import SCRIPT_SIMP, SCRIPT_TRAD, convert_script
from core.scan_cache import WARM_PHASES, clear_line_caches, freeze_line_caches
from core.structure_builder import BuildContext, build_document_structure
from core.persistence import (
    APP_DATA_DIR, RULES_FILE, UI_STATE_FILE, WINDOW_FILE, _save_json, load_ui_state, load_user_chapter_rules,
    save_ui_state, save_window_state,
)
from core.title_blocks import TEMPLATE_LABELS, TEMPLATES, block_rule, template
from core.title_markers import strip_export_markers, strip_persistent_title_marker

from . import app_log, dialogs, i18n, icons
from .app_log import action, log, native_dialog, timed
from .help_dialog import HelpDialog
from .content_panel import ContentPanel
from .chapter_panel import ChapterPanel
from .find_bar import FindBar
from .filename_dialog import FilenameDialog
from .metadata_bar import ENCODING_CODECS, MetadataBar
from .options_panel import OptionsPanel, describe_options
from .text_positions import PositionMap
from .theme import DARK, DEFAULT_THEME, THEMES, build_stylesheet, set_active_tokens, theme_tokens
from .widgets import (
    AppWidgetPolisher, Card, ClickableLabel, Editor, IconButton, IconTextButton, LanguageToggle, ElidedLabel,
    ThemeButton, VDivider, make_card_header,
)
from . import __version__
from .window_common import (
    DEFAULT_STRUCTURE_MODE, EDITOR_BASE_FONT_PX, EDITOR_ZOOM_MAX, EDITOR_ZOOM_MIN, MARKER_GUIDE,
    MARK_SCAN_DELAY_MS, MAX_HIGHLIGHT_SPANS, MAX_HISTORY_CHARS, MAX_HISTORY_STEPS, MIN_HISTORY_STEPS,
    MIN_WINDOW_WIDTH, PENDING_LINE_MAP_LIMIT, TYPING_CHECKPOINT_DELAY_MS, WARM_CHUNK_LINES, WARM_NOW_LINES,
    WARM_START_DELAY_MS, WARM_WAITING_SLICE, _LayoutWatcher, _MARKER_REGEX, _MarkScanSignals,
    _NUMBER_WITHOUT_UNIT, _ToolDialogWatcher, _chapter_line_mapper, _diff_line_mapper, _format_line_mapper,
    _line_opcodes, _settle, _tree_depth, short_toc_label,
)
from .window_state import WindowStateMixin
from .window_tools import ToolWindowsMixin
from .window_toc_edit import TocEditMixin


class MainWindow(WindowStateMixin, ToolWindowsMixin, TocEditMixin, QMainWindow):
    @property
    def tokens(self):
        """目前主題的配色。"""
        return theme_tokens(self.theme_name)

    @property
    def dark_mode(self) -> bool:
        return self.tokens.is_dark

    def __init__(self):
        super().__init__()
        # 英文名稱：繁簡切換時不用跟著換（設定檔資料夾也叫 TXTFormatter）
        self.setWindowTitle("TXT Formatter")
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
        self.structure_mode = DEFAULT_STRUCTURE_MODE
        # 標題結尾允許字元（自訂章節規則 → 標題結尾）
        self.title_tail_allowed = DEFAULT_TITLE_TAIL_ALLOWED
        self.title_tail_custom = ""     # 使用者在「標題結尾」分頁自己加的標點
        self.disabled_words = frozenset()           # 「辨識章節」關掉的章節單位、特殊標題
        self.special_levels = {}                    # 「辨識章節」改成卷或章的特殊標題（只記跟預設不同的）
        self._side_width = 0                        # 功能卡片最後的寬度：關掉再打開時照這個寬度
        self.max_title_length = MAX_TITLE_LENGTH    # 「辨識格式 → 標題長度」
        self.filename_ongoing = DEFAULT_ONGOING_TEMPLATE     # 匯出檔名格式（連載中／未指定）
        self.filename_completed = DEFAULT_COMPLETED_TEMPLATE  # 匯出檔名格式（已完結）
        self.filename_script = SCRIPT_TRAD                    # 匯出檔名轉繁體／簡體；跟著介面繁簡切換
        self.absorbed_titles: dict = {}             # 重複標題的預覽：重複那一行 → 保留的標題行
        self.absorbed_title_items: set = set()
        self._infer_volumes = False     # 章節管理的「自動補齊卷號與卷名」開關
        self._auto_apply_preview = False  # 「自動套用到一鍵排版」：一鍵排版前先把章節管理的預覽寫進本文
        self._merge_titles = False      # 「自動合併標題」：下行章名＋重複標題（預覽，套用到本文才寫進去）
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
        self._warm_phase = 0                # index into WARM_PHASES; phase 0 is what the scan windows need
        self._warm_waiters: list = []      # run once phase 0 of the idle-time cache warming is done
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
        self.content_panel.mark_toggle.clicked.connect(
            lambda on: self._show_status("已顯示本文字色：無關連內容、作者感言與作品資訊（顏色定義見說明）"
                                         if on else "已隱藏本文字色"))
        side_layout.addWidget(self.content_panel)
        self.content_panel.hide()

        self.chapter_panel = ChapterPanel()
        self.chapter_panel.closed.connect(lambda: self._set_active_side_panel(None))
        self.chapter_panel.recognition_requested.connect(self.open_recognition_dialog)
        self.chapter_panel.rules_requested.connect(self.open_rules_dialog)
        self.chapter_panel.merge_duplicates_requested.connect(self.open_duplicate_chapters_dialog)
        self.chapter_panel.check_missing_requested.connect(self.check_missing_chapters)
        self.chapter_panel.missing_mode_changed.connect(self._refresh_missing_report)
        self.chapter_panel.merge_titles_toggled.connect(self._on_merge_titles_toggled)
        self.chapter_panel.infer_volumes_toggled.connect(self._on_infer_volumes_toggled)
        self.chapter_panel.auto_apply_preview_toggled.connect(self._on_auto_apply_preview_toggled)
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
            get_cursor=self._find_cursor,
        )
        side_layout.addWidget(self.find_bar)
        self.find_bar.hide()

        # 不寫死最小寬度：讓卡片最窄就是「剛好裝得下面板內容」，寫死的數字
        # 一旦比內容窄，拉到最小時下拉框、按鈕就會超出卡片。No maximum either: once the TOC card is at its
        # minimum, QSplitter keeps pushing and the text card gives up the width.
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
        self._toc_hint_key = self._toc_hint_result = None
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
        # 章節標記平常藏起來（1px 透明字），但它們是真的寫在檔案裡的：顯示與否在「章節管理」卡片，
        # 匯出時要不要拿掉在匯出設定；說明按鈕在檔名列。
        self.marker_button = self.chapter_panel.show_markers_toggle
        self.marker_button.toggled.connect(self._on_markers_toggled)
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
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self.open_replace)
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
        # 匯出 TXT 旁邊的箭頭打開匯出設定；兩顆靠在一起，像同一顆按鈕分成兩半
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
        minus_mark = icons.icon_file_path("minus", tokens.accent_text, 13)
        QApplication.instance().setStyleSheet(
            build_stylesheet(tokens, chevron_closed, chevron_open, check_mark, chevron_up, minus_mark))
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
    # 關閉視窗、換檔前確認未匯出的修改
    # ------------------------------------------------------------------


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
        if getattr(self, "_restarting", False):
            event.accept()
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
    # 開檔
    # ------------------------------------------------------------------


    @action
    def open_file(self):
        if not self._confirm_discard_changes():
            return
        with native_dialog():
            path, _ = QFileDialog.getOpenFileName(
                self, i18n.T("開啟 TXT 檔案"), self._remembered_dir("last_open_dir"),
                i18n.T("文字檔 (*.txt);;所有檔案 (*)"))
        if not path:
            return
        self._ui_state["last_open_dir"] = os.path.dirname(path)
        self.load_file_path(path)

    def _remembered_dir(self, key: str) -> str:
        """上次開檔／匯出的資料夾；已經不在了（隨身碟拔掉、改名）就不用。"""
        folder = self._ui_state.get(key)
        return folder if isinstance(folder, str) and folder and os.path.isdir(folder) else ""

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
        self._warm_phase = 0
        self._warm_timer.start(WARM_START_DELAY_MS)

    def _drop_line_caches(self):
        self._warm_timer.stop()
        self._warm_lines = None
        self._warm_waiters.clear()
        clear_line_caches()

    def _caches_cold(self) -> bool:
        """Are the scan windows' caches (phase 0) still far from warm? A little left (a small file, or warming
        almost done) is just finished now — well under half a second — so only a large file makes a scan wait."""
        if self._warm_lines is None or self._warm_phase > 0:
            return False
        if len(self._warm_lines) - self._warm_position > WARM_NOW_LINES:
            return True
        WARM_PHASES[0](self._warm_lines[self._warm_position:])
        self._warm_position = len(self._warm_lines)
        self._warm_step()           # moves on to the next phase and runs anything waiting
        return False

    def _when_warm(self, callback):
        """Run callback once the scan windows' caches are warm; the warming continues right away in idle-time
        chunks, so the window stays usable meanwhile."""
        self._warm_waiters.append(callback)
        self._warm_timer.start(0)

    def _warm_step(self):
        lines = self._warm_lines
        if lines is None:
            return
        if self._warm_position >= len(lines):
            if self._warm_phase == 0:
                waiters, self._warm_waiters = self._warm_waiters, []
                for callback in waiters:
                    callback()
            self._warm_phase += 1
            self._warm_position = 0
            if self._warm_phase >= len(WARM_PHASES):
                self._warm_lines = None
                freeze_line_caches()
                return
        warm = WARM_PHASES[self._warm_phase]
        # A scan window waiting gets bigger slices (one per 40 ms) so its results come sooner; the window still
        # gets a turn between slices.
        deadline = time.perf_counter() + (WARM_WAITING_SLICE if self._warm_waiters else 0)
        while True:
            end = self._warm_position + WARM_CHUNK_LINES
            warm(lines[self._warm_position:end])
            self._warm_position = end
            if self._warm_position >= len(lines) or time.perf_counter() >= deadline:
                break
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
        # 配舊的行號」，復原之後強制層級、自動標題記錄會落在別行。
        state = self._document_state()
        if self._history_position >= 0 and self._history[self._history_position] == state:
            self._update_history_buttons()
            return
        del self._history[self._history_position + 1:]
        self._history.append(state)
        self._trim_history()
        self._history_position = len(self._history) - 1
        self._update_history_buttons()

    def _document_state(self) -> tuple:
        """目前的正文與章節結構（復原歷史的一步）。"""
        self._sync_raw_lines()
        text = (self._synced_text if self._synced_text_version == self._text_version
                and self._synced_text is not None else self.editor.toPlainText())
        return (
            text,
            frozenset(self.force_lv1_chapters),
            frozenset(self.force_lv2_chapters),
            dict(self.auto_titles),
        )

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

    def _restore_state(self, state: tuple):
        """換回 _document_state() 存下的一份正文與章節結構（不記成新的一步）。"""
        self._restoring_history = True
        try:
            self._set_editor_text(state[0], "diff")
            # 先記下行號位移（目錄選取要用），再用快照整組覆蓋章節狀態。
            self._adopt_lines(state[0].split("\n"), remap_state=False)
            self.force_lv1_chapters = set(state[1])
            self.force_lv2_chapters = set(state[2])
            self.auto_titles = dict(state[3])
            self._rebuild_toc()
        finally:
            self._restoring_history = False

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
        self._restore_state(self._history[target])
        self._history_position = target
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
        # 上次匯出的資料夾；還沒匯出過就放在原檔旁邊
        folder = (self._remembered_dir("last_export_dir")
                  or (os.path.dirname(self.input_file) if self.input_file else "")
                  or self._remembered_dir("last_open_dir"))
        with native_dialog():
            path, _ = QFileDialog.getSaveFileName(self, i18n.T("另存新檔"),
                                                  os.path.join(folder, default_name) if folder else default_name,
                                                  i18n.T("文字檔 (*.txt)"))
        if not path:
            return False
        stripped = 0
        if self._strip_markers_on_export if strip_markers is None else strip_markers:
            content, stripped = strip_export_markers(content)
        try:
            # 寫暫存檔、成功才取代目標檔：中途失敗時原本的檔案不會被清空。
            # 加 BOM：沒有 BOM 的 UTF-8，Windows 檔案總管的預覽、舊版記事本會當成系統編碼（Big5）顯示成亂碼
            write_text_atomic(path, content, encoding="utf-8-sig")
        except (OSError, UnicodeError) as error:
            dialogs.error(self, "存檔失敗", f"無法寫入檔案：\n{path}\n\n{error}\n\n原本的檔案沒有被更動。")
            return False
        self._ui_state["last_export_dir"] = os.path.dirname(path)
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
                                self._filename_fields(), self.metadata_bar.status(), self,
                                strip_markers=self._strip_markers_on_export)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.filename_ongoing, self.filename_completed = dialog.result_ongoing, dialog.result_completed
        self.filename_script = dialog.result_script
        self._strip_markers_on_export = dialog.result_strip_markers
        self._show_status(i18n.T("匯出檔名：") + self._suggest_export_filename()
                          + i18n.T("；匯出時移除章節標記" if self._strip_markers_on_export else "；匯出時保留章節標記"),
                          translated=True)

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
            invalid_tail_regex=self._title_check(),
            infer_volumes=self._infer_volumes,
            merge_titles=self._merge_titles,
            disabled_words=self.disabled_words,
            special_levels=self.special_levels,
            skip_duplicate_titles=self._merge_titles,
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
        目錄沒有卷——本文有連號的「卷一 卷名」（不帶「第」），加成組合；
        目錄是空的或太稀——本文裡最多的一種常用寫法（高信心的可疑章節至少 3 行、目錄章數的五倍），
        加成辨識章節的組合；
        後半本換了寫法——最後一章之後的常用寫法章號接著往下數，一樣加成組合；接不上號、但後面還有
        一大段、裡面有同一種寫法的可疑章節（「2-1」這種沒有內建組合的），打開可疑章節；
        書中間換了寫法——目錄章號缺的那一段在本文是另一種常用寫法，加成組合。"""
        self._toc_hint_template = None
        self._toc_hint_format = None
        if not any(line.strip() for line in self.raw_lines):
            self.toc_hint.hide()
            return
        # 行數、目錄項數、辨識規則都沒變（打字、改字）：沿用上次的判斷，不用再掃
        key = (self.input_file, len(self.raw_lines), len(self.chapter_raw_map), self.max_title_length,
               tuple(rule.get("pattern") for rule in self.user_chapter_rules))
        if key != self._toc_hint_key:
            self._toc_hint_key, self._toc_hint_result = key, self._find_toc_hint()
        text, self._toc_hint_template, self._toc_hint_format = self._toc_hint_result
        if text is None:
            self.toc_hint.hide()
            return
        i18n.set_text(self.toc_hint_label, text)
        i18n.set_text(self.toc_hint_button, "查看可疑章節" if self._toc_hint_format else "加入辨識章節")
        self.toc_hint.show()

    def _find_toc_hint(self):
        """（提示文字, 要加的內建組合, 要看的可疑章節格式）；沒有要提示的回傳 (None, None, None)。"""
        known = set(self.chapter_raw_map.values())
        kinds = Counter(record.get("kind") for record in self.chapter_records.values())
        # 目錄是空的或太稀（只認到零星幾個「第N章」，整本其實是另一種寫法）：看整本最多的常用寫法。
        # 平常的書每章幾十到一兩百行，不用整本掃（開檔時快取還沒算好，整本掃會卡）
        if len(known) <= 5 or len(self.raw_lines) > 400 * len(known):
            counts = Counter(candidate["format"] for candidate in
                             scan_chapter_candidates(self.raw_lines, known, self.max_title_length)
                             if candidate["confidence"] == "高" and candidate["format"].startswith("preset:"))
            template_ids = {key for key, level, _blocks in TEMPLATES if level == 2}
            found = next(((fmt.split(":", 1)[1], count) for fmt, count in counts.most_common()
                          if fmt.split(":", 1)[1] in template_ids
                          and count >= max(3, 5 * kinds.get("chapter", 0))), None)
            if found is not None:
                return f"本文有 {found[1]} 行是「{TEMPLATE_LABELS[found[0]]}」這種寫法", found[0], None
        # 章都收到了再看卷：目錄一個卷都沒有，本文卻有「卷一 卷名」這種不帶「第」的卷，卷號連號
        if not kinds.get("volume"):
            count = missed_volumes(self.raw_lines, known)
            if count:
                return (f"本文有 {count} 行是「{TEMPLATE_LABELS['leading_unit_volume']}」這種寫法",
                        "leading_unit_volume", None)
        if self.chapter_raw_map:
            hint = self._toc_tail_hint()
            if hint[0] is None:
                hint = self._toc_middle_hint()
            return hint
        return None, None, None

    def _toc_middle_hint(self):
        """目錄中間缺的章是另一種寫法（core.collection.missed_middle_chapters）。"""
        chapters = sorted((row, self.chapter_records.get(item, {}).get("number"))
                          for item, row in self.chapter_raw_map.items()
                          if self.chapter_records.get(item, {}).get("kind") == "chapter")
        missed = missed_middle_chapters(self.raw_lines, chapters, self.max_title_length)
        if missed is None:
            return None, None, None
        after = missed["after"]
        name = f"第{int(after) if float(after).is_integer() else after}章"
        return (f"目錄在{name}之後少的 {missed['count']} 章是「{TEMPLATE_LABELS[missed['key']]}」這種寫法",
                missed["key"], None)

    def _toc_tail_hint(self):
        """最後一個目錄項目之後漏掉的章節（core.collection.missed_tail_chapters）。"""
        last_row = max(self.chapter_raw_map.values())
        chapters = sorted((row, self.chapter_records.get(item, {}).get("number"))
                          for item, row in self.chapter_raw_map.items()
                          if self.chapter_records.get(item, {}).get("kind") == "chapter")
        missed = missed_tail_chapters(self.raw_lines, chapters, last_row, self.max_title_length)
        if missed is None:
            return None, None, None
        last_number = chapters[-1][1]
        name = (f"第{int(last_number) if float(last_number).is_integer() else last_number}章"
                if isinstance(last_number, (int, float)) else "最後一章")
        if missed["kind"] == "template":
            return (f"目錄到{name}為止，後面有 {missed['count']} 行是「{TEMPLATE_LABELS[missed['key']]}」這種寫法",
                    missed["key"], None)
        return f"目錄到{name}為止，後面還有 {missed['count']} 行像章節標題", None, missed["format"]

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
        """自動合併標題（重複標題）的預覽：會被刪掉的那一行畫刪除線、用非原文色（本文不改）。"""
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

    def _on_markers_toggled(self, shown: bool):
        """切換只影響顯示，一個字都不會動到。"""
        self._show_title_markers = shown
        self._refresh_title_formats()
        self._show_status("已顯示章節標記（章節標記定義見說明）" if shown else "已隱藏章節標記")

    def restore_defaults(self, parent=None):
        """說明視窗的「還原預設」：刪掉設定檔（組合規則、介面狀態、視窗大小），重新開啟程式；
        開著的檔案一起帶過去。一項一項改回預設很容易漏，重開最保險。"""
        if not dialogs.confirm(parent or self, "還原預設",
                               "辨識章節的組合、排版設定、開關、視窗大小都會還原成預設，程式會重新開啟。"):
            return
        if not self._confirm_discard_changes():
            return
        for path in (RULES_FILE, UI_STATE_FILE, WINDOW_FILE):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                app_log.log.warning("還原預設：刪不掉 %s", path, exc_info=True)
        if getattr(sys, "frozen", False):
            program, arguments = sys.executable, []
        else:
            program, arguments = sys.executable, ["-m", "ui_qt"]
        if self.input_file:
            arguments.append(self.input_file)
        QProcess.startDetached(program, arguments, os.getcwd())
        # 關閉時不再把目前的設定寫回去（剛刪掉的檔案會又出現）
        self._restarting = True
        if parent is not None:
            parent.reject()
        self.close()

    def _show_marker_help(self):
        """檔名列的問號（說明）：章節標記、本文字色、自動補齊卷號與卷名、設定檔位置。"""
        dialog = HelpDialog(self.tokens, [(mark, i18n.T(detail)) for mark, detail in MARKER_GUIDE],
                            APP_DATA_DIR, self, on_restore=self.restore_defaults)
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
            entries.append({"kind": kind, "start": int(number), "end": guess, "row": row, "found": [],
                            "text": self.raw_lines[row].strip()[:16] if 0 <= row < len(self.raw_lines) else ""})
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
                reason = "已移出目錄"
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
        段落之間不空行、標題前兩行後一行、段首兩個全形空格、編號間隔用半形空格
        （使用者按過「保存到一鍵排版」就用存下來的組合）。不做合併下行標題：
        那是猜測，要在章節管理預覽過再套用。

        排版前先檢查缺章與高信心廣告：排版會重排整份文字，事後比較難回頭
        確認原本的問題，所以有狀況時先問過再動手。"""
        if not self.editor.toPlainText().strip():
            return
        self._sync_raw_lines()
        # 「自動套用到一鍵排版」：章節管理的預覽先寫進本文，跟排版算同一步；取消時整個退回
        preview = self._toc_preview_lines() if self._auto_apply_preview else None
        before = None
        message = "已套用一鍵排版，可以按 Ctrl+Z 復原"
        if preview is not None:
            before = self._document_state()
            self._set_editor_text("\n".join(preview[0]), "diff")
            # the replacement restarts the typing timer: stopped here, the sync below would save the
            # preview as a step of its own (typed text was already saved by _set_editor_text)
            self._typing_checkpoint_timer.stop()
            self._sync_raw_lines()
            self._ensure_toc_current()
            message = "已套用一鍵排版（" + "、".join(preview[1]) + "），可以按 Ctrl+Z 復原"
        options = self._one_click_options()
        # 排版前的檢查直接用排版那一次的辨識結果，不另外再建一次結構（大檔每次要好幾秒）。
        applied = self._apply_format_options(
            options, message, confirm=self._confirm_one_click_warnings)
        if not applied:
            if before is not None:
                self._restore_state(before)
            self._show_status("已取消一鍵排版")
            return
        self.options_panel.reset_to_defaults()

    def _one_click_options(self) -> FormatOptions:
        """一鍵排版的組合：使用者按過「保存到一鍵排版」就用存下來的，否則用內建的。"""
        saved = self._ui_state.get("one_click_options")
        if isinstance(saved, dict):
            # 合併下行標題不在一鍵排版做（在章節管理預覽再套用）：存下來的組合裡有 merge_title 也不用
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
                self.raw_lines, set(AD_CATEGORY_LABELS) - FIX_CATEGORIES,
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
        for attribute in ("force_lv1_chapters", "force_lv2_chapters"):
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
        強制層級與自動標題記錄才不會跑掉（跟剪下／貼上同一套作法）。"""
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
            self, "套用格式到選取的章",
            f"將對選取的 {chapter_count} 章（共 {lines_count} 行）套用：\n\n"
            + "\n".join(f"• {item}" for item in items)
            + "\n\n沒有選到的章節維持原樣，所以整本書可能看起來不一致"
              "（章節編號樣式、編號間隔這類設定尤其明顯）。\n"
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
            invalid_tail_regex=self._title_check(),
            infer_volumes=self._infer_volumes,
            disabled_words=self.disabled_words,
            special_levels=self.special_levels,
        )

    @property
    def _title_rows(self) -> set:
        """目前所有章節標題所在的行（0 起算）。"""
        return {row - 1 for row in self.chapter_index_map.values()}

    def _remap_chapter_state(self, moved_titles: dict):
        """排版後行號全變了，把以行號為鍵的章節狀態搬到新行號。"""
        for attribute in ("force_lv1_chapters", "force_lv2_chapters"):
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

    def open_replace(self):
        """Ctrl+H: open find & replace (never closes it) with the caret in the replace box."""
        if not self.raw_lines:
            return
        if not self.find_bar.isVisible():
            self._set_active_side_panel(self.find_bar)
            self.find_bar.refresh()
        self.find_bar.focus_replace()

    def close_find_bar(self):
        if self.find_bar.isVisible():
            self._set_active_side_panel(None)
        self.editor.setExtraSelections([])

    # 尋找列算出來的是 Python 字元位置，游標吃的是 Qt（UTF-16）位置：
    # 本文只要出現過一個 emoji 或擴充漢字，後面每個位置就會差一格，取代會
    # 改到前一個字。所有進出游標的位置都經過這裡換算。

    def _find_cursor(self):
        cursor = self.editor.textCursor()
        positions = self._positions()
        return positions.to_python(cursor.selectionStart()), positions.to_python(cursor.selectionEnd())

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
        if not spans:
            # nothing to convert: don't build the whole-document position map (it happens on every keystroke
            # while the find panel is open)
            self.editor.setExtraSelections([])
            return
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
    # 本文與行號：工作版本（raw_lines）、整份替換後的行號換算
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
        for attribute in ("force_lv1_chapters", "force_lv2_chapters"):
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


