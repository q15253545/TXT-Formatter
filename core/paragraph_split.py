"""排版的「長段落」：把一行擠了好幾百字的段落，在句末拆成幾段。

只在引號、括號外面的句末拆（。！？!? 後面可以跟著右引號、右括號），每段至少 MIN_PIECE 字，
盡量拆得一樣長；拆出來的段落沿用原本的段首縮排。

每一段分高、中兩種信心，排版設定的「長段落」選要拆到哪一種：
- 高：超過 HIGH_LENGTH 字，引號、括號都成對，在句號、問號、驚嘆號後面拆得開。
- 中：LOW_LENGTH～HIGH_LENGTH 字；或引號、括號沒成對（引號外面的判斷不可靠）；
  或只能拆在刪節號後面。
- 不到 LOW_LENGTH 字不拆。
"""

import math
import re

from .reflow import QUOTE_PAIRS
from .word_count import char_count

SPLIT_OFF = "不拆分"
SPLIT_HIGH = "只拆高信心"
SPLIT_MEDIUM = "拆高、中信心"
SPLIT_CHOICES = (SPLIT_OFF, SPLIT_HIGH, SPLIT_MEDIUM)

LOW_LENGTH = 300
HIGH_LENGTH = 500
MIN_PIECE = 80
TARGET_PIECE = 300

HIGH, MEDIUM = "high", "medium"

_STRONG_ENDS = set("。！？!?")
_CLOSERS = set(QUOTE_PAIRS.values()) | set("”’")
_OPENERS = set(QUOTE_PAIRS)
_INDENT_CHARS = " \t　"
# 引號、括號各自一個；連在一起的句末標點（「！？」「……」）一次取整串
_MARKS = re.compile("(?P<open>[" + re.escape("".join(_OPENERS)) + "])|(?P<close>["
                    + re.escape("".join(_CLOSERS)) + "])|(?P<end>[。！？!?…]+)")


def _split_points(text: str):
    """回傳（強句末拆點, 刪節號拆點, 引號括號是否成對）。拆點是「下一段開頭」的位置。

    只看引號、括號、句末標點這幾種字（_MARKS 找出來），一般的字直接跳過：長段落逐字走會太慢。"""
    strong, weak = [], []
    # 照開的順序記著該用哪個符號關：「…）這種種類不對的也當成關掉（不然後面整段都算在引號裡、一個拆點都沒有），
    # 只是不算成對
    expected = []
    balanced = True
    total = len(text)
    skip_to = 0

    def close(char):
        nonlocal balanced
        if not expected:
            balanced = False
            return
        if expected.pop() != char:
            balanced = False

    for match in _MARKS.finditer(text):
        index = match.start()
        if index < skip_to:
            continue
        kind = match.lastgroup
        if kind == "open":
            expected.append(QUOTE_PAIRS[match.group()])
        elif kind == "close":
            close(match.group())
        else:
            end = match.end()
            # 句末後面緊跟的右引號、右括號算在這一句裡
            while end < total and expected and text[end] in _CLOSERS:
                close(text[end])
                end += 1
            if not expected and end < total:
                # 整串都是刪節號才算弱拆點（「……？」是問句）
                (weak if not match.group().strip("…") else strong).append(end)
            skip_to = end
    if expected:
        balanced = False
    return strong, weak, balanced


def _choose(points, total: int) -> list:
    """從拆點裡挑幾個，拆成大約 TARGET_PIECE 字一段、每段至少 MIN_PIECE 字。"""
    pieces = max(2, math.ceil(total / TARGET_PIECE))
    chosen = []
    previous = 0
    for part in range(1, pieces):
        ideal = total * part / pieces
        usable = [point for point in points if point - previous >= MIN_PIECE and total - point >= MIN_PIECE]
        if not usable:
            break
        best = min(usable, key=lambda point: abs(point - ideal))
        if best <= previous:
            continue
        chosen.append(best)
        previous = best
    return chosen


def plan_split(line: str):
    """回傳（信心, 拆開後的幾段）；不拆就回傳（None, [line]）。"""
    stripped = line.lstrip(_INDENT_CHARS)
    indent = line[:len(line) - len(stripped)]
    text = stripped.rstrip()
    length = char_count(text)
    if length < LOW_LENGTH:
        return None, [line]
    strong, weak, balanced = _split_points(text)
    # 拆點位置用字元位置；空白很少，直接用 len 當總長
    cuts = _choose(strong, len(text))
    confidence = HIGH if (length >= HIGH_LENGTH and balanced and cuts) else MEDIUM
    if not cuts:
        cuts = _choose(sorted(strong + weak), len(text))
    if not cuts:
        return None, [line]
    bounds = [0] + cuts + [len(text)]
    pieces = [indent + text[start:end].strip(_INDENT_CHARS) for start, end in zip(bounds, bounds[1:])]
    return confidence, [piece for piece in pieces if piece.strip()]


def split_long_paragraphs(lines: list, protected_rows, level: str) -> tuple:
    """排版的「長段落」。protected_rows（章節標題…）不拆。

    回傳（新的行, 舊行號 → 新行號）：拆開的行對到拆出來的第一段。level 不是「只拆高信心」
    「拆高、中信心」就原樣回傳。"""
    allowed = {SPLIT_HIGH: {HIGH}, SPLIT_MEDIUM: {HIGH, MEDIUM}}.get(level)
    protected = set(protected_rows)
    new_lines: list = []
    row_map: dict = {}
    for row, line in enumerate(lines):
        row_map[row] = len(new_lines)
        if allowed and row not in protected and len(line) >= LOW_LENGTH:
            confidence, pieces = plan_split(line)
            if confidence in allowed:
                new_lines.extend(pieces)
                continue
        new_lines.append(line)
    return new_lines, row_map
