"""標點校對（引號、行首標點、分隔線、重複標點、破折號與波浪號），以及有明確答案時的自動修正。

參考網路上整理的純文字小說排版步驟（第 5～8 步）。檢查的部分只負責
標出可疑的地方；自動修正只處理「正確寫法只有一種」的情況（見 plan_fix），
看不出來的——例如一行裡引號亂成一團——留給使用者自己改。

參考資料附的正則在網頁上被吃掉了反斜線（`n` 其實是換行），不能直接照抄，判斷邏輯
是自己寫的；引號配對沿用 core/reflow.py 的 QUOTE_PAIRS。
"""

import re
from functools import lru_cache

from .reflow import QUOTE_PAIRS

QUOTE_PROBLEM_LABELS = {
    "unclosed": "對話中途斷行",
    "unpaired": "引號沒有成對",
    "leading_punct": "標點在行首",
    "missing_separator": "兩段對話黏在一起",
    "separator_line": "分隔線太長",
    "repeated_punct": "重複標點",
    "dash_run": "破折號、波浪號",
    "separator_style": "分隔線不一致",
}

# 從網頁轉存時沒被轉回來的字元碼：&#29368;、&#x72B8;、&nbsp;、&amp;……
# 
_HTML_ENTITY = re.compile(r"&#[0-9]{2,7};|&#[xX][0-9a-fA-F]{2,6};|&(?:nbsp|amp|lt|gt|quot|apos|hellip|mdash|ldquo|rdquo);")


def _decode_entities(text: str) -> str:
    import html
    return _HTML_ENTITY.sub(lambda match: html.unescape(match.group(0)).replace("\xa0", " "), text)

# ---------------------------------------------------------------- 分隔線、重複標點
# 整行都是同一個符號、超過三個：保留原本的符號，縮成三個（-------- → ---）。
# 句號、刪節號、半形點不算分隔線，由「重複標點」改成刪節號。
_SEPARATOR_CHARS = frozenset("-－—–―─━=＝*＊#＃~～_＿※☆★◆◇●○■□+＋")
SEPARATOR_LENGTH = 3

# 中文字與中文標點：半形點「...」前後是這些才改成刪節號，網址、英文裡的不動。
_CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0003134f"
                       r"，。！？：；、「」『』“”‘’（）《》〈〉【】—…～]")
_PERIOD_RUN = re.compile(r"。{3,}")            # 。。。 → ……
_ELLIPSIS_RUN = re.compile(r"…{3,}")           # ………… → ……
_DOT_RUN = re.compile(r"\.{3,}")                # ... 或 ......... → ……（前後的半形空格見 _dot_runs）
# 中間夾空白的刪節號（「… …」「…… ……」「... ...」「. . .」）是同一個刪節號：先把空白拿掉再整理
_SPACED_ELLIPSIS = re.compile(r"…+(?:[ 　]+…+)+")
_SPACED_DOT_RUNS = re.compile(r"\.{3,}(?:[ 　]+\.{3,})+|(?<![.A-Za-z0-9])\.(?:[ 　]\.){2,}(?![.A-Za-z0-9])")
ELLIPSIS = "……"


def separator_char(text: str):
    """整行都是同一個分隔符號（至少三個）時回傳那個符號，否則 None。"""
    text = text.strip()
    if len(text) < SEPARATOR_LENGTH or text[0] not in _SEPARATOR_CHARS or text.count(text[0]) != len(text):
        return None
    return text[0]


_SEPARATOR_LINE = re.compile(r"^\s*([" + re.escape("".join(sorted(_SEPARATOR_CHARS))) + r"])\1{"
                             + str(SEPARATOR_LENGTH - 1) + r",}\s*$")


def separator_styles(lines, line_ranges=None, title_rows=None) -> dict:
    """這本書（或選取的章節）用了哪些分隔線：{符號: 出現幾行}，多的在前。
    給「分隔線不一致」的下拉選單列出可以統一成哪一種。"""
    from collections import Counter
    if title_rows is None:
        title_rows = {row for row, line in enumerate(lines) if _is_title_row(line)}
    counts = Counter()
    for start, end in _text_segments(len(lines), line_ranges, set(title_rows)):
        # 整本每一行都要看：用 map 讓正則在 C 裡跑（條件跟 separator_char 一樣）
        counts.update(match.group(1) for match in map(_SEPARATOR_LINE.match, lines[start:end]) if match)
    return dict(counts.most_common())


