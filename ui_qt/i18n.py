"""介面文字繁／簡切換。

程式碼裡所有介面文字都照舊直接寫繁體原文；切到簡體時才用 OpenCC（t2s，
繁轉簡是一對一轉換，不會有簡轉繁那種一字多義的問題）即時轉換，不另外
維護一份翻譯檔。

轉換分三種來源：
  1. 已經建好的元件：retranslate() 走訪元件樹，把標題、按鈕、標籤、提示
     文字、下拉選項換掉。原文記在元件的 dynamic property 裡，切回繁體時
     還原原文，不做簡轉繁。
  2. 之後才打開的對話框、右鍵選單：AppWidgetPolisher 在視窗顯示時呼叫
     retranslate()，建立它們的程式碼完全不用管語言。
  3. 程式執行中才改的文字（狀態列、計數標籤…）：用 T() 或 set_text()。

書的內容（本文、目錄、檔名、搜尋結果）不是介面文字，不轉換——這些元件
用 skip() 標記，走訪時整棵子樹跳過。

下拉選單轉換後顯示的是簡體，程式邏輯卻要拿原文比對（例如「未完結」），
所以讀寫下拉值一律改用 combo_value()／set_combo_value()。
"""

from PySide6.QtCore import QLibraryInfo, QTranslator, Qt
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QLabel, QLineEdit, QMenu, QPlainTextEdit, QTableWidget,
    QTextEdit, QWidget,
)

from core.script_convert import get_opencc_converter

_simplified = False
_cache: dict[str, str] = {}
_qt_translator = None

# 下拉選項原文存在這個 role，不佔用一般的 UserRole。
_SOURCE_ROLE = int(Qt.ItemDataRole.UserRole) + 77
_TIP_SOURCE_ROLE = _SOURCE_ROLE + 1
_SKIP = "i18nSkip"


def available() -> bool:
    return get_opencc_converter("t2s") is not None


def is_simplified() -> bool:
    return _simplified


def set_simplified(value: bool):
    global _simplified
    _simplified = bool(value) and available()


def T(text: str) -> str:
    """把繁體原文轉成目前介面語言。"""
    if not _simplified or not text:
        return text
    converted = _cache.get(text)
    if converted is None:
        converter = get_opencc_converter("t2s")
        converted = converter.convert(text) if converter is not None else text
        if len(_cache) > 4000:   # 狀態列訊息帶著數字、章名，種類沒有上限
            _cache.clear()
        _cache[text] = converted
    return converted


def install_qt_translation(app):
    """載入 Qt 自己的中文翻譯。

    輸入框的內建右鍵選單（復原／剪下／複製／貼上／全選）和訊息框按鈕都是
    Qt 畫的，沒有翻譯檔就會是英文，跟其他全中文的介面看起來很突兀。翻譯檔
    隨 PySide6 一起安裝，切換繁簡時換成另一份即可。"""
    global _qt_translator
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    translator = QTranslator(app)
    name = "qtbase_zh_CN" if _simplified else "qtbase_zh_TW"
    if translator.load(name, QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
        _qt_translator = translator
        return True
    return False


def skip(widget: QWidget) -> QWidget:
    """標記為「內容」元件：retranslate() 不碰它和它底下的子元件。"""
    widget.setProperty(_SKIP, True)
    return widget


def _swap(obj, key: str, getter, setter):
    """記住原文、換成目前語言。畫面上的字跟上次換上去的不一樣，代表程式
    後來自己改過文字（改的一定是原文），就改用新的字當原文。"""
    source_key, shown_key = f"i18n_{key}_src", f"i18n_{key}_shown"
    current = getter()
    source, shown = obj.property(source_key), obj.property(shown_key)
    if source is None or current != shown:
        source = current
    converted = T(source)
    obj.setProperty(source_key, source)
    obj.setProperty(shown_key, converted)
    if converted != current:
        setter(converted)


def set_text(widget, text: str):
    """執行中才更新的介面文字：一併記下原文，之後切換語言才換得回來。"""
    converted = T(text)
    widget.setProperty("i18n_text_src", text)
    widget.setProperty("i18n_text_shown", converted)
    widget.setText(converted)


def combo_value(combo: QComboBox, index: int | None = None) -> str:
    """下拉選項的原文（不論畫面上顯示繁體還是簡體）。"""
    index = combo.currentIndex() if index is None else index
    if index < 0:
        return ""
    source = combo.itemData(index, _SOURCE_ROLE)
    return source if source is not None else combo.itemText(index)


def set_combo_value(combo: QComboBox, value: str) -> bool:
    for index in range(combo.count()):
        if combo_value(combo, index) == value:
            combo.setCurrentIndex(index)
            return True
    return False


def _translate_combo(combo: QComboBox):
    blocked = combo.blockSignals(True)
    try:
        for index in range(combo.count()):
            source = combo.itemData(index, _SOURCE_ROLE)
            if source is None:
                source = combo.itemText(index)
                combo.setItemData(index, source, _SOURCE_ROLE)
            combo.setItemText(index, T(source))
            tip = combo.itemData(index, _TIP_SOURCE_ROLE)
            if tip is None:
                tip = combo.itemData(index, Qt.ItemDataRole.ToolTipRole)
                if tip:
                    combo.setItemData(index, tip, _TIP_SOURCE_ROLE)
            if tip:
                combo.setItemData(index, T(tip), Qt.ItemDataRole.ToolTipRole)
    finally:
        combo.blockSignals(blocked)


def _translate_table_headers(table: QTableWidget):
    for column in range(table.columnCount()):
        item = table.horizontalHeaderItem(column)
        if item is None:
            continue
        source = item.data(_SOURCE_ROLE)
        if source is None:
            source = item.text()
            item.setData(_SOURCE_ROLE, source)
        item.setText(T(source))


def _translate_actions(widget: QWidget):
    for action in widget.actions():
        if action.text():
            _swap(action, "text", action.text, action.setText)
        if action.toolTip() and action.toolTip() != action.text():
            _swap(action, "tip", action.toolTip, action.setToolTip)
        if action.menu() is not None:
            retranslate(action.menu())


def _translate_widget(widget: QWidget):
    if widget.isWindow() and widget.windowTitle():
        _swap(widget, "title", widget.windowTitle, widget.setWindowTitle)
    if widget.toolTip():
        _swap(widget, "tip", widget.toolTip, widget.setToolTip)
    if isinstance(widget, (QLabel, QAbstractButton)):
        if widget.text():
            _swap(widget, "text", widget.text, widget.setText)
    elif isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
        if widget.placeholderText():
            _swap(widget, "placeholder", widget.placeholderText, widget.setPlaceholderText)
    elif isinstance(widget, QComboBox):
        _translate_combo(widget)
    if isinstance(widget, QTableWidget):
        _translate_table_headers(widget)
    if isinstance(widget, QMenu):
        _translate_actions(widget)


def retranslate(root: QWidget):
    """把 root 底下所有介面文字換成目前語言（跳過標記為內容的子樹）。"""
    stack = [root]
    while stack:
        widget = stack.pop()
        if widget.property(_SKIP):
            continue
        _translate_widget(widget)
        stack.extend(child for child in widget.children() if isinstance(child, QWidget))
