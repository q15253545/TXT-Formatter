"""共用的小型元件：圓角卡片容器、依主題與 hover 狀態換色的圖示按鈕、
攔截 Ctrl+Z/Ctrl+Shift+Z 交給自訂復原系統的編輯器、繁／簡切換鈕。"""

import difflib

from PySide6.QtCore import (
    Property, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QGuiApplication, QKeySequence, QPainter, QPainterPath, QPen, QTextBlockFormat,
    QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractScrollArea, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMenu, QPlainTextEdit,
    QPushButton, QScrollArea,
    QSizePolicy, QSplitter, QTabBar, QTextEdit, QSlider, QSpinBox, QStyledItemDelegate, QToolButton, QVBoxLayout,
    QWidget,
)

from . import i18n, icons
from .text_positions import PositionMap
from .theme import active_tokens


_TRACK_W, _TRACK_H, _KNOB_MARGIN, _TOGGLE_GAP = 36, 20, 2, 12


class ToggleSwitch(QCheckBox):
    """開關樣式的勾選框：文字在左、開關在右，圓鈕滑動有動畫。

    用在「設定」類的選項（排版設定、只檢查選取的章節…）；多選清單（表格裡的
    勾選、常用格式、檢查項目）維持一般勾選框。繼承 QCheckBox，isChecked／
    setChecked／toggled 都照舊。

    fill=True：撐滿整列，開關貼齊右邊（排版設定面板）；
    fill=False：只佔「文字＋開關」的寬度，放在寬對話框裡不會被拉到最右邊。"""

    def __init__(self, text="", parent=None, fill=True):
        super().__init__(text, parent)
        self._offset = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_offset)
        self.toggled.connect(self._animate)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        horizontal = QSizePolicy.Policy.Expanding if fill else QSizePolicy.Policy.Fixed
        self.setSizePolicy(horizontal, QSizePolicy.Policy.Fixed)

    def _set_offset(self, value):
        self._offset = float(value)
        self.update()

    def _get_offset(self) -> float:
        return self._offset

    offset = Property(float, _get_offset, _set_offset)   # 0＝關、1＝開

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        if self.signalsBlocked():
            # 程式自己設定（例如載入設定時擋住訊號）：toggled 不會送出，直接到位。
            self._animation.stop()
            self._set_offset(1.0 if checked else 0.0)

    def showEvent(self, event):
        self._animation.stop()
        self._set_offset(1.0 if self.isChecked() else 0.0)
        super().showEvent(event)

    def _animate(self, checked: bool):
        target = 1.0 if checked else 0.0
        if not self.isVisible():
            self._set_offset(target)
            return
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(target)
        self._animation.start()

    def sizeHint(self):
        metrics = self.fontMetrics()
        width = metrics.horizontalAdvance(self.text()) + _TOGGLE_GAP + _TRACK_W + 2
        return QSize(width, max(metrics.height(), _TRACK_H) + 6)

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, pos):
        return self.rect().contains(pos)      # 整列都可以點，不只開關本身

    def paintEvent(self, _event):
        tokens = active_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        if not enabled:
            painter.setOpacity(0.45)
        rect = self.rect()
        track = QRectF(rect.right() - _TRACK_W - 1, (rect.height() - _TRACK_H) / 2, _TRACK_W, _TRACK_H)
        painter.setPen(QColor(tokens.text))
        painter.setFont(self.font())
        text_rect = QRectF(rect.left(), rect.top(), max(0.0, track.left() - _TOGGLE_GAP - rect.left()), rect.height())
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        # 軌道：關＝半透明淡灰、開＝主要按鈕色，滑動時兩色漸變
        off = QColor(tokens.text_faint)
        off.setAlpha(120)
        on = QColor(tokens.accent)
        t = self._offset
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(round(off.red() + (on.red() - off.red()) * t),
                                round(off.green() + (on.green() - off.green()) * t),
                                round(off.blue() + (on.blue() - off.blue()) * t),
                                round(off.alpha() + (255 - off.alpha()) * t)))
        painter.drawRoundedRect(track, _TRACK_H / 2, _TRACK_H / 2)
        knob = _TRACK_H - _KNOB_MARGIN * 2
        x = track.left() + _KNOB_MARGIN + (_TRACK_W - _KNOB_MARGIN * 2 - knob) * t
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(x, track.top() + _KNOB_MARGIN, knob, knob))
        if self.hasFocus() and enabled:          # 鍵盤焦點（Tab 過來、空白鍵切換）
            focus = QColor(tokens.icon_hover)      # 焦點框跟其他控制項一樣用互動色
            focus.setAlpha(90)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(focus)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), _TRACK_H / 2 + 2, _TRACK_H / 2 + 2)
        painter.end()