# ---------------------------------------------------------------- 破折號、波浪號
# 段落裡的長串符號改成標準寫法：破折號兩格「——」，波浪號一個全形「～」。
# 整行都是符號的是分隔線（另外處理）；行首就是一長串的通常是裝飾（「————分割線————」），不動。
DASH = "——"
TILDE = "～"
_DASH_CHARS = "—―─━–"
_DASH_RUN = re.compile(r"[—―─━–\-－]{2,}")
_TILDE_RUN = re.compile(r"[~～]{2,}")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def _next_to_chinese(text: str, start: int, end: int) -> bool:
    """這一串符號旁邊（至少一邊）是中文，另一邊不是英數：英文、網址、日期裡的 -- 不動。"""
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if (before and _ASCII_WORD.match(before)) or (after and _ASCII_WORD.match(after)):
        return False
    return bool((before and _CJK_CHAR.fullmatch(before)) or (after and _CJK_CHAR.fullmatch(after)))


def _normalize_dashes(text: str) -> str:
    stripped = text.strip()
    if not stripped or separator_char(stripped) is not None:
        return text
    if (_DASH_RUN.match(stripped) and len(_DASH_RUN.match(stripped).group(0)) >= 3) or \
            (_TILDE_RUN.match(stripped) and len(_TILDE_RUN.match(stripped).group(0)) >= 3):
        return text             # 行首一長串：裝飾或分隔，不是句子裡的破折號

    def dash(match):
        run = match.group(0)
        if run == DASH:
            return run
        has_dash = any(char in _DASH_CHARS for char in run)
        if not has_dash and not _next_to_chinese(text, match.start(), match.end()):
            return run          # 純半形／全形減號：只有夾在中文裡才算破折號
        if has_dash and len(run) == 2 and not _next_to_chinese(text, match.start(), match.end()):
            return run
        return DASH

    def tilde(match):
        return TILDE if _next_to_chinese(text, match.start(), match.end()) else match.group(0)

    return _TILDE_RUN.sub(tilde, _DASH_RUN.sub(dash, text))


def _separator_fix(text: str):
    """整行是同一個分隔符號、超過三個時，回傳縮短後的樣子；否則 None。"""
    text = text.rstrip()
    if len(text) <= SEPARATOR_LENGTH or text[0] not in _SEPARATOR_CHARS or text.count(text[0]) != len(text):
        return None
    return text[0] * SEPARATOR_LENGTH


def _dots_in_chinese(text: str, start: int, end: int) -> bool:
    """text[start:end]（一串半形點，含前後空格）是不是在中文裡：前後（跳過空格）是中文字／中文標點，
    另一邊是中文或行首行尾。英文、網址裡的不算（Wait... / http://a...）。"""
    left = start - 1
    while left >= 0 and text[left] == " ":
        left -= 1
    right = end
    while right < len(text) and text[right] == " ":
        right += 1
    before = text[left] if left >= 0 else ""
    after = text[right] if right < len(text) else ""
    left_chinese = bool(before) and bool(_CJK_CHAR.fullmatch(before))
    right_chinese = bool(after) and bool(_CJK_CHAR.fullmatch(after))
    return (not before or left_chinese) and (not after or right_chinese) and (left_chinese or right_chinese)


def _dot_runs(text: str):
    """「 *\.{3,} *」每一段的範圍（前後的半形空格一起算），跟 re.finditer 的結果一樣。
    先找點再往兩邊吃空格：直接用那條正則，一行有幾萬個空格時會一格一格重試、變成平方時間。"""
    last = 0
    for match in _DOT_RUN.finditer(text):
        start, end = match.start(), match.end()
        if start < last:
            continue
        while start > last and text[start - 1] == " ":
            start -= 1
        while end < len(text) and text[end] == " ":
            end += 1
        yield start, end
        last = end


