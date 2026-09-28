"""行尾持久標記：[::] 人工收錄、[::X] 排除、[::W]/[::T] 自動作品／附錄結構。"""

import re

from .cn_numerals import chinese_to_arabic

END_MARK_REGEX = re.compile(r"[部卷篇集季章回節节折幕][\s]*完(?:本)?[\s】\]）\)]*$", re.IGNORECASE)

# 卷／章「結尾行」：第一卷終、【第二卷终】、第一章 完、（本卷完）、卷三結束……
# 先拿掉括號、空白與句末符號再整行比對，所以寫法再怎麼包都認得出來；整行
# 必須「只有」編號＋結尾字，「第十章 終章」「第三卷 完結篇」這種真正的
# 標題不會被當成結尾。
_END_MARK_NOISE = re.compile(r"[\s【】\[\]（）()「」『』〔〕《》〈〉<>·・:：\-—~～!！。．.…*＊=＝]")
_END_NUMBER = r"[0-9０-９一二兩两三四五六七八九十百千萬万〇零]+"
_END_WORDS = r"(?:完結|完结|完本|結束|结束|終了|终了|完|終|终)"
_END_MARK_FULL = re.compile(
    rf"^(?:第(?P<n1>{_END_NUMBER})(?P<u1>[部卷篇集季章回節节折幕])"
    rf"|(?P<u2>[部卷篇集])(?P<n2>{_END_NUMBER})"
    rf"|[本全](?P<u3>[部卷篇集季章回節节]))"
    rf"{_END_WORDS}$")
# 卷結尾行中間帶卷名（只認卷級，而且結尾字要單獨一段，例如「第一卷 山路 完」）
_NAMED_VOLUME_END = re.compile(
    rf"^[【\[（(〔「『]?\s*(?:第\s*(?P<n1>{_END_NUMBER})\s*(?P<u1>[部卷篇集季])|(?P<u2>[部卷篇集])\s*(?P<n2>{_END_NUMBER}))"
    rf"\s+(?P<name>[^\s【】\[\]（）()]{{1,12}})\s+{_END_WORDS}\s*[】\]）)〕」』]?$")
_VOLUME_UNITS = set("部卷篇集季")


def parse_end_mark(text):
    """辨識卷／章結尾行；回傳 {"level": "volume"|"chapter", "number": int|None,
    "unit": 單位字} 或 None。number 為 None 代表「本卷完」這類沒寫編號的寫法。"""
    named = _NAMED_VOLUME_END.match(text.strip()) if len(text) <= 30 else None
    if named:
        number_text = named.group("n1") or named.group("n2")
        value = chinese_to_arabic(number_text)
        return {"level": "volume", "number": int(value) if value and float(value).is_integer() else None,
                "unit": named.group("u1") or named.group("u2"), "name": named.group("name")}
    compact = _END_MARK_NOISE.sub("", text)
    if not compact or len(compact) > 16:
        return None
    match = _END_MARK_FULL.match(compact)
    if not match:
        return None
    unit = match.group("u1") or match.group("u2") or match.group("u3")
    number_text = match.group("n1") or match.group("n2")
    number = None
    if number_text:
        value = chinese_to_arabic(number_text)
        number = int(value) if value and float(value).is_integer() else None
    return {"level": "volume" if unit in _VOLUME_UNITS else "chapter", "number": number, "unit": unit}
_MARKER_SUFFIXES = (("[::x]", "exclude"), ("[::]", "include"),
                    ("[::w]", "auto_work"), ("[::t]", "auto_title"))


def strip_persistent_title_marker(text):
    """移除行尾持久標記，回傳（正文、include／exclude／auto_work／auto_title／空字串）。

    只看行尾的固定字串，不用正則（每一行開檔、重掃都會呼叫，正則遇到大量空白會回溯）。
    大小寫不分（[::w]、[::t] 跟畫面隱藏、匯出移除的規則一致）。"""
    tail = text.rstrip()
    if not tail.endswith("]"):
        return text, ""
    ending = tail[-5:].casefold()
    for suffix, kind in _MARKER_SUFFIXES:
        if ending.endswith(suffix):
            return tail[:-len(suffix)].rstrip(), kind
    return text, ""


# 匯出用：整份文字裡所有行尾標記。標記前面的空白一起拿掉，避免留下一行
# 結尾的多餘空格；行尾可能是換行或檔案結尾。
EXPORT_MARKER_REGEX = re.compile(
    r"[ \t\u3000]*\[::[XxWwTt]?\][ \t\u3000]*(?=\r?\n|\Z)")


def strip_export_markers(text):
    """移除全文的行尾持久標記，回傳（移除後的文字、移除了幾個）。

    匯出給別人看的成品時用。拿掉之後，人工指定的章節、移出目錄的設定
    都會跟著消失——那些狀態就是靠這些標記存在檔案裡的。"""
    return EXPORT_MARKER_REGEX.subn("", text)


