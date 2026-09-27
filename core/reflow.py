"""章節硬換行／異常空白整理。

規劃階段（_plan_body_edits）只產生 TextEdit 清單，不觸碰任何文字元件；
套用編輯、檢查游標/選取範圍是呼叫端（UI 層）的責任。

兩種編輯共用同一份原始文字座標：
  - linebreak：把判定為固定欄寬硬換行的段落接回同一行。
  - space    ：清理段落內容中間的異常空白（不動段首縮排）。
兩者互不重疊，_plan_body_edits 會在規劃階段確保這一點。
"""

import re
import unicodedata
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class TextEdit:
    start: int
    end: int
    before: str
    after: str
    kind: str = "linebreak"    # "linebreak" 或 "space"，僅供狀態列摘要使用


@dataclass(frozen=True)
class PhysicalLine:
    start: int
    end: int
    text: str
    newline: str


# 寧可漏合併，也不將標題、清單或中繼資料併入正文；空白整理沿用同一份保護清單。
PROTECTED_LINE = re.compile(
    r"^\s*(?:"
    r"第\s*[0-9０-９零〇一二兩两三四五六七八九十百千萬万億亿兆]+\s*"
    r"[章回節节卷集部篇](?:\s|[：:、.．]|$)"
    r"|(?:作者|書名|书名|來源|来源)\s*[：:]"
    r"|(?:序章|序言|前言|楔子|引子|後記|后记|尾聲|尾声|終章|终章)"
    r"(?:\s|[：:]|$)"
    r"|[-*•]\s+"
    r"|[（(]?[0-9０-９]+[)）.．、]\s*"
    r"|\|"
    r")"
)

PARAGRAPH_INDENT = re.compile(r"^(?:　|[ \t]*\t| {2,})")
# 含成對對話引號、或以句末標點結尾的行，幾乎必定是正文而非標題。
DIALOGUE_OR_SENTENCE_REGEX = re.compile(r"[「『“\"].*[」』”\"]|[。！？!?…]\s*$")
TERMINAL = re.compile(r"""[。！？!?…]["'”’」』）》】]*$""")
ONLY_CLOSERS = re.compile(r"""[」』）)》】”’]+[。！？!?，、；;]*""")
CJK_CHAR = re.compile(r"[㐀-鿿豈-﫿々〇]")
SPACE_RUN = re.compile(r"[ \t　 ]+")

QUOTE_PAIRS = {
    "「": "」", "『": "』", "（": "）", "(": ")",
    "《": "》", "【": "】", "“": "”", "‘": "’",
}


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        if char == "\t":
            width += 4 - width % 4
        else:
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _physical_lines(text: str) -> list[PhysicalLine]:
    result = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        if raw.endswith("\r\n"):
            body, newline = raw[:-2], "\r\n"
        elif raw.endswith(("\n", "\r")):
            body, newline = raw[:-1], raw[-1]
        else:
            body, newline = raw, ""
        result.append(PhysicalLine(offset, offset + len(raw), body, newline))
        offset += len(raw)
    return result


def _extend_pairs(stack: list[str], text: str) -> list[str]:
    """就地更新未閉合引號堆疊，供逐行累積使用。"""
    closing = set(QUOTE_PAIRS.values())
    for char in text:
        if char in QUOTE_PAIRS:
            stack.append(QUOTE_PAIRS[char])
        elif char in closing and stack and char == stack[-1]:
            stack.pop()
    return stack


def _closers_match_stack(stack: list[str], following: str) -> bool:
    """只接回整行閉合符號，且前文必須有對應的未閉合符號。

    收的是「已累積的引號堆疊」而不是整段文字，避免每合併一行就重新掃描整段。
    """
    if not ONLY_CLOSERS.fullmatch(following):
        return False
    stack = list(stack)
    closing = set(QUOTE_PAIRS.values())
    matched = False
    for char in following:
        if char not in closing:
            continue
        if not stack or stack[-1] != char:
            return False
        stack.pop()
        matched = True
    return matched


def _local_wrap_width(lines: list[PhysicalLine], index: int, width_cache=None) -> float | None:
    """僅在鄰近區域尋找重複行寬，不套用全書單一欄寬。

    width_cache 由呼叫端提供（每行算一次就好）；這個函式會被每一行呼叫，
    而每次又要看前後各 10 行，不快取的話同一行的寬度會被重算約 21 次。
    """
    def width_of(position, line):
        if width_cache is None:
            return _display_width(line.text.strip())
        cached = width_cache.get(position)
        if cached is None:
            cached = _display_width(line.text.strip())
            width_cache[position] = cached
        return cached

    widths: list[tuple[int, bool]] = []
    low = max(0, index - 10)
    for offset, line in enumerate(lines[low:index + 11]):
        body = line.text.strip()
        if not body or PROTECTED_LINE.match(body):
            continue
        width = width_of(low + offset, line)
        if 24 <= width <= 180:
            widths.append((width, bool(TERMINAL.search(body))))
    target = width_of(index, lines[index])
    tolerance = max(4, target * 0.08)
    cluster = [(w, t) for w, t in widths if abs(w - target) <= tolerance]
    if len(cluster) < 3:
        return None
    if sum(not terminal for _, terminal in cluster) < 2:
        return None
    return median(w for w, _ in cluster)


