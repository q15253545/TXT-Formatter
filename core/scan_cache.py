"""開檔後在背景先把各種掃描的「逐行判斷」算好。

掃描無關連內容、作者感言、標點校對、本文可疑章節都照「行的內容」快取每一行的判斷，
第一次開視窗時才算會卡一兩秒（大檔十幾萬行）；先在背景算好，開視窗時就只剩查表。
"""

import gc

from .ad_scan import _LINE_PROFILES, _line_profile, compact_ad_text, forum_line_strength, meta_line_kind
from .chapter_parse import heading_number, heading_word, parse_weak_numbered_title
from .quote_check import _check_line
from .title_markers import strip_persistent_title_marker
from .user_rules import preset_match

_CANDIDATE_MAX_LENGTH = 60


def warm_line_caches(lines):
    for line in lines:
        _line_profile(line)
        forum_line_strength(line)
        meta_line_kind(line)
        _check_line(line)
        text = line.strip()
        if text and len(text) <= _CANDIDATE_MAX_LENGTH:
            clean, marker = strip_persistent_title_marker(text)
            if clean and marker != "exclude":
                preset_match(clean)
                parse_weak_numbered_title(clean)
                heading_word(clean)


def freeze_line_caches():
    """整本算完後把現有的物件移出垃圾回收的掃描範圍。快取裡是十幾萬個字串、tuple，
    每次完整回收都要全部掃一遍（大檔約 0.1 秒），剛好碰上開視窗、打字就多卡一下；
    這些物件照樣靠參考計數釋放，只是不再被反覆掃描。"""
    gc.freeze()


def clear_line_caches():
    """換檔、清空時丟掉上一本的逐行快取：鍵是行的內容，換了書幾乎用不到，
    留著只會佔記憶體（一本十幾萬行的大檔就有兩三百 MB）。凍結的物件放回回收範圍，
    上一本留下的循環參考才收得掉。"""
    gc.unfreeze()
    _LINE_PROFILES.clear()
    for cached in (compact_ad_text, forum_line_strength, meta_line_kind, _check_line, preset_match, heading_word, heading_number,
                   parse_weak_numbered_title):
        cached.cache_clear()
