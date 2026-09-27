"""章節字數：每一章正文有幾個字（不含空白），並標出特別短或特別長的章。

特別短的章常常是只剩作者的話、正文被截掉；特別長的章常常是兩章之間少了標題、被併成一章。
"""

from statistics import median

_WHITESPACE = dict.fromkeys(map(ord, " \t　 \r\f\v"), None)
SHORT_RATIO = 0.3      # 比中位數的三成還少
LONG_RATIO = 3.0       # 超過中位數的三倍


def char_count(text: str) -> int:
    return len(text.translate(_WHITESPACE))


def chapter_word_counts(lines, sections):
    """sections：[(標題行號, 結束行號（不含）, 標題, 所屬的卷)]，照本文順序。
    回傳（每章的資料, 總結）。"""
    per_line = [char_count(line) for line in lines]
    entries = []
    for row, end, title, volume in sections:
        entries.append({"row": row, "title": title, "volume": volume, "count": sum(per_line[row + 1:end])})
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
        "total": sum(per_line),
        "chapters": len(entries),
        "average": round(sum(counts) / len(counts)) if counts else 0,
        "median": round(middle),
        "flagged": sum(1 for entry in entries if entry["note"]),
    }
    return entries, summary
