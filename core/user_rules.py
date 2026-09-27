"""使用者自訂章節規則的比對，以及可在「自訂章節規則」裡勾選的常用格式。"""

import re
from functools import lru_cache

from . import safe_regex
from .chapter_parse import strip_noise_lead, too_long_for_title
from .cn_numerals import chinese_to_arabic

# 純數字這類弱格式（#1、1.、1、、(1)…）不自動辨識：正文的條列、對話裡的數字長得一模一樣，
# 誤判時很難察覺。所以做成常用格式，一種格式一條規則，需要哪種就勾哪種。
# 章名的長度上限、結尾是哪些標點都照「辨識格式」的設定（match_user_chapter_rule 的 title_check），
# 預設不收逗號、句號結尾：正文的條列項目通常是完整句子。這裡的 200 字只是防呆。
_NUMBER = r"(?P<number>[0-9０-９]{1,4})"
_CN_NUMBER = r"(?P<number>[一二三四五六七八九十百千零〇兩两]{1,6})"
_ANY_NUMBER = r"(?P<number>[0-9０-９]{1,4}|[一二三四五六七八九十百千零〇兩两]{1,6})"
_TITLE = r"(?P<title>\S(?:.{0,198}\S)?)"
# 引號收尾的標題（「1 他說“走吧”」）不受標題結尾限制，跟自動辨識的例外一樣
_CLOSING_QUOTE_TAIL = re.compile(r"[”’」』]\s*$")
_LEAD_SEP = r"(?:[\.．、:：\-—]\s*)?"