class CompactToggle(ToggleSwitch):
    """放在寬對話框裡的開關：只佔文字＋開關的寬度。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent, fill=False)


class ScopeToggle(CompactToggle):
    """工具視窗最上面的「只○○選取的 N 章」（掃描、校對、繁簡轉換共用）。

    每次打開都是關著（整本）：目錄的選取多半只是跳到那幾章看內容，預設只處理選取的章節，
    找不到東西會讓人以為整本都沒問題。使用者自己打開才只處理選取的。"""

    def __init__(self, verb: str, count: int = 0, parent=None):
        super().__init__("", parent)
        self._verb = verb
        self.set_count(count)
        self.setChecked(False)

    def set_count(self, count: int):
        """count＝0：目錄沒有選取，不能開。"""
        i18n.set_text(self, f"只{self._verb}選取的 {count} 章" if count
                      else f"只{self._verb}選取的章節（先在目錄選取章節）")
        self.setEnabled(bool(count))
        if not count:
            self.setChecked(False)


class ThemeButton(QToolButton):
    """工具列的主題按鈕：按鈕上畫目前主題的雙色圓點（左半底色、右半主要按鈕色），
    點了跳出主題選單。選單依序列出淺色系、分隔線、深色系，目前的主題打勾。"""

    themeSelected = Signal(str)

    def __init__(self, themes, tooltip: str, parent=None):
        super().__init__(parent)
        self._themes = list(themes)
        self._current = None
        self.setObjectName("toolbarButton")
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(20, 20))
        self.clicked.connect(self._show_menu)

    def set_current(self, name: str):
        self._current = name
        tokens = next((t for t in self._themes if t.name == name), self._themes[0])
        self.setIcon(icons.make_swatch_icon(tokens.bg, tokens.primary_bg, 20))

    def build_menu(self) -> QMenu:
        """組出選單（不 exec，方便測試）。"""
        menu = QMenu(self)
        previous_dark = None
        for tokens in self._themes:
            if previous_dark is not None and tokens.is_dark != previous_dark:
                menu.addSeparator()
            previous_dark = tokens.is_dark
            # 「\t」後面的字會靠右顯示（選單的快速鍵欄位），拿來放目前主題的打勾。
            label = i18n.T(tokens.label) + ("\t✓" if tokens.name == self._current else "")
            action = menu.addAction(icons.make_swatch_icon(tokens.bg, tokens.primary_bg, 16), label)
            action.setData(tokens.name)
            action.triggered.connect(lambda _checked=False, name=tokens.name: self.themeSelected.emit(name))
        return menu

    def _show_menu(self):
        menu = self.build_menu()
        menu.exec(self.mapToGlobal(QPoint(0, self.height() + 4)))
        menu.deleteLater()


class FlowLayout(QLayout):
    """由左到右排，排不下就換行。

    固定格線（例如一列 4 個）在寬視窗會在右邊留一大塊空白、在窄視窗又會
    撐出邊界；這個版面照實際寬度決定一列放幾個。uniform=True 時每一格都用
    最寬那一項的寬度，排出來仍然像對齊的表格，只是欄數會跟著寬度變。"""

    def __init__(self, parent=None, h_spacing: int = 18, v_spacing: int = 6, uniform: bool = False):
        super().__init__(parent)
        self._items: list = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._uniform = uniform
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._visible_items():
            size = size.expandedTo(item.minimumSize())
        if self._uniform:
            size.setWidth(max(size.width(), self._cell_width()))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _visible_items(self):
        return [item for item in self._items if not item.isEmpty()]

    def _cell_width(self) -> int:
        return max((item.sizeHint().width() for item in self._visible_items()), default=0)

    def _do_layout(self, rect, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        cell = self._cell_width() if self._uniform else 0
        for item in self._visible_items():
            hint = item.sizeHint()
            width = cell or hint.width()
            if line_height and x + width > area.right() + 1:
                x = area.x()
                y += line_height + self._v_spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(width, hint.height())))
            x += width + self._h_spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


def flow_container(uniform: bool = False, h_spacing: int = 18, v_spacing: int = 6):
    """回傳（放在一般版面裡的 QWidget, 往裡面加元件用的 FlowLayout）。

    包一層 QWidget 是為了讓外層的 QVBoxLayout 照「這個寬度需要幾列」給高度。"""
    widget = QWidget()
    layout = FlowLayout(widget, h_spacing=h_spacing, v_spacing=v_spacing, uniform=uniform)
    policy = widget.sizePolicy()
    policy.setHeightForWidth(True)
    widget.setSizePolicy(policy)
    return widget, layout


def fit_window_to_screen(window):
    """把視窗的大小與位置夾回它「現在所在的螢幕」的可用範圍（含標題列）。"""
    screen = window.screen() or QGuiApplication.primaryScreen()
    if screen is None:
        return
    available = screen.availableGeometry()
    frame = window.frameGeometry()
    extra_width = frame.width() - window.width()
    extra_height = frame.height() - window.height()
    width = max(window.minimumWidth(), min(window.width(), available.width() - extra_width))
    height = max(window.minimumHeight(), min(window.height(), available.height() - extra_height))
    if (width, height) != (window.width(), window.height()):
        window.resize(width, height)
        frame = window.frameGeometry()
    x = min(max(frame.x(), available.left()), max(available.left(), available.right() - frame.width() + 1))
    y = min(max(frame.y(), available.top()), max(available.top(), available.bottom() - frame.height() + 1))
    if (x, y) != (frame.x(), frame.y()):
        window.move(x, y)


class _KeepOnScreen(QObject):
    """對話框顯示出來之後，照它實際所在的螢幕再夾一次。

    對話框還沒顯示時，Qt 回報的螢幕是「主螢幕」，不是主視窗所在的那一台；
    主視窗在直立螢幕上時，照主螢幕算出來的大小會整個超出去（實際發生過）。
    所以一定要等顯示之後、位置確定了再量一次。"""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            QTimer.singleShot(0, lambda: fit_window_to_screen(watched))
        return False


def keep_on_screen(dialog):
    guard = _KeepOnScreen(dialog)
    dialog.installEventFilter(guard)
    dialog._keep_on_screen = guard     # 留住參照，免得被回收


def size_dialog(dialog, width: int, height: int):
    """依對話框想要的大小開，但不超過螢幕可用範圍的九成。

    螢幕用「主視窗所在的那一台」算（對話框自己還沒顯示，問不準）；顯示之後
    再照實際位置夾一次（keep_on_screen）。直立或小螢幕上，寫死的 900×640
    會有一截在畫面外，而且對話框沒有工作列可以把它拖回來。"""
    parent = dialog.parentWidget()
    screen = (parent.window().screen() if parent is not None else None) or dialog.screen() \
        or QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, int(available.width() * 0.9))
        height = min(height, int(available.height() * 0.9))
    dialog.resize(width, height)
    keep_on_screen(dialog)


def slider_with_spin(layout, label_text, value_range, value, suffix):
    """數值設定：標籤＋拉桿＋數字框（單位寫在數字框裡），拉桿與數字框互相同步；回傳（拉桿, 數字框）。"""
    label = QLabel(label_text)
    label.setObjectName("fileLabel")
    layout.addWidget(label)
    value = max(value_range[0], min(value_range[1], int(value)))
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(*value_range)
    slider.setValue(value)
    slider.setMinimumWidth(72)
    layout.addWidget(slider, 1)
    spin = QSpinBox()
    spin.setRange(*value_range)
    spin.setValue(value)
    spin.setSuffix(i18n.T(suffix))
    spin.setMinimumWidth(104)          # 兩位數＋單位＋上下箭頭
    layout.addWidget(spin)
    spin.valueChanged.connect(slider.setValue)

    def follow(new_value):
        if spin.value() != new_value:
            spin.blockSignals(True)
            spin.setValue(new_value)
            spin.blockSignals(False)
    slider.valueChanged.connect(follow)
    return slider, spin


class PanelScroll(QScrollArea):
    """側邊卡片的內容區：視窗矮時整段捲動，不會把按鈕壓扁、也不會把視窗的最小高度撐高。
    內容放在 content 裡。

    捲動區預設不會把內容的最小寬度往上回報，卡片就能被拉得比內容還窄，
    下拉框、按鈕直接超出卡片邊界。這裡把「內容最小寬度＋捲軸寬度」當成
    自己的最小寬度，卡片最窄就只到剛好裝得下內容。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panelScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content.setObjectName("panelScrollContent")
        self.setWidget(self.content)

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        width = (self.content.minimumSizeHint().width() + self.verticalScrollBar().sizeHint().width()
                 + 2 * self.frameWidth())
        return QSize(max(hint.width(), width), hint.height())