def _normalize_repeated(text: str) -> str:
    """連續三個以上的句號、太長的刪節號、中文裡的半形點都換成標準的「……」。"""
    if "… " in text or "…　" in text:
        text = _SPACED_ELLIPSIS.sub(ELLIPSIS, text)
    if ". ." in text or ".　." in text:
        # 只有中文裡的才併起來（英文的「Wait. . . what」照舊）
        text = _SPACED_DOT_RUNS.sub(
            lambda match: (match.group().replace(" ", "").replace("　", "")
                           if _dots_in_chinese(match.string, match.start(), match.end()) else match.group()), text)
    text = _PERIOD_RUN.sub(ELLIPSIS, text)
    text = _ELLIPSIS_RUN.sub(ELLIPSIS, text)
    if "..." in text:
        only_dots = not text.strip(" .")          # 整行只有點：一定是刪節號
        parts, last = [], 0
        for start, end in _dot_runs(text):
            if only_dots or _dots_in_chinese(text, start, end):
                parts.append(text[last:start])
                parts.append(ELLIPSIS)
                last = end
        parts.append(text[last:])
        text = "".join(parts)
    return text


def _starts_with_ellipsis(text: str) -> bool:
    """行首的「。。。」「...」其實是刪節號（會被改成……），不是被截斷的標點。"""
    return text.startswith("。。。") or text.startswith("...")

# 只檢查對話用的引號。括號、書名號常常本來就會跨行（例如條列、註解），
# 一起檢查會蓋出一堆雜訊，反而看不到真正的問題。
DIALOGUE_PAIRS = {opener: closer for opener, closer in QUOTE_PAIRS.items()
                  if opener in "「『“‘"}
_DIALOGUE_CLOSERS = set(DIALOGUE_PAIRS.values())

# 不該出現在行首的標點。「、」「，」這類一定是上一行被截斷了；
# 引號、破折號、刪節號則是正常的開頭，要排除。
LEADING_PUNCT = "，。！？：；、,.!?;"

# 參考步驟的第 8 步還要求「開引號前面是句號就算問題（該用冒號）」，但那是簡體
# 出版社的規範；中文小說裡「他愣住了。「走吧。」」完全正常，照做會掃出成千
# 上萬筆雜訊。這裡只留真正可疑的一種：上一句的收尾引號後面「直接」接下一句
# 的開引號，中間沒有任何分隔——那通常是兩個人的對話被黏在同一行。
#
# 但只有「前一段是完整的一句話」才算：收尾引號前面要是句末標點。
# 說是“天太冷”“路太遠”“家裡有事” 這種是並列的引用語，
# 依標點符號用法（GB/T 15834），加了引號的並列成分之間本來就不用頓號，
# 不是問題，也不能拆行。
SENTENCE_END = "。！？!?…～~—.」』”’"


def _strip_indent(line: str):
    """回傳（縮排, 內文）。段首的全形空格是排版，不是內容。"""
    stripped = line.lstrip(" \t　")
    return line[:len(line) - len(stripped)], stripped


def check_line(line: str) -> list:
    """單獨檢查一行，回傳這一行的問題種類（可能不只一個）。"""
    return list(_check_line(line))


@lru_cache(maxsize=1 << 19)
def _check_line(line: str) -> tuple:
    """只看這一行本身，照內容快取：改過本文重新檢查時，沒改到的行不用再算。"""
    problems = []
    _indent, text = _strip_indent(line)
    if not text:
        return ()

    if text[0] in LEADING_PUNCT and not _starts_with_ellipsis(text):
        problems.append("leading_punct")

    stack = []
    stray_closer = False
    for char in _QUOTE_CHAR.findall(text):
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if stack and stack[-1] == char:
                stack.pop()
            else:
                stray_closer = True
    if stray_closer:
        problems.append("unpaired")
    elif stack:
        # 有開沒關：整行到結束都沒有收尾，通常是對話被硬生生斷成兩行。
        problems.append("unclosed")

    if _stuck_dialogue_positions(text):
        problems.append("missing_separator")
    if _separator_fix(text) is not None:
        problems.append("separator_line")
    elif _normalize_repeated(text) != text:
        problems.append("repeated_punct")
    if _DASH_OR_TILDE.search(text) and _normalize_dashes(text) != text:
        problems.append("dash_run")
    return tuple(problems)


_QUOTE_CHAR = re.compile("[" + re.escape("".join(DIALOGUE_PAIRS) + "".join(DIALOGUE_PAIRS.values())) + "]")
_STUCK_QUOTES = re.compile("[" + re.escape("".join(DIALOGUE_PAIRS.values())) + "]["
                           + re.escape("".join(DIALOGUE_PAIRS)) + "]")
_DASH_OR_TILDE = re.compile(r"[-~—―─━–－～]")