PRESET_RULES = [
    {"preset": "hash_number", "name": "井號數字", "example": "#1 標題、#12. 標題",
     "pattern": rf"^[#＃](?![#＃])\s*{_NUMBER}\s*{_LEAD_SEP}{_TITLE}$"},
    {"preset": "dot_number", "name": "數字加點", "example": "1. 標題、12．標題",
     "pattern": rf"^{_NUMBER}\s*[\.．]\s*(?![0-9０-９]){_TITLE}$"},   # 3.14 是小數，不是章號
    {"preset": "comma_number", "name": "數字頓號", "example": "1、標題",
     "pattern": rf"^{_NUMBER}\s*、\s*{_TITLE}$"},
    {"preset": "space_number", "name": "數字空格", "example": "1 標題",
     "pattern": rf"^{_NUMBER}[ \t　]+{_TITLE}$"},
    {"preset": "bracket_number", "name": "括號數字", "example": "(1) 標題、【1】標題、（一）",
     "pattern": rf"^[\(（\[【]\s*{_ANY_NUMBER}\s*[\)）\]】]\s*(?:{_LEAD_SEP}{_TITLE})?$"},   # 「(3)」單獨一行也算
    {"preset": "cn_comma_number", "name": "中文數字頓號", "example": "一、標題",
     "pattern": rf"^{_CN_NUMBER}\s*、\s*{_TITLE}$"},
    {"preset": "bare_number", "name": "純數字獨立一行", "example": "1、001",
     "pattern": rf"^{_NUMBER}(?P<title>)$"},
    # 編號後面一定要有分隔，才不會把「卷三十萬大軍」「集三千寵愛於一身」當成卷。
    {"preset": "leading_unit_volume", "name": "不帶「第」的卷號", "example": "卷一 風起、集三：歸來",
     "level": 1,
     "pattern": r"^[【\[\(（]?\s*[卷部篇集]\s*"
                r"(?P<number>[0-9０-９一二兩两三四五六七八九十百千零〇]{1,8})\s*[】\]\)）]?"
                rf"(?:[\s:：、．.\-—·]+{_TITLE})?$"},
    # 用名稱當卷名、沒有編號的卷（青雲篇、上卷）。整行就只能是「名稱＋篇／卷」，
    # 名稱最多 8 個字，不收「第」開頭（那是「第三篇」，預設規則就認得），也不收
    # 「這一篇」「每一卷」這種指示詞、數量詞開頭的說法；
    # 不收「部」：「全部」「那部」這類詞太常單獨出現。
    {"preset": "named_volume", "name": "名稱＋篇／卷", "example": "青雲篇、上卷",
     "level": 1,
     "pattern": r"^[【\[\(（]?\s*(?P<title>(?!第|[這这那哪每某整一兩两幾几])[\u4e00-\u9fff]{1,8}[篇卷])\s*[】\]\)）]?$"},
    {"preset": "english_chapter", "name": "英文 Chapter N", "example": "Chapter 1、Ch.12",
     "pattern": r"^(?:Chapter|Chap|Ch)\.?\s*(?P<number>[0-9]{1,4})"
                r"(?:[\s:：.\-—]+(?P<title>\S.{0,60}?))?\s*$"},
    {"preset": "english_section", "name": "英文 Section N", "example": "Section 1、Sec.12",
     "pattern": r"^(?:Section|Sect|Sec)\.?\s*(?P<number>[0-9]{1,4})"
                r"(?:[\s:：.\-—]+(?P<title>\S.{0,60}?))?\s*$"},
    # 少了「第」的「12章 標題」：單位後面一定要有分隔才接章名，「3章節」「10回合」這種不算
    {"preset": "number_unit", "name": "數字＋章", "example": "12章 標題、12章、標題",
     "pattern": rf"^{_NUMBER}\s*[章回節节](?:[ \t　]+|\s*[、:：.．\-—·]\s*){_TITLE}$"},
    # 「2-1」「2-3 過河」：卷（季）號-章號。章號是後面那個，卷號另外記著（本文沒寫那一卷時要補）
    {"preset": "volume_dash_chapter", "name": "卷號-章號", "example": "2-1、2-3 標題",
     "pattern": r"^(?P<volume>[0-9０-９]{1,3})\s*[-－—]\s*(?P<number>[0-9０-９]{1,4})(?![0-9０-９])"
                rf"(?:(?:[ \t　]+|\s*[、:：.．·]\s*){_TITLE})?$"},
    # 編號後面一定要有分隔，才不會把「章三十萬字」這種句子當成章。
    {"preset": "leading_unit_chapter", "name": "不帶「第」的章號", "example": "章一 風起、回三：歸來",
     "pattern": r"^[【\[\(（]?\s*[章回節节]\s*"
                r"(?P<number>[0-9０-９一二兩两三四五六七八九十百千零〇]{1,8})\s*[】\]\)）]?"
                rf"(?:[\s:：、．.\-—·]+{_TITLE})?$"},
]


# 從範例推導規則用的字元分類。
_ARABIC = "0-9０-９"
_CHINESE_NUMBER = "一二兩两三四五六七八九十百千零〇"
_SEPARATORS = "：:、，,.．。\\-—─～~|｜/／"
_VOLUME_UNITS_IN_SAMPLE = "卷部篇集季"
_CLOSING = "】\\]）\\)》>」』〕"
# 編號後面依序是：單位（章／話／節…）、收尾括號、分隔符、章名，後三者都可有可無。
# 章節單位白名單：編號後面緊接這些字才是單位，其餘都是章名（「第1章山河」的單位只有「章」）。
_UNITS_IN_SAMPLE = "章回節节折幕卷部篇集季話话"
_FORMAL_SAMPLE = re.compile(
    rf"第\s*(?P<number>[{_ARABIC}]+|[{_CHINESE_NUMBER}]+)\s*(?P<unit>[{_UNITS_IN_SAMPLE}])")
_SAMPLE_REST = re.compile(
    rf"^(?P<unit>[^\s{_SEPARATORS}{_CLOSING}]{{0,4}})(?P<close>[{_CLOSING}]?)"
    rf"(?:[\s{_SEPARATORS}]*(?P<title>\S.*))?$")


