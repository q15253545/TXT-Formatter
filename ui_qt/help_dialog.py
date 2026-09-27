"""「說明」視窗（檔名列上的問號）：章節標記、本文字色、章節管理的預覽開關、設定檔位置。
每一節一個粗體標題（跟其他視窗的標題同一種樣式），內文用介面的一般字級。"""

import html
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from .widgets import Divider, size_dialog


class HelpDialog(QDialog):
    def __init__(self, tokens, marker_guide, data_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("說明")
        size_dialog(self, 640, 720)
        self._data_dir = Path(data_dir)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 14)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("panelScrollContent")
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self._body = QVBoxLayout(content)
        self._body.setContentsMargins(24, 20, 24, 12)
        self._body.setSpacing(8)

        cell = "padding:3px 16px 3px 0"
        # 標記、路徑也用介面字體（<code> 會換成等寬字，跟旁邊的字對不齊）
        rows = "".join(
            f"<tr><td style='{cell}'>{html.escape(mark)}</td>"
            f"<td style='{cell}'>{name}</td><td style='padding:3px 0'>{detail}</td></tr>"
            for mark, name, detail in marker_guide)
        self._section("章節標記",
                      f"<table>{rows}</table>"
                      "<p>寫在檔案裡，保存目錄的手動調整。</p>")

        swatches = "".join(
            f"<tr><td style='{cell}'><span style='color:{color}'>■ {name}</span></td>"
            f"<td style='padding:3px 0'>{detail}</td></tr>"
            for color, name, detail in (
                (tokens.ad_mark_text, "廣告", "網址、發布頁、QQ／微信、小說來源、重複段落"),
                (tokens.note_mark_text, "作者感言、作品資訊", "作者的話、作者／字數／發表平台、分隔線"),
                (tokens.marker_text, "不是原文的內容", "顯示中的章節標記、章節管理開關的預覽"),
            ))
        self._section("本文字色", f"<table>{swatches}</table><p>只是顯示，不寫進檔案。</p>")

        toggles = "".join(
            f"<tr><td style='{cell}'>{name}</td><td style='padding:3px 0'>{detail}</td></tr>"
            for name, detail in (
                ("自動合併下行標題", "「第1章」接上下一行的章名"),
                ("自動合併重複標題", "連續出現兩次的同一章標題只留第一個"),
                ("自動補齊卷號", "從卷結尾行、章號重新起算推出缺少的卷"),
                ("自動補齊卷名", "卷結尾行寫的卷名一起補上"),
            ))
        self._section("合併標題、補齊卷號與卷名",
                      f"<table>{toggles}</table><p>開關只預覽，按「套用到本文」才寫入。</p>")

        link = QUrl.fromLocalFile(str(self._data_dir)).toString()
        self._section("設定檔位置",
                      f"<p>{html.escape(str(self._data_dir))}　"
                      f"<a href='{link}' style='color:{tokens.accent}'>開啟資料夾</a></p>",
                      last=True)
        self._body.addStretch(1)

        buttons = QDialogButtonBox()
        close_button = buttons.addButton("關閉", QDialogButtonBox.ButtonRole.AcceptRole)
        close_button.setObjectName("primary")
        close_button.clicked.connect(self.accept)
        # 按鈕列跟捲動區分開：上面一條分隔線，不會壓到最後一行說明
        outer.addWidget(Divider())
        button_row = QVBoxLayout()
        button_row.setContentsMargins(24, 12, 24, 0)
        button_row.addWidget(buttons)
        outer.addLayout(button_row)

    def _section(self, title: str, body_html: str, last: bool = False):
        heading = QLabel(title)
        heading.setObjectName("appTitle")
        self._body.addWidget(heading)
        body = QLabel(body_html)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        body.setOpenExternalLinks(False)
        body.linkActivated.connect(self._open_link)
        self._body.addWidget(body)
        if not last:
            self._body.addSpacing(6)
            self._body.addWidget(Divider())
            self._body.addSpacing(6)

    def _open_link(self, url: str):
        self._data_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl(url))