def _stuck_dialogue_positions(text: str) -> list:
    """「」「」黏在一起、而且前一段是完整句子的位置（下一段開引號的索引）。"""
    if not _STUCK_QUOTES.search(text):
        return []
    positions = []
    for index in range(2, len(text)):
        if (text[index] in DIALOGUE_PAIRS and text[index - 1] in _DIALOGUE_CLOSERS
                and text[index - 2] in SENTENCE_END):
            positions.append(index)
    return positions


def _balanced(text: str) -> bool:
    """對話引號是否全部成對、順序正確。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return False
            stack.pop()
    return not stack


def _missing_closers(text: str) -> str:
    """有開沒收（而且沒有多出來的收尾引號）時，回傳要補的收尾引號（由內而外）；
    否則回傳空字串。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return ""
            stack.pop()
    return "".join(reversed(stack))


def _next_line_opens_quote(lines, row: int) -> bool:
    """下一段（跳過空行）是不是以開引號開頭。"""
    for next_row in range(row + 1, min(row + 4, len(lines))):
        text = _strip_indent(lines[next_row])[1]
        if text:
            return text[0] in DIALOGUE_PAIRS
    return False


def _has_unclosed_opener(text: str) -> bool:
    """有開引號沒收尾（而且沒有多出來的收尾引號）。"""
    stack = []
    for char in text:
        if char in DIALOGUE_PAIRS:
            stack.append(DIALOGUE_PAIRS[char])
        elif char in _DIALOGUE_CLOSERS:
            if not stack or stack[-1] != char:
                return False
            stack.pop()
    return bool(stack)


# 左右長得很像、常被打錯方向的引號：同一組裡依出現順序重新配對
# （第一個當開、第二個當收…）。半形的 " 也算在雙引號這組：轉檔常留下
# 「"真的？”」這種半形開、全形收的寫法。半形的 ' 不算，英文縮寫會用到。
_QUOTE_FAMILIES = (
    ("“", "”", frozenset("“”\"")),
    ("‘", "’", frozenset("‘’")),
)


def _realign_quotes(text: str):
    """把同一組引號依順序重新配成開、收、開、收…；配不起來或本來就對，回傳 None。

    例：「他回頭看了一眼。”你今天……早點回來。”」第二段的開頭用了右引號，
    重新配對後變成左引號；「"真的？”」的半形開引號換成全形。
    只在該組引號數量是雙數時處理——單數代表真的少了一個，不能用猜的。"""
    fixed = list(text)
    changed = False
    for opener, closer, family in _QUOTE_FAMILIES:
        positions = [index for index, char in enumerate(text) if char in family]
        if not positions or len(positions) % 2:
            continue
        for order, index in enumerate(positions):
            wanted = opener if order % 2 == 0 else closer
            if fixed[index] != wanted:
                fixed[index] = wanted
                changed = True
    result = "".join(fixed)
    return result if changed and _balanced(result) else None


def plan_fix(lines, row: int, kind: str):
    """這一個問題能不能自動修？能的話回傳 {"start", "end", "after"}：
    把 lines[start:end] 換成 after 這幾行；修不了回傳 None。

    能修的情況都只有一種正確寫法：
      兩段對話黏在一起  「你說什麼？」「走吧。」→ 拆成兩行（兩個人各一段）
      標點在行首        上一行被截斷，接回上一行
      對話中途斷行      下一行正好把引號收尾時，兩行接回一行
      引號方向顛倒      」你好「 → 「你好」；……一眼。”你今天 → ……一眼。“你今天
      半形全形混用      "真的？” → “真的？”
      少了收尾引號      ……笑著問：“今天，……要去哪？ → 行尾補上 ”
                        （下一段又是開引號開頭時不補：可能是跨段對話的寫法）
      少了開引號        好啊。」他回答。 → 「好啊。」他回答。
                        （上一行有沒收尾的開引號時，改成兩行接回一行）
      分隔線太長        ---------- → ---（保留原本的符號）
      重複標點          。。。。 → ……；………… → ……；他說...好吧 → 他說……好吧
      破折號、波浪號    蹄聲————— → 蹄聲——；嘎吱——- → 嘎吱——；啊~~~~ → 啊～
      分隔線不一致      ===（使用者選了統一成 ---）→ ---（見 scan_quote_problems 的 separator_target）
    """
    plan = _plan_fix(lines, row, kind)
    if plan is not None:
        plan["kind"], plan["row"] = kind, row
    return plan


