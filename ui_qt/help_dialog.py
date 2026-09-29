"""「說明」視窗（檔名列上的問號）：章節標記、本文字色、設定檔。
每一節一個粗體標題、後面括號寫這一節的範圍；內容放在淺色底的表格裡，跟介面同一個字級。"""

import html
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import i18n
from .widgets import Divider, size_dialog


class HelpDialog(QDialog):
    def __init__(self, tokens, marker_guide, data_dir: Path, parent=None, on_restore=None):
        super().__init__(parent)
        self.setWindowTitle("說明")
        size_dialog(self, 640, 640)
        self._data_dir = Path(data_dir)
        self._tokens = tokens

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

        # 標記、路徑也用介面字體（<code> 會換成等寬字，跟旁邊的字對不齊）
        self._section("章節標記", "由使用者手動標記並寫入本文，便於排版；可設定匯出時移除",
                      self._table([(f"<span style='color:{tokens.marker_text}'>{html.escape(mark)}</span>", detail)
                                   for mark, detail in marker_guide]))
        self._section("本文字色", "逐筆檢查時標出來，僅影響顯示；章節標記、預覽隨時顯示", self._table([
            (f"<span style='color:{color}'>{name}</span>", detail)
            for color, name, detail in (
                (tokens.ad_mark_text, "廣告與網頁字元", "網址、發布頁、QQ／微信、小說來源、論壇轉貼資訊、重複段落"),
                (tokens.note_mark_text, "作者感言與作品資訊", "作者的話、作者／字數／發表平台、分隔線"),
                (tokens.marker_text, "非原文內容", "顯示中的章節標記、章節管理開關的預覽"),
            )]))

        # 設定檔：路徑本身就是連結（點了打開資料夾），右邊是還原預設
        link = QUrl.fromLocalFile(str(self._data_dir)).toString()
        path_label = QLabel(f"<a href='{link}' style='color:{tokens.accent}'>{html.escape(str(self._data_dir))}</a>")
        path_label.setTextFormat(Qt.TextFormat.RichText)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        path_label.setOpenExternalLinks(False)
        path_label.linkActivated.connect(self._open_link)
        i18n.skip(path_label)
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(path_label, 1)
        self.restore_button = QPushButton("還原預設")
        self.restore_button.setEnabled(on_restore is not None)
        if on_restore is not None:
            self.restore_button.clicked.connect(lambda: on_restore(self))
        row.addWidget(self.restore_button)
        self._heading("設定檔", "辨識章節的組合、排版設定、開關與視窗大小")
        self._body.addLayout(row)
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

    def _table(self, rows) -> str:
        """淺色底、沒有框線的兩欄表格；第一欄不換行。"""
        cells = "".join(
            f"<tr><td style='padding:6px 12px; white-space:nowrap'>{first}</td>"
            f"<td style='padding:6px 12px 6px 0'>{second}</td></tr>"
            for first, second in rows)
        return (f"<table width='100%' cellspacing='0' cellpadding='0' "
                f"bgcolor='{self._tokens.surface_hover}'>{cells}</table>")

    def _heading(self, title: str, note: str):
        heading = QLabel(f"{html.escape(title)}<span style='font-weight:normal; color:{self._tokens.text_muted}'>"
                         f"（{html.escape(note)}）</span>")
        heading.setObjectName("appTitle")
        heading.setTextFormat(Qt.TextFormat.RichText)
        heading.setWordWrap(True)
        self._body.addWidget(heading)

    def _section(self, title: str, note: str, body_html: str):
        self._heading(title, note)
        body = QLabel(body_html)
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        self._body.addWidget(body)
        self._body.addSpacing(6)
        self._body.addWidget(Divider())
        self._body.addSpacing(6)

    def _open_link(self, url: str):
        self._data_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl(url))
