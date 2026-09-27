"""「辨識章節」的積木：外框、前綴、數字、單位、分隔、章名六種選項組成一種標題寫法（組合），
轉成跟自訂規則一樣的正則。同一欄可以選好幾個，意思是「其中一種就算」。

組合存成一條自訂規則（多一個 blocks 欄位記積木），讀檔時照積木重新產生正則，
寫法調整會跟著程式更新。常用寫法（TEMPLATES）是預先組好的積木，照信心分組。"""

import re

FRAME_OPTIONS = ("無", "( )", "【 】", "[ ]")
_FRAMES = {"( )": ("(（", ")）"), "【 】": ("【〔", "】〕"), "[ ]": ("[［", "]］")}

PREFIX_OPTIONS = {2: ("無", "第", "#", "Chapter", "Ch.", "Section", "章", "回", "節", "N-"),
                  1: ("無", "第", "Volume", "Vol.", "卷", "部", "篇", "集")}
_PREFIXES = {"第": "第", "#": r"[#＃](?![#＃])", "Chapter": "Chapter", "Ch.": r"Ch(?:ap)?\.?",
             "Section": r"Sec(?:t|tion)?\.?", "Volume": "Volume", "Vol.": r"Vol\.?",
             "章": "章", "回": "回", "節": "[節节]", "卷": "卷", "部": "部", "篇": "篇", "集": "集",
             # 「2-1 過河」前面的卷（季）號：後面一定要接著數字（「1 - 過河」的 1 是章號）
             "N-": r"(?P<volume>[0-9０-９]{1,3})\s*[-－—]\s*(?=[0-9０-９])"}

NUMBER_OPTIONS = ("一二三", "123", "全形１２", "壹貳參")
_NUMBERS = {"一二三": "一二兩两三四五六七八九十百千零〇", "123": "0-9", "全形１２": "０-９",
            "壹貳參": "壹貳贰參叁肆伍陸陆柒捌玖拾佰仟零"}
_NUMBER_LENGTH = {"一二三": 8, "123": 5, "全形１２": 5, "壹貳參": 8}

UNIT_OPTIONS = {2: ("無", "章", "回", "節", "話", "折", "幕"), 1: ("無", "卷", "部", "篇", "集", "季", "冊")}
_UNITS = {"章": "章", "回": "回", "節": "[節节]", "話": "[話话]", "折": "折", "幕": "幕",
          "卷": "卷", "部": "部", "篇": "篇", "集": "集", "季": "季", "冊": "[冊册]"}

SEP_OPTIONS = ("無", "空格", "、", ".", "：", "-", "·")
_SEPS = {"無": "", "空格": r"\s+", "、": r"\s*、\s*", ".": r"\s*[\.．](?![0-9０-９])\s*",
         "：": r"\s*[:：]\s*", "-": r"\s*[-－—]+\s*", "·": r"\s*[·・]\s*"}
_SEP_SHOWN = {"無": "", "空格": " ", "、": "、", ".": ". ", "：": "：", "-": " - ", "·": "·"}

TITLE_OPTIONS = ("要有", "可有可無", "沒有")
_TITLE = r"(?P<title>\S(?:.{0,198}\S)?)"

COLUMNS = ("frame", "prefix", "number", "unit", "sep", "title")
# 積木組出來的正則都是安全的寫法：自訂規則比對時用標準 re（比有逾時保護的 regex 快）
GENERATED_PATTERNS: set = set()
CONFIDENCE_LEVELS = ("高", "中", "低")


def options(column: str, level: int) -> tuple:
    return {"frame": FRAME_OPTIONS, "prefix": PREFIX_OPTIONS[level], "number": NUMBER_OPTIONS,
            "unit": UNIT_OPTIONS[level], "sep": SEP_OPTIONS, "title": TITLE_OPTIONS}[column]


def normalize(blocks, level: int):
    """整理存檔讀進來的積木：拿掉不認得的選項；哪一欄空了就回傳 None（這條規則不能用）。"""
    if not isinstance(blocks, dict):
        return None
    result = {}
    for column in COLUMNS:
        value = blocks.get(column)
        allowed = options(column, level)
        if column == "title":
            if value not in allowed:
                return None
            result[column] = value
            continue
        chosen = [item for item in allowed if isinstance(value, list) and item in value]
        if not chosen:
            return None
        result[column] = chosen
    return result