def _plan_fix(lines, row: int, kind: str):
    line = lines[row]
    indent, text = _strip_indent(line)
    if not text:
        return None

    if kind == "separator_line":
        fixed = _separator_fix(text)
        return None if fixed is None else {"start": row, "end": row + 1, "after": [indent + fixed]}

    if kind == "repeated_punct":
        fixed = _normalize_repeated(text)
        return None if fixed == text else {"start": row, "end": row + 1, "after": [indent + fixed]}

    if kind == "dash_run":
        fixed = _normalize_dashes(text)
        return None if fixed == text else {"start": row, "end": row + 1, "after": [indent + fixed]}

    if kind == "missing_separator":
        parts, start = [], 0
        for index in _stuck_dialogue_positions(text):
            parts.append(text[start:index])
            start = index
        parts.append(text[start:])
        if len(parts) < 2:
            return None
        return {"start": row, "end": row + 1, "after": [indent + part for part in parts]}

    if kind == "leading_punct":
        if row == 0 or not lines[row - 1].strip():
            return None            # 前面是空行：分不出來是接哪一段，不猜
        return {"start": row - 1, "end": row + 1, "after": [lines[row - 1].rstrip() + text]}

    if kind == "unclosed":
        if row + 1 >= len(lines) or not lines[row + 1].strip():
            return None
        _next_indent, next_text = _strip_indent(lines[row + 1])
        if not _balanced(text + next_text):
            return None            # 下一行也收不齊：可能是作者讓對話跨段，不動
        return {"start": row, "end": row + 2, "after": [line.rstrip() + next_text]}

    if kind == "unpaired":
        realigned = _realign_quotes(text)
        if realigned is not None:
            return {"start": row, "end": row + 1, "after": [indent + realigned]}
        # 半形 " 還在、又配不起來：分不出它是開還是收，不猜。
        if '"' in text:
            return None
        closers = _missing_closers(text)
        if closers and text[-1] in SENTENCE_END and not _next_line_opens_quote(lines, row):
            # 開了沒收、而且整句已經講完（行尾是句末標點）：少打了收尾引號，
            # 補在行尾。下一段開頭又是開引號時，可能是「跨段對話每段只開不收」
            # 的寫法，不動。
            return {"start": row, "end": row + 1, "after": [indent + text + closers]}
        quotes = [(index, char) for index, char in enumerate(text)
                  if char in DIALOGUE_PAIRS or char in _DIALOGUE_CLOSERS]
        if len(quotes) == 2:
            (first, a), (second, b) = quotes
            if a in _DIALOGUE_CLOSERS and DIALOGUE_PAIRS.get(b) == a:
                fixed = text[:first] + b + text[first + 1:second] + a + text[second + 1:]
                return {"start": row, "end": row + 1, "after": [indent + fixed]}
        if len(quotes) == 1 and quotes[0][1] in _DIALOGUE_CLOSERS:
            closer = quotes[0][1]
            opener = next(o for o, c in DIALOGUE_PAIRS.items() if c == closer)
            if row > 0 and lines[row - 1].strip():
                _prev_indent, prev_text = _strip_indent(lines[row - 1])
                if _has_unclosed_opener(prev_text) and _balanced(prev_text + text):
                    return {"start": row - 1, "end": row + 1, "after": [lines[row - 1].rstrip() + text]}
            return {"start": row, "end": row + 1, "after": [indent + opener + text]}
        return None

    return None


# Fixes that only rewrite characters inside one line: two of them on the same line are independent, so the
# second one is re-planned on the line the first one produced instead of being dropped.
_LINE_LOCAL_KINDS = frozenset({"separator_line", "repeated_punct", "dash_run"})