def _literal_pattern(text: str) -> str:
    """把範例裡的固定文字轉成正則：空白放寬成「可有可無」，其餘逐字跳脫。"""
    parts, index = [], 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            while index < len(text) and text[index].isspace():
                index += 1
            parts.append(r"\s*")
            continue
        parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def rule_from_sample(sample: str):
    r"""從一行章節標題推導出一條自訂規則（不懂正則也能建規則）。

    例如貼上「正文 001章：初入江湖」，得到
    `^\s*正文\s*(?P<number>[0-9０-９]{1,6})章[\s：:…]+(?P<title>\S.*)?\s*$`，
    章號與章名都有命名群組，缺章檢查、連續編號才有依據。

    找不到編號時回傳 None：沒有編號的規則沒辦法對應章號，價值有限，
    這種情況請使用者自己寫或用「將選取格式存為規則」。
    """
    clean = (sample or "").strip()
    if not clean or len(clean) > 120:
        return None
    # 先認正規的「第＋數字＋單位」：章名裡的數字（「第一章 2026年的故事」）不能被當成章號
    # 。
    formal = _FORMAL_SAMPLE.search(clean)
    if formal:
        start, end = formal.span("number")
        number_text = formal.group("number")
        number_class = f"[{_ARABIC}]" if re.match(rf"[{_ARABIC}]", number_text) else f"[{_CHINESE_NUMBER}]"
        prefix = clean[:start]
        rest = clean[end:]
    else:
        match = re.search(rf"[{_ARABIC}]+", clean)
        number_class = f"[{_ARABIC}]"
        if match is None:
            match = re.search(rf"[{_CHINESE_NUMBER}]+", clean)
            number_class = f"[{_CHINESE_NUMBER}]"
        if match is None:
            return None
        prefix, rest = clean[:match.start()], clean[match.end():]

    unit_match = re.match(rf"\s*([{_UNITS_IN_SAMPLE}])([{_CLOSING}]?)", rest)
    if unit_match:
        # 單位只有一個字（章、回、節…），後面全部是可變的章名：「第1章山河」要能對到「第2章星海」
        unit = unit_match.group(1) + unit_match.group(2)
        title = rest[unit_match.end():].lstrip(" \t　" + "：:、，,.．。-—─～~|｜/／").strip()
    else:
        rest_match = _SAMPLE_REST.match(rest)
        if rest_match:
            unit = rest_match.group("unit") + rest_match.group("close")
            title = rest_match.group("title") or ""
        else:
            unit, title = rest.strip(), ""

    # 章名一律做成「可有可無」：同一種格式常常有幾章只有章號、沒有標題。
    pattern = (r"^\s*" + _literal_pattern(prefix)
               + f"(?P<number>{number_class}{{1,8}})" + _literal_pattern(unit)
               + rf"(?:[\s{_SEPARATORS}]*(?P<title>\S.*?))?\s*$")

    name = f"{prefix}N{unit}".strip()
    if title:
        name += " 標題"
    return {
        "name": name[:30] or "自訂規則",
        "pattern": pattern,
        # 卷級：單位是卷／部／篇／集，或編號前面就是這些字（「卷二 風起」）。
        "level": 1 if (unit[:1] or prefix[-1:]) in _VOLUME_UNITS_IN_SAMPLE else 2,
        "enabled": True,
    }


def preset_rule(preset_id):
    """回傳一條可存進規則清單的常用格式規則（新的 dict，已啟用）。"""
    for preset in PRESET_RULES:
        if preset["preset"] == preset_id:
            return {"name": preset["name"], "pattern": preset["pattern"],
                    "level": preset.get("level", 2), "enabled": True, "preset": preset_id}
    raise KeyError(preset_id)


# 使用者自訂的特殊標題（「辨識章節 → 特殊標題」新增的字，例如「續章」）：跟序章、番外一樣照原文顯示，
# 不改寫成「第N章」。可以帶編號（「續章12」）、章名，前面可以有一段名稱加空白（「書名 續章3」）；
# 字後面要接編號、分隔或行尾，「續章寫得好」這種句子不算。簡繁兩種寫法都認（有 OpenCC 時）。
SPECIAL_WORD_MAX = 8
_SPECIAL_NUMBER = r"[0-9０-９]{1,5}|[一二兩两三四五六七八九十百千零〇]{1,8}"
_SPECIAL_SEPARATOR = r"[\s:：、．.·\-—_]"


