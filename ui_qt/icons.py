"""內嵌 SVG 線條圖示。

圖形採用使用者提供的 Lucide 圖示包（/icon，Lucide v1.8.0，ISC 授權，
授權全文見 icon/LICENSE-LUCIDE.txt）。路徑直接內嵌在這個檔案裡、不在執行
時讀取外部檔案：顏色在畫的時候才代入，同一份路徑就能跟著主題色與 hover
狀態換色。用 QSvgRenderer 畫到 QPixmap 上再包成 QIcon。
"""

import tempfile
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer

_TEMP_DIR = Path(tempfile.gettempdir()) / "txt_tool_qt_icons"

# Lucide 原始設計就是 24×24、線寬 2；維持原樣才會跟圖示包的外觀一致。
_STROKE = 2

_SVG_WRAPPER = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="{width}" '
    'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)

# 名稱沿用 Lucide 原名，方便對照圖示包裡的 overview.svg。
_ICON_BODIES = {
    "brackets": '<path d="M16 3h3v18h-3"/><path d="M8 21H5V3h3"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "minus": '<path d="M5 12h14"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "circle-check": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "circle-alert": '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    "circle-help": ('<circle cx="12" cy="12" r="10"/>'
                    '<path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>'),
    "download": '<path d="M12 15V3"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/>',
    "file-search": '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><circle cx="11.5" cy="14.5" r="2.5"/><path d="M13.3 16.3 15 18"/>',
    "file-check": '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="m9 15 2 2 4-4"/>',
    "fold-vertical": '<path d="M12 22v-6"/><path d="M12 8V2"/><path d="M4 12H2"/><path d="M10 12H8"/><path d="M16 12h-2"/><path d="M22 12h-2"/><path d="m15 19-3-3-3 3"/><path d="m15 5-3 3-3-3"/>',
    "folder-open": '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
    "heading": '<path d="M6 12h12"/><path d="M6 20V4"/><path d="M18 20V4"/>',
    "indent-increase": '<path d="M21 5H11"/><path d="M21 12H11"/><path d="M21 19H11"/><path d="m3 8 4 4-4 4"/>',
    "list-checks": '<path d="M13 5h8"/><path d="M13 12h8"/><path d="M13 19h8"/><path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/>',
    "languages": '<path d="m5 8 6 6"/><path d="m4 14 6-6 2-3"/><path d="M2 5h12"/><path d="M7 2h1"/><path d="m22 22-5-10-5 10"/><path d="M14 18h6"/>',
    "step-forward": '<path d="M6 4l10 8-10 8z"/><path d="M19 4v16"/>',
    "hash": '<path d="M4 9h16"/><path d="M4 15h16"/><path d="M10 3 8 21"/><path d="M16 3l-2 18"/>',
    "list-plus": '<path d="M16 5H3"/><path d="M11 12H3"/><path d="M16 19H3"/><path d="M18 9v6"/><path d="M21 12h-6"/>',
    "list-tree": '<path d="M8 5h13"/><path d="M13 12h8"/><path d="M13 19h8"/><path d="M3 10a2 2 0 0 0 2 2h3"/><path d="M3 5v12a2 2 0 0 0 2 2h3"/>',
    "list-x": '<path d="M16 5H3"/><path d="M11 12H3"/><path d="M16 19H3"/><path d="m15.5 9.5 5 5"/><path d="m20.5 9.5-5 5"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "quote": '<path d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/><path d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"/>',
    "redo-2": '<path d="m15 14 5-5-5-5"/><path d="M20 9H9.5A5.5 5.5 0 0 0 4 14.5A5.5 5.5 0 0 0 9.5 20H13"/>',
    "refresh-cw": '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>',
    "scan-search": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/><path d="M7 21H5a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3"/><path d="m16 16-1.9-1.9"/>',
    "search": '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    "text-search": '<path d="M21 5H3"/><path d="M10 12H3"/><path d="M10 19H3"/><circle cx="17" cy="15" r="3"/><path d="m21 19-1.9-1.9"/>',
    "sliders-horizontal": '<path d="M10 5H3"/><path d="M12 19H3"/><path d="M14 3v4"/><path d="M16 17v4"/><path d="M21 12h-9"/><path d="M21 19h-5"/><path d="M21 5h-7"/><path d="M8 10v4"/><path d="M8 12H3"/>',
    "space": '<path d="M22 17v1c0 .5-.5 1-1 1H3c-.5 0-1-.5-1-1v-1"/>',
    "undo-2": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/>',
    "unfold-vertical": '<path d="M12 22v-6"/><path d="M12 8V2"/><path d="M4 12H2"/><path d="M10 12H8"/><path d="M16 12h-2"/><path d="M22 12h-2"/><path d="m15 19-3 3-3-3"/><path d="m15 5-3-3-3 3"/>',
    "wand-sparkles": '<path d="m21.64 3.64-1.28-1.28a1.21 1.21 0 0 0-1.72 0L2.36 18.64a1.21 1.21 0 0 0 0 1.72l1.28 1.28a1.2 1.2 0 0 0 1.72 0L21.64 5.36a1.2 1.2 0 0 0 0-1.72"/><path d="m14 7 3 3"/><path d="M5 6v4"/><path d="M19 14v4"/><path d="M10 2v2"/><path d="M7 8H3"/><path d="M21 16h-4"/><path d="M11 3H9"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    # 以下幾個不在圖示包裡（正則開關、深淺色切換、尋找列的上／下一筆、目錄收合箭頭），
    # 用的是 Lucide 同名圖示的路徑，筆畫風格一致。
    # Lucide「regex」：星號加上一個圓角方塊，用在尋找面板的正則開關。
    "regex": '<path d="M17 3v10"/><path d="m12.67 5.5 8.66 5"/><path d="m12.67 10.5 8.66-5"/>'
             '<path d="M9 17a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2z"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
}

