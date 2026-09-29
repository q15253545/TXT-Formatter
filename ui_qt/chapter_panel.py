"""左側「章節管理」面板（左側圖示列「章節」）。
只影響目錄顯示的動作（重掃、展開、摺疊、只顯示章號）在目錄卡片標題列，不在這裡。"""

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import i18n
from .widgets import Divider, HoverIconButton, IconButton, PanelScroll, ToggleSwitch, make_card_header

MISSING_CHECK_MODES = ["僅檢查中間缺口", "每卷從第1章起算", "同作品跨卷接續"]

# 針對某一行／某幾章的動作（新增章節、移出目錄、加入目錄…）在目錄與本文各自的右鍵選單，
# 操作對象才清楚；這裡只留整體性的動作。
_ACTIONS = [
    ("file-search", "辨識章節", "recognition_requested"),
    ("list-ordered", "自訂章節規則", "rules_requested"),
    ("merge", "合併重複章節", "merge_duplicates_requested"),
]


class ChapterPanel(QWidget):
    recognition_requested = Signal()
    rules_requested = Signal()
    merge_duplicates_requested = Signal()
    check_missing_requested = Signal()
    missing_mode_changed = Signal()
    merge_titles_toggled = Signal(bool)
    infer_volumes_toggled = Signal(bool)
    show_markers_toggled = Signal(bool)
    auto_apply_preview_toggled = Signal(bool)
    apply_volumes_requested = Signal()
    # 點檢查結果裡的某一筆：「群組索引|章號|gap 或 dup」
    report_link_activated = Signal(str)
    report_closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._action_buttons: dict[str, QPushButton] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header, header_layout = make_card_header("章節管理")
        outer.addWidget(header)

        scroll = PanelScroll()
        outer.addWidget(scroll, 1)
        root = QVBoxLayout(scroll.content)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        for icon_name, text, signal_name in _ACTIONS:
            button = HoverIconButton(icon_name, text)
            signal = getattr(self, signal_name)
            button.clicked.connect(signal.emit)
            self._action_buttons[signal_name] = button
            root.addWidget(button)

        root.addWidget(Divider())

        # 這一段的開關都只是預覽（目錄、本文用非原文色標出來），按「套用到本文」才真的寫進去。
        # 自動合併標題：只有章號的標題（「第1章」）把下一行的章名接上來；同一章的標題重複出現、
        # 中間只有幾行作者的話時只留第一個；只有章號、沒有正文又緊接著另一章有章名的標題
        # （網站的貼文編號）只留有章名的。都是整理標題行，一起開、一起套用。
        self.merge_titles_toggle = ToggleSwitch("自動合併標題")
        self.merge_titles_toggle.toggled.connect(self._on_merge_titles_toggled)
        root.addWidget(self.merge_titles_toggle)
        # 自動補齊卷號與卷名：本文沒寫卷標題時，從卷結尾行（第一卷終…）、章號重新從 1 起算、
        # 每章前面帶的卷（「卷一 山路 第一章」）推出缺少的卷，只加在目錄上當預覽（說明見檔名列的「說明」）。
        self.infer_volumes_toggle = ToggleSwitch("自動補齊卷號與卷名")
        self.infer_volumes_toggle.toggled.connect(self._on_infer_volumes_toggled)
        root.addWidget(self.infer_volumes_toggle)
        # 一鍵排版時先把上面幾個開關的預覽寫進本文，跟排版算同一步
        self.auto_apply_toggle = ToggleSwitch("自動套用到一鍵排版")
        self.auto_apply_toggle.clicked.connect(lambda checked: self.auto_apply_preview_toggled.emit(checked))
        root.addWidget(self.auto_apply_toggle)
        # 開關只是預覽；確認後按這裡才寫進本文（合併標題、補上的卷一起寫）
        self.apply_volumes_button = QPushButton("套用到本文")
        self.apply_volumes_button.setEnabled(False)
        self.apply_volumes_button.clicked.connect(self.apply_volumes_requested.emit)
        root.addWidget(self.apply_volumes_button)

        root.addWidget(Divider())
        # 章節標記（[::] 這類，寫在檔案裡保存目錄的手動調整）顯示與否；匯出時要不要拿掉在匯出設定
        self.show_markers_toggle = ToggleSwitch("顯示章節標記")
        self.show_markers_toggle.toggled.connect(self.show_markers_toggled.emit)
        root.addWidget(self.show_markers_toggle)
        root.addWidget(Divider())

        # 缺章檢查：範圍只有看結果時才要選，放在結果區最上面，卡片上只留按鈕。
        self.missing_mode_combo = QComboBox()
        self.missing_mode_combo.addItems(MISSING_CHECK_MODES)
        self.missing_mode_combo.currentIndexChanged.connect(lambda _index: self.missing_mode_changed.emit())
        self.check_missing_button = HoverIconButton("list-checks", "檢查缺章")
        self.check_missing_button.clicked.connect(self.check_missing_requested.emit)
        root.addWidget(self.check_missing_button)

        root.addWidget(self._build_report_pane(), 1)
        # 結果區隱藏時由這段空白把按鈕往上推；顯示時改讓結果區吃掉剩餘高度。
        root.addStretch(1)
        self._root_layout = root
        self._tail_stretch_index = root.count() - 1
        self._report = None
        self._tokens = None

    def _on_merge_titles_toggled(self, on: bool):
        self._refresh_apply_button()
        self.merge_titles_toggled.emit(on)

    def _on_infer_volumes_toggled(self, on: bool):
        self._refresh_apply_button()
        self.infer_volumes_toggled.emit(on)

    def _refresh_apply_button(self):
        """合併標題或補齊卷任一個開著，才有東西可以套用。"""
        self.apply_volumes_button.setEnabled(
            self.merge_titles_toggle.isChecked() or self.infer_volumes_toggle.isChecked())

    def set_merge_titles(self, on: bool):
        """還原上次的設定（不送出訊號）。"""
        self.merge_titles_toggle.blockSignals(True)
        self.merge_titles_toggle.setChecked(on)
        self.merge_titles_toggle.blockSignals(False)
        self._refresh_apply_button()

    def set_infer_volumes(self, on: bool):
        """還原上次的設定（不送出訊號）。"""
        self.infer_volumes_toggle.blockSignals(True)
        self.infer_volumes_toggle.setChecked(on)
        self.infer_volumes_toggle.blockSignals(False)
        self._refresh_apply_button()

    def set_auto_apply(self, on: bool):
        self.auto_apply_toggle.setChecked(on)

    def _build_report_pane(self) -> QWidget:
        """檢查缺章的結果：固定顯示在按鈕下方，不再用狀態列（狀態列會被下
        一個操作的訊息蓋掉）。之後每次目錄重建都自動重算，一邊合併、修改
        章節，一邊就能看到問題清單縮短，不用反覆按檢查。"""
        self.report_pane = QFrame()
        self.report_pane.setObjectName("reportPane")
        pane_layout = QVBoxLayout(self.report_pane)
        pane_layout.setContentsMargins(12, 8, 6, 10)
        pane_layout.setSpacing(4)

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        title = QLabel("檢查結果")
        title.setObjectName("reportTitle")
        title_row.addWidget(title)
        title_row.addStretch(1)
        self.report_close_button = IconButton("x", "關閉檢查結果（不再自動重算）", size=14)
        self.report_close_button.clicked.connect(self._close_report)
        title_row.addWidget(self.report_close_button)
        pane_layout.addLayout(title_row)
        pane_layout.addWidget(self.missing_mode_combo)

        scroll = QScrollArea()
        scroll.setObjectName("panelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.report_label = QLabel()
        self.report_label.setObjectName("reportBody")
        self.report_label.setWordWrap(True)
        self.report_label.setTextFormat(Qt.TextFormat.RichText)
        self.report_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.report_label.linkActivated.connect(self.report_link_activated.emit)
        # 內容含卷名（書的內容），自己組字串時再決定哪些要轉簡體。
        i18n.skip(self.report_label)
        scroll.setWidget(self.report_label)
        pane_layout.addWidget(scroll, 1)
        self.report_pane.hide()
        return self.report_pane

    def _set_report_visible(self, visible: bool):
        self.report_pane.setVisible(visible)
        self._root_layout.setStretch(self._tail_stretch_index, 0 if visible else 1)

    def _close_report(self):
        self._set_report_visible(False)
        self._report = None
        self.report_closed.emit()

    def missing_mode(self) -> str:
        return i18n.combo_value(self.missing_mode_combo)

    def show_missing_report(self, report: dict):
        self._report = report
        self._render_report()
        self._set_report_visible(True)

    def clear_report(self):
        self._set_report_visible(False)
        self._report = None

    def set_report_theme(self, tokens):
        self._tokens = tokens
        self.report_close_button.set_colors(tokens.icon, tokens.icon_hover, tokens.text_faint)
        if self._report is not None:
            self._render_report()

    def refresh_language(self):
        if self._report is not None:
            self._render_report()

    def _render_report(self):
        """由上到下分段：總結 → 各卷的缺口／重複（可點，跳到附近章節）→ 備註。"""
        T = i18n.T
        report = self._report
        tokens = self._tokens
        warn = tokens.warn_text if tokens else "#B4383C"
        ok = tokens.ok_text if tokens else "#1F7A4C"
        muted = tokens.text_muted if tokens else "#647084"
        accent = tokens.accent if tokens else "#3869D8"
        parts = []
        total, mode = report["total"], T(report["mode"])
        if total == 0:
            parts.append(f'<p style="color:{muted}">{T("目前目錄沒有可連號檢查的正式章節。")}</p>')
        else:
            problem_count = sum(len(group["entries"]) for group in report["groups"])
            if problem_count:
                headline = f'<b style="color:{warn}">{T(f"發現 {problem_count} 處問題")}</b>'
            else:
                headline = f'<b style="color:{ok}">✓ {T("未發現缺章")}</b>'
            parts.append(f'<p style="margin:0">{headline}<br>'
                         f'<span style="color:{muted}">{T(f"共 {total} 個正式章節")} · {mode}</span></p>')
            clean_groups = 0
            for index, group in enumerate(report["groups"]):
                if not group["entries"]:
                    clean_groups += 1
                    continue
                label = html.escape(group["label"]) if group["label"] != "全書" else T("全書")
                lines, typos, strays, misplaced = [], [], [], []
                link = f'<a href="{{}}" style="color:{accent};text-decoration:none">{{}}</a>'
                for entry in group["entries"]:
                    start, end = entry["start"], entry["end"]
                    if entry["kind"] == "dup":
                        lines.append(link.format(f"{index}|{start}|dup", T(f"第 {start} 章重複")))
                        continue
                    if entry["kind"] == "misplaced":
                        misplaced.append(link.format(f"line|{entry['row']}", T(
                            f"第 {start} 章在{entry['now']}，應該在{entry['to']}")))
                        continue
                    # 打錯、不像章節的章號：已經照前後章算進去（或不算），另外一段列出是哪一行，點了跳過去
                    quoted = html.escape(entry.get("text", ""))
                    if entry["kind"] == "typo":
                        typos.append(link.format(f"line|{entry['row']}", T(
                            f"第 {entry['row'] + 1} 行「{quoted}」當成第 {end} 章")))
                        continue
                    if entry["kind"] == "stray":
                        strays.append(link.format(f"line|{entry['row']}", T(f"第 {entry['row'] + 1} 行「{quoted}」")))
                        continue
                    text = T(f"缺第 {start} 章") if start == end else T(f"缺第 {start}–{end} 章")
                    lines.append(link.format(f"{index}|{start}|gap", text))
                    # 原文有、只是沒收錄：列出在哪一行、為什麼（最多三行，其餘合計）
                    found = entry["found"]
                    for number, row, reason in found[:3]:
                        hint = T(f"第 {number} 章在原文第 {row + 1} 行（{reason}）")
                        lines.append(f'&nbsp;&nbsp;&nbsp;<span style="color:{muted}">↳</span> '
                                     + link.format(f"line|{row}", hint))
                    if len(found) > 3:
                        lines.append(f'&nbsp;&nbsp;&nbsp;<span style="color:{muted}">'
                                     f'{T(f"↳ 還有 {len(found) - 3} 行沒收錄")}</span>')
                body = "<br>".join(f"· {line}" for line in lines)
                if misplaced:
                    body += ((("<br>" if body else "") + f'<span style="font-weight:600">{T("順序錯亂")}</span>'
                              + "　" + link.format("reorder", T("依章號重排")) + "<br>")
                             + "<br>".join(f"· {item}" for item in misplaced))
                for title, note, items in ((T("可能打錯的章號"), T("照前後章的章號算進去了"), typos),
                                           (T("可能不是章節"), T("章號跟前後章接不上，沒有算進檢查"), strays)):
                    if items:
                        body += ((("<br>" if body else "") + f'<span style="font-weight:600">{title}</span>'
                                  f'<span style="color:{muted}">　{note}</span><br>')
                                 + "<br>".join(f"· {item}" for item in items))
                parts.append(f'<p style="margin-top:8px;margin-bottom:0"><b>{label}</b><br>{body}</p>')
            if clean_groups and clean_groups < len(report["groups"]):
                parts.append(f'<p style="margin-top:8px;margin-bottom:0;color:{muted}">'
                             f'{T(f"其餘 {clean_groups} 組章節編號連續。")}</p>')
        notes = [T("番外與小數章不列入檢查。")]
        parts.append(f'<p style="margin-top:8px;color:{muted}">' + "<br>".join(notes) + "</p>")
        self.report_label.setText("".join(parts))

    def set_icon_colors(self, color: str, hover: str = "", disabled: str = ""):
        for _icon_name, _text, signal_name in _ACTIONS:
            self._action_buttons[signal_name].set_colors(color, hover or color, disabled or color)
        self.check_missing_button.set_colors(color, hover or color, disabled or color)