def _join_separator(left: str, right: str) -> str | None:
    """中文直接接續；無法確定英文是斷詞或詞間換行時保留。"""
    if not left or not right:
        return None
    if (left[-1].isascii() and left[-1].isalnum()
            and right[0].isascii() and right[0].isalnum()):
        return None
    return ""


def _should_join(lines: list[PhysicalLine], index: int, paragraph_stack: list[str],
                 paragraph_indented: bool, width_cache=None) -> str | None:
    current, following = lines[index], lines[index + 1]
    left, right = current.text.rstrip(), following.text.strip()
    if not current.newline or not left or not right:
        return None
    if PROTECTED_LINE.match(left) or PROTECTED_LINE.match(right):
        return None
    if PARAGRAPH_INDENT.match(following.text):
        return None
    if _closers_match_stack(paragraph_stack, right):
        return ""
    if TERMINAL.search(left):
        return None
    width = _display_width(left.strip())
    if not 24 <= width <= 180:
        return None
    wrap_width = _local_wrap_width(lines, index, width_cache)
    has_width_evidence = (wrap_width is not None
                           and abs(width - wrap_width) <= max(4, wrap_width * 0.08))
    has_indent_evidence = (paragraph_indented
                            and not PARAGRAPH_INDENT.match(following.text))
    if not (has_width_evidence or has_indent_evidence):
        return None
    return _join_separator(left, right)


def _leading_indent_length(text: str) -> int:
    """量測段首縮排的字元數：連續全形空白，或 Tab／兩個以上半形空白。

    不可直接沿用 PARAGRAPH_INDENT.match().end()——那個正則只用來做布林判斷，
    第一個分支 "\\u3000" 沒有量詞，遇到「兩個全形空白」的標準縮排只會算出 1，
    導致第二個縮排字元被誤判為「段落中間的空白」而被空白整理動到。
    """
    index = 0
    while index < len(text) and text[index] == "　":
        index += 1
    if index:
        return index
    match = re.match(r"^(?:[ \t]*\t| {2,})", text)
    return match.end() if match else 0


def _normalize_space_run(run: str, prev_char: str, next_char: str, at_line_end: bool) -> str | None:
    """判斷一段空白是否異常；回傳 None 表示視為正常，不產生編輯。"""
    if at_line_end:
        return ""
    prev_is_cjk = bool(prev_char) and bool(CJK_CHAR.match(prev_char))
    next_is_cjk = bool(next_char) and bool(CJK_CHAR.match(next_char))
    if prev_is_cjk and next_is_cjk:
        return ""                                  # 中文字之間不應該有空白
    if len(run) > 1 or "　" in run:
        return " "                                 # 連續空白或誤植的全形空白，收斂為一個半形空白
    return None                                     # 單一半形空白、未夾在兩個中文字之間，視為中英夾雜的正常間距


def collapse_inline_spaces(text: str) -> str:
    """段落中間的空白：兩邊都是中文字就刪掉，其餘收斂成一個半形空格。

    「他 說 了 一句 話。」→「他說了一句話。」
    「我用 Notepad 排版 的 檔案。」→「我用 Notepad 排版的檔案。」

    中英數之間的空格是有意義的排版，不能一起刪掉。規則跟「整理換行與空白」
    用的 _normalize_space_run 一致，不另外發明一套。

    這個函式只處理「段落中間」：呼叫端要先把段首縮排切出來，否則全形縮排
    會被當成中文字之間的空白吃掉。
    """
    def replace(match):
        start, end = match.span()
        previous = text[start - 1] if start else ""
        following = text[end] if end < len(text) else ""
        if previous and following and CJK_CHAR.match(previous) and CJK_CHAR.match(following):
            return ""
        return " "
    return SPACE_RUN.sub(replace, text)