def _group(parts, optional=False):
    parts = [part for part in parts if part]
    if not parts:
        return ""
    body = parts[0] if len(parts) == 1 and not optional else "(?:" + "|".join(parts) + ")"
    return body + ("?" if optional else "")


def compile_blocks(blocks) -> str:
    """積木 → 正則（有 number、title 兩個命名群組，跟自訂規則一樣）。"""
    prefix_re = _group([_PREFIXES[item] for item in blocks["prefix"] if item != "無"], "無" in blocks["prefix"])
    classes = "".join(_NUMBERS[item] for item in blocks["number"])
    groups = [[_NUMBERS[item], _NUMBER_LENGTH[item]] for item in blocks["number"] if item not in _ARABIC]
    arabic = [item for item in blocks["number"] if item in _ARABIC]
    if arabic:
        # 半形、全形都選時合成一種：作者打字時切換輸入法的「06４」「0７０」也算
        groups.insert(0, ["".join(_NUMBERS[item] for item in arabic), 5])
    numbers = "|".join(f"[{characters}]{{1,{length}}}" for characters, length in groups)
    # 數字要整串吃完（「123」不能拆成章號 12、章名 3）
    number_re = f"(?P<number>{numbers})(?![{classes}])"
    unit_re = _group([_UNITS[item] for item in blocks["unit"] if item != "無"], "無" in blocks["unit"])
    # 標點先試、空白其次、什麼都沒有放最後：不然「12. 標題」的章名會吃進「. 」
    order = sorted(blocks["sep"], key=lambda item: (item == "無", item == "空格"))
    sep_re = "(?:" + "|".join(_SEPS[item] for item in order) + ")"
    title = blocks["title"]
    if title == "要有":
        tail = sep_re + _TITLE
    elif title == "可有可無":
        tail = f"(?:{sep_re}{_TITLE})?"
    else:
        marks = [_SEPS[item] for item in blocks["sep"] if item not in ("無", "空格")]
        tail = _group(marks, True) + "(?P<title>)"
    pattern = r"^\s*" + _framed(blocks["frame"], prefix_re, number_re, unit_re) + tail + r"\s*$"
    GENERATED_PATTERNS.add(pattern)
    return pattern


def _framed(frame_options, prefix_re: str, number_re: str, unit_re: str) -> str:
    """前綴、數字、單位，外面可以包一層括號。括號的位置照常見的寫法都收：包住整段（「【第一章】」「(1章)」）、
    只包數字（「(1)章」「第(1)章」）——左括號在前綴前面或後面、右括號在數字後面或單位後面都可以，
    但左右一定成對。"""
    unit = (r"\s*" + unit_re) if unit_re else ""
    frames = [frame for frame in frame_options if frame != "無"]
    if not frames:
        return prefix_re + r"\s*" + number_re + unit
    opens = "".join(re.escape(char) for frame in frames for char in _FRAMES[frame][0])
    closes = "".join(re.escape(char) for frame in frames for char in _FRAMES[frame][1])
    optional = "?" if "無" in frame_options else ""
    # 兩個位置各一個群組（同名群組不能出現兩次）；右括號照「有沒有左括號」決定要不要出現
    opening = f"(?:(?P<open>[{opens}])\\s*{prefix_re}|{prefix_re}\\s*(?P<open2>[{opens}]){optional})"
    close = rf"(?(open)\s*[{closes}]|(?(open2)\s*[{closes}]))"
    if not unit:
        return opening + r"\s*" + number_re + close
    return opening + r"\s*" + number_re + f"(?:{close}{unit}|{unit}{close})"


def auto_name(blocks) -> str:
    """照積木取的名稱（「#N 標題」「(N) 標題」）：使用者沒改過名稱時用這個。"""
    # 可有可無的外框、前綴、單位不寫進名稱
    frame = "" if "無" in blocks["frame"] else blocks["frame"][0]
    opening, closing = (frame.split() if frame else ("", ""))
    prefix = "" if "無" in blocks["prefix"] else blocks["prefix"][0]
    unit = "" if "無" in blocks["unit"] else blocks["unit"][0]
    head = f"{opening}{prefix}{' ' if prefix.isascii() and prefix.isalpha() else ''}N{unit}{closing}"
    if blocks["title"] == "沒有":
        return head
    sep = _SEP_SHOWN[blocks["sep"][0]]
    return head + (sep if sep.strip() else " ") + "標題"


