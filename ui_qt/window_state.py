"""主視窗：視窗大小、多螢幕與顯示比例，以及記住／還原上次的介面狀態（ui_state.json）。

MainWindow 的一部分（mixin），只用 MainWindow 的屬性與方法。"""

from PySide6.QtCore import QByteArray, QRect
from PySide6.QtGui import QGuiApplication

from core.chapter_parse import SPECIAL_LEVELS
from core.persistence import load_window_state

from . import i18n, icons
from .theme import THEMES
from .window_common import (
    COMPACT_ARROW_WIDTH, COMPACT_TOOLBAR_WIDTH, DEFAULT_WINDOW_SIZE, EDITOR_ZOOM_MAX, EDITOR_ZOOM_MIN, FULL_TOOLBAR_WIDTH,
)


class WindowStateMixin:
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

        整排按鈕不會換行也不會縮，有文字時最小寬度約 820；只顯示圖示時約 490，
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
        # 只顯示圖示時間距、左右留白、兩個箭頭都收一點：要能塞進 800 寬的視窗
        layout = self.open_button.parentWidget().layout()
        layout.setSpacing(3 if compact else 8)
        margin = 8 if compact else 20
        layout.setContentsMargins(margin, 0, margin, 0)
        # 只剩圖示時用滑鼠提示補上原本的文字（有文字時不放，文字已經說了）
        shortcuts = {self.open_button: "Ctrl+O", self.save_button: "Ctrl+S"}
        for button in (self.open_button, self.one_click_button, self.save_button):
            button.set_compact(compact)
            shortcut = shortcuts.get(button)
            button.setToolTip(button.text() + (f"（{shortcut}）" if shortcut else "") if compact else "")
        # 只有圖示的按鈕做成正方形、兩個箭頭再窄一點：最窄的視窗（MIN_WINDOW_WIDTH）也要放得下，
        # 不然版面會把按鈕擠在一起、繁簡切換被蓋住
        side = self.open_button.height()
        for button in self._toolbar_icon_buttons():
            if compact:
                button.setFixedWidth(side)
            else:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)
            button.setProperty("compact", compact)
            button.style().unpolish(button)
            button.style().polish(button)
        for arrow in (self.open_menu_button, self.filename_button):
            if compact:
                arrow.setFixedWidth(COMPACT_ARROW_WIDTH)
            else:
                arrow.setMinimumWidth(0)
                arrow.setMaximumWidth(16777215)

    def _toolbar_icon_buttons(self) -> tuple:
        """工具列上只顯示圖示時是單一圖示的按鈕（不含兩個箭頭、繁簡切換）。"""
        return (self.open_button, self.one_click_button, self.save_button, self.undo_button,
                self.redo_button, self.theme_button)

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
            "ask_old_files_on_export": self._ask_old_files_on_export,
            "export_format": self.export_format,
            "export_split": self.export_split,
            "show_whitespace": self.content_panel.show_whitespace_toggle.isChecked(),
            "metadata_expanded": self.metadata_bar.toggle_button.isChecked(),
            "side_panel": side_panel if self.raw_lines and any(self.raw_lines) else
            (self._pending_side_panel or side_panel),
            "splitter": bytes(self.splitter.saveState().toHex()).decode("ascii"),
            "side_width": self.side_card.width() if self.side_card.isVisible() else self._side_width,
            "toc_compact_mode": self.toc_compact_mode,
            "one_click_format": self.options_panel.options_state(),
            "missing_mode": self.chapter_panel.missing_mode(),
            "title_tail_allowed": self.title_tail_allowed,
            "title_tail_custom": self.title_tail_custom,
            "find_regex": self.find_bar.regex_button.isChecked(),
            "review_types": sorted(self.review_bar.review_types()),
            "mark_confidence": sorted(self.review_bar.mark_confidence()),
            "infer_volumes": self._infer_volumes,
            "auto_apply_preview": self._auto_apply_preview,
            "merge_titles": self._merge_titles,
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
        name = state.get("theme")
        if isinstance(name, str) and name in THEMES and name != self.theme_name:
            self.set_theme(name)
        if state.get("simplified") and i18n.available():
            self.language_toggle.set_simplified(True)
            self._on_language_toggled(True)
        zoom = state.get("editor_zoom")
        if isinstance(zoom, int) and EDITOR_ZOOM_MIN <= zoom <= EDITOR_ZOOM_MAX and zoom != 100:
            self._editor_zoom = zoom
            self._apply_editor_style()
            self.zoom_label.setText(f"{zoom}%")
            self._update_zoom_buttons()
        self._strip_markers_on_export = bool(state.get("strip_markers_on_export", True))
        self._ask_old_files_on_export = bool(state.get("ask_old_files_on_export", True))
        self._set_export_format(state.get("export_format", "TXT"))
        self.export_split = bool(state.get("export_split", False)) and self.export_format == "TXT"
        if state.get("show_title_markers"):
            self.marker_button.setChecked(True)
        if state.get("show_whitespace"):
            self.content_panel.show_whitespace_toggle.setChecked(True)
        if state.get("metadata_expanded"):
            self.metadata_bar.toggle_button.setChecked(True)
        self.toc_compact_mode = bool(state.get("toc_compact_mode"))
        self.toc_compact_button.setChecked(self.toc_compact_mode)
        # 排版設定卡片就是一鍵排版的設定。舊版的兩組設定（卡片上暫時的 format_options、按「保存到一鍵排版」
        # 存的 one_click_options）不再用：有存過一鍵排版組合的照那組擺好卡片，否則用內建的常用組合。
        if isinstance(state.get("one_click_format"), dict):
            self.options_panel.restore_options_state(state["one_click_format"])
        else:
            self.options_panel.set_options(self._default_one_click_options(state.get("one_click_options")))
        for old_key in ("format_options", "one_click_options"):
            state.pop(old_key, None)
        mode = state.get("missing_mode")
        if isinstance(mode, str) and self.chapter_panel.missing_mode_combo.findText(mode) >= 0:
            i18n.set_combo_value(self.chapter_panel.missing_mode_combo, mode)
        if isinstance(state.get("title_tail_custom"), str):
            self.title_tail_custom = state["title_tail_custom"]
        tail = state.get("title_tail_allowed")
        if isinstance(tail, str):
            self.title_tail_allowed = tail
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
        self.chapter_panel.set_infer_volumes(self._infer_volumes)
        self._auto_apply_preview = bool(state.get("auto_apply_preview"))
        self.chapter_panel.set_auto_apply(self._auto_apply_preview)
        self._merge_titles = bool(state.get("merge_titles"))
        self.chapter_panel.set_merge_titles(self._merge_titles)
        if isinstance(state.get("disabled_words"), list):
            self.disabled_words = frozenset(str(word) for word in state["disabled_words"])
        if isinstance(state.get("special_levels"), dict):
            self.special_levels = {key: level for key, level in state["special_levels"].items()
                                   if key in SPECIAL_LEVELS and level in (1, 2) and level != SPECIAL_LEVELS[key]}
        if isinstance(state.get("max_title_length"), int) and 10 <= state["max_title_length"] <= 200:
            self.max_title_length = state["max_title_length"]
        for key in ("filename_ongoing", "filename_completed"):
            if isinstance(state.get(key), str) and state[key].strip():
                setattr(self, key, state[key])
        levels = state.get("mark_confidence")
        if isinstance(levels, list):
            self.review_bar.set_mark_confidence(set(levels))
        # 逐筆檢查要看的類型；舊版在重複段落分頁打開「標在本文上」的，照舊把重複段落算進去。
        # 本文字色只在逐筆檢查時顯示（開程式時不會自己開始），舊版的 mark_colors 不再用。
        types = state.get("review_types")
        if isinstance(types, list):
            self.review_bar.set_review_types(set(types))
        elif state.get("repeat_marking"):
            self.review_bar.set_review_types(self.review_bar.review_types() | {"repeat"})
        for old_key in ("mark_colors", "repeat_marking"):
            state.pop(old_key, None)
        # 還原過程中各項會在狀態列留下訊息，最後統一改回來。
        self._show_status("準備就緒")

    def _open_pending_side_panel(self):
        """第一次開檔後，把上次開著的左側面板打開。"""
        panel = {"options": self.options_panel, "chapter": self.chapter_panel,
                 "content": self.content_panel}.get(self._pending_side_panel)
        self._pending_side_panel = None
        if panel is not None and not self.side_card.isVisible():
            self._set_active_side_panel(panel)
