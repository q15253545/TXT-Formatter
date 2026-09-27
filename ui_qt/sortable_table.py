"""可以點標題列排序的表格（掃描無關連內容、標點校對、本文可疑章節共用）。

「自訂章節規則」的規則清單刻意不用：那張表的順序就是規則的套用優先順序。

點欄位標題依序切換：遞增 → 遞減 → 回到原本順序（文件中的順序）。
第三下回到原本順序很重要：這幾張表的預設順序本身就有意義，排過之後要
回得去。

排序後「第幾列」就不再等於「第幾筆資料」，所以每一列的每一格都存著它在
原始清單裡的索引（INDEX_ROLE）；呼叫端一律用 data_index()／row_of_index()
換算，不能再直接拿 row 去索引資料。

不用 QTableWidget.setSortingEnabled(True)：開著它的時候，程式每填一格表格
就會被重新排序一次，逐列填資料會亂掉；而且它只有遞增／遞減兩種狀態。
"""

from collections import Counter

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem

INDEX_ROLE = int(Qt.ItemDataRole.UserRole) + 100
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 101
_STATE = "_sortState"   # 表格上的 dynamic property：(欄位, 遞增?) 或 None

# 信心欄位要照「高 → 中 → 低」排，不是照字碼。
CONFIDENCE_ORDER = {"高": 0, "中": 1, "低": 2}

# 掃描結果一次最多列出幾筆：十幾萬筆重複段落逐格建表會拖住
# 介面。超過時只列出前面這些，勾選、全選、刪除／修正都只作用在列出來的項目，
# 不會動到看不到的；處理完重新掃描就會列出其餘的。
TABLE_ROW_LIMIT = 2000


def carry_over(old_items, new_items, key, old_values: dict) -> dict:
    """重新掃描後，把舊清單上的勾選（或選取）對到新清單：{舊的索引: 值} → {新的索引: 值}。

    照內容（key）對，一模一樣的內容照「第幾筆」對。同樣內容的筆數變了（有一處被改掉或新增），
    就分不出誰是誰，那種內容一律不對，交給呼叫端照預設：寧可少勾，也不能勾到使用者沒勾過的地方。"""
    def occurrences(items):
        seen = Counter()
        keys = []
        for item in items:
            value = key(item)
            keys.append((value, seen[value]))
            seen[value] += 1
        return keys, seen

    old_keys, old_counts = occurrences(old_items)
    new_keys, new_counts = occurrences(new_items)
    by_key = {old_keys[index]: value for index, value in old_values.items()}
    return {index: by_key[item_key] for index, item_key in enumerate(new_keys)
            if item_key in by_key and old_counts[item_key[0]] == new_counts[item_key[0]]}


def limit_rows(indices, priority=None):
    """回傳（要列出的索引清單（原本順序）, 全部筆數）。超過 TABLE_ROW_LIMIT 時
    依 priority(index) 由小到大挑（同分照原本順序）。"""
    indices = list(indices)
    total = len(indices)
    if total <= TABLE_ROW_LIMIT:
        return indices, total
    if priority is None:
        return indices[:TABLE_ROW_LIMIT], total
    order = {index: position for position, index in enumerate(indices)}
    chosen = sorted(indices, key=lambda index: (priority(index), order[index]))[:TABLE_ROW_LIMIT]
    return sorted(chosen, key=order.__getitem__), total


class PreviewTable(QTableWidget):
    """結果表格：點一列時只捲上下、不捲左右。預覽欄很寬時，Qt 預設會把整格捲進畫面，
    橫向捲軸跟著跳到右邊；使用者自己拉的橫向位置要留著。"""

    def scrollTo(self, index, hint=QTableWidget.ScrollHint.EnsureVisible):
        x = self.horizontalScrollBar().value()
        super().scrollTo(index, hint)
        self.horizontalScrollBar().setValue(x)


class SortableItem(QTableWidgetItem):
    def __lt__(self, other):
        table = self.tableWidget()
        if table is not None and table.property(_STATE) is None:
            return (self.data(INDEX_ROLE) or 0) < (other.data(INDEX_ROLE) or 0)
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is not None and theirs is not None and mine != theirs:
            return mine < theirs
        if self.flags() & Qt.ItemFlag.ItemIsUserCheckable and not self.text():
            # 只有勾選框的欄位：依勾選狀態排（未勾在前）。
            if self.checkState() != other.checkState():
                return self.checkState().value < other.checkState().value
        elif self.text() != other.text():
            return self.text() < other.text()
        # 相同的值保持原本的相對順序。
        return (self.data(INDEX_ROLE) or 0) < (other.data(INDEX_ROLE) or 0)


def make_item(text: str = "", index: int = 0, sort_key=None) -> SortableItem:
    item = SortableItem(text)
    item.setData(INDEX_ROLE, index)
    if sort_key is not None:
        item.setData(SORT_ROLE, sort_key)
    return item


# 欄寬初始值：「contents」照內容寬度（有上限，免得一格長文字把表格撐爆）；
# 小數＝表格寬度的比例；整數＝固定像素。
_CONTENT_WIDTH_LIMIT = 320
MIN_COLUMN_WIDTH = 44