_cache: dict[tuple[str, str, int, float], QIcon] = {}
_forced_scale: float | None = None


def set_device_scale(scale: float | None):
    """指定圖示要照哪個裝置像素比去畫（主視窗會傳入它所在螢幕的比例）。

    兩台螢幕縮放比例不同時，要跟著視窗現在所在的螢幕畫，搬過去才不會糊。傳 None 恢復自動判斷。"""
    global _forced_scale
    if scale != _forced_scale:
        _forced_scale = scale
        _cache.clear()


def _device_scale() -> float:
    """畫圖示要用的裝置像素比；沒有 QGuiApplication 時當成 1。

    圖示要照實際像素數去畫：之前一律畫成 1 倍大小，在 125%／150% 的螢幕上
    再被放大，線條就會糊掉。"""
    if _forced_scale:
        return _forced_scale
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    ratios = [screen.devicePixelRatio() for screen in QGuiApplication.screens()]
    return max(ratios + [1.0])


def _render_pixmap(name: str, color: str, size: int, scale: float) -> QPixmap:
    svg = _SVG_WRAPPER.format(color=color, width=_STROKE, body=_ICON_BODIES[name])
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixels = max(1, round(size * scale))
    pixmap = QPixmap(QSize(pixels, pixels))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def make_icon(name: str, color: str, size: int = 20) -> QIcon:
    scale = _device_scale()
    key = (name, color, size, scale)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    icon = QIcon(_render_pixmap(name, color, size, scale))
    _cache[key] = icon
    return icon


def make_swatch_icon(left: str, right: str, size: int = 16) -> QIcon:
    """主題選單用的雙色圓點：左半是底色、右半是主要按鈕色，外面一圈淡灰框
    （深色、淺色底上都看得到）。"""
    scale = _device_scale()
    key = ("swatch", f"{left}/{right}", size, scale)   # 跟 make_icon 一樣：第 4 格是顯示比例
    cached = _cache.get(key)
    if cached is not None:
        return cached
    pixels = max(1, round(size * scale))
    pixmap = QPixmap(QSize(pixels, pixels))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(1, 1, size - 2, size - 2)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(left))
    painter.drawPie(rect, 90 * 16, 180 * 16)
    painter.setBrush(QColor(right))
    painter.drawPie(rect, -90 * 16, 180 * 16)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(128, 128, 128, 160), 1))
    painter.drawEllipse(rect)
    painter.end()
    icon = QIcon(pixmap)
    _cache[key] = icon
    return icon


def make_app_icon(size: int = 64) -> QIcon:
    """應用程式圖示：跟主題無關，固定用強調色的圓角色塊＋文件線條，
    在工作列小尺寸下也看得清楚（細線條圖示在那個尺寸會糊成一片）。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect x="2" y="2" width="20" height="20" rx="6" fill="#3869D8"/>'
        '<path d="M7.5 7.5h9M7.5 11.5h9M7.5 15.5h5.5" stroke="#FFFFFF" '
        'stroke-width="1.8" stroke-linecap="round"/>'
        '</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


def icon_file_path(name: str, color: str, size: int = 14) -> str:
    """把圖示存成暫存 PNG，回傳路徑——QSS 的 ::branch image 只能吃檔案路徑，
    沒辦法像 QIcon 一樣直接吃記憶體裡的 pixmap。內容還是從同一份內嵌 SVG
    畫出來的，只是多一步落地存檔，不是外部圖片素材。

    另外存一份 @2x：Qt 讀圖時會依螢幕倍率自動改用 name@2x.png，高解析度
    螢幕上的收合箭頭才不會是放大後的模糊版本。"""
    _TEMP_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{name}_{color.lstrip('#')}_{size}"
    path = _TEMP_DIR / f"{stem}.png"
    if not path.exists():
        _render_pixmap(name, color, size, 1.0).save(str(path), "PNG")
    retina = _TEMP_DIR / f"{stem}@2x.png"
    if not retina.exists():
        _render_pixmap(name, color, size, 2.0).save(str(retina), "PNG")
    return str(path).replace("\\", "/")


def clear_icon_cache():
    """主題切換後圖示顏色要重算，呼叫這個清掉舊的快取。"""
    _cache.clear()
