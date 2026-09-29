"""相鄰、章號相同的章節：找出來，讓使用者勾選要保留哪幾章。

連載轉貼的文字檔常常同一章出現兩次，例如先貼了「第12章 風起」，後面又貼了
一次「第12章 風起【求月票】」，中間夾著作者的話。目錄裡看起來就是兩個第 12 章。

以「最不容易誤判」為前提：
- 只看目錄裡「緊鄰」的兩個（以上）章節，而且篇名、卷名、前綴、章號、單位全部
  一樣；中間隔了卷標題或其他章節的不算。
- 合併後保留第一個標題的位置。標題文字預設用第一個；只有在兩個標題除了尾端
  一段括號附註（求票、月票、加更…）以外完全相同時，才改用沒有附註的那個。
- 標題不同的（「第12章 風起」「第12章 雲湧」）多半是作者編號打錯、其實是兩章，
  清單裡列出來但預設不勾。
- 兩個標題之間的內容原封不動併進同一章；其中的作者的話交給非正文內容「作者感言與作品資訊」分頁的
  「作者感言」處理。

每一章另外比對正文（find_duplicate_groups 的 relations、default_keep），清單上顯示
「重貼標題」「幾乎相同」「包含上一個」…，使用者勾選要保留的章（apply_keep_choices）：
- 組裡有正文不到 DUPLICATE_BODY_LIMIT 字的章（重貼標題）：不保留的只刪標題行，
  內容照上面的規則併進同一章（跟以前的合併一樣）。
- 每章都有正文：不保留的章標題連正文整章刪除（同一章貼了兩份完整正文時，只留一份）。
- 一組全部保留或全部不保留：不動。
"""

import re
from functools import lru_cache

from .chapter_parse import parse_lv2
from .title_markers import strip_persistent_title_marker
from .word_count import char_count

# 尾端括號附註裡出現這些字，才算「求票這類附註」；其他括號（「（上）」「(修)」）
# 是標題本身的一部分，有差就是不同標題。
_NOTE_WORDS = ("票", "加更", "爆更", "补更", "補更", "收藏", "打赏", "打賞", "订阅", "訂閱",
               "推荐", "推薦")
_TRAILING_NOTE = re.compile(r"\s*(?:\[[^\[\]]*\]|【[^【】]*】|（[^（）]*）|\([^()]*\))\s*$")
_MARKER_TEXT = {"include": "[::]", "auto_work": "[::W]", "auto_title": "[::T]"}

# 正文少於這麼多字（不含空白）的章當成「重貼標題」：只是標題又貼了一次，中間夾著作者的話
DUPLICATE_BODY_LIMIT = 100
# 比對正文用的句子：句末標點或換行切開，太短的句子（「嗯。」「好。」）到處都有，不算
_SENTENCE_SPLIT = re.compile(r"[。！？!?…\n]+")
_SENTENCE_MIN = 10
_SPACES = re.compile(r"\s+")
SAME_RATIO = 0.9          # 較短那份有九成的句子在另一份裡：同一章的兩個版本
PARTLY_RATIO = 0.3        # 三成以上：部分相同
SIMILAR_LENGTH = 1.1      # 字數差一成以內算「差不多長」

REPOST, SAME, CONTAINS, CONTAINED, PARTLY, DIFFERENT = (
    "repost", "same", "contains", "contained", "partly", "different")
RELATION_LABELS = {REPOST: "重貼標題", SAME: "與上一個幾乎相同", CONTAINS: "包含上一個",
                   CONTAINED: "被上一個包含", PARTLY: "與上一個部分相同", DIFFERENT: "與上一個不同"}


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
      ends         各章的結束行號（不含）：下一個目錄標題
      counts       各章正文字數（不含空白）
      relations    各章跟上一章比的結果（RELATION_LABELS 的鍵；第一章只可能是 REPOST 或 ""）
      default_keep 預設保留哪幾章（bool，跟 rows 對齊）
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
            chapter_ends = [rows[position + 1] if position + 1 < len(rows) else len(lines)
                            for position in range(index, end)]
            groups.append(_make_group(lines, rows[index:end], parsed, chapter_ends))
        index = end
    return groups


def _sentences(lines) -> set:
    return _split_sentences("\n".join(lines))


@lru_cache(maxsize=4096)
def _text_sentences(text: str) -> frozenset:
    """照整章的文字快取：改了本文重新找時，沒改到的章不用再切一次。"""
    return frozenset(_split_sentences(text))


