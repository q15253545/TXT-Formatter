"""章節字數：每一章正文有幾個字（不含空白），並標出特別短或特別長的章。

特別短的章常常是只剩作者的話、正文被截掉；特別長的章常常是兩章之間少了標題、被併成一章。
"""

from statistics import median

_WHITESPACE = dict.fromkeys(map(ord, " \t　 \r\f\v"), None)
SHORT_RATIO = 0.3      # 比中位數的三成還少
LONG_RATIO = 3.0       # 超過中位數的三倍


_WHITESPACE_CHARS = tuple(map(chr, _WHITESPACE))


def char_count(text: str) -> int:
    return len(text.translate(_WHITESPACE))


def _joined_count(lines) -> int:
    """一段行的字數：接成一個字串再數空白（跟逐行 char_count 加總一樣）。檢查章節每次目錄重建都要算整本，
    逐行呼叫二十萬次要半秒，接起來數只要幾十毫秒。"""
    text = "".join(lines)
    return len(text) - sum(text.count(char) for char in _WHITESPACE_CHARS)


def chapter_word_counts(lines, sections):
    """sections：[(標題行號, 結束行號（不含）, 標題, 所屬的卷)]，照本文順序。
    回傳（每章的資料, 總結）。"""
    entries = []
    for row, end, title, volume in sections:
        entries.append({"row": row, "title": title, "volume": volume, "count": _joined_count(lines[row + 1:end])})
    counts = [entry["count"] for entry in entries if entry["count"]]
    middle = median(counts) if counts else 0
    for entry in entries:
        count = entry["count"]
        if not count:
            entry["note"] = "沒有正文"
        elif count < middle * SHORT_RATIO:
            entry["note"] = "偏短"
        elif count > middle * LONG_RATIO:
            entry["note"] = "偏長"
        else:
            entry["note"] = ""
    summary = {
        "total": _joined_count(lines),
        "chapters": len(entries),
        "average": round(sum(counts) / len(counts)) if counts else 0,
        "median": round(middle),
        "flagged": sum(1 for entry in entries if entry["note"]),
    }
    return entries, summary
