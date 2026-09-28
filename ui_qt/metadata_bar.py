"""檔案資訊列＋可展開的「書籍資料」欄位。

摺疊時只顯示檔名與幾個狀態徽章；平常不需要書名／作者／結構／編碼這些
設定一直佔畫面，點「書籍資料」展開才看得到，呼應使用者提供的介面設計圖。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMenu, QSizePolicy,
    QVBoxLayout, QWidget,
)

from . import i18n
from .widgets import Divider, IconButton, IconTextButton, VDivider

STRUCTURE_CHOICES = ["自動判斷", "單本小說", "多作品合集"]
STATUS_CHOICES = ["未指定", "未完結", "已完結"]
ENCODING_CHOICES = ["自動", "UTF-8", "UTF-16", "Big5", "GB18030"]
ENCODING_CODECS = {"UTF-8": "utf-8", "UTF-16": "utf-16", "Big5": "big5", "GB18030": "gb18030"}


class MetadataBar(QWidget):
    details_height_changed = Signal()      # 書籍資料展開、收起、換成兩行：主視窗重算最矮的高度
    structure_changed = Signal(str)
    encoding_changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("metadataBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        summary = QWidget()
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(20, 10, 20, 10)
        summary_layout.setSpacing(10)

        # 檔名要能反白複製（常常要拿去搜尋或改名）；大小另外放一個標籤，
        # 複製到的才會是乾淨的檔名。
        self.filename_label = QLabel("尚未開啟檔案")
        # 檔名是內容，不跟著介面切換繁簡；沒開檔時的提示字另外處理。
        i18n.skip(self.filename_label)
        self._filename_placeholder = True
        self.filename_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.filename_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.filename_label.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.filename_label.customContextMenuRequested.connect(self._show_filename_menu)
        summary_layout.addWidget(self.filename_label)
        self.size_label = QLabel("")
        self.size_label.setObjectName("fileLabel")
        summary_layout.addWidget(self.size_label)
        self.status_badge = QLabel("")
        self.status_badge.setObjectName("badge")
        self.status_badge.hide()
        summary_layout.addWidget(self.status_badge)
        self.encoding_badge = QLabel("")
        self.encoding_badge.setObjectName("badge")
        self.encoding_badge.hide()
        summary_layout.addWidget(self.encoding_badge)
        # 說明（章節標記、本文字色、自動補齊卷、設定檔位置）
        self.help_button = IconButton("circle-help", "說明", size=16)
        summary_layout.addWidget(self.help_button)
        summary_layout.addStretch(1)

        self.toggle_button = IconTextButton("chevron-down", "書籍資料", checkable=True)
        self.toggle_button.setObjectName("barToggle")
        self.toggle_button.toggled.connect(self._on_toggled)
        summary_layout.addWidget(self.toggle_button)
        root.addWidget(summary)

        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(20, 0, 20, 14)
        details_layout.setSpacing(0)
        details_layout.addWidget(Divider())

        grid = QGridLayout()
        grid.setContentsMargins(0, 12, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(10)
        details_layout.addLayout(grid)
        # 視窗矮的時候由下面的卡片讓出高度，欄位不能被壓扁
        self.details.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.details.hide()
        root.addWidget(self.details)

        self._grid = grid
        self._boxes = []            # 七個欄位（標籤＋輸入框），照順序
        self.title_input = QLineEdit()
        self._add_field(grid, 0, 0, "書名", self.title_input)
        self.author_input = QLineEdit()
        self._add_field(grid, 0, 1, "作者", self.author_input)
        # 最新卷／最新章由目錄辨識結果自動填入，但可以改：例如不想讓檔名
        # 帶卷數，直接把「最新卷」清空即可。自動填入的規則見 set_last_found。
        self._auto_last = {}
        self.last_vol_label = QLineEdit()
        self.last_vol_label.setPlaceholderText("－")
        self._add_field(grid, 0, 3, "最新卷", self.last_vol_label)
        self.last_ch_label = QLineEdit()
        self.last_ch_label.setPlaceholderText("－")
        self._add_field(grid, 0, 4, "最新章", self.last_ch_label)

        self.status_combo = QComboBox()
        self.status_combo.addItems(STATUS_CHOICES)
        self.status_combo.currentIndexChanged.connect(
            lambda _index: self.set_status_badge(i18n.combo_value(self.status_combo)))
        self._add_field(grid, 0, 5, "狀態", self.status_combo)

        self.structure_combo = QComboBox()
        self.structure_combo.addItems(STRUCTURE_CHOICES)
        self.structure_combo.currentIndexChanged.connect(
            lambda _index: self.structure_changed.emit(i18n.combo_value(self.structure_combo)))
        self._add_field(grid, 0, 7, "結構", self.structure_combo)

        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(ENCODING_CHOICES)
        self.encoding_combo.currentIndexChanged.connect(
            lambda _index: self.encoding_changed.emit(i18n.combo_value(self.encoding_combo)))
        self._add_field(grid, 0, 8, "讀取編碼", self.encoding_combo)
        # 三組用直線隔開：書的基本資料｜從目錄帶出來、寫進檔名的｜怎麼讀這個檔
        self._dividers = []
        for column in (2, 6):
            divider = VDivider()
            divider.setMinimumHeight(0)
            divider.setMaximumHeight(16777215)
            grid.addWidget(divider, 0, column)
            self._dividers.append(divider)
        char = self.fontMetrics().horizontalAdvance("字")
        for field, chars in ((self.title_input, 4), (self.author_input, 3), (self.last_vol_label, 3),
                             (self.last_ch_label, 4)):
            field.setMinimumWidth(char * chars + 24)
        metrics = self.fontMetrics()
        for combo in (self.status_combo, self.structure_combo, self.encoding_combo):
            longest = max(metrics.horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
            combo.setMinimumWidth(longest + 80)       # 左右內距＋下拉箭頭（theme.py 的 QComboBox）
        self._two_rows = None
        self._arrange(False)
        self._one_row_width = grid.minimumSize().width() + 40     # details 左右各 20 的邊距

    # 全部排成一行：書名最長、作者其次，其他欄位平分剩下的空間。每一欄至少放得下常見的內容
    # （下拉框照選項、輸入框照幾個字），不會被壓到看不到字。視窗窄到一行放不下時改成兩行
    # （書名、作者、最新卷、最新章／狀態、結構、讀取編碼），不然欄位會疊在一起。
    _ONE_ROW = ((0, 0, 1), (0, 1, 1), (0, 3, 1), (0, 4, 1), (0, 5, 1), (0, 7, 1), (0, 8, 1))
    _ONE_ROW_STRETCH = (4, 3, 0, 2, 2, 2, 0, 2, 2)
    _TWO_ROWS = ((0, 0, 1), (0, 1, 1), (0, 2, 1), (0, 3, 1), (1, 0, 1), (1, 1, 1), (1, 2, 2))
    _TWO_ROWS_STRETCH = (1, 1, 1, 1, 0, 0, 0, 0, 0)

    def _arrange(self, two_rows: bool):
        if two_rows == self._two_rows:
            return
        first = self._two_rows is None
        self._two_rows = two_rows
        grid = self._grid
        for box in self._boxes:
            grid.removeItem(box)
        for divider in self._dividers:
            grid.removeWidget(divider)
            divider.setVisible(not two_rows)
        for box, (row, column, span) in zip(self._boxes, self._TWO_ROWS if two_rows else self._ONE_ROW):
            grid.addLayout(box, row, column, 1, span)
        if not two_rows:
            for divider, column in zip(self._dividers, (2, 6)):
                grid.addWidget(divider, 0, column)
        for column, stretch in enumerate(self._TWO_ROWS_STRETCH if two_rows else self._ONE_ROW_STRETCH):
            grid.setColumnStretch(column, stretch)
        if not first:
            self.details_height_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(self.width() < self._one_row_width)


    def _add_field(self, grid: QGridLayout, row: int, col: int, label_text: str, widget):
        box = QVBoxLayout()
        box.setSpacing(4)
        label = QLabel(label_text)
        label.setObjectName("fileLabel")
        box.addWidget(label)
        box.addWidget(widget)
        grid.addLayout(box, row, col)
        self._boxes.append(box)

    def _on_toggled(self, checked: bool):
        self.details.setVisible(checked)
        self.details_height_changed.emit()
        self.toggle_button.set_icon_name("chevron-up" if checked else "chevron-down")

    # ------------------------------------------------------------------

    def set_filename(self, text: str, size_text: str = ""):
        self._filename_placeholder = False
        self.filename_label.setText(text)
        self.size_label.setText(size_text)

    def refresh_language(self):
        """切換介面繁簡後補上 retranslate() 管不到的部分（檔名標籤被標成內容）。"""
        if self._filename_placeholder:
            self.filename_label.setText(i18n.T("尚未開啟檔案"))

    def _show_filename_menu(self, pos):
        menu = QMenu(self)
        selected = self.filename_label.selectedText()
        copy_action = menu.addAction("複製" if selected else "複製檔名")
        copy_action.triggered.connect(
            lambda: QApplication.clipboard().setText(selected or self.filename_label.text()))
        menu.exec(self.filename_label.mapToGlobal(pos))

    def set_status_badge(self, text: str):
        """連載狀態用顏色區分：未完結偏紅提醒、已完結偏綠，其餘維持中性灰。"""
        i18n.set_text(self.status_badge, text)
        self.status_badge.setVisible(bool(text))
        object_name = {"未完結": "badgeWarn", "已完結": "badgeOk"}.get(text, "badge")
        if self.status_badge.objectName() != object_name:
            self.status_badge.setObjectName(object_name)
            # objectName 換了之後樣式表不會自動重算，要自己踢一次。
            self.status_badge.style().unpolish(self.status_badge)
            self.status_badge.style().polish(self.status_badge)

    def set_encoding_badge(self, text: str):
        self.encoding_badge.setText(text)
        self.encoding_badge.setVisible(bool(text))

    def book_title(self) -> str:
        return self.title_input.text().strip()

    def author(self) -> str:
        return self.author_input.text().strip()

    def status(self) -> str:
        return i18n.combo_value(self.status_combo)

    def structure(self) -> str:
        return i18n.combo_value(self.structure_combo)

    def last_vol_text(self) -> str:
        return self.last_vol_label.text().strip()

    def last_ch_text(self) -> str:
        return self.last_ch_label.text().strip()

    def set_title_if_empty(self, value: str):
        if not self.title_input.text().strip() and value:
            self.title_input.setText(value)

    def set_author_if_empty(self, value: str):
        if not self.author_input.text().strip() and value:
            self.author_input.setText(value)

    def set_status(self, value: str):
        if value in STATUS_CHOICES:
            i18n.set_combo_value(self.status_combo, value)

    def set_last_found(self, vol: str, ch: str, *, force: bool = False):
        """填入目錄辨識到的最新卷／章。

        force（開檔、按重新整理目錄）一律覆蓋；其他會重建目錄的操作（排版、
        合併、復原…）只在使用者沒改過這一欄時才更新——不然剛清空的卷數，
        按一次一鍵排版又跑回來。"""
        for field, value in ((self.last_vol_label, vol), (self.last_ch_label, ch)):
            previous_auto = self._auto_last.get(field)
            if force or previous_auto is None or field.text() == previous_auto:
                field.setText(value)
            self._auto_last[field] = value

    def set_structure(self, value: str):
        if value in STRUCTURE_CHOICES:
            self.structure_combo.blockSignals(True)
            i18n.set_combo_value(self.structure_combo, value)
            self.structure_combo.blockSignals(False)

    def set_encoding_choice(self, value: str):
        self.encoding_combo.blockSignals(True)
        i18n.set_combo_value(self.encoding_combo, value if value in ENCODING_CHOICES else "自動")
        self.encoding_combo.blockSignals(False)

    def reset(self):
        self.title_input.clear()
        self.author_input.clear()
        i18n.set_combo_value(self.status_combo, "未指定")
        # 結構／編碼改回預設值時不能發出變更通知：主視窗收到會以為是使用者
        # 切換，立刻重掃目錄、甚至重新讀一次檔案（開檔時就會整份讀兩遍）。
        for combo, value in ((self.structure_combo, "自動判斷"), (self.encoding_combo, "自動")):
            combo.blockSignals(True)
            i18n.set_combo_value(combo, value)
            combo.blockSignals(False)
        self.last_vol_label.clear()
        self.last_ch_label.clear()
        self._auto_last = {}
        self.set_filename(i18n.T("尚未開啟檔案"))
        self._filename_placeholder = True
        self.set_status_badge("")
        self.set_encoding_badge("")