class Card(QFrame):
    """帶圓角、細邊框的卡片容器，取代明顯的分隔線。

    刻意不用 QGraphicsDropShadowEffect：那會把卡片整個子樹（含裡面的文字）
    丟進離屏緩衝區做合成，圓角外側沒蓋到的方形區域會被當成不透明範圍去
    投影，變成圓角後面露出方形陰影；文字也會因為多一層合成而變得毛邊。
    只靠背景色深淺加一條極淡的邊框，就足夠跟底色分出層次了。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class AppWidgetPolisher(QObject):
    """裝在 QApplication 上，每個元件套完樣式表（Polish 事件）之後補兩件事。
    整個程式（含之後才開的對話框、右鍵選單、下拉選單）都要處理，所以用
    全域事件過濾器，不必在每個建立元件的地方各改一次。

    1. 字型完整微調（hinting）：中文字筆畫才會對齊像素、不糊。只在程式
       啟動時設定 app 字型不夠——樣式表在 Polish 之後還會再發一次字型變更、
       重新計算字型，這個設定就被丟掉，選單、下拉清單、狀態列會退回模糊的
       預設值（實機逐一追過事件）。所以 FontChange 也要攔：只在不是完整
       微調時才補設，補設後的那次 FontChange 就不會再動作，不會無限循環。
    2. 下拉框改用 QStyledItemDelegate：預設的 delegate 會忽略樣式表的
       ::item 規則（列高、圓角、hover 底色都吃不到），選單就只剩系統原生
       那種擠在一起、目前項目外面套一個黑框的樣子。
    3. 下拉框的最小寬度不再由最長的選項決定：否則卡片拉到最窄時，下拉框
       撐不下去就會連同整個面板內容一起超出卡片邊界。放不下時 Qt 會自動
       截斷顯示中的文字，展開的清單仍然完整。
    5. 按鈕不接受滑鼠點擊取得焦點（只接受 Tab）：「測試目前文件」這類一次動作的
       按鈕按完會一直掛著焦點框，看起來像還開著。按鈕行為的規則見 UI_RULES.md。
    6. 捲動區一律預留直向捲軸的位置（見 reserve_scrollbar_gutter）。
    7. 點得下去的元件用手指游標。"""

    _HINTING = QFont.HintingPreference.PreferFullHinting
    _FONT_EVENTS = (QEvent.Type.Polish, QEvent.Type.FontChange)

    def eventFilter(self, watched, event):
        # 4. 介面切成簡體時，之後才打開的對話框、右鍵選單在顯示前轉換文字。
        if (event.type() == QEvent.Type.Show and i18n.is_simplified()
                and isinstance(watched, QWidget) and watched.isWindow()):
            i18n.retranslate(watched)
        if event.type() in self._FONT_EVENTS and isinstance(watched, QWidget):
            font = watched.font()
            if font.hintingPreference() != self._HINTING:
                font.setHintingPreference(self._HINTING)
                watched.setFont(font)
        if (event.type() == QEvent.Type.Polish and isinstance(watched, (QPushButton, QToolButton))
                and watched.focusPolicy() in (Qt.FocusPolicy.StrongFocus, Qt.FocusPolicy.ClickFocus)):
            watched.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if event.type() == QEvent.Type.Polish and isinstance(watched, QComboBox):
            if not watched.property("styledPopup"):
                watched.setProperty("styledPopup", True)
                watched.setItemDelegate(QStyledItemDelegate(watched))
                watched.setSizeAdjustPolicy(
                    QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
                watched.setMinimumContentsLength(3)
        if event.type() == QEvent.Type.Polish and isinstance(watched, QAbstractScrollArea):
            reserve_scrollbar_gutter(watched)
        # 7. 點得下去的一律是手指游標（按鈕、勾選框、開關、分頁、下拉框），在這裡統一套，
        #    新元件不用各自設定；輸入框、本文是文字游標，表格、目錄這種選取清單維持箭頭。
        if (event.type() == QEvent.Type.Polish and isinstance(watched, _CLICKABLE)
                and not watched.testAttribute(Qt.WidgetAttribute.WA_SetCursor)):
            watched.setCursor(Qt.CursorShape.PointingHandCursor)
        return False


_CLICKABLE = (QAbstractButton, QComboBox, QTabBar)


def reserve_scrollbar_gutter(area):
    """直向捲軸一直佔著位置，不需要捲動時畫成透明：視窗變矮、捲軸冒出來時，
    內容寬度不會突然變窄、整個版面往左跳一下。下拉清單、補全這類彈出的清單不處理。"""
    if area.property("gutterReserved") or area.window().windowType() == Qt.WindowType.Popup:
        return
    area.setProperty("gutterReserved", True)
    if area.verticalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAsNeeded:
        return
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    bar = area.verticalScrollBar()

    def mark_idle(minimum, maximum):
        idle = maximum <= minimum
        if bar.property("idle") != idle:
            bar.setProperty("idle", idle)
            bar.style().unpolish(bar)
            bar.style().polish(bar)
            bar.update()

    bar.rangeChanged.connect(mark_idle)
    mark_idle(bar.minimum(), bar.maximum())


# 三張卡片（格式選項／章節管理、目錄、本文）的標題列一律同一個高度：
# 以沒有圖示按鈕的「本文」為準，剛好容得下目錄那排 30px 高的圖示按鈕。
CARD_HEADER_HEIGHT = 44


def make_card_header(title_text: str, with_stretch: bool = True):
    """回傳（標題列 widget, 可以繼續往右塞按鈕的 layout）。

    with_stretch=False 是給「自己要放一個會撐滿剩餘空間的元件」的呼叫端用的
    （本文卡片的麵包屑）：留著這裡的彈簧的話，兩者會平分空間。"""
    header = QWidget()
    header.setObjectName("cardHeader")
    header.setFixedHeight(CARD_HEADER_HEIGHT)
    layout = QHBoxLayout(header)
    layout.setContentsMargins(16, 0, 10, 0)
    layout.setSpacing(4)
    title = QLabel(title_text)
    title.setObjectName("cardTitle")
    layout.addWidget(title)
    if with_stretch:
        layout.addStretch(1)
    return header, layout


class ElidedLabel(QLabel):
    """長文字用「…」截掉，而且絕對不會把版面撐寬。

    本文卡片的麵包屑會放整條「卷 / 章」路徑，遇到很長的標題時，一般的
    QLabel 會把這串文字的寬度回報成自己的最小寬度，整張卡片跟著被撐開、
    擠掉旁邊的目錄；切到別章時卡片寬度還會跳來跳去。這裡把水平尺寸策略
    設成 Ignored（完全不理會文字寬度），文字則照目前實際寬度截斷。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()

    def setText(self, text: str):
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def text(self) -> str:
        return self._full_text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_elide()

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def _apply_elide(self):
        metrics = self.fontMetrics()
        super().setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight,
                                           max(0, self.contentsRect().width())))