def confidence(blocks) -> str:
    """高：一定有前綴或單位，正文很少這樣開頭；中：一定有括號，或編號後面一定接標點
    （正文的條列也會這樣寫）；低：其他（只有數字）。"""
    if "無" not in blocks["prefix"] or "無" not in blocks["unit"]:
        return "高"
    if "無" not in blocks["frame"] or not set(blocks["sep"]) & {"無", "空格"}:
        return "中"
    return "低"


def block_rule(blocks, level: int, name: str = "", enabled: bool = True) -> dict:
    """一條可以存進規則清單的組合。name 空白＝照積木自動取名。"""
    rule = {"name": name or auto_name(blocks), "pattern": compile_blocks(blocks), "level": level,
            "enabled": enabled, "blocks": {column: (list(value) if column != "title" else value)
                                           for column, value in blocks.items()}}
    if name:
        rule["named"] = True
    return rule


def refresh_block_rule(rule: dict):
    """存檔讀進來的組合：積木整理過、照現在的寫法重新產生正則與自動名稱。積木壞掉回傳 None。"""
    level = rule.get("level", 2)
    blocks = normalize(rule.get("blocks"), level)
    if blocks is None:
        return None
    name = rule.get("name", "") if rule.get("named") else ""
    return block_rule(blocks, level, name, bool(rule.get("enabled", True)))


_ALL_SEPS = ["無", "空格", "、", ".", "：", "-"]
_ARABIC = ["123", "全形１２"]
_ANY_NUMBER = ["一二三", "123", "全形１２"]
# 常用寫法：（代號, 層級, 積木）。代號跟以前的常用格式一樣，舊設定裡打開的常用格式照代號換成組合。
TEMPLATES = [
    ("hash_number", 2, {"frame": ["無"], "prefix": ["#"], "number": _ARABIC, "unit": ["無"],
                        "sep": _ALL_SEPS, "title": "要有"}),
    ("english_chapter", 2, {"frame": ["無"], "prefix": ["Chapter", "Ch."], "number": ["123"], "unit": ["無"],
                            "sep": _ALL_SEPS, "title": "可有可無"}),
    ("english_section", 2, {"frame": ["無"], "prefix": ["Section"], "number": ["123"], "unit": ["無"],
                            "sep": _ALL_SEPS, "title": "可有可無"}),
    ("leading_unit_chapter", 2, {"frame": list(FRAME_OPTIONS), "prefix": ["章", "回", "節"], "number": _ANY_NUMBER,
                                 "unit": ["無"], "sep": ["空格", "、", ".", "：", "-", "·"], "title": "可有可無"}),
    ("leading_unit_volume", 1, {"frame": list(FRAME_OPTIONS), "prefix": ["卷", "部", "篇", "集"],
                                "number": _ANY_NUMBER, "unit": ["無"], "sep": ["空格", "、", ".", "：", "-", "·"],
                                "title": "可有可無"}),
    ("english_volume", 1, {"frame": ["無"], "prefix": ["Volume", "Vol."], "number": ["123"], "unit": ["無"],
                           "sep": _ALL_SEPS, "title": "可有可無"}),
    ("bracket_number", 2, {"frame": ["( )", "【 】", "[ ]"], "prefix": ["無"], "number": _ANY_NUMBER, "unit": ["無"],
                           "sep": _ALL_SEPS, "title": "可有可無"}),
    ("dot_number", 2, {"frame": ["無"], "prefix": ["無"], "number": _ARABIC, "unit": ["無"], "sep": ["."],
                       "title": "要有"}),
    ("comma_number", 2, {"frame": ["無"], "prefix": ["無"], "number": _ARABIC, "unit": ["無"], "sep": ["、"],
                         "title": "要有"}),
    ("cn_comma_number", 2, {"frame": ["無"], "prefix": ["無"], "number": ["一二三"], "unit": ["無"], "sep": ["、"],
                            "title": "要有"}),
    ("space_number", 2, {"frame": ["無"], "prefix": ["無"], "number": _ARABIC, "unit": ["無"], "sep": ["空格"],
                         "title": "要有"}),
    ("bare_number", 2, {"frame": ["無"], "prefix": ["無"], "number": _ARABIC, "unit": ["無"], "sep": ["無"],
                        "title": "沒有"}),
    ("number_unit", 2, {"frame": ["無"], "prefix": ["無"], "number": _ARABIC, "unit": ["章", "回", "節"],
                        "sep": ["空格", "、", ".", "：", "-", "·"], "title": "要有"}),
    ("volume_dash_chapter", 2, {"frame": ["無"], "prefix": ["N-"], "number": _ARABIC, "unit": ["無"],
                                "sep": ["空格", "、", ".", "：", "·"], "title": "可有可無"}),
]
# 選單上的名稱（跟自動名稱不同的才寫）
TEMPLATE_LABELS = {"hash_number": "#1 標題", "english_chapter": "Chapter 1", "english_section": "Section 1",
                   "leading_unit_chapter": "章一 標題", "leading_unit_volume": "卷一 標題",
                   "english_volume": "Volume 1", "bracket_number": "(1) 標題、（一）", "dot_number": "1. 標題",
                   "comma_number": "1、標題", "cn_comma_number": "一、標題", "space_number": "1 標題",
                   "bare_number": "單獨一行的 1、001", "number_unit": "1章 標題",
                   "volume_dash_chapter": "2-1 標題（卷號-章號）"}


