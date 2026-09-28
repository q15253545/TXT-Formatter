"""標點全形／半形轉換與對話引號轉換。"""

PUNCT_TRANS = str.maketrans({",": "，", "!": "！", "?": "？", ":": "：", ";": "；", "(": "（", ")": "）"})
HALF_PUNCT_TRANS = str.maketrans({
    "，": ",", "。": ".", "！": "!", "？": "?", "：": ":", "；": ";",
    "（": "(", "）": ")", "【": "[", "】": "]", "、": ","
})
FULLWIDTH_DIGIT_TRANS = str.maketrans("0123456789", "０１２３４５６７８９")
HALFWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９", "0123456789")


QUOTE_KEEP = "保留原樣"
QUOTE_CORNER = "「」『』"
QUOTE_CURLY = "“”‘’"
QUOTE_STYLES = (QUOTE_KEEP, QUOTE_CORNER, QUOTE_CURLY)
# 外層開、外層關、內層開、內層關
_QUOTE_MARKS = {QUOTE_CORNER: ("「", "」", "『", "』"), QUOTE_CURLY: ("“", "”", "‘", "’")}
_OUTER_OPEN, _OUTER_CLOSE = set("“「"), set("”」")
_INNER_OPEN, _INNER_CLOSE = set("‘『"), set("’』")
_STRAIGHT = set("\"'")


def _has_cjk(text: str) -> bool:
    return any("㐀" <= char <= "鿿" or "𠀀" <= char <= "𱍏" for char in text)


def _straight_pairs(text: str) -> set:
    """同一行裡成對、而且中間有中文的直引號（" 或 '）的位置：只有這些當成引號轉，
    單數個（沒有成對）、英文裡的（don't、"go"）都不動。"""
    positions = set()
    for mark in _STRAIGHT:
        indices = [index for index, char in enumerate(text) if char == mark]
        if mark == "'":
            # 英文字母中間的是縮寫（don't）
            indices = [index for index in indices
                       if not (0 < index < len(text) - 1 and text[index - 1].isascii() and text[index - 1].isalpha()
                               and text[index + 1].isascii() and text[index + 1].isalpha())]
        if len(indices) % 2:
            continue
        for first, second in zip(indices[::2], indices[1::2]):
            if _has_cjk(text[first + 1:second]):
                positions.update((first, second))
    return positions


def convert_quotes(text: str, style: str) -> str:
    """對話引號換成 style 的寫法（「」『』 或 “”‘’），一行一行處理，不帶到下一行。

    - 有方向的引號照原本的內外層直接對應：“「→外層開、”」→外層關、‘『→內層開、’』→內層關，
      不去數全書開了幾層——作者漏掉一個結尾引號時，錯的只有那一行，不會後面整本都變成內層。
    - 同一行裡外層還沒關又出現外層開（「他說“走吧”」），才當成內層。
    - 直引號（" '）同一行成對、中間有中文才轉；沒有中文的行（英文）整行不動。
    - 一段話跨好幾段、每段開頭都有開引號的寫法，每一行各自處理，不會被當成巢狀。"""
    marks = _QUOTE_MARKS.get(style)
    if marks is None or not _has_cjk(text):
        return text
    outer_open, outer_close, inner_open, inner_close = marks
    straight = _straight_pairs(text) if any(char in _STRAIGHT for char in text) else set()
    stack = []          # 這一行裡還沒關的引號："outer"、"inner"，直引號另外記是哪一種字元
    result = []
    for index, char in enumerate(text):
        if index in straight:
            if stack and stack[-1][1] == char:
                kind = stack.pop()[0]
                result.append(outer_close if kind == "outer" else inner_close)
            else:
                kind = "inner" if stack and stack[-1][0] == "outer" else "outer"
                stack.append((kind, char))
                result.append(outer_open if kind == "outer" else inner_open)
        elif char in _OUTER_OPEN:
            kind = "inner" if stack and stack[-1][0] == "outer" else "outer"
            stack.append((kind, None))
            result.append(outer_open if kind == "outer" else inner_open)
        elif char in _INNER_OPEN:
            stack.append(("inner", None))
            result.append(inner_open)
        elif char in _OUTER_CLOSE or char in _INNER_CLOSE:
            if (char == "’" and 0 < index < len(text) - 1 and text[index - 1].isascii()
                    and text[index - 1].isalpha() and text[index + 1].isascii() and text[index + 1].isalpha()):
                result.append(char)             # 英文縮寫的撇號（don’t）
                continue
            if stack:
                kind = stack.pop()[0]
            else:
                kind = "outer" if char in _OUTER_CLOSE else "inner"
            result.append(outer_close if kind == "outer" else inner_close)
        else:
            result.append(char)
    return "".join(result)