def special_word_variants(word: str) -> list:
    from .script_convert import SCRIPT_SIMP, SCRIPT_TRAD, convert_script
    word = word.strip()
    variants = {word, convert_script(word, SCRIPT_TRAD), convert_script(word, SCRIPT_SIMP)}
    return sorted((variant for variant in variants if variant), key=lambda item: (-len(item), item))


def special_word_rule(word: str, level: int = 2, enabled: bool = True) -> dict:
    """一條自訂特殊標題的規則（存進規則清單，special 欄位記著那個字）。"""
    from .title_blocks import GENERATED_PATTERNS
    word = word.strip()
    alternatives = "|".join(re.escape(variant) for variant in special_word_variants(word))
    # 行首的「?」是轉存時丟掉的表情符號；「續章155/156」「續章335,336」一行兩章，章號用第一個
    pattern = (r"^[\s【\[(（?]*(?:(?P<arc>[^\s，。！？：；,.!?;]{1,15})\s+)?(?P<word>" + alternatives + r")"
               r"(?:\s*(?P<number>" + _SPECIAL_NUMBER + r")(?P<more>(?:[/／,，、]\s*(?:" + _SPECIAL_NUMBER + r"))*))?"
               r"\s*[】\]）)]?"
               r"(?=$|" + _SPECIAL_SEPARATOR + r"|[（(【\[「『])"
               r"(?:" + _SPECIAL_SEPARATOR + r"*(?P<title>\S.*?))?\s*$")
    GENERATED_PATTERNS.add(pattern)
    return {"name": word, "pattern": pattern, "level": 1 if level == 1 else 2, "enabled": bool(enabled),
            "special": word}


# 巢狀量詞（(a+)+、(.*)* …）在比對失敗時會呈指數成長，是正則卡死的典型寫法。
# 尋找面板與自訂章節規則共用這個判斷。
RISKY_REGEX = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*]")


def is_risky_pattern(pattern: str) -> bool:
    return bool(pattern) and RISKY_REGEX.search(pattern) is not None


@lru_cache(maxsize=512)
def _compile_rule_pattern(pattern):
    """快取編譯後的自訂規則正則。

    刻意不把編譯結果存進規則字典本身——那個字典會被 `_save_json` 整包
    序列化回 JSON，塞進不可序列化的 re.Pattern 物件會讓存檔直接炸掉。
    用獨立的 lru_cache 換取同樣的效果：規則通常只有幾條，512 個位置
    綽綽有餘，重複呼叫可命中；即使遇到壞掉的正則，交由呼叫端的
    try/except 處理，這裡不特別攔截。
    """
    from .title_blocks import GENERATED_PATTERNS
    if pattern in _BUILTIN_PATTERNS or pattern in GENERATED_PATTERNS:
        return re.compile(pattern, re.IGNORECASE)     # 內建的常用格式、積木組出來的：安全，用標準 re 比較快
    return safe_regex.compile(pattern, re.IGNORECASE)


_BUILTIN_PATTERNS = frozenset(preset["pattern"] for preset in PRESET_RULES)


_PRESET_COMPILED = tuple((preset["preset"], re.compile(preset["pattern"], re.IGNORECASE),
                          {"name": preset["name"], "level": preset.get("level", 2)})
                         for preset in PRESET_RULES)


@lru_cache(maxsize=1 << 18)
def preset_match(text):
    """這一行第一個符合的常用格式：（preset id, 比對結果）或 None。照內容快取
    （「本文可疑章節」每次開都要把整本每一行比一遍）；內建格式都是安全的正則，直接用 re。"""
    if not text or len(text) > _MAX_RULE_TEXT:
        return None
    for candidate in (text, strip_noise_lead(text)):
        if not candidate:
            continue
        for preset_id, pattern, rule in _PRESET_COMPILED:
            match = pattern.fullmatch(candidate)
            result = _rule_result(match, rule, candidate) if match else None
            if result:
                return preset_id, result
    return None


