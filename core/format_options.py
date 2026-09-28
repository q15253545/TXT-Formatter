"""單次排版期間的選項快照。"""

from dataclasses import dataclass


@dataclass
class FormatOptions:
    """單次排版期間的選項快照：開始前從介面讀一次，排版過程中不會變；
    core 的排版只看這份資料、不碰介面，所以可以單獨測試。"""
    remove_extra_empty: bool = False
    remove_indent: bool = False
    remove_extra_spaces: bool = False
    auto_indent: bool = False
    # 增加縮排用四個半形空格（沒開是兩個全形空格）
    halfwidth_indent: bool = False
    add_paragraph_empty: bool = False
    add_empty: bool = False
    format_title: bool = False
    merge_title: bool = False
    # 整理段落換行：固定欄寬的硬換行接回同一段
    reflow_paragraphs: bool = False
    normalize_punct: bool = False
    halfwidth_punct: bool = False
    # 對話引號：保留原樣／「」『』／“”‘’（core/text_format.QUOTE_STYLES）
    quote_style: str = "保留原樣"
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