class VDivider(QFrame):
    """直的 1px 分隔線（工具列上分開兩組按鈕），顏色同 Divider。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("divider")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(1)
        self.setFixedHeight(24)


class _EnterStaysLocal(QObject):
    """輸入框、數字框、表格裡按 Enter 只做那一欄自己的事（加標點、從範例產生…）：沒被吃掉的 Enter
    會一路傳到對話框，按下預設的主要按鈕（保存並重掃、刪除已勾選項目），視窗就關了或內容被改了。
    焦點在按鈕上時按 Enter 照常按那顆按鈕（按鈕自己會處理，不會傳到這裡）。"""

    def eventFilter(self, watched, event):
        return (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier))


def dialog_frame(dialog, margins=(20, 18, 20, 14), enter_submits: bool = False, intro: str = ""):
    """功能視窗的版面：上面放內容，最下面一條貫穿整個視窗的分隔線，線下面是按鈕列
    （跟側邊卡片置底的按鈕同一種做法）。回傳（內容用的 QVBoxLayout, 按鈕列用的 QHBoxLayout）。

    enter_submits：只有簡單的確認視窗（新增章節、匯出設定…）在輸入框按 Enter 等於按主要按鈕；
    工具視窗的 Enter 只做那一欄的事（見 _EnterStaysLocal）。
    intro：內容最上面一行灰字，一句話說明這個視窗做什麼（不重複視窗標題）。"""
    if not enter_submits:
        guard = _EnterStaysLocal(dialog)
        dialog.installEventFilter(guard)
    outer = QVBoxLayout(dialog)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    body_host = QWidget()
    body = QVBoxLayout(body_host)
    left, top, right, bottom = margins
    body.setContentsMargins(left, top, right, bottom)
    body.setSpacing(12)
    if intro:
        label = QLabel(intro)
        label.setObjectName("dialogIntro")
        label.setWordWrap(True)
        body.addWidget(label)
    outer.addWidget(body_host, 1)
    outer.addWidget(Divider())
    footer = QHBoxLayout()
    footer.setContentsMargins(left, 12, right, 14)
    footer.setSpacing(10)
    outer.addLayout(footer)
    return body, footer


class Divider(QFrame):
    """1px 分隔線，顏色吃主題的 border token。

    不用 QFrame 的 HLine：HLine 是 Qt 自己用調色盤畫的陰影線，樣式表
    改不到它的顏色，畫出來會比卡片邊框、標題列底線深一截。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("divider")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(1)