def _split_sentences(text: str) -> set:
    text = _SPACES.sub("", text)
    return {part for part in _SENTENCE_SPLIT.split(text) if len(part) >= _SENTENCE_MIN}


def _relation(previous: dict, current: dict) -> str:
    if current["count"] < DUPLICATE_BODY_LIMIT:
        return REPOST
    if previous is None or previous["count"] < DUPLICATE_BODY_LIMIT:
        return ""
    first, second = previous["sentences"], current["sentences"]
    if not first or not second:
        return DIFFERENT
    shared = len(first & second) / min(len(first), len(second))
    if shared < PARTLY_RATIO:
        return DIFFERENT
    if shared < SAME_RATIO:
        return PARTLY
    longer, shorter = max(previous["count"], current["count"]), min(previous["count"], current["count"])
    if longer <= shorter * SIMILAR_LENGTH:
        return SAME
    return CONTAINS if current["count"] > previous["count"] else CONTAINED


def _default_keep(titles, keep_title, same_title, info, relations) -> list:
    """重貼標題：保留沒有附註的那個（標題相同時；等於以前的合併），標題不同就保留有正文的；
    幾章的正文互相幾乎相同或包含：保留最長的（差不多長就保留後面那個，通常是修訂版）；其他全部保留。"""
    reposts = [info_item["count"] < DUPLICATE_BODY_LIMIT for info_item in info]
    if any(reposts):
        if same_title:
            keep_index = titles.index(keep_title)
            return [index == keep_index for index in range(len(titles))]
        keep = [not repost for repost in reposts]
        return keep if any(keep) else [index == 0 for index in range(len(titles))]
    if all(relation in (SAME, CONTAINS, CONTAINED) for relation in relations[1:]):
        longest = max(item["count"] for item in info)
        best = max(index for index, item in enumerate(info) if item["count"] * SIMILAR_LENGTH >= longest)
        return [index == best for index in range(len(info))]
    return [True] * len(info)


def _make_group(lines, rows, parsed, chapter_ends) -> dict:
    titles = [parsed[row][0] for row in rows]
    stripped = [_strip_note(title) for title in titles]
    same_title = len({re.sub(r"\s+", "", base) for base, _noted in stripped}) == 1
    keep_title = titles[0]
    if same_title and stripped[0][1]:
        keep_title = next((title for title, (_base, noted) in zip(titles, stripped) if not noted), titles[0])
    between = [sum(1 for row in range(first + 1, second) if lines[row].strip())
               for first, second in zip(rows, rows[1:])]
    info = []
    for row, end in zip(rows, chapter_ends):
        body = lines[row + 1:end]
        info.append({"count": sum(char_count(line) for line in body), "sentences": _sentences(body)})
    relations = [_relation(info[index - 1] if index else None, item) for index, item in enumerate(info)]
    return {"rows": list(rows), "titles": titles, "keep_title": keep_title,
            "same_title": same_title, "between": between, "ends": list(chapter_ends),
            "counts": [item["count"] for item in info], "relations": relations,
            "default_keep": _default_keep(titles, keep_title, same_title, info, relations), "adjacent": True}


_BLANKS = " \t　\u00a0\r\f\v\n"     # char_count 不算的字，加上換行
SIMILAR_RATIO = 0.6       # 不相鄰的兩章：較短那份有六成的句子在另一份裡才列出來
COMMON_SENTENCE = 20      # 出現在超過這麼多章的句子（固定的開場白、分隔語）不拿來比
SIMILAR_TITLE_LENGTH = 12