def template(template_id: str):
    """（層級, 積木的複本）"""
    for key, level, blocks in TEMPLATES:
        if key == template_id:
            return level, {column: (list(value) if column != "title" else value) for column, value in blocks.items()}
    raise KeyError(template_id)


def templates_by_confidence(level: int) -> list:
    """[(信心, [(代號, 選單名稱), …]), …]：新增組合選單照信心分組。"""
    groups = []
    for grade in CONFIDENCE_LEVELS:
        items = [(key, TEMPLATE_LABELS[key]) for key, template_level, blocks in TEMPLATES
                 if template_level == level and confidence(blocks) == grade]
        if items:
            groups.append((grade, items))
    return groups


def migrate_preset_rule(rule: dict):
    """舊設定裡打開的常用格式 → 組合（名稱改成現在的自動名稱）；不是常用格式、或沒有對應的回傳 None。"""
    try:
        level, blocks = template(rule.get("preset", ""))
    except KeyError:
        return None
    return block_rule(blocks, level, enabled=bool(rule.get("enabled", True)))


def blocks_from_sample(sample: str, level: int):
    """從一行標題猜積木（「貼一行標題」）：認得出編號才回傳，其餘照這一行的寫法選好。"""
    text = (sample or "").strip()
    frame_open = {"(": "( )", "（": "( )", "【": "【 】", "〔": "【 】", "[": "[ ]", "［": "[ ]"}
    frame = frame_open.get(text[:1], "無")
    if frame != "無":
        text = text[1:].lstrip()
    prefix = "無"
    for item in sorted((item for item in PREFIX_OPTIONS[level] if item != "無"), key=len, reverse=True):
        if re.match(_PREFIXES[item], text, re.IGNORECASE):
            prefix = item
            text = re.sub("^" + _PREFIXES[item], "", text, count=1, flags=re.IGNORECASE).lstrip()
            break
    if frame == "無" and frame_open.get(text[:1]):       # 「第(1)章」：括號在前綴後面
        frame = frame_open[text[:1]]
        text = text[1:].lstrip()
    number = next((item for item in NUMBER_OPTIONS if re.match(f"[{_NUMBERS[item]}]", text)), None)
    if number is None:
        return None
    text = re.sub(f"^[{_NUMBERS[number]}]+", "", text)
    closing = r"^\s*[)）】〕\]］]"
    if frame != "無":
        text = re.sub(closing, "", text)            # 「(1)章」：右括號在數字後面
    unit = "無"
    for item in UNIT_OPTIONS[level][1:]:
        match = re.match(r"\s*" + _UNITS[item], text)
        if match:
            unit, text = item, text[match.end():]
            break
    if frame != "無":
        text = re.sub(closing, "", text)
    stripped = text
    sep = "無"
    for item in ("、", ".", "：", "-", "·"):
        match = re.match(_SEPS[item], text)
        if match and match.end():
            sep, stripped = item, text[match.end():]
            break
    else:
        if text[:1].isspace():
            sep = "空格"
    return {"frame": [frame], "prefix": [prefix], "number": [number], "unit": [unit], "sep": [sep],
            "title": "要有" if stripped.strip() else "沒有"}