class ClickableLabel(QLabel):
    """點得下去的標籤（例如本文右下角的縮放比例：點一下回到 100%）。"""

    clicked = Signal()
    double_clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class IconButton(QToolButton):
    """圖示按鈕：一般／hover／停用三種狀態各自有對應的線條顏色；
    checkable 時「開啟中」改用 active 顏色。"""

    def __init__(self, icon_name: str, tooltip: str, *, size: int = 20, parent=None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._size = size
        self._color = "#000000"
        self._hover_color = "#000000"
        self._disabled_color = "#000000"
        self._active_color = None
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        self.toggled.connect(lambda _checked: self._refresh_icon(hovering=self.underMouse()))

    def set_icon_name(self, icon_name: str):
        self._icon_name = icon_name
        self._refresh_icon(hovering=self.underMouse())

    def set_colors(self, color: str, hover_color: str, disabled_color: str, active_color: str | None = None):
        self._color = color
        self._hover_color = hover_color
        self._disabled_color = disabled_color
        self._active_color = active_color
        self._refresh_icon(hovering=self.underMouse())

    def _refresh_icon(self, hovering: bool):
        if not self.isEnabled():
            color = self._disabled_color
        elif self.isChecked() and self._active_color:
            color = self._active_color
        else:
            color = self._hover_color if hovering else self._color
        self.setIcon(icons.make_icon(self._icon_name, color, self._size))

    def setEnabled(self, enabled: bool):
        super().setEnabled(enabled)
        self._refresh_icon(hovering=False)

    def enterEvent(self, event):
        self._refresh_icon(hovering=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon(hovering=False)
        super().leaveEvent(event)


class IconTextButton(QPushButton):
    """圖示＋文字的按鈕（工具列的面板切換等），支援 checkable 的「開啟中」樣式。

    用 QPushButton 而不是 QToolButton：QToolButton 的「文字在圖示旁」模式
    會把圖示＋文字靠左排，按鈕比內容寬時右邊空一截；QPushButton 會把
    整組內容水平、垂直都置中。不接受鍵盤焦點：點工具列按鈕不該把焦點
    從本文搶走，也不會因為取得焦點而多出一圈焦點框。"""

    def __init__(self, icon_name: str, text: str, *, checkable: bool = False,
                 size: int = 18, parent=None):
        super().__init__(text, parent)
        self.setObjectName("toolbarButton")
        # 視窗太窄時只顯示圖示（見 set_compact）。文字仍然記在 _label 裡，
        # text() 回傳的一律是完整文字，繁簡切換才有東西可以轉。
        self._label = text
        self._compact = False
        self._icon_name = icon_name
        self._size = size
        self._color = "#000000"
        self._hover_color = "#000000"
        self._active_color = "#000000"
        self._disabled_color = None
        self.setCheckable(checkable)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(size, size))

    def setText(self, text: str):
        self._label = text
        super().setText("" if self._compact else text)

    def text(self) -> str:
        return self._label

    def set_compact(self, compact: bool):
        """只顯示圖示／圖示加文字。功能與提示文字都不變。"""
        if compact == self._compact:
            return
        self._compact = compact
        super().setText("" if compact else self._label)

    def set_icon_name(self, icon_name: str):
        self._icon_name = icon_name
        self._refresh_icon()

    def set_colors(self, color: str, hover_color: str, active_color: str, disabled_color: str | None = None):
        """disabled_color：不能按的時候圖示跟文字一樣淡（不給就沿用一般顏色）。"""
        self._color = color
        self._hover_color = hover_color
        self._active_color = active_color
        self._disabled_color = disabled_color
        self._refresh_icon()

    def _refresh_icon(self):
        if not self.isEnabled() and self._disabled_color:
            color = self._disabled_color
        elif self.isChecked():
            color = self._active_color
        else:
            color = self._hover_color if self.underMouse() else self._color
        self.setIcon(icons.make_icon(self._icon_name, color, self._size))

    def setChecked(self, checked: bool):
        super().setChecked(checked)
        self._refresh_icon()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange:
            self._refresh_icon()
        super().changeEvent(event)

    def enterEvent(self, event):
        self._refresh_icon()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon()
        super().leaveEvent(event)


class HoverIconButton(QPushButton):
    """卡片裡的一般按鈕（掃描無關連內容、合併重複章節…）：滑鼠移上去時圖示跟文字
    一起變色（文字色由樣式表的 QPushButton:hover 負責），停用時圖示變淡。"""

    def __init__(self, icon_name: str, text: str, *, size: int = 16, parent=None):
        super().__init__(text, parent)
        self._icon_name = icon_name
        self._size = size
        self._color = self._hover_color = self._disabled_color = "#000000"
        self.setIconSize(QSize(size, size))

    def set_colors(self, color: str, hover_color: str, disabled_color: str):
        self._color = color
        self._hover_color = hover_color
        self._disabled_color = disabled_color
        self._refresh_icon()

    def _refresh_icon(self):
        if not self.isEnabled():
            color = self._disabled_color
        else:
            color = self._hover_color if self.underMouse() else self._color
        self.setIcon(icons.make_icon(self._icon_name, color, self._size))

    def enterEvent(self, event):
        super().enterEvent(event)
        self._refresh_icon()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._refresh_icon()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.EnabledChange:
            self._refresh_icon()
        super().changeEvent(event)


class Editor(QPlainTextEdit):
    """章節結構（force_lv1/2、忽略集合…）跟正文綁在一起，Qt 內建的
    QTextDocument undo 只認得文字、不認得這些——所以 Ctrl+Z／Ctrl+Shift+Z
    在這裡整組攔截下來，交給 MainWindow 自己的快照式復原系統處理，
    不落回 QPlainTextEdit 內建（只復原文字、會讓結構跟正文脫節）的版本。"""

    undo_requested = Signal()
    redo_requested = Signal()
    # Ctrl＋滾輪調整預覽字級：+1 放大、-1 縮小；Ctrl+0 送 0 代表回到 100%。
    zoom_requested = Signal(int)
    # 把 TXT 檔拖進本文：交給主視窗開檔，而不是把檔案路徑當文字插進本文。
    file_dropped = Signal(str)
    file_drag_entered = Signal()        # 拖著檔案進到本文：開著檔案時主視窗會蓋上放置區

    def __init__(self, parent=None):
        super().__init__(parent)
        # 復原走 MainWindow 的快照系統；Qt 自己的復原堆疊開著的話，每次套標題格式都會存一份，
        # 大檔累積上百萬步。
        self.setUndoRedoEnabled(False)
        self._wheel_accum = 0
        self._show_whitespace = False
        self._whitespace_color = QColor("#A9B1BE")
        self._trailing_color = QColor(180, 56, 60, 40)
        # 合併下行標題的預覽：接在標題後面畫出來的章名、暫時藏起來的行
        self._preview_texts: list = []
        self._preview_blocks: list = []
        self._hidden_blocks: list = []
        self._preview_color = QColor("#0F7B6C")
        self.cursorPositionChanged.connect(self._reveal_cursor_block)

    # --- 拖曳開檔 -------------------------------------------------------

    @staticmethod
    def _dropped_files(event):
        mime = event.mimeData()
        if not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            self.file_drag_entered.emit()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        files = self._dropped_files(event)
        if files:
            event.acceptProposedAction()
            self.file_dropped.emit(files[0])
            return
        super().dropEvent(event)

    # --- 顯示空格 -------------------------------------------------------

    def set_show_whitespace(self, enabled: bool):
        self._show_whitespace = enabled
        self.viewport().update()

    def set_whitespace_colors(self, mark: str, trailing: QColor):
        self._whitespace_color = QColor(mark)
        self._trailing_color = QColor(trailing)
        if self._show_whitespace:
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._show_whitespace:
            self._paint_whitespace(event.rect())
        if self._preview_texts:
            self._paint_title_preview(event.rect())

    # --- 合併下行標題的預覽 ---------------------------------------------

    def set_title_preview(self, appended: dict, hidden_rows, color: str):
        """「自動合併標題」開著時：本文一個字都不改，只在畫面上把章名接在標題後面
        （非原文色），原本放章名的那幾行先藏起來。appended 是 行號 → 要接上去的章名。

        記在段落（QTextBlock）自己身上（userState 當索引），使用者在前面打字、行號位移時
        預覽還是跟著原本那一行；整份文字換掉時這些段落就不存在了，自然不會殘留。"""
        document = self.document()
        for block in self._preview_blocks:
            if block.isValid():
                block.setUserState(-1)
        for block in self._hidden_blocks:
            if block.isValid() and not block.isVisible():
                block.setVisible(True)
                document.markContentsDirty(block.position(), block.length())
        self._preview_texts, self._preview_blocks, self._hidden_blocks = [], [], []
        self._preview_color = QColor(color)
        for row, text in sorted(appended.items()):
            block = document.findBlockByNumber(row)
            if block.isValid():
                block.setUserState(len(self._preview_texts))
                self._preview_texts.append(text)
                self._preview_blocks.append(block)
        for row in sorted(hidden_rows):
            block = document.findBlockByNumber(row)
            if block.isValid() and block.isVisible():
                block.setVisible(False)
                document.markContentsDirty(block.position(), block.length())
                self._hidden_blocks.append(block)
        self.viewport().update()

    def _reveal_cursor_block(self):
        """游標跑進被預覽藏起來的行（尋找、方向鍵、點目錄）：那一行顯示回來，不能停在看不見的地方。"""
        block = self.textCursor().block()
        if not block.isVisible():
            block.setVisible(True)
            self.document().markContentsDirty(block.position(), block.length())
            self.viewport().update()

    def set_preview_color(self, color: str):
        self._preview_color = QColor(color)
        if self._preview_texts:
            self.viewport().update()

    def _paint_title_preview(self, clip):
        painter = QPainter(self.viewport())
        painter.setPen(self._preview_color)
        offset = self.contentOffset()
        block = self.firstVisibleBlock()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > clip.bottom():
                break
            index = block.userState()
            if block.isVisible() and 0 <= index < len(self._preview_texts) and geometry.bottom() >= clip.top():
                layout = block.layout()
                line = layout.lineAt(layout.lineCount() - 1)
                # 字型照標題本身（粗體、放大）；行尾隱藏標記是 1px，不能拿它的字型
                fragments = block.begin()
                char_format = fragments.fragment().charFormat() if not fragments.atEnd() else block.charFormat()
                font = char_format.font().resolve(self.font())
                painter.setFont(font)
                gap = QFontMetricsF(font).horizontalAdvance(" ")
                x = geometry.left() + line.x() + line.naturalTextWidth() + gap
                baseline = geometry.top() + line.y() + line.ascent()
                painter.drawText(QPointF(x, baseline), self._preview_texts[index])
            block = block.next()
        painter.end()

    def _paint_whitespace(self, clip):
        """把空白字元畫出來：半形空格「·」、全形空格「□」、Tab「→」，行尾
        多餘的空白再鋪一層淡紅底。Qt 內建的 ShowTabsAndSpaces 看不到全形
        空格，而小說縮排幾乎都是全形空格，所以自己畫；只畫畫面上看得到的
        幾行，檔案再大也不影響捲動速度。"""
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self._whitespace_color)
        pen.setWidthF(1.0)
        offset = self.contentOffset()
        block = self.firstVisibleBlock()
        bottom = clip.bottom()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > bottom:
                break
            if block.isVisible() and geometry.bottom() >= clip.top():
                self._paint_block_whitespace(painter, pen, block, geometry.topLeft(), clip)
            block = block.next()
        painter.end()

    def _paint_block_whitespace(self, painter, pen, block, origin, clip):
        """一個段落可能折成很多視覺行；只處理跟畫面相交的那幾行。

        整段掃描在正常小說看不出差別，但硬換行整理後常出現好幾萬字的單一
        段落，那時每次重繪都要掃完整段（還要問 Qt 每個空白的座標）。"""
        text = block.text()
        if not text or not any(ch in text for ch in " \t\u3000\u00a0"):
            return
        layout = block.layout()
        stripped = len(text.rstrip(" \t\u3000\u00a0"))
        # 視覺行的起點、長度是 Qt 的位置（emoji、擴充漢字算 2），text 是 Python 字串：
        # 要換算，不然空白會畫錯位置。每個段落建一次，多行共用。
        positions = PositionMap(text)
        for line_index in range(layout.lineCount()):
            line = layout.lineAt(line_index)
            top = origin.y() + line.y()
            if top + line.height() < clip.top():
                continue
            if top > clip.bottom():
                break
            self._paint_line_whitespace(painter, pen, line, text, stripped, origin, positions)

    def _paint_line_whitespace(self, painter, pen, line, text, stripped, origin, positions):
        first = positions.to_python(line.textStart())
        last = min(len(text), positions.to_python(line.textStart() + line.textLength()))
        for index in range(first, last):
            char = text[index]
            if char not in " \t\u3000\u00a0":
                continue
            left = line.cursorToX(positions.to_qt(index))[0]
            right = line.cursorToX(positions.to_qt(index + 1))[0]
            width = right - left
            # 行尾隱藏標記前面的空白被縮成 1px，畫出來只會是雜訊。
            if width < 3:
                continue
            top = origin.y() + line.y()
            height = line.height()
            rect = QRectF(origin.x() + left, top, width, height)
            if index >= stripped:
                painter.fillRect(rect, self._trailing_color)
            center = QPointF(rect.center().x(), top + line.ascent() - line.ascent() * 0.36)
            painter.setPen(pen)
            if char == "\u3000":
                side = min(width * 0.5, line.ascent() * 0.5)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(center.x() - side / 2, center.y() - side / 2, side, side), 1.5, 1.5)
            elif char == "\t":
                arrow = QPainterPath()
                arrow.moveTo(rect.left() + 3, center.y())
                arrow.lineTo(rect.right() - 3, center.y())
                arrow.moveTo(rect.right() - 7, center.y() - 3.5)
                arrow.lineTo(rect.right() - 3, center.y())
                arrow.lineTo(rect.right() - 7, center.y() + 3.5)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(arrow)
            else:
                painter.setBrush(self._whitespace_color)
                painter.drawEllipse(center, 1.4, 1.4)

    def wheelEvent(self, event):
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            super().wheelEvent(event)
            return
        # 觸控板一次只送一小段角度，累積到一格（120）才算一步，
        # 不然輕輕一滑就會跳好幾級。
        self._wheel_accum += event.angleDelta().y()
        while abs(self._wheel_accum) >= 120:
            step = 1 if self._wheel_accum > 0 else -1
            self._wheel_accum -= 120 * step
            self.zoom_requested.emit(step)
        event.accept()

    def keyPressEvent(self, event):
        if (event.key() == Qt.Key.Key_0
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.zoom_requested.emit(0)
            return
        if event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Redo):
            self.redo_requested.emit()
            return
        super().keyPressEvent(event)