# 在期限內跑不完的規則（寫法造成大量回溯）：這次開啟期間停用，不再拿來比對
# （改了正則內容就是另一條，會重新試）。還沒提醒過使用者的名稱另外記著，
# 介面重建目錄後用 pop_timed_out_rules() 取出來顯示。
_TIMED_OUT_PATTERNS: set = set()
_UNREPORTED_TIMEOUTS: set = set()


def pop_timed_out_rules() -> list:
    """取出（並清空）還沒提醒過的逾時規則名稱；規則本身維持停用。"""
    names = sorted(_UNREPORTED_TIMEOUTS)
    _UNREPORTED_TIMEOUTS.clear()
    return names


_MAX_RULE_TEXT = 180


def _rule_result(match, rule, text):
    """比對成功之後：取出章號與標題，章號不合理或什麼都沒有就不算。"""
    groups = match.groupdict()
    if rule.get("special"):
        number_text = (groups.get("number") or "").strip()
        return {"rule": rule["name"], "level": int(rule["level"]),
                "number": int(chinese_to_arabic(number_text)) if number_text else 0,
                "title": (groups.get("title") or "").strip(), "special": rule["special"],
                "arc": (groups.get("arc") or "").strip(),
                "tag": groups["word"] + number_text + (groups.get("more") or "")}
    number_text = (groups.get("number") or "").strip()
    number = chinese_to_arabic(number_text) if number_text else 0
    # 規則裡有 title 群組就用它（可以是空的，例如只有章號的「純數字獨立一行」）；
    # 沒有 title 群組才拿整行當標題。
    title = (groups["title"] or "").strip() if "title" in groups else text.strip()
    if number_text and (number <= 0 or not float(number).is_integer()):
        return None
    if not title and not number_text:
        return None
    result = {"rule": rule["name"], "level": int(rule["level"]),
              "number": int(number) if number else 0, "title": title}
    volume = chinese_to_arabic(groups["volume"]) if groups.get("volume") else 0
    if volume > 0 and float(volume).is_integer():
        result["volume"] = int(volume)
    return result


def match_user_chapter_rule(text, rules, title_check=None):
    """套用使用者規則；規則仍受獨立行、長度與有效擷取內容限制。
    title_check（「標題結尾」「標題長度」的設定）只管常用格式與積木組合：使用者自己寫的正則照寫法，不另外擋。
    開頭多了「正文」這種雜訊前綴的（「正文 001 山路」），常用格式與積木組合拿掉前綴再比一次。"""
    result = _match_rules(text, rules, title_check, False)
    if result is None:
        rest = strip_noise_lead(text)
        if rest:
            result = _match_rules(rest, rules, title_check, True)
    return result


def _match_rules(text, rules, title_check, built_in_only):
    if not text or len(text) > _MAX_RULE_TEXT:
        return None
    for rule in rules:
        if not rule.get("enabled", True) or (built_in_only and not (rule.get("preset") or rule.get("blocks")
                                                                    or rule.get("special"))):
            continue
        pattern_text = rule.get("pattern", "")
        if pattern_text in _TIMED_OUT_PATTERNS:
            continue
        try:
            match = safe_regex.fullmatch(_compile_rule_pattern(pattern_text), text)
        except safe_regex.RegexTimeout:
            _TIMED_OUT_PATTERNS.add(pattern_text)
            _UNREPORTED_TIMEOUTS.add(rule.get("name", "") or pattern_text)
            continue
        except (KeyError, *safe_regex.errors):
            continue
        result = _rule_result(match, rule, text) if match else None
        if result and (rule.get("preset") or rule.get("blocks") or rule.get("special")) and title_check is not None and (
                too_long_for_title(text, getattr(title_check, "max_length", len(text)))
                or (title_check.search(text) and not _CLOSING_QUOTE_TAIL.search(text))):
            continue
        if result:
            return result
    return None