def _plan_line_space_edits(line: PhysicalLine, base_offset: int, skip_trailing: bool) -> list[TextEdit]:
    """規劃單一實體行內的空白編輯；段首縮排一律略過，交給既有的縮排選項處理。"""
    text = line.text
    stripped = text.strip()
    if not stripped or PROTECTED_LINE.match(stripped):
        return []
    indent_len = _leading_indent_length(text)
    edits: list[TextEdit] = []
    for match in SPACE_RUN.finditer(text, indent_len):
        start, end = match.start(), match.end()
        at_line_end = end == len(text)
        if at_line_end and skip_trailing:
            continue        # 行尾空白已由換行編輯（合併到下一行）一併處理，避免重疊
        prev_char = text[start - 1] if start > 0 else ""
        next_char = text[end] if end < len(text) else ""
        after = _normalize_space_run(match.group(), prev_char, next_char, at_line_end)
        if after is None or after == match.group():
            continue
        edits.append(TextEdit(
            start=base_offset + line.start + start,
            end=base_offset + line.start + end,
            before=match.group(), after=after, kind="space",
        ))
    return edits


def _plan_body_edits(body: str, base_offset: int, check_spaces: bool = True) -> list[TextEdit]:
    """規劃選取範圍內的編輯：合併不當的硬換行，並視需要清理段落中的異常空白。"""
    lines = _physical_lines(body)
    linebreak_edits: list[TextEdit] = []
    joined_indices: set[int] = set()
    width_cache: dict[int, int] = {}
    # 只保留判斷需要的狀態（未閉合引號堆疊、段首是否縮排），不累積整段文字，
    # 否則由大量續行構成的長段落會產生平方級的字串複製。
    paragraph_stack: list[str] = []
    paragraph_indented = False
    paragraph_open = False

    for index, line in enumerate(lines):
        if not paragraph_open:
            paragraph_stack = _extend_pairs([], line.text)
            paragraph_indented = PARAGRAPH_INDENT.match(line.text) is not None
            paragraph_open = True
        if index + 1 >= len(lines):
            break
        separator = _should_join(lines, index, paragraph_stack, paragraph_indented, width_cache)
        if separator is None:
            paragraph_open = False
            continue
        following = lines[index + 1]
        left_trimmed = line.text.rstrip(" \t")
        right_trimmed = following.text.lstrip(" \t")
        start = line.start + len(left_trimmed)
        end = following.start + len(following.text) - len(right_trimmed)
        linebreak_edits.append(TextEdit(
            start=base_offset + start, end=base_offset + end,
            before=body[start:end], after=separator, kind="linebreak",
        ))
        joined_indices.add(index)
        _extend_pairs(paragraph_stack, separator + right_trimmed)

    space_edits: list[TextEdit] = []
    if check_spaces:
        for index, line in enumerate(lines):
            space_edits.extend(
                _plan_line_space_edits(line, base_offset, skip_trailing=index in joined_indices)
            )

    return sorted(linebreak_edits + space_edits, key=lambda edit: edit.start)


def reflow_lines(lines: list, protected_rows) -> tuple:
    """排版的「整理段落換行」：把正文裡固定欄寬的硬換行接回同一段。

    protected_rows（章節標題、被標成非章節的行…）不動，也不會跟前後接在一起；
    兩個保護行之間的正文各自整理。回傳（新的行, 舊行號 → 新行號）：被接到上一行的
    舊行對到它接進去的那一行，章節標題的行號靠這份對照搬過去。"""
    from bisect import bisect_right

    protected = set(protected_rows)
    new_lines: list = []
    row_map: dict = {}
    index, total = 0, len(lines)
    while index < total:
        if index in protected:
            row_map[index] = len(new_lines)
            new_lines.append(lines[index])
            index += 1
            continue
        end = index
        while end < total and end not in protected:
            end += 1
        text = "\n".join(lines[index:end])
        edits = [edit for edit in _plan_body_edits(text, 0, check_spaces=False) if edit.kind == "linebreak"]
        # 每個編輯吃掉的換行位置；第 k 行前面被吃掉幾個換行，新行號就往前挪幾行
        removed = sorted(edit.start + offset for edit in edits
                         for offset, char in enumerate(edit.before) if char == "\n")
        base = len(new_lines)
        for offset, start in enumerate(_line_starts(text)):
            row_map[index + offset] = base + offset - bisect_right(removed, start - 1)
        # 編輯照位置排好、不重疊：一次接起來，不要每改一處就複製整段（長段落會變成平方時間）
        parts, cursor = [], 0
        for edit in sorted(edits, key=lambda edit: edit.start):
            parts.append(text[cursor:edit.start])
            parts.append(edit.after)
            cursor = edit.end
        parts.append(text[cursor:])
        new_lines.extend("".join(parts).split("\n"))
        index = end
    return new_lines, row_map


def _line_starts(text: str) -> list[int]:
    return [0] + [match.end() for match in re.finditer("\n", text)]