class LanguageToggle(QWidget):
    """介面文字「繁／简」切換：膠囊形底座上兩個選項，選中的那一邊墊一塊
    白色圓角方塊（依 /icon/繁簡切換 設計稿）。按哪一邊就切到哪一邊，方塊
    滑過去有一小段動畫。字用字型畫，不用設計稿的向量外框，高解析度螢幕
    上筆畫才會跟其他介面文字一樣銳利。"""

    toggled = Signal(bool)   # True＝簡體

    _LABELS = ("繁", "简")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._simplified = False
        self._position = 0.0   # 0＝方塊在「繁」，1＝在「简」
        self._colors = {}
        self._hover_index = None   # 滑鼠在哪一邊（0／1），不在上面是 None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(80, 36)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._on_animation)

    def set_colors(self, track: str, knob: str, active_text: str, inactive_text: str, shadow: str,
                   hover_text: str | None = None):
        self._colors = {"track": QColor(track), "knob": QColor(knob), "active": QColor(active_text),
                        "inactive": QColor(inactive_text), "shadow": QColor(shadow),
                        "hover": QColor(hover_text or active_text)}
        self.update()

    def mouseMoveEvent(self, event):
        index = 1 if event.position().x() >= self.width() / 2 else 0
        if index != self._hover_index:
            self._hover_index = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_index = None
        self.update()
        super().leaveEvent(event)

    def is_simplified(self) -> bool:
        return self._simplified

    def set_simplified(self, value: bool, *, animate: bool = False):
        value = bool(value)
        if value == self._simplified and not animate:
            self._position = 1.0 if value else 0.0
            self.update()
            return
        self._simplified = value
        target = 1.0 if value else 0.0
        if animate:
            self._animation.stop()
            self._animation.setStartValue(self._position)
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._position = target
            self.update()

    def _on_animation(self, value):
        self._position = float(value)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isEnabled():
            return
        wanted = event.position().x() >= self.width() / 2
        if wanted != self._simplified:
            self.set_simplified(wanted, animate=True)
            self.toggled.emit(wanted)
        event.accept()

    def paintEvent(self, _event):
        if not self._colors:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()
        scale = height / 52.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._colors["track"])
        painter.drawRoundedRect(QRectF(0, 0, width, height), 14 * scale, 14 * scale)

        inset_x, inset_y = 8 * scale, 7 * scale
        segment = (width - 2 * inset_x) / 2
        knob = QRectF(inset_x + segment * self._position, inset_y, segment, height - 2 * inset_y)
        if self.isEnabled():
            shadow = QColor(self._colors["shadow"])
            painter.setBrush(shadow)
            painter.drawRoundedRect(knob.translated(0, 1), 8 * scale, 8 * scale)
            painter.setBrush(self._colors["knob"])
            painter.drawRoundedRect(knob, 8 * scale, 8 * scale)

        font = QFont(self.font())
        font.setPixelSize(max(12, round(height * 0.39)))
        font.setWeight(QFont.Weight.DemiBold)
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        painter.setFont(font)
        for index, label in enumerate(self._LABELS):
            rect = QRectF(inset_x + segment * index, 0, segment, height)
            nearness = 1.0 - abs(self._position - index)
            color = QColor(self._colors["inactive"])
            active = self._colors["active"]
            color.setRedF(color.redF() + (active.redF() - color.redF()) * nearness)
            color.setGreenF(color.greenF() + (active.greenF() - color.greenF()) * nearness)
            color.setBlueF(color.blueF() + (active.blueF() - color.blueF()) * nearness)
            if self._hover_index == index and self.isEnabled():
                color = self._colors["hover"]      # 滑鼠移上去：跟其他按鈕一樣變成 hover 色
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class ContextPreview(QTextEdit):
    """工具視窗表格下面的「前後文」：選到的那幾行加上前後幾行。選到的那段加淡底色，
    找到的部分照本文的字色標出來；可以修正的（標點校對）直接在同一行標出改動。沒有選取時藏起來。

    選到的那段一律放在第二行：上面露出前一段的最後一行，往上捲還看得到更前面的內容
    （從最上面開始顯示的話，前面幾段很長時選到的那段會被擠到最下面、甚至看不到）。"""

    CONTEXT = 3           # 前後各幾行（空行不算）
    MAX_BODY = 20         # 選到的段落太長時只列頭尾

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("contextPreview")
        self.setReadOnly(True)
        self.setMinimumHeight(90)
        self._anchor = -1         # 選到的那段是第幾個段落（捲動定位用）
        self.hide()

    def stacked_under(self, table) -> QWidget:
        """表格在上、預覽在下，中間的分隔可以拖動調整高度。"""
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(10)
        splitter.addWidget(table)
        splitter.addWidget(self)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _context(self, lines, start: int, end: int):
        def neighbours(rows):
            found = []
            for row in rows:
                if lines[row].strip():
                    found.append(row)
                    if len(found) == self.CONTEXT:
                        break
            return found

        return neighbours(range(start - 1, -1, -1))[::-1], neighbours(range(end + 1, len(lines)))

    def _formats(self, color: str = ""):
        tokens = active_tokens()
        plain, marked, faint = QTextCharFormat(), QTextCharFormat(), QTextCharFormat()
        plain.setForeground(QColor(tokens.text))
        marked.setForeground(QColor(color or tokens.text))
        faint.setForeground(QColor(tokens.text_muted))
        target = QTextBlockFormat()
        target.setBackground(QColor(tokens.jump_bg))
        return plain, marked, faint, target

    def _begin(self):
        self.clear()
        self._cursor = QTextCursor(self.document())
        self._first_block = True

    def _block(self, block_format=None):
        if not self._first_block:
            self._cursor.insertBlock(block_format or QTextBlockFormat())
        else:
            self._cursor.setBlockFormat(block_format or QTextBlockFormat())
        self._first_block = False

    def _finish(self):
        self.show()
        self.moveCursor(QTextCursor.MoveOperation.Start)
        QTimer.singleShot(0, self._scroll_to_anchor)

    def _scroll_to_anchor(self):
        """前一段的最後一行放在最上面，選到的那段就在第二行。後面的內容不多時也要捲得到那裡：
        底下留一段跟預覽一樣高的空白。"""
        document = self.document()
        root = document.rootFrame()
        frame_format = root.frameFormat()
        if frame_format.bottomMargin() != self.viewport().height():
            frame_format.setBottomMargin(self.viewport().height())
            root.setFrameFormat(frame_format)
        block = document.findBlockByNumber(self._anchor)
        if self._anchor <= 0 or not block.isValid():
            self.verticalScrollBar().setValue(0)
            return
        previous = block.previous()
        layout = previous.layout()
        top = document.documentLayout().blockBoundingRect(previous).top()
        if layout is not None and layout.lineCount():
            top += layout.lineAt(layout.lineCount() - 1).y()
        self.verticalScrollBar().setValue(int(top))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._anchor >= 0 and self.isVisible():
            QTimer.singleShot(0, self._scroll_to_anchor)

    def show_rows(self, lines, start: int, end: int, color: str, spans=None):
        """lines 的 start～end 行（含）加淡底色、字用 color，前後各帶 CONTEXT 行。
        spans＝{行號: (起, 迄)}：那一行只有這一段用 color（網址片段、網頁字元碼），其餘照正文。"""
        start, end = max(0, start), min(len(lines) - 1, end)
        if start > end:
            self.hide()
            return
        spans = spans or {}
        before, after = self._context(lines, start, end)
        body = list(range(start, end + 1))
        skipped = 0
        if len(body) > self.MAX_BODY:
            skipped = len(body) - self.MAX_BODY
            body = body[:self.MAX_BODY - 5] + [None] + body[-5:]
        plain, marked, faint, target = self._formats(color)
        self._begin()
        for row in before:
            self._block()
            self._cursor.insertText(lines[row], plain)
        self._anchor = len(before)
        for row in body:
            self._block(target)
            if row is None:
                self._cursor.insertText(i18n.T(f"……（中間 {skipped} 行）……"), faint)
            elif row in spans:
                left, right = spans[row]
                text = lines[row]
                self._cursor.insertText(text[:left], plain)
                self._cursor.insertText(text[left:right], marked)
                self._cursor.insertText(text[right:], plain)
            else:
                self._cursor.insertText(lines[row], marked)
        for row in after:
            self._block()
            self._cursor.insertText(lines[row], plain)
        self._finish()

    def show_fix(self, lines, start: int, end: int, after_lines: list, changed_color: str):
        """可以修正的問題：start～end 行（含）換成修正後的樣子，同一行標出改動——刪掉的字灰色加刪除線、
        補上的字用 changed_color；接起來的兩行在接縫畫一個刪掉的「↵」。前後各帶 CONTEXT 行。"""
        start, end = max(0, start), min(len(lines) - 1, end)
        before, after = self._context(lines, start, end)
        plain, changed, faint, target = self._formats(changed_color)
        changed.setFontWeight(QFont.Weight.Bold)
        removed = QTextCharFormat(faint)
        removed.setFontStrikeOut(True)
        old, new = "\n".join(lines[start:end + 1]), "\n".join(after_lines)
        self._begin()
        for row in before:
            self._block()
            self._cursor.insertText(lines[row], plain)
        self._anchor = len(before)
        self._block(target)

        def put(text, char_format, deleted=False):
            for index, part in enumerate(text.split("\n")):
                if index:
                    if deleted:
                        self._cursor.insertText("↵", char_format)
                    else:
                        self._block(target)
                if part:
                    self._cursor.insertText(part, char_format)

        matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                put(new[j1:j2], plain)
                continue
            if i2 > i1:
                put(old[i1:i2], removed, deleted=True)
            if j2 > j1:
                put(new[j1:j2], changed)
        for row in after:
            self._block()
            self._cursor.insertText(lines[row], plain)
        self._finish()
        self.show()
        # 捲到選到的那幾行
        block = self.document().findBlockByNumber(len(before))
        self.setTextCursor(QTextCursor(block))
        self.ensureCursorVisible()