def apply_fixes(lines, plans) -> tuple:
    """套用多個修正計畫，回傳（新的行, 實際套用了幾個）。

    兩個計畫改到同一行時只套用前面那一個（例如「對話中途斷行」與下一行的
    「少了開引號」其實是同一件事，接一次就好）。由後往前套，前面的行號才
    不會被改掉。只改一行裡的字的修正（重複標點、破折號…）例外：被擋下來的
    那個照套完之後的那一行重新算一次，再套上去。"""
    accepted, skipped, last_end = [], [], -1
    for plan in sorted(plans, key=lambda item: (item["start"], item["end"])):
        if plan["start"] < last_end:
            skipped.append(plan)
            continue
        accepted.append(plan)
        last_end = plan["end"]
    result = list(lines)
    for plan in reversed(accepted):
        result[plan["start"]:plan["end"]] = plan["after"]
    applied = len(accepted)

    def new_rows(row):
        """The lines an original row ended up in: one line, or every line of the block that replaced it
        (a line split into two dialogues, two lines joined into one)."""
        shift = 0
        for plan in accepted:
            if plan["end"] <= row:
                shift += len(plan["after"]) - (plan["end"] - plan["start"])
            elif plan["start"] <= row:
                return range(plan["start"] + shift, plan["start"] + shift + len(plan["after"]))
        return range(row + shift, row + shift + 1)

    for plan in skipped:
        if plan.get("kind") not in _LINE_LOCAL_KINDS or "row" not in plan:
            continue
        fixed_any = False
        for row in new_rows(plan["row"]):
            again = plan_fix(result, row, plan["kind"]) if 0 <= row < len(result) else None
            if again is not None:          # line-local: one line in, one line out, so later rows don't move
                result[again["start"]:again["end"]] = again["after"]
                fixed_any = True
        applied += fixed_any
    return result, applied


def _is_title_row(line: str) -> bool:
    """沒有傳入目錄的標題行時，自己判斷這一行像不像章節標題（含人工標記）。"""
    from .chapter_parse import parse_lv1, parse_lv2, parse_special
    from .title_markers import strip_persistent_title_marker
    clean, marker = strip_persistent_title_marker(line.strip())
    if not clean:
        return False
    if marker in ("include", "auto_work", "auto_title"):
        return True
    return marker != "exclude" and bool(parse_lv1(clean) or parse_lv2(clean) or parse_special(clean))


def _text_segments(total: int, line_ranges, title_rows) -> list:
    """可以修改的正文區段（半開區間）：檢查範圍再用章節標題切開，標題行本身不算。"""
    ranges = [(0, total)] if line_ranges is None else \
        [(max(0, start), min(end, total)) for start, end in line_ranges if start < end]
    segments = []
    for start, end in sorted(ranges):
        segment_start = start
        for row in range(start, end):
            if row in title_rows:
                if segment_start < row:
                    segments.append((segment_start, row))
                segment_start = row + 1
        if segment_start < end:
            segments.append((segment_start, end))
    return segments


def _refine_unclosed(lines, row: int, kind: str) -> str:
    """有開引號沒收尾，只有下一行正好把它收齊時，才是「對話中途斷行」；
    否則就只是引號沒有成對（例如少打了收尾引號），不要說成斷行。"""
    if kind != "unclosed":
        return kind
    if row + 1 < len(lines) and lines[row + 1].strip():
        text = _strip_indent(lines[row])[1]
        next_text = _strip_indent(lines[row + 1])[1]
        if _balanced(text + next_text):
            return kind
    return "unpaired"


class QuoteScan(list):
    """scan_quote_problems 的結果（照樣是問題清單）。wrapped：整本是硬換行、因此沒有列出的
    跨行引號有幾段（0＝不是硬換行的書，跨行的引號照常列成「對話中途斷行」）。
    hard_wrapped：這些段落是照字數切斷的（多半停在句子中間），「整理段落換行」接得回去；
    False 是沒有縮排的書裡一段話分成好幾行（每行都停在句號、引號），整理段落換行不會接。"""
    wrapped = 0
    hard_wrapped = False


# 引號問題裡至少這麼多、而且過半是「一段被硬換行切成幾行、接起來就成對」，才當成整本硬換行
HARD_WRAP_MIN_PARAGRAPHS = 50


_SENTENCE_ENDS = tuple("。！？!?…”」』～~")


def _wrapped_paragraph_rows(lines, segments) -> list:
    """跨行的段落：連續幾行都有字、後面的行沒有縮排（是上一行接下來的），
    而且接起來之後對話引號剛好成對——硬換行，或是沒有縮排的書裡一段話分成好幾行
    （每段開頭沒有再補引號）。回傳每一段的行號清單。"""
    paragraphs = []
    for start, end in segments:
        row = start
        while row < end:
            # 空行用 isspace() 看，不用 strip()：整本每一行都會問，不必每次產生新字串
            line = lines[row]
            if not line or line.isspace():
                row += 1
                continue
            following = row + 1
            while following < end and lines[following][:1] not in " \t\u3000\xa0" and lines[following] \
                    and not lines[following].isspace():
                following += 1
            if following - row > 1:
                texts = [_strip_indent(lines[index])[1] for index in range(row, following)]
                if not all(_balanced(text) for text in texts) and _balanced("".join(texts)):
                    paragraphs.append(list(range(row, following)))
            row = following
    return paragraphs