def find_similar_groups(lines, title_rows, adjacent_groups=()) -> list:
    """不相鄰、內容重複的章（同一章換了標題又貼了一次）：每一對一組，格式跟 find_duplicate_groups 一樣，
    另外 adjacent＝False、notes＝內容比對欄要顯示的字、default_keep 全部保留（比相鄰的保守）。

    每個句子（10 字以上）記在哪幾章出現，只比對真的有共同句子的章，不用每一章對每一章比。
    已經在相鄰重複裡的兩章不重複列。正文不到 DUPLICATE_BODY_LIMIT 字的章不比。"""
    rows = sorted(row for row in set(title_rows) if 0 <= row < len(lines))
    chapters = []
    for index, row in enumerate(rows):
        clean, marker, parsed = _parse_title(lines[row])
        if marker == "exclude" or not clean:
            continue
        end = rows[index + 1] if index + 1 < len(rows) else len(lines)
        # 整章接起來、用 str.count 算空白（逐行 char_count／translate 在十幾萬行的書上要多花 0.3 秒）
        text = "\n".join(lines[row + 1:end])
        count = len(text) - sum(text.count(char) for char in _BLANKS)
        if count < DUPLICATE_BODY_LIMIT:
            continue
        chapters.append({"row": row, "end": end, "title": clean, "count": count,
                         "sentences": _text_sentences(text)})
    where: dict = {}
    for position, chapter in enumerate(chapters):
        for sentence in chapter["sentences"]:
            where.setdefault(sentence, []).append(position)
    shared: dict = {}
    for positions in where.values():
        if 1 < len(positions) <= COMMON_SENTENCE:
            for first_index, first in enumerate(positions):
                for second in positions[first_index + 1:]:
                    shared[(first, second)] = shared.get((first, second), 0) + 1
    adjacent = {frozenset(pair) for group in adjacent_groups
                for pair in zip(group["rows"], group["rows"][1:])}
    # 互相重複的章併成一組（同一段內容貼了三、四次時是一組，不是兩兩一組）
    parent = {}

    def root(position):
        while parent.get(position, position) != position:
            position = parent[position]
        return position

    ratios = {}
    for (first, second), common in shared.items():
        a, b = chapters[first], chapters[second]
        ratio = common / min(len(a["sentences"]), len(b["sentences"]))
        if ratio < SIMILAR_RATIO or second == first + 1 or frozenset((a["row"], b["row"])) in adjacent:
            continue
        ratios[(first, second)] = ratio
        parent[root(second)] = root(first)
    clusters: dict = {}
    for first, second in ratios:
        for position in (first, second):
            clusters.setdefault(root(position), set()).add(position)
    groups = []
    for members in sorted(sorted(cluster) for cluster in clusters.values()):
        head = chapters[members[0]]
        label = head["title"] if len(head["title"]) <= SIMILAR_TITLE_LENGTH else head["title"][:SIMILAR_TITLE_LENGTH] + "…"
        notes = [""]
        for position in members[1:]:
            ratio = ratios.get((members[0], position))
            notes.append(f"與{label} {round(ratio * 100)}% 相同" if ratio else f"與{label}內容重複")
        picked = [chapters[position] for position in members]
        groups.append({"rows": [chapter["row"] for chapter in picked], "titles": [chapter["title"] for chapter in picked],
                       "keep_title": head["title"], "same_title": False, "between": [],
                       "ends": [chapter["end"] for chapter in picked], "counts": [chapter["count"] for chapter in picked],
                       "relations": [""] * len(picked), "notes": notes,
                       "adjacent": False, "default_keep": [True] * len(picked)})
    return groups


def merge_duplicate_groups(lines, groups) -> list:
    """把每一組合併成一章：第一個標題改成 keep_title，其餘標題行刪掉。
    刪掉的標題前後都是空行時，順便拿掉一個空行，不留下連續兩個空行。"""
    result = list(lines)
    replace = {}
    delete = set()
    for group in groups:
        first = group["rows"][0]
        original = result[first]
        # a marker on any of the merged headings ([::] on the second copy) stays on the kept one: it is the
        # user's "this is a heading", which must not disappear with the deleted line
        markers = [_parse_title(result[row])[1] for row in group["rows"]]
        marker = next((kind for kind in markers if kind in _MARKER_TEXT), "")
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


def apply_keep_choices(lines, choices) -> list:
    """choices：[(group, keep)]，keep 是跟 group["rows"] 對齊的 bool。一組全部保留或全部不保留：不動。

    組裡有重貼標題：沒保留的只刪標題行，正文全部併在一起（跟 merge_duplicate_groups 一樣，
    標題在第一個標題的位置、文字用留下的那個）。沒有重貼標題（每章都有正文）：
    沒保留的章標題連正文整章刪除。"""
    result = list(lines)
    replace = {}
    delete = set()
    for group, keep in choices:
        if all(keep) or not any(keep):
            continue
        rows = group["rows"]
        if any(count < DUPLICATE_BODY_LIMIT for count in group["counts"]):
            dropped = {row for row, kept in zip(rows, keep) if not kept}
            if rows[0] in dropped:
                first_kept = next(row for row, kept in zip(rows, keep) if kept)
                replace[rows[0]] = result[first_kept]
                dropped = (dropped - {rows[0]}) | {first_kept}
            delete.update(dropped)
        else:
            for row, end, kept in zip(rows, group["ends"], keep):
                if not kept:
                    delete.update(range(row, end))
    for row, text in replace.items():
        result[row] = text
    for row in sorted(delete, reverse=True):
        del result[row]
        if 0 < row < len(result) and not result[row].strip() and not result[row - 1].strip():
            del result[row]
    return result