# ---------------------------------------------------------------- "+" snippet menus (find & replace, custom rules)
# groups: (heading, ((label, text, hint), ...)). In the text, "‸" is where the caret ends up (a selection is wrapped
# there) and «X» is a placeholder that gets selected, ready to be typed over.
_CARET, _PLACEHOLDER = "‸", ("«", "»")


def insert_snippet(line_edit: QLineEdit, template: str):
    """Insert a snippet at the caret (replacing the selection, or wrapping it at "‸")."""
    selected = line_edit.selectedText()
    text = template.replace(_CARET, selected) if _CARET in template else template
    placeholder = None
    if _PLACEHOLDER[0] in text:
        before, rest = text.split(_PLACEHOLDER[0], 1)
        inside, after = rest.split(_PLACEHOLDER[1], 1)
        placeholder = (len(before), len(inside))
        text = before + inside + after
    line_edit.insert(text)
    start = line_edit.cursorPosition() - len(text)
    if placeholder is not None:
        line_edit.setSelection(start + placeholder[0], placeholder[1])
    elif _CARET in template:
        line_edit.setCursorPosition(start + template.index(_CARET) + len(selected))
    line_edit.setFocus()


class _SnippetMenu(QMenu):
    """Clicking an item inserts it and keeps the menu open, so several pieces can be put together in one go."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isEnabled() and action.data() is not None:
            action.trigger()
            return
        super().mouseReleaseEvent(event)


def snippet_menu(parent, line_edit: QLineEdit, groups) -> QMenu:
    menu = _SnippetMenu(parent)
    menu.setToolTipsVisible(True)
    for position, (heading, items) in enumerate(groups):
        if position:
            menu.addSeparator()
        header = menu.addAction(i18n.T(heading))
        header.setEnabled(False)
        for label, template, hint in items:
            shown = template.replace(_CARET, "").replace(_PLACEHOLDER[0], "").replace(_PLACEHOLDER[1], "")
            action = menu.addAction(f"　{i18n.T(label)}\t{shown}")
            action.setData(template)
            action.setToolTip(i18n.T(hint))
            action.triggered.connect(lambda _checked=False, t=template: insert_snippet(line_edit, t))
    return menu


def snippet_button(parent, line_edit, groups, tooltip: str) -> IconButton:
    """The small "+" next to an input: a menu of building blocks inserted at the caret."""
    button = IconButton("plus", tooltip, size=16)
    tokens = active_tokens()          # windows re-colour their own icon buttons when the theme changes
    button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
    menu = snippet_menu(parent, line_edit, groups)
    # popup, not exec: nothing waits on the menu (it stays open while snippets are picked)
    button.clicked.connect(lambda: menu.popup(button.mapToGlobal(button.rect().bottomLeft())))
    return button


class GroupCheckBox(QCheckBox):
    """A section title that is itself the checkbox for a group of checkboxes (detect types, check items): a click
    checks all of them — or, when all are checked, unchecks all; some checked shows the partial mark. Saves a row
    of "select all / none" buttons next to the result table's own. A click on the title changes the members
    silently and emits members_changed once, so the window rescans once instead of once per member."""

    members_changed = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("groupCheck")
        self.setTristate(True)
        self._members: list = []

    def add_member(self, box: QCheckBox):
        self._members.append(box)
        box.toggled.connect(self._sync)
        self._sync()

    def nextCheckState(self):
        on = self.checkState() != Qt.CheckState.Checked
        for box in self._members:
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        self._sync()
        self.members_changed.emit()

    def _sync(self, *_args):
        count = sum(box.isChecked() for box in self._members)
        if count and count == len(self._members):
            state = Qt.CheckState.Checked
        else:
            state = Qt.CheckState.PartiallyChecked if count else Qt.CheckState.Unchecked
        self.blockSignals(True)
        self.setCheckState(state)
        self.blockSignals(False)



def dropped_paths(event) -> list:
    """拖進來的本機檔案路徑（拖的是文字不是檔案就是空的）。"""
    mime = event.mimeData()
    if not mime.hasUrls():
        return []
    return [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]


class DropOverlay(QWidget):
    """拖檔案進視窗時蓋在上面的放置區：每一區一個動作，放在哪一區就做哪一件，不用再跳一個詢問視窗。
    拖出視窗（或按 Esc 取消拖曳）就收起來。zones：[(代號, 標題, 說明)]，由左到右排。"""

    dropped = Signal(str, str)        # 放在哪一區（代號）、檔案路徑

    def __init__(self, zones, parent=None):
        super().__init__(parent)
        self.setObjectName("dropOverlay")
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 28)
        layout.setSpacing(20)
        self._zones = {}
        for key, title, text in zones:
            zone = QFrame()
            zone.setObjectName("dropZone")
            zone_layout = QVBoxLayout(zone)
            zone_layout.addStretch(1)
            for label_text, name in ((title, "dropZoneTitle"), (text, "dropZoneText")):
                label = QLabel(label_text)
                label.setObjectName(name)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                label.setWordWrap(True)
                zone_layout.addWidget(label)
            zone_layout.addStretch(1)
            layout.addWidget(zone, 1)
            self._zones[key] = zone
        self._hot = None
        self.hide()

    def cover(self):
        """蓋滿父元件、放到最上面。"""
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def zone_at(self, pos) -> str | None:
        for key, zone in self._zones.items():
            if zone.geometry().contains(pos):
                return key
        return None

    def _set_hot(self, key):
        if key == self._hot:
            return
        self._hot = key
        for zone_key, zone in self._zones.items():
            zone.setProperty("hot", zone_key == key)
            zone.style().unpolish(zone)
            zone.style().polish(zone)

    def dragEnterEvent(self, event):
        if dropped_paths(event):
            event.acceptProposedAction()
            self._set_hot(self.zone_at(event.position().toPoint()))

    def dragMoveEvent(self, event):
        if dropped_paths(event):
            key = self.zone_at(event.position().toPoint())
            self._set_hot(key)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_hot(None)
        self.hide()

    def dropEvent(self, event):
        paths = dropped_paths(event)
        key = self.zone_at(event.position().toPoint())
        self._set_hot(None)
        self.hide()
        if paths and key:
            event.acceptProposedAction()
            self.dropped.emit(key, paths[0])


class ScrollEndButtons(QFrame):
    """捲動區右下角浮著的「到最前面／到最後面」兩顆小按鈕：內容長到需要捲動時才出現，
    不佔卡片標題列的位置。按下去做什麼由呼叫端決定（top／bottom 訊號）。"""

    top_clicked = Signal()
    bottom_clicked = Signal()

    MARGIN = 10

    def __init__(self, area: QAbstractScrollArea):
        # 掛在捲動區本身、不是 viewport 上：清單捲動時 viewport 會把自己的子元件一起捲走
        super().__init__(area)
        self.setObjectName("scrollEnds")
        self._area = area
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 4, 3, 4)
        layout.setSpacing(2)
        self.top_button = IconButton("chevron-up", "到最前面", size=16)
        self.bottom_button = IconButton("chevron-down", "到最後面", size=16)
        self.top_button.clicked.connect(self.top_clicked.emit)
        self.bottom_button.clicked.connect(self.bottom_clicked.emit)
        layout.addWidget(self.top_button)
        layout.addWidget(self.bottom_button)
        area.viewport().installEventFilter(self)
        area.verticalScrollBar().rangeChanged.connect(lambda *_args: self._update())
        self._update()

    def buttons(self) -> tuple:
        return self.top_button, self.bottom_button

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self._update()
        return False

    def _update(self):
        self.setVisible(self._area.verticalScrollBar().maximum() > 0)
        self.adjustSize()
        viewport = self._area.viewport().geometry()
        self.move(viewport.right() + 1 - self.width() - self.MARGIN,
                  viewport.bottom() + 1 - self.height() - self.MARGIN)
        self.raise_()