def scan_quote_problems(lines, line_ranges=None, title_rows=None, separator_target=None) -> list:
    """掃描整份（或指定行範圍）的標點問題（引號、分隔線、重複標點…）。

    line_ranges 是 0 起算的半開區間，給「只檢查選取的章節」用；行號一律
    回傳整份文件裡的絕對行號（1 起算），點選才能跳到正確的地方。

    separator_target：使用者選了「分隔線統一成哪一種」（一個符號，例如 "-"）；其他符號的
    分隔線列成「分隔線不一致」，修正成那個符號三個。None＝不統一。
    """
    if title_rows is None:
        title_rows = {row for row, line in enumerate(lines) if _is_title_row(line)}
    else:
        title_rows = set(title_rows)
    # 修正只能落在同一段正文裡：不跨出使用者選的範圍，也不跨過章節標題
    # （例如「標點在行首」接回上一行時，上一行是章節標題就不能接）。
    segments = _text_segments(len(lines), line_ranges, title_rows)
    segment_of = {}
    for start, end in segments:
        for row in range(start, end):
            segment_of[row] = (start, end)
    # 整本是硬換行（句子被切成兩三行）時，每一段跨行的對話都會變成「引號沒有成對」，清單被灌爆；
    # 這種書跨行成對的就不列，交給「整理段落換行」接回去。一般的書只有零星幾處，照常列出來修。
    wrapped_paragraphs = _wrapped_paragraph_rows(lines, segments)
    hard_wrapped = len(wrapped_paragraphs) >= HARD_WRAP_MIN_PARAGRAPHS
    if hard_wrapped:            # 段數夠多才數引號問題有幾行（一般的書不必多掃一遍）
        quote_rows = sum(1 for row in segment_of if {"unclosed", "unpaired"} & set(_check_line(lines[row])))
        hard_wrapped = sum(len(rows) for rows in wrapped_paragraphs) * 2 >= quote_rows
    skip_quotes = {row for rows in wrapped_paragraphs for row in rows} if hard_wrapped else set()
    problems = QuoteScan()
    problems.wrapped = len(wrapped_paragraphs) if hard_wrapped else 0
    if hard_wrapped:
        inner = [lines[row].rstrip() for rows in wrapped_paragraphs for row in rows[:-1]]
        problems.hard_wrapped = sum(not text.endswith(_SENTENCE_ENDS) for text in inner) * 2 >= len(inner)
    for row in sorted(segment_of):
        line = lines[row]
        segment_start, segment_end = segment_of[row]
        kinds = check_line(line)
        if row in skip_quotes:
            kinds = [kind for kind in kinds if kind not in ("unclosed", "unpaired")]
        char = separator_char(line) if separator_target else None
        if char is not None and char != separator_target:
            # 要換成別的符號：換完就是三個，不必再另外列「分隔線太長」
            kinds = [kind for kind in kinds if kind != "separator_line"]
            indent = _strip_indent(line)[0]
            problems.append({"line": row + 1, "kind": "separator_style",
                             "label": QUOTE_PROBLEM_LABELS["separator_style"], "preview": line.strip(),
                             "fix": {"start": row, "end": row + 1,
                                     "after": [indent + separator_target * SEPARATOR_LENGTH]}})
        for kind in kinds:
            # 判斷「下一行正好收尾」時也不看段外的行：段外的內容不能影響分類。
            if kind == "unclosed" and row + 1 >= segment_end:
                kind = "unpaired"
            kind = _refine_unclosed(lines, row, kind)
            fix = plan_fix(lines, row, kind)
            if fix and not (segment_start <= fix["start"] < fix["end"] <= segment_end):
                fix = None          # 跨出這段正文：只列出來，交給使用者手動處理
            problems.append({
                "line": row + 1,
                "kind": kind,
                "label": QUOTE_PROBLEM_LABELS[kind],
                "preview": line.strip(),
                "fix": fix,
            })
    return problems
