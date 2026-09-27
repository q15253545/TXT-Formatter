"""單次排版期間的選項快照。"""

from dataclasses import dataclass


@dataclass
class FormatOptions:
    """單次排版期間的選項快照。

    tk.BooleanVar.get() 每次都要穿過 Tcl 直譯器，比屬性存取慢一個數量級。
    這些選項在一次排版過程中不會改變，所以開始前讀一次就好；
    順帶讓各個 _render_* 方法不再各自偷讀 self.opt_*，變得可以單獨測試。
    """
    remove_extra_empty: bool = False
    remove_indent: bool = False
    remove_extra_spaces: bool = False
    auto_indent: bool = False
    add_paragraph_empty: bool = False
    add_empty: bool = False
    format_title: bool = False
    merge_title: bool = False
    # 整理段落換行：固定欄寬的硬換行接回同一段
    reflow_paragraphs: bool = False
    normalize_punct: bool = False
    halfwidth_punct: bool = False
    format_dialogue: bool = False
    fullwidth_digits: bool = False
    halfwidth_digits: bool = False
    num_style: str = "保留原文"
    sep_style: str = "保留原文"
    structure: str = "自動判斷"

    @property
    def keep_number(self):
        return self.num_style == "保留原文"

    @property
    def keep_separator(self):
        return self.sep_style == "保留原文"
