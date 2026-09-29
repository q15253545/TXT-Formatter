"""主視窗：目錄與本文的右鍵操作——章節管理的預覽開關與「套用到本文」、複製、剪下／貼上章節、
合併、連續編號、設成卷／章、移出目錄、刪除，本文右鍵的新增章節、加入／移出目錄。

會改本文的操作一律走 MainWindow._set_editor_text／_adopt_lines，游標、目錄選取與復原歷史才對得上。
MainWindow 的一部分（mixin），只用 MainWindow 的屬性與方法。"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QTextCursor
from PySide6.QtWidgets import QApplication, QMenu

from core.cn_numerals import chinese_to_arabic
from core.chapter_parse import (
    chapter_unit_signature, locate_chapter_number, looks_like_auto_chapter, parse_mixed_volume_chapter_header,
    render_chapter_number_like,
)
from core.title_markers import strip_persistent_title_marker

from . import dialogs, i18n, toc_ops
from .app_log import action
from .window_common import _PREVIEW_NOTE, _chapter_line_mapper


class TocEditMixin:
    def _on_merge_titles_toggled(self, on: bool):
        self._merge_titles = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動合併標題：「第1章」接上下一行的章名；同一章的標題連續出現兩次只留第一個；"
                          "只有章號、底下緊接著另一章標題的只留有章名的" + _PREVIEW_NOTE if on else "已關閉自動合併標題")

    def _rebuild_preview_toc(self):
        if self.raw_lines and any(line.strip() for line in self.raw_lines):
            self._sync_raw_lines()
            self._rebuild_toc()

    def _on_infer_volumes_toggled(self, on: bool):
        self._infer_volumes = on
        self._rebuild_preview_toc()
        self._show_status("已開啟自動補齊卷號與卷名：從卷結尾行、章號重新起算、每章前面的卷號推出缺少的卷，"
                          "找得到卷名一起補上" + _PREVIEW_NOTE if on else "已關閉自動補齊卷號與卷名")

    def _on_auto_apply_preview_toggled(self, on: bool):
        self._auto_apply_preview = on
        self._show_status("已開啟自動套用到一鍵排版：一鍵排版時先把章節管理的預覽寫進本文"
                          if on else "已關閉自動套用到一鍵排版")

    def _chapter_nodes_share_unit(self, nodes: list) -> bool:
        """判斷這些節點是不是同一種章節類型（例如都是「第…章」，
        不能混到「第…集」）。任一節點判斷不出類型，就保守視為不一致。"""
        if len(nodes) < 2:
            return True
        signatures = set()
        for node in nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                return False
            clean, _marker = strip_persistent_title_marker(self.raw_lines[raw_idx].strip())
            signature = chapter_unit_signature(clean)
            if signature is None:
                return False
            signatures.add(signature)
        return len(signatures) == 1

    def _build_toc_context_menu(self, pos):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        只放「現在按得下去、而且有意義」的項目：灰掉的項目只會讓人猜為什麼
        不能按。剪下之後就不再顯示「剪下」，改成貼上與取消；已經是章的不顯示
        「設為章標題」，卷也一樣。三組：內容（複製、剪下／貼上、套用格式）、
        結構（合併、連續編號、設為卷／章）、移除（移出目錄、刪除）。"""
        item = self.tree.itemAt(pos)
        if item is None:
            return None
        if item not in self.tree.selectedItems():
            self.tree.setCurrentItem(item)
        selected = self._selected_toc_items()
        # 用詞統一：選到卷也照「章」描述——一章叫「這章」，多章叫「這 N 章」；
        chapters = self._selected_chapter_count()
        these = f"這 {chapters} 章" if chapters > 1 else "這章"

        group = [(f"複製{these}", self.copy_selected_chapters)]
        pending = self._cut_state
        if pending is None:
            group.append((f"剪下{these}", self.cut_selected_chapters))
        else:
            if self._paste_target(item) is not None:
                group.append((f"貼到這章之前（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=True)))
                group.append((f"貼到這章之後（{pending['summary']}）",
                              lambda: self.paste_cut_chapters(item, before=False)))
            group.append(("取消剪下（Esc）", self.cancel_cut))
        group.append((f"套用格式到{these}", self.format_selected_chapters))
        groups = [group]

        group = []
        leaves = [node for node in selected if node.childCount() == 0]
        if len(selected) > 1 and len(leaves) == len(selected):
            group.append((f"合併{these}", self.merge_selected_chapters))
        chapter_nodes = [node for node in selected
                         if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(chapter_nodes) > 1 and self._chapter_nodes_share_unit(chapter_nodes):
            group.append(("連續編號", self.renumber_selected_chapters))
        kinds = {self.chapter_records.get(node, {}).get("kind") for node in selected}
        if kinds - {"volume"}:
            group.append(("設為卷標題", lambda: self.set_chapter_level(1)))
        if kinds - {"chapter"}:
            group.append(("設為章標題", lambda: self.set_chapter_level(2)))
        groups.append(group)

        groups.append([("移出目錄（保留正文）", self.ignore_selected_chapter),
                       (f"刪除{these}（含正文）", self.delete_selected_chapter)])

        menu = QMenu(self)
        for group in groups:
            if not group:
                continue
            if menu.actions():
                menu.addSeparator()
            for text, slot in group:
                menu.addAction(text).triggered.connect(slot)
        return menu

    def _selected_chapter_count(self) -> int:
        """選取範圍有幾章：選到卷就算卷底下的章。"""
        chapters = set()

        def collect(item):
            if item.childCount() == 0:
                chapters.add(id(item))
            for index in range(item.childCount()):
                collect(item.child(index))

        for item in self._selected_toc_items():
            collect(item)
        return len(chapters)

    def _selected_toc_items(self) -> list:
        """目前選取的目錄項目；推定卷沒有對應的標題行，換成它底下的章節。"""
        items = []
        for item in self.tree.selectedItems():
            if item in self.virtual_volume_items:
                items.extend(item.child(index) for index in range(item.childCount()))
            else:
                items.append(item)
        return list(dict.fromkeys(items))

    @action
    def apply_toc_preview(self):
        """目錄上方預覽列的「套用到本文」：把開關預覽的結果真的寫進本文，可以按 Ctrl+Z 復原。"""
        preview = self._toc_preview_lines()
        if preview is None:
            self._show_status("沒有可以套用的內容：預覽開關（自動合併標題、自動補齊卷號與卷名）沒有要改的地方")
            return
        lines, done = preview
        self._replace_text_from_tool(lines)
        self._show_status(i18n.T("已套用到本文：" + "、".join(done) + "，可以按 Ctrl+Z 復原"), translated=True)

    def _toc_preview_lines(self):
        """章節管理預覽開關的結果寫進本文後的整份行，與做了哪些事；沒有東西可以套用時回傳 None。

        - 自動合併標題：章名接到標題行後面，原本放章名的行（和中間的空行）拿掉；重複的標題、
          只有章號又緊接著另一章標題的那一行拿掉。
        - 推算出來的卷（斜體）：卷標題插在卷內第一個項目前面，前後留空行。
        - 每章都帶著卷的寫法（「卷一 山路 第一章 出發」）：換卷的地方插一行卷標題，章節行只留
          「第一章 出發」。卷標題寫成正式的「第一卷 山路」——單獨一行的「卷一」預設不算卷（避免誤判），
          寫成「第…卷」重新整理後才認得出來。
        寫進去之後就是一般的卷標題，排版、匯出都會帶著它。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        lines = list(self.raw_lines)
        inserts = {}          # 行號 → 要插在這一行前面的卷標題
        replaces = {}         # 行號 → 換成這一行
        for info in self.virtual_volume_items.values():
            inserts[info["row"]] = info["title"]
        if self._infer_volumes:
            previous = None
            for row in sorted(set(self.chapter_raw_map.values())):
                clean, marker = strip_persistent_title_marker(lines[row].strip())
                mixed = parse_mixed_volume_chapter_header(clean, True)
                if not mixed or mixed["volume_raw"].startswith("第"):
                    # 「第一卷 卷名 第N章」原本就會分卷，排版時處理；這裡只拆「卷一」這種
                    if not mixed:
                        previous = None
                    continue
                number = mixed["volume_raw"][1:].strip()        # 「篇一」→「一」，數字寫法、單位照原文
                volume = f"第{number}{mixed['volume_unit']}{mixed['volume_note']} {mixed['volume_body']}".strip()
                indent = lines[row][:len(lines[row]) - len(lines[row].lstrip())]
                suffix = {"include": "[::]", "auto_title": "[::T]"}.get(marker, "")    # 手動收錄的標記照留
                replaces[row] = f"{indent}{mixed['chapter_raw'].strip()}{suffix}"
                if volume != previous:
                    inserts[row] = volume
                previous = volume
        removed = set()
        for row, (subtitle_row, subtitle) in self.merged_titles.items():
            line = replaces.get(row, lines[row])
            clean, marker = strip_persistent_title_marker(line.strip())
            indent = line[:len(line) - len(line.lstrip())]
            suffix = {"include": "[::]", "auto_title": "[::T]"}.get(marker, "")
            replaces[row] = f"{indent}{clean} {subtitle}{suffix}"
            removed.update(range(row + 1, subtitle_row + 1))
        removed.update(self.absorbed_titles)
        if not inserts and not replaces and not removed:
            return None
        done = []           # 寫進去之前先數好：寫完目錄就重建了，預覽資料會清掉
        if self.merged_titles:
            done.append(f"合併 {len(self.merged_titles)} 個標題")
        if self.absorbed_titles:
            done.append(f"刪掉 {len(self.absorbed_titles)} 行多餘的標題")
        if inserts:
            done.append(f"寫入 {len(inserts)} 個卷標題")
        result = []
        for row, line in enumerate(lines):
            if row in removed:
                continue
            if row in inserts:
                if result and result[-1].strip():
                    result.append("")
                result.extend([inserts[row], ""])
            result.append(replaces.get(row, line))
        return result, done

    def _show_toc_context_menu(self, pos):
        menu = self._build_toc_context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    @action
    def copy_selected_chapters(self):
        """目錄右鍵「複製這章」：整段內容（標題＋正文）複製到剪貼簿，同時在本文選起來，
        看得到複製了哪裡。

        多選時是涵蓋範圍的頭到尾——編輯器一次只能有一段選取，硬要分段
        反而看不出選到哪裡。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        total_lines = len(self.raw_lines)
        spans = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None:
                spans.append(span)
        if not spans:
            return
        document = self.editor.document()
        first_row = min(start for start, _end in spans)
        last_row = min(max(end for _start, end in spans), document.blockCount()) - 1
        start_block = document.findBlockByNumber(first_row)
        end_block = document.findBlockByNumber(max(first_row, last_row))
        if not start_block.isValid() or not end_block.isValid():
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(start_block.position())
        cursor.setPosition(end_block.position() + end_block.length() - 1,
                           QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.copy()
        self.editor.setFocus()
        chapters = self._selected_chapter_count()
        self._show_status(i18n.T(f"已複製 {chapters} 章（{end_block.blockNumber() - first_row + 1} 行）"))

    def _selected_section_spans(self) -> list:
        """選取章節涵蓋的行範圍（0-based 半開區間），重疊的合併起來。"""
        total_lines = len(self.raw_lines)
        spans = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in self._selected_toc_items():
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None and span[0] < span[1]:
                spans.append(span)
        spans.sort()
        merged: list = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @action
    def cut_selected_chapters(self):
        """剪下＝先做記號，按貼上才真的搬動（跟檔案總管一樣）。

        沒有貼上就不會少任何東西；按 Esc 或重新剪下就取消。內容同時放進系統
        剪貼簿，也可以直接貼到其他程式。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        spans = self._selected_section_spans()
        if not spans:
            return
        labels = [self.toc_full_labels.get(node, node.text(0)) for node in self.tree.selectedItems()]
        summary = "、".join(label[:12] for label in labels[:2])
        if len(labels) > 2:
            summary += f" 等 {len(labels)} 項"
        moved_lines = [line for start, end in spans for line in self.raw_lines[start:end]]
        QApplication.clipboard().setText("\n".join(moved_lines))
        self._cut_state = {
            "spans": spans,
            "text_version": self._text_version,
            "summary": summary,
            "count": len(labels),
        }
        self._style_cut_items()
        self._show_status(i18n.T(f"已剪下 {len(labels)} 章（共 {len(moved_lines)} 行）："
                                 "在目錄對著要放的位置按右鍵貼上，按 Esc 取消"))

    def cancel_cut(self):
        if self._cut_state is None:
            return
        self._cut_state = None
        self._style_cut_items()
        self._show_status("已取消剪下")

    def _style_cut_items(self):
        """已剪下的章節在目錄裡變淡，一眼看得出「等著被搬走」。"""
        tokens = self.tokens
        spans = self._cut_state["spans"] if self._cut_state else []
        # 推算／拆出來的卷（預覽）自己有斜體＋非原文色，不能在這裡被重設
        preview = (set(self.virtual_volume_items) | self.split_volume_items | self.merged_title_items
                   | self.absorbed_title_items)
        for item, row in self.chapter_raw_map.items():
            inside = any(start <= row < end for start, end in spans)
            font = item.font(0)
            if font.italic() != (inside or item in preview) or inside:
                font.setItalic(inside or item in preview)
                item.setFont(0, font)
            if inside:
                item.setForeground(0, QColor(tokens.text_faint))
                item.setToolTip(0, i18n.T("已剪下，貼上後才會真的移動；按 Esc 取消"))
            elif item not in preview:
                item.setData(0, Qt.ItemDataRole.ForegroundRole, None)
                item.setToolTip(0, "")

    def _paste_target(self, item):
        """可以貼到這個項目旁邊嗎？回傳它的行範圍，不行則回傳 None。"""
        if self._cut_state is None or item is None:
            return None
        if self._cut_state["text_version"] != self._text_version:
            return None
        if item in self.virtual_volume_items:
            item = item.child(0) if item.childCount() else None
            if item is None:
                return None
        span = toc_ops.toc_section_lines(
            self.tree, item, self.chapter_index_map, self.toc_boundary_map, len(self.raw_lines))
        if span is None:
            return None
        # 不能貼到自己（或自己底下）：那等於把整段搬進它自己裡面。
        for start, end in self._cut_state["spans"]:
            if start <= span[0] < end:
                return None
        return span

    @action
    def paste_cut_chapters(self, item, before: bool):
        """把剪下的章節搬到指定章節的前面或後面，整個算一步。"""
        if self._cut_state is None:
            return
        if self._cut_state["text_version"] != self._text_version:
            self.cancel_cut()
            dialogs.info(self, "無法貼上", "剪下之後本文有變動，原本的範圍已經對不上，請重新剪下。")
            return
        target = self._paste_target(item)
        if target is None:
            dialogs.info(self, "無法貼上", "不能貼到剪下的章節自己（或它底下的章節）旁邊。")
            return
        spans = self._cut_state["spans"]
        insert_at = target[0] if before else target[1]
        # 目標節點在重建目錄時會被銷毀，標題要先留下來。
        target_label = self.toc_full_labels.get(item, item.text(0))[:16]
        new_lines, mapping = self._move_line_blocks(self.raw_lines, spans, insert_at)
        summary, count = self._cut_state["summary"], self._cut_state["count"]
        self._cut_state = None

        text = "\n".join(new_lines)
        self._set_editor_text(text, lambda row: mapping.get(row, row))
        # 行號是自己算出來的，不必也不能靠 difflib 猜：搬移在比對時會變成
        # 「一處刪掉、一處新增」，被搬走章節的強制層級、自動標題記錄都會遺失。
        for attribute in ("force_lv1_chapters", "force_lv2_chapters"):
            setattr(self, attribute, {mapping[row] for row in getattr(self, attribute) if row in mapping})
        self.auto_titles = {mapping[row]: value for row, value in self.auto_titles.items() if row in mapping}
        self.raw_lines = new_lines
        self._mark_synced(text)
        self._push_line_map(_chapter_line_mapper(mapping))
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(i18n.T(
            f"已把 {count} 章（{summary}）移到「{target_label}」"
            f"{'之前' if before else '之後'}，可以按 Ctrl+Z 復原"))

    @staticmethod
    def _move_line_blocks(lines: list, spans: list, insert_at: int):
        """把 spans 這幾段行搬到 insert_at 之前，回傳（新的行, 舊行號→新行號）。

        接縫處順便整理空行：搬走的位置不留下連續空行，貼上的地方前後各留
        一個空行，這樣章節標題不會黏在上一段的最後一行。只整理接縫：其他
        章節裡刻意留的多個空行、檔尾的空行都照原樣。"""
        moved_rows = [row for start, end in spans for row in range(start, end)]
        # 搬動的內容去掉前後空行，貼上時再統一補。
        while moved_rows and not lines[moved_rows[0]].strip():
            moved_rows.pop(0)
        while moved_rows and not lines[moved_rows[-1]].strip():
            moved_rows.pop()
        cut_rows = {row for start, end in spans for row in range(start, end)}

        result: list = []
        mapping: dict = {}
        at_seam = False           # 剛經過剪下的位置或貼上的位置，還沒遇到下一行文字

        def emit(row):
            mapping[row] = len(result)
            result.append(lines[row])

        def emit_moved():
            if not moved_rows:
                return
            if result and result[-1].strip():
                result.append("")
            for row in moved_rows:
                emit(row)
            result.append("")

        for index, line in enumerate(lines):
            if index == insert_at:
                emit_moved()
                at_seam = True
            if index in cut_rows:
                at_seam = True
                continue
            if at_seam and not line.strip() and result and not result[-1].strip():
                continue          # 接縫：搬走之後不要留下連續空行
            if line.strip():
                at_seam = False
            emit(index)
        if insert_at >= len(lines):
            emit_moved()
        if insert_at >= len(lines) or at_seam:
            # 檔尾是接縫（貼在檔尾，或剪下的是最後一章）：檔尾的空行照原本的樣子
            original_blank_tail = 0
            for line in reversed(lines):
                if line.strip():
                    break
                original_blank_tail += 1
            while result and not result[-1].strip():
                result.pop()
            result.extend([""] * original_blank_tail)
        return result, mapping

    @action
    def merge_selected_chapters(self):
        """保留第一個選取章節的標題，把其餘選取章節的正文接到它後面。
        支援不連續多選：例如選取第 2、3、7 章後，第 3 與第 7 章的正文
        會依序搬到第 2 章之後，兩者的標題行一併移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        nodes = [node for node in self._selected_toc_items() if node in self.chapter_index_map]
        if len(nodes) < 2:
            dialogs.info(self, "合併章節", "請先用 Ctrl 點選或 Shift 連續選取兩個以上的章節。")
            return

        lines = self.editor.toPlainText().split("\n")
        sections = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in nodes:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, len(lines), ordered)
            if span is not None:
                sections.append((span[0], span[1], node))
        if len(sections) < 2:
            dialogs.info(self, "合併章節", "選取的章節無法定位到正文，請先按「重新掃描目錄」再試一次。")
            return
        sections.sort()

        for (_, previous_end, _), (next_start, _, _) in zip(sections, sections[1:]):
            if next_start < previous_end:
                dialogs.info(
                    self, "合併章節",
                    "選取範圍互相重疊（可能同時選到某一卷與它底下的章節）。\n"
                    "請只選取同一層級、彼此獨立的章節。",
                )
                return

        keep_start, keep_end, keep_node = sections[0]
        keep_title = keep_node.text(0)
        others = sections[1:]
        preview_text = "、".join(node.text(0)[:20] for _, _, node in others[:4])
        if len(others) > 4:
            preview_text += f" 等 {len(others)} 章"
        if not dialogs.confirm(
            self, "合併章節",
            f"將把以下章節的正文併入「{keep_title[:30]}」：\n\n{preview_text}\n\n"
            "這些章節的標題行會被移除，正文依原順序接續在後。是否繼續？",
        ):
            return

        moved = []
        for start, end, _ in others:
            body = lines[start + 1:end]           # 跳過標題行，只取正文
            while body and not body[0].strip():    # 去掉正文前後的空行，稍後統一補
                body.pop(0)
            while body and not body[-1].strip():
                body.pop()
            if body:
                moved.append(body)

        new_lines = list(lines)
        for start, end, _ in reversed(others):     # 由後往前刪除，避免行號位移
            del new_lines[start:end]

        block = []
        for body in moved:
            if block:
                block.append("")
            block.extend(body)
        if block:
            if keep_end > 0 and new_lines[keep_end - 1].strip():
                block.insert(0, "")
            if keep_end < len(new_lines) and new_lines[keep_end].strip():
                block.append("")
            new_lines[keep_end:keep_end] = block

        self._set_editor_text("\n".join(new_lines), "diff")
        self._sync_raw_lines()
        self._rebuild_toc()

        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == keep_start:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._checkpoint_document()
        self._show_status(f"已把 {len(others)} 章的正文併入「{keep_title[:20]}」，可以按 Ctrl+Z 復原")

    @action
    def renumber_selected_chapters(self):
        """把選取的章節（或卷／集／篇）改成連續編號：保留第一個（依文件順序）
        的原編號當起點，其餘依序遞增。同一次只能對同一種單位操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected_nodes = [node for node in self._selected_toc_items()
                          if self.chapter_records.get(node, {}).get("kind") in ("chapter", "volume")]
        if len(selected_nodes) < 2:
            dialogs.info(self, "連續編號", "請選取兩個以上的章節或卷節點（不含作品層級）。")
            return
        if not self._chapter_nodes_share_unit(selected_nodes):
            dialogs.info(
                self, "連續編號",
                "選取的項目類型不一致（例如同時選到「第…卷」和「第…集」，"
                "或「第…集」和「第…章」），這些是各自獨立的編號系統，"
                "混在一起重排沒有意義，請分開執行。",
            )
            return

        selected_nodes.sort(key=lambda node: self.chapter_raw_map.get(node, 0))

        plans, skipped = [], []
        for node in selected_nodes:
            raw_idx = self.chapter_raw_map.get(node)
            if raw_idx is None or raw_idx >= len(self.raw_lines):
                skipped.append((node, "找不到對應行"))
                continue
            raw_line = self.raw_lines[raw_idx]
            leading = raw_line[:len(raw_line) - len(raw_line.lstrip())]
            clean, marker = strip_persistent_title_marker(raw_line.strip())
            located = locate_chapter_number(clean)
            if located is None:
                skipped.append((node, "無法自動判斷編號位置"))
                continue
            start, end, number_text = located
            current_number = int(round(chinese_to_arabic(number_text)))
            plans.append({
                "node": node, "raw_idx": raw_idx, "leading": leading,
                "clean": clean, "marker": marker, "start": start, "end": end,
                "number_text": number_text, "current_number": current_number,
            })

        if len(plans) < 2:
            detail = "\n".join(f"• {node.text(0)[:24]}：{reason}" for node, reason in skipped)
            dialogs.info(
                self, "連續編號",
                "選取範圍內能自動判斷編號位置的項目不足兩個，無法執行。\n\n" + detail,
            )
            return

        anchor_number = plans[0]["current_number"]
        targets = [anchor_number + offset for offset in range(len(plans))]

        selected_node_set = {plan["node"] for plan in plans}
        reference_signature = chapter_unit_signature(plans[0]["clean"])
        sibling_index = {}
        for parent in {plan["node"].parent() for plan in plans}:
            for sibling in toc_ops.tree_children(self.tree, parent):
                if sibling in selected_node_set:
                    continue
                sibling_row = self.chapter_raw_map.get(sibling)
                if sibling_row is None or sibling_row >= len(self.raw_lines):
                    continue
                sibling_clean, _m = strip_persistent_title_marker(self.raw_lines[sibling_row].strip())
                if chapter_unit_signature(sibling_clean) != reference_signature:
                    continue
                sibling_located = locate_chapter_number(sibling_clean)
                if sibling_located is None:
                    continue
                _s, _e, sibling_number_text = sibling_located
                key = (parent, int(round(chinese_to_arabic(sibling_number_text))))
                sibling_index.setdefault(key, sibling)

        conflicts = []
        for plan, target in zip(plans, targets):
            sibling = sibling_index.get((plan["node"].parent(), target))
            if sibling is not None:
                conflicts.append((plan, target, sibling))

        changes = [(plan, target) for plan, target in zip(plans, targets)
                  if plan["current_number"] != target]
        if not changes:
            dialogs.info(self, "連續編號", "選取的項目編號本來就已經連續，不需要調整。")
            return

        preview_lines = [f"「{plan['node'].text(0)[:22]}」：{plan['current_number']} → {target}"
                         for plan, target in changes[:8]]
        if len(changes) > 8:
            preview_lines.append(f"……等共 {len(changes)} 處")
        message = "將調整以下編號：\n\n" + "\n".join(preview_lines)
        if skipped:
            message += "\n\n以下項目無法判斷編號位置，將維持不變：\n" + "\n".join(
                f"• {node.text(0)[:22]}：{reason}" for node, reason in skipped)
        if conflicts:
            conflict_lines = "\n".join(
                f"• 目標編號 {target} 與「{sibling.text(0)[:22]}」重複"
                for _plan, target, sibling in conflicts[:5])
            message += "\n\n⚠️ 以下目標編號會與未選取的項目重複，繼續的話會出現重複編號：\n" + conflict_lines
        message += "\n\n此操作可用「復原」撤銷。是否繼續？"

        if not dialogs.confirm(self, "連續編號", message):
            return

        marker_suffix = {"exclude": "[::X]", "include": "[::]",
                         "auto_work": "[::W]", "auto_title": "[::T]", "": ""}
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for plan, target in changes:
            new_number_text = render_chapter_number_like(target, plan["number_text"])
            new_clean = plan["clean"][:plan["start"]] + new_number_text + plan["clean"][plan["end"]:]
            new_line = plan["leading"] + new_clean + marker_suffix[plan["marker"]]
            self._replace_line(cursor, plan["raw_idx"], new_line)
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(f"已把 {len(changes)} 章改成連續編號，可以按 Ctrl+Z 復原")

    def _exclude_marker_for(self, raw_idx: int, line: str) -> str:
        """把某一行移出目錄時，行尾該留什麼？

        只有「使用者自己用 [::] 加進目錄、而且拿掉標記之後不會被自動認成
        章節」的行，才直接把標記清掉——那段正文本來就不是章節，留一個 [::X]
        只是在檔案裡多一個看不懂的符號。

        其他情況一律寫 [::X]：沒有標記、或是 [::W]／[::T] 這類自動標記的行，
        它會在目錄裡是因為別的機制（合集結構、自動標題快取、強制層級…），
        只清標記的話文字根本沒變，重新掃描它又會回來，使用者卻看到「已移除」。"""
        clean, marker = strip_persistent_title_marker(line.strip())
        if marker != "include":
            return "[::X]"
        if raw_idx in self.force_lv1_chapters or raw_idx in self.force_lv2_chapters:
            return "[::X]"
        return "[::X]" if looks_like_auto_chapter(clean, self.user_chapter_rules) else ""

    @action
    def ignore_selected_chapter(self):
        """章節保留在正文中，只是行尾加上 [::X]，從目錄移除。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = sorted({raw_idx for node in selected
                          if (raw_idx := self.chapter_raw_map.get(node)) is not None}, reverse=True)
        if not targets:
            return
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        marked_count, last_clean, marked_any = 0, "", False
        for raw_idx in targets:
            raw_str = self.raw_lines[raw_idx]
            clean, marker = strip_persistent_title_marker(raw_str.strip())
            if marker == "exclude":
                continue
            leading = raw_str[:len(raw_str) - len(raw_str.lstrip())]
            suffix = self._exclude_marker_for(raw_idx, raw_str)
            self._replace_line(cursor, raw_idx, leading + clean + suffix)
            marked_any = marked_any or bool(suffix)
            marked_count += 1
            last_clean = clean
        cursor.endEditBlock()
        if not marked_count:
            return
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        note = "（行尾已加上 [::X]）" if marked_any else "（原本就不是章節，已直接清掉標記）"
        if marked_count == 1:
            self._show_status(f"已移出目錄：{last_clean[:18]}{note}")
        else:
            self._show_status(f"已把 {marked_count} 章移出目錄{note}")

    @action
    def set_chapter_level(self, level: int):
        """章節層級（force_lv1/2_chapters）是跟著文件走的結構狀態，不是文字，
        所以就算沒有改到任何一個字，也要單獨存一次復原步驟，不然 Ctrl+Z
        永遠碰不到這個操作。"""
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        targets = {raw_idx for node in selected if (raw_idx := self.chapter_raw_map.get(node)) is not None}
        if not targets:
            return
        for raw_idx in targets:
            if level == 1:
                self.force_lv1_chapters.add(raw_idx)
                self.force_lv2_chapters.discard(raw_idx)
            elif level == 2:
                self.force_lv2_chapters.add(raw_idx)
                self.force_lv1_chapters.discard(raw_idx)
        self._rebuild_toc()
        self._checkpoint_document()
        if len(targets) == 1:
            raw_str = self.raw_lines[next(iter(targets))].strip()
            self._show_status(f"已設為{'卷' if level == 1 else '章'}標題：{raw_str[:15]}")
        else:
            self._show_status(f"已把 {len(targets)} 項設為{'卷' if level == 1 else '章'}標題")

    @action
    def delete_selected_chapter(self):
        self._sync_raw_lines()
        self._ensure_toc_current()
        selected = self._selected_toc_items()
        if not selected:
            return
        lines = self.editor.toPlainText().split("\n")
        total_lines = len(lines)
        sections = []
        ordered = toc_ops.ordered_boundaries(self.chapter_index_map, self.toc_boundary_map)
        for node in selected:
            span = toc_ops.toc_section_lines(
                self.tree, node, self.chapter_index_map, self.toc_boundary_map, total_lines, ordered)
            if span is not None:
                sections.append((span[0], span[1], node))
        if not sections:
            return
        sections.sort()
        merged = [sections[0]]
        for start, end, node in sections[1:]:
            last_start, last_end, last_node = merged[-1]
            if start < last_end:
                merged[-1] = (last_start, max(last_end, end), last_node)
            else:
                merged.append((start, end, node))
        sections = merged

        if len(sections) == 1:
            ch_name = sections[0][2].text(0)
            question = f"確定要刪除「{ch_name}」及其所有正文內容嗎？"
            done_text = f"已刪除：{ch_name}，可以按 Ctrl+Z 復原"
        else:
            preview_text = "、".join(node.text(0)[:20] for _, _, node in sections[:4])
            if len(sections) > 4:
                preview_text += f" 等 {len(sections)} 章"
            question = f"確定要刪除以下章節及其所有正文內容嗎？\n\n{preview_text}"
            done_text = f"已刪除 {len(sections)} 章，可以按 Ctrl+Z 復原"
        if not dialogs.confirm(self, "刪除章節", question):
            return

        document = self.editor.document()
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        for start, end, _ in reversed(sections):    # 由後往前刪除，讓前面區段的行號保持有效
            start_block = document.findBlockByNumber(start)
            end_block = document.findBlockByNumber(end)
            end_of_document = document.characterCount() - 1     # Qt 單位，不能用 len(文字)
            start_pos = start_block.position() if start_block.isValid() else end_of_document
            end_pos = end_block.position() if end_block.isValid() else end_of_document
            cursor.setPosition(start_pos)
            cursor.setPosition(end_pos, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        cursor.endEditBlock()

        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        self._show_status(done_text)

    def _build_editor_context_menu(self):
        """只負責組出選單，不呼叫 exec()——方便測試時不用真的彈出視窗。

        刻意不用 QPlainTextEdit.createStandardContextMenu()：那組復原／剪下／
        複製／貼上／全選是 Qt 內建、沒套用中文翻譯，混在自己中文選單裡顯得
        突兀，而且復原／剪貼本來就有全域快捷鍵可用，選單裡不需要重複。

        「加入目錄」「移出目錄」只列現在能用的那一個（看目標那一行在不在目錄裡）；
        空行、選了好幾行時兩個都不列。"""
        menu = QMenu(self)
        insert_action = menu.addAction("新增章節")
        insert_action.triggered.connect(self.open_insert_title_dialog)

        target = self._editor_target_line()
        if target is not None:
            menu.addSeparator()
            if target[0] in self._current_title_lines():
                menu.addAction("這一行移出目錄").triggered.connect(self.exclude_selected_line)
            else:
                menu.addAction("這一行加入目錄").triggered.connect(self.mark_selected_as_title)

        # 下方接上 Qt 自己的剪下／複製／貼上／全選：這些動作的文字由 Qt 的
        # 翻譯檔提供（見 i18n.install_qt_translation），會跟著繁簡切換。
        standard = self.editor.createStandardContextMenu()
        # 去掉 Qt 自己的復原／重做：Qt 的復原已經關掉（復原走自訂快照系統），
        # 留著只會是兩個永遠灰掉的項目。Ctrl+Z／Ctrl+Y 與工具列按鈕照常可用。
        undo_keys = {QKeySequence(QKeySequence.StandardKey.Undo).toString(),
                     QKeySequence(QKeySequence.StandardKey.Redo).toString()}
        # Qt 把快捷鍵直接寫在動作文字裡（「復原(&U)	Ctrl+Z」），shortcut() 是空的，
        # 所以從文字尾端取快捷鍵來比對。
        editing_actions = [action for action in standard.actions()
                           if action.text() and action.text().rpartition("	")[2] not in undo_keys]
        if editing_actions:
            menu.addSeparator()
            # 只把「動作」接過來，改由這個選單持有；標準選單本身要丟掉，
            # 不能拿它當子元件——QMenu 是一個真的視窗元件，掛成子元件會被
            # 整個畫在自訂選單上面，變成兩層選單疊在一起。
            for standard_action in editing_actions:
                standard_action.setParent(menu)
            menu.addActions(editing_actions)
        standard.deleteLater()
        return menu

    def _show_editor_context_menu(self, pos):
        # 在選取範圍以外按右鍵時，先把游標移到按的位置：選單裡的動作（新增
        # 章節、加入／移除目錄）都是針對「游標所在那一行」。
        clicked = self.editor.cursorForPosition(pos)
        current = self.editor.textCursor()
        if not (current.hasSelection()
                and current.selectionStart() <= clicked.position() <= current.selectionEnd()):
            self.editor.setTextCursor(clicked)
        menu = self._build_editor_context_menu()
        menu.exec(self.editor.viewport().mapToGlobal(pos))

    def _current_title_lines(self) -> set:
        """目前目錄裡每個節點在正文的行號（已換算到編輯器現在的內容）。"""
        self._sync_raw_lines()
        return {self._map_tree_line(row) for row in self.chapter_raw_map.values()}

    def _editor_target_line(self):
        """右鍵動作的目標行：有選取時是選取範圍內唯一的非空白行，沒選取時是
        游標所在行。多行或空白行回傳 None——避免把整段正文誤加進目錄。"""
        cursor = self.editor.textCursor()
        document = self.editor.document()
        if not cursor.hasSelection():
            block = cursor.block()
            return (block.blockNumber(), block.text()) if block.text().strip() else None
        start_block = document.findBlock(cursor.selectionStart())
        end_block = document.findBlock(cursor.selectionEnd())
        end_number = end_block.blockNumber()
        if cursor.selectionEnd() == end_block.position() and end_number > start_block.blockNumber():
            end_number -= 1
        lines = []
        for number in range(start_block.blockNumber(), end_number + 1):
            text = document.findBlockByNumber(number).text()
            if text.strip():
                lines.append((number, text))
        return lines[0] if len(lines) == 1 else None

    @action
    def mark_selected_as_title(self):
        """把目標行標成目錄章節（行尾加 [::]；原本若是 [::X] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一個標題", "請選取（或把游標放在）單獨一行章節標題，避免將正文誤加到目錄。")
            return
        clean_title = self._set_line_marker(target[0], "[::]")
        if clean_title is None:
            return
        for item, raw_index in self.chapter_raw_map.items():
            if raw_index == target[0]:
                self.tree.setCurrentItem(item)
                self._on_tree_item_clicked(item, 0)
                break
        self._show_status(f"已加入目錄：{clean_title}")

    @action
    def exclude_selected_line(self):
        """把目標行移出目錄（行尾加 [::X]；原本若是 [::] 會一併換掉）。"""
        target = self._editor_target_line()
        if target is None:
            dialogs.info(self, "一次限一行", "請選取（或把游標放在）單獨一行章節標題。")
            return
        suffix = self._exclude_marker_for(target[0], target[1])
        clean_title = self._set_line_marker(target[0], suffix)
        if clean_title is None:
            return
        self._show_status(f"已移出目錄：{clean_title}" if suffix
                          else f"已移除章節標記：{clean_title}")

    def _set_line_marker(self, line_number: int, marker: str):
        block = self.editor.document().findBlockByNumber(line_number)
        original = block.text()
        clean, _old_marker = strip_persistent_title_marker(original.strip())
        if not clean:
            return None
        leading = original[:len(original) - len(original.lstrip())]
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        self._replace_line(cursor, line_number, leading + clean + marker)
        cursor.endEditBlock()
        self._sync_raw_lines()
        self._rebuild_toc()
        self._checkpoint_document()
        return clean