class _InitialColumnWidths(QObject):
    """表格第一次顯示時才算欄寬（這時才知道資料與表格寬度），之後不再動，
    使用者拉過的寬度才不會被重新整理蓋掉。"""

    def __init__(self, table, widths):
        super().__init__(table)
        self._widths = widths
        self._done = False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show and not self._done:
            self._done = True
            available = watched.viewport().width()
            for column, width in self._widths.items():
                if width == "contents":
                    watched.resizeColumnToContents(column)
                    watched.setColumnWidth(column, min(watched.columnWidth(column), _CONTENT_WIDTH_LIMIT))
                elif isinstance(width, float):
                    watched.setColumnWidth(column, max(MIN_COLUMN_WIDTH, int(available * width)))
                else:
                    watched.setColumnWidth(column, width)
        return False


def setup_columns(table: QTableWidget, widths: dict):
    """每一欄都可以拖拉調整寬度、畫出格線；最後一欄（通常是內容預覽）至少撐滿表格，
    內容比表格寬時照內容寬度，下面出現橫向捲軸。

    widths 是各欄的初始寬度（最後一欄不用給）。"""
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(MIN_COLUMN_WIDTH)
    table.setShowGrid(True)
    table.setWordWrap(False)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setHorizontalScrollMode(QTableWidget.ScrollMode.ScrollPerPixel)
    table.installEventFilter(_InitialColumnWidths(table, widths))
    _FitLastColumn(table)


class _FitLastColumn(QObject):
    """最後一欄的寬度＝max(表格剩下的寬度, 內容最長那一格的寬度)。
    表格內容、表格大小、其他欄寬變了都重算（合併成一次，填表時不會每格算一遍）。"""

    _PADDING = 28          # 儲存格左右 padding＋一點餘裕（改動的字是粗體，會寬一些）

    def __init__(self, table):
        super().__init__(table)
        self._table = table
        self._content_width = 0
        self._content_dirty = True
        self._user_width = None          # 使用者自己拉過最後一欄：之後照他拉的寬度（至少撐滿）
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._apply)
        model = table.model()
        for signal in (model.dataChanged, model.rowsInserted, model.rowsRemoved, model.modelReset):
            signal.connect(self._content_changed)
        table.horizontalHeader().sectionResized.connect(self._section_resized)
        table.viewport().installEventFilter(self)

    def _content_changed(self, *_args):
        self._content_dirty = True
        self._timer.start()

    def _section_resized(self, column, _old, new):
        if column == self._table.columnCount() - 1:
            self._user_width = new
        else:
            self._timer.start()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._timer.start()
        return False

    def _measure(self) -> int:
        table = self._table
        column = table.columnCount() - 1
        font = QFont(table.font())
        font.setBold(True)
        metrics = QFontMetrics(font)
        widest = 0
        for row in range(table.rowCount()):
            item = table.item(row, column)
            if item is not None and item.text():
                widest = max(widest, metrics.horizontalAdvance(item.text()))
        return widest + self._PADDING if widest else 0

    def _apply(self):
        table = self._table
        column = table.columnCount() - 1
        if column < 0:
            return
        if self._content_dirty:
            self._content_width = self._measure()
            self._content_dirty = False
        others = sum(table.columnWidth(index) for index in range(column) if not table.isColumnHidden(index))
        fill = table.viewport().width() - others
        wanted = self._user_width if self._user_width is not None else self._content_width
        width = max(fill, wanted, MIN_COLUMN_WIDTH)
        # 欄比表格寬時，欄位名稱靠左（置中會跑到要捲動才看得到的地方）
        header_item = table.horizontalHeaderItem(column)
        if header_item is not None:
            header_item.setTextAlignment(
                (Qt.AlignmentFlag.AlignLeft if width > fill + 1 else Qt.AlignmentFlag.AlignHCenter)
                | Qt.AlignmentFlag.AlignVCenter)
        if table.columnWidth(column) != width:
            table.horizontalHeader().blockSignals(True)
            table.setColumnWidth(column, width)
            table.horizontalHeader().blockSignals(False)
            table.viewport().update()
            table.horizontalHeader().viewport().update()


def enable_sorting(table: QTableWidget, on_sorted=None):
    """on_sorted：排序狀態改變之後要呼叫的函式（例如更新按鈕是否可用）。"""
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(False)
    table.setProperty(_STATE, None)

    def on_clicked(column: int):
        state = table.property(_STATE)
        if state is None or state[0] != column:
            state = (column, True)
        elif state[1]:
            state = (column, False)
        else:
            state = None
        table.setProperty(_STATE, state)
        resort(table)
        if on_sorted is not None:
            on_sorted()

    header.sectionClicked.connect(on_clicked)


def resort(table: QTableWidget):
    """依目前的排序狀態重排（重新填完表格之後也要呼叫）。"""
    state = table.property(_STATE)
    header = table.horizontalHeader()
    blocked = table.blockSignals(True)
    try:
        if state is None:
            header.setSortIndicatorShown(False)
            table.sortItems(0, Qt.SortOrder.AscendingOrder)   # 無狀態時 __lt__ 依原始索引比較
        else:
            column, ascending = state
            order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
            header.setSortIndicatorShown(True)
            header.setSortIndicator(column, order)
            table.sortItems(column, order)
    finally:
        table.blockSignals(blocked)


def data_index(table: QTableWidget, row: int) -> int:
    item = table.item(row, 0)
    return int(item.data(INDEX_ROLE)) if item is not None and item.data(INDEX_ROLE) is not None else row


def row_of_index(table: QTableWidget, index: int) -> int:
    for row in range(table.rowCount()):
        if data_index(table, row) == index:
            return row
    return -1
