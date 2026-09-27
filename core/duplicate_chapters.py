"""相鄰、章號相同的章節：找出來，合併成一章。

連載轉貼的文字檔常常同一章出現兩次，例如先貼了「第12章 風起」，後面又貼了
一次「第12章 風起【求月票】」，中間夾著作者的話。目錄裡看起來就是兩個第 12 章。

以「最不容易誤判」為前提：
- 只看目錄裡「緊鄰」的兩個（以上）章節，而且篇名、卷名、前綴、章號、單位全部
  一樣；中間隔了卷標題或其他章節的不算。
- 合併後保留第一個標題的位置。標題文字預設用第一個；只有在兩個標題除了尾端
  一段括號附註（求票、月票、加更…）以外完全相同時，才改用沒有附註的那個。
- 標題不同的（「第12章 風起」「第12章 雲湧」）多半是作者編號打錯、其實是兩章，
  清單裡列出來但預設不勾。
- 兩個標題之間的內容原封不動併進同一章；其中的作者的話交給「作者感言與作品資訊」的
  「作者感言」處理。
"""

import re

from .chapter_parse import parse_lv2
from .title_markers import strip_persistent_title_marker

# 尾端括號附註裡出現這些字，才算「求票這類附註」；其他括號（「（上）」「(修)」）
# 是標題本身的一部分，有差就是不同標題。
_NOTE_WORDS = ("票", "加更", "爆更", "补更", "補更", "收藏", "打赏", "打賞", "订阅", "訂閱",
               "推荐", "推薦")
_TRAILING_NOTE = re.compile(r"\s*(?:\[[^\[\]]*\]|【[^【】]*】|（[^（）]*）|\([^()]*\))\s*$")
_MARKER_TEXT = {"include": "[::]", "auto_work": "[::W]", "auto_title": "[::T]"}


def _strip_note(title: str):
    """回傳（去掉尾端附註的標題, 有沒有附註）。"""
    match = _TRAILING_NOTE.search(title)
    if match and match.start() > 0 and any(word in match.group(0) for word in _NOTE_WORDS):
        return title[:match.start()].rstrip(), True
    return title, False


def _identity(parsed):
    arc, vol, prefix, number, unit = parsed[:5]
    return (arc or "").strip(), (vol or "").strip(), prefix, number, unit


def _parse_title(line: str):
    clean, marker = strip_persistent_title_marker(line.strip())
    if marker == "exclude" or not clean:
        return clean, marker, None
    return clean, marker, parse_lv2(clean)


def find_duplicate_groups(lines, title_rows) -> list:
    """title_rows：目錄上所有標題的行號（0 起算，含卷、特殊標題）。

    回傳每一組重複的章節：
      rows         這一組標題的行號（文件順序）
      titles       各自的標題文字（不含行尾標記）
      keep_title   合併後的標題文字
      same_title   除了尾端附註以外標題完全相同（預設勾選）
      between      各標題之間夾了幾行文字（不算空行）
    """
    rows = sorted(row for row in set(title_rows) if 0 <= row < len(lines))
    parsed = {row: _parse_title(lines[row]) for row in rows}
    groups = []
    index = 0
    while index < len(rows):
        data = parsed[rows[index]][2]
        if not data or not data[3]:
            index += 1
            continue
        end = index + 1
        while end < len(rows) and parsed[rows[end]][2] and _identity(parsed[rows[end]][2]) == _identity(data):
            end += 1
        if end - index > 1:
            groups.append(_make_group(lines, rows[index:end], parsed))
        index = end
    return groups


def _make_group(lines, rows, parsed) -> dict:
    titles = [parsed[row][0] for row in rows]
    stripped = [_strip_note(title) for title in titles]
    same_title = len({re.sub(r"\s+", "", base) for base, _noted in stripped}) == 1
    keep_title = titles[0]
    if same_title and stripped[0][1]:
        keep_title = next((title for title, (_base, noted) in zip(titles, stripped) if not noted), titles[0])
    between = [sum(1 for row in range(first + 1, second) if lines[row].strip())
               for first, second in zip(rows, rows[1:])]
    return {"rows": list(rows), "titles": titles, "keep_title": keep_title,
            "same_title": same_title, "between": between}


def merge_duplicate_groups(lines, groups) -> list:
    """把每一組合併成一章：第一個標題改成 keep_title，其餘標題行刪掉。
    刪掉的標題前後都是空行時，順便拿掉一個空行，不留下連續兩個空行。"""
    result = list(lines)
    replace = {}
    delete = set()
    for group in groups:
        first = group["rows"][0]
        original = result[first]
        _clean, marker, _data = _parse_title(original)
        indent = original[:len(original) - len(original.lstrip())]
        replace[first] = indent + group["keep_title"] + _MARKER_TEXT.get(marker, "")
        delete.update(group["rows"][1:])
    for row, text in replace.items():
        result[row] = text
    for row in sorted(delete, reverse=True):
        del result[row]
        if 0 < row < len(result) and not result[row].strip() and not result[row - 1].strip():
            del result[row]
    return result
