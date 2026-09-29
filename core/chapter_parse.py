"""單行章節標題辨識：正則、有效性判斷、編號轉換與標題清理。"""

import re
import unicodedata
from functools import lru_cache

from . import title_blocks
from .cn_numerals import chinese_to_arabic, arabic_to_chinese

# 大寫數字（第壹章、第拾貳章）也算：cn_numerals 轉得了，辨識章節的積木也有「壹貳參」
CN_UPPER_DIGITS = "壹貳贰參叁肆伍陸陆柒捌玖拾佰仟"
CN_NUM_PATTERN = r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿兆〇零" + CN_UPPER_DIGITS + r"]+"
CN_NUM_FLOAT_PATTERN = CN_NUM_PATTERN + r"(?:[\.．]\d+)?"
CN_NUM_FLOAT_OPT_PATTERN = r"[0-9０-９一二兩两三四五六七八九十百千萬万億亿兆〇零" + CN_UPPER_DIGITS + r"]*(?:[\.．]\d+)?"

SEP = r"[ \t:：]+"
# 一個標題涵蓋好幾章：「第38-40章」「第三十八——四十章」「第5至6章」
RANGE_SEP = r"(?:[-－~～—–]{1,2}|至|到)"
MAX_RANGE_SPAN = 10

# 章節前面常見、但沒有任何意義的前綴：網站匯出的 TXT 常在每一章前面加上
# 「正文」「VIP章節」。規則會把「第N章」前面的字當成篇名／書名保留下來
# （合集的「某書 第一章」需要），這些字卻只是雜訊——排版後有的章留著
# 「正文」、有的章沒有（例如一部分走自訂規則），整本書就不一致了。
NOISE_TITLE_PREFIXES = frozenset(prefix.casefold() for prefix in (
    "正文", "正文卷", "正文部分", "正文篇", "作品正文", "作品正文卷",
    "內容", "内容", "章節", "章节", "本章", "全文",
    "VIP章節", "VIP章节", "VIP卷", "VIP", "免費章節", "免费章节", "最新章節", "最新章节",
))


def is_noise_prefix(text: str) -> bool:
    return bool(text) and text.strip().casefold() in NOISE_TITLE_PREFIXES


_NOISE_LEAD = re.compile(r"^\s*(?:" + "|".join(sorted(map(re.escape, NOISE_TITLE_PREFIXES), key=len, reverse=True))
                         + r")[\s:：、·\-—]+(?=\S)", re.IGNORECASE)


def strip_noise_lead(text: str):
    """「正文 001 山路」「VIP章節：12. 城裡」：開頭的雜訊前綴拿掉後剩下的；沒有前綴回傳 None。"""
    match = _NOISE_LEAD.match(text) if text else None
    return text[match.end():] if match else None


def _clean_arc(arc: str) -> str:
    """「第N章」前面的篇名：是雜訊前綴就當成沒有。"""
    return "" if is_noise_prefix(arc) else arc
ARC_PATTERN = rf"(?:(?P<arc>[^，。！？：；\.,!?;”’\n\(\)\[\]]{{1,15}}){SEP})?"
VOL_PATTERN = rf"(?:(?P<volume>第{CN_NUM_PATTERN}\s*[部卷篇集季]){SEP})?"

# 關鍵字後面必須是行尾、空白、分隔符號或括號，不能直接接著文字：
# 否則「序幕拉開了」「簡介一下我自己」「後記得……」這種句子都會被當成
# 特殊標題（實測預設規則會把它們全部收進目錄）。
_TAG_BOUNDARY = r"(?=$|[\s　:：、．.·・•▪\-—_~～（(【\[「『《〈】\]\)）」』》〉])"

COMBO_SPECIAL_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    r"(?P<tag>前言|簡介|简介|人物簡介|人物简介|序章|序言|序|楔子|引子|後記|后记|尾聲|尾声|內容簡介|内容简介|間章|间章)"
    + _TAG_BOUNDARY +
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

LV1_A_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN +
    r"(?:(?P<prefix>第)\s*(?P<number>" + CN_NUM_PATTERN + r")\s*(?P<unit>[部卷篇集季])|(?P<leading_unit>[部卷篇集])\s*(?P<trailing_number>" + CN_NUM_PATTERN + r"))"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

# 外傳／終章同樣要有分隔，但多允許一個「之」：「外傳之青梅竹馬」是常見寫法，
# 「外傳他說不必了」則是正文。
LV1_B_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN +
    r"(?P<unit>(?:外傳|外传|終章|终章)(?=之|" + _TAG_BOUNDARY[3:] + r"|分卷[\s:：])"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    rf"第\s*(?P<number>{CN_NUM_FLOAT_PATTERN})(?:\s*[、，,]\s*(?P<also>[0-9０-９]{{1,4}})"
    rf"|\s*{RANGE_SEP}\s*(?P<range_end>{CN_NUM_PATTERN}))?\s*(?P<unit>[章回節节折幕])"
    r"[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_EXTRA_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    rf"番外\s*(?P<number>{CN_NUM_FLOAT_OPT_PATTERN})\s*(?P<unit>[章回節节]?)\s*(?P<title>.*)$", re.IGNORECASE)

COMBO_LV2_NUM_REGEX = re.compile(
    r"^[\s【\[\(-]*" + ARC_PATTERN + VOL_PATTERN +
    r"(?:Chapter|Chap|Ch)\.?\s*(?P<number>\d+(?:[\.．]\d+)?)[\s】\]\)-]*(?P<title>.*)$", re.IGNORECASE)

SUBTITLE_NUMBER_PATTERN = r"(?:[0-9０-９]+|[一二兩两三四五六七八九十百千萬万億亿兆〇零]+)"
WEAK_HASH_TITLE_REGEX = re.compile(
    r"^\s*[#＃]\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*(?:[\.．,，、:：\-—]+\s*)?(.+?)\s*$"
)
WEAK_BRACKET_TITLE_REGEX = re.compile(
    r"^\s*[\(（\[]\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*[\)）\]]\s*(?:[\.．,，、:：\-—]+\s*)?(.+?)\s*$"
)
WEAK_PLAIN_TITLE_REGEX = re.compile(
    r"^\s*(" + SUBTITLE_NUMBER_PATTERN +
    r")\s*([\.．,，、:：\-—]+|\s+)\s*(.+?)\s*$"
)


# 「2-1」「2-3 過河」：前面是卷（季）號、後面才是章號。不先認出來的話會被當成「第2章」、章名「1」
VOLUME_DASH_CHAPTER_REGEX = re.compile(
    r"^\s*(?P<volume>[0-9０-９]{1,3})\s*[-－—]\s*(?P<number>[0-9０-９]{1,4})(?![0-9０-９])"
    r"(?:(?:\s*[:：、.．·]\s*|\s+)(?P<title>\S.*?))?\s*$")


def volume_dash_number(text):
    """「2-1 過河」的卷號 2；不是這種寫法回傳 None。"""
    match = VOLUME_DASH_CHAPTER_REGEX.match(text) if text and ("-" in text or "－" in text or "—" in text) else None
    return int(chinese_to_arabic(match.group("volume"))) if match else None


@lru_cache(maxsize=1 << 18)
def parse_weak_numbered_title(text):
    """共用弱格式解析器：供次行章名合併與稀有章節掃描使用。"""
    dash = VOLUME_DASH_CHAPTER_REGEX.match(text)
    if dash:
        number = chinese_to_arabic(dash.group("number"))
        if number <= 0 or not chinese_to_arabic(dash.group("volume")):
            return None
        return {"number": int(number), "number_text": dash.group("number"), "body": (dash.group("title") or "").strip(),
                "volume": int(chinese_to_arabic(dash.group("volume"))), "style": "卷-章", "style_key": "卷-章"}
    for style, regex in (
        ("井號", WEAK_HASH_TITLE_REGEX),
        ("括號", WEAK_BRACKET_TITLE_REGEX),
        ("一般", WEAK_PLAIN_TITLE_REGEX),
    ):
        match = regex.match(text)
        if not match:
            continue
        if style == "一般":
            number_text, separator, body = match.group(1), match.group(2), match.group(3)
            normalized_separator = unicodedata.normalize("NFKC", separator).strip()
            style_key = f"一般:{normalized_separator or '空格'}"
        else:
            number_text, body = match.group(1), match.group(2)
            style_key = style
        number = chinese_to_arabic(number_text)
        if number <= 0 or not float(number).is_integer() or not body.strip():
            return None
        return {
            "number": int(number),
            "number_text": number_text,
            "body": body.strip(),
            "style": style,
            "style_key": style_key,
        }
    return None


def is_weak_numbered_title(text):
    return parse_weak_numbered_title(text) is not None


def weak_candidate_to_user_rule(candidate):
    """將弱格式候選轉為可保存的完整行自訂規則。"""
    number = r"(?P<number>" + SUBTITLE_NUMBER_PATTERN + r")"
    title = r"(?P<title>.+?)"
    style_key = candidate.get("style_key", candidate.get("style", "一般"))
    if style_key == "卷-章":
        return title_blocks.block_rule(title_blocks.template("volume_dash_chapter")[1], 2)
    if style_key == "井號":
        pattern = (r"^\s*[#＃]\s*" + number
                   + r"\s*(?:[\.．,，、:：\-—]+\s*)?" + title + r"\s*$")
    elif style_key == "括號":
        pattern = (r"^\s*[\(（\[]\s*" + number + r"\s*[\)）\]]\s*"
                   + r"(?:[\.．,，、:：\-—]+\s*)?" + title + r"\s*$")
    else:
        separator = style_key.split(":", 1)[1] if ":" in style_key else "空格"
        separator_patterns = {
            "空格": r"\s+", ".": r"\s*[\.．]+\s*", ",": r"\s*[,，]+\s*",
            "、": r"\s*、+\s*", ":": r"\s*[:：]+\s*", "-": r"\s*[\-—]+\s*",
        }
        separator_pattern = separator_patterns.get(separator, r"\s*" + re.escape(separator) + r"+\s*")
        pattern = r"^\s*" + number + separator_pattern + title + r"\s*$"
    return {
        "name": f"未辨識格式－{style_key}",
        "pattern": pattern,
        "level": 2,
        "enabled": True,
    }


# 章號裡把 0 打成英文字母 o（「第2oo章」、「第1O5章」）：至少有一個真的數字才換，
# 單獨的「第o章」不算。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
# 「第.1808章」：章號前面插了句點（防轉載），辨識時先拿掉
_LEADING_DOT = re.compile(r"(?<=第)(\s*)[.．]\s*(?=[0-9０-９])")
_OCR_ZERO = re.compile(r"(?<=第)(\s*)([0-9][0-9oO]*[oO][0-9oO]*)(?=\s*[章回節节])")


_LEADING_CHAPTER = re.compile(rf"^\s*第\s*(?P<number>{CN_NUM_FLOAT_PATTERN})\s*(?P<unit>[章回節节折幕])"
                             r"[\s:：、·\-—]*(?P<title>.*)$")


def _valid_range(start, end) -> bool:
    return float(start).is_integer() and start < end <= start + MAX_RANGE_SPAN


_RANGE_TITLE = re.compile(rf"第\s*({CN_NUM_PATTERN})\s*{RANGE_SEP}\s*({CN_NUM_PATTERN})\s*[章回節节折幕]")


def chapter_range_end(text):
    """「第38-40章」這種一個標題涵蓋好幾章的：回傳最後一章的號碼（40）；不是就回傳 None。"""
    match = _RANGE_TITLE.search(text or "")
    if not match:
        return None
    start, end = chinese_to_arabic(match.group(1)), chinese_to_arabic(match.group(2))
    return int(end) if _valid_range(start, end) else None


def parse_lv2(line):
    # 自動辨識只收正規格式（第N章／回／節…、番外）；英文 Chapter N 是辨識章節的常用寫法。
    # COMBO_LV2_NUM_REGEX 給連續編號、保留標題間隔這些「已經確定是標題」之後的處理使用。
    if "." in line or "．" in line:
        line = _LEADING_DOT.sub(lambda match: match.group(1), line)
    if "o" in line or "O" in line:
        line = _OCR_ZERO.sub(lambda match: match.group(1) + match.group(2).replace("o", "0").replace("O", "0"), line)
    for regex, prefix in ((COMBO_LV2_REGEX, "第"), (COMBO_LV2_EXTRA_REGEX, "番外")):
        match = regex.match(line)
        if match:
            fields = match.groupdict(default="")
            if fields["arc"] and prefix == "第":
                # 「第572章 番外之二：第一章 山路」：前面那段本身就是同一種單位的章號，這一章是第 572 章，
                # 後面的「第一章」是章名的一部分（「第一章 第二節」這種章加節照舊當成節）
                outer = _LEADING_CHAPTER.match(line)
                if outer and outer.group("unit") == fields.get("unit"):
                    return ("", "", "第", chinese_to_arabic(outer.group("number")), outer.group("unit"),
                            outer.group("title"))
            number = chinese_to_arabic(fields["number"]) if fields["number"] else 0.0
            if fields.get("range_end") and not _valid_range(number, chinese_to_arabic(fields["range_end"])):
                return None
            return (_clean_arc(fields["arc"]), fields["volume"], prefix, number,
                    fields.get("unit", "章"), fields["title"])
    weak = parse_weak_numbered_title(line)
    if weak:
        return ("", "", "第", weak["number"], "章", weak["body"])
    return None


# A chapter number before the volume word: 「卷六 山路 第532章 第五季」 is a chapter titled 第五季
_CHAPTER_IN_ARC = re.compile(r"第\s*" + CN_NUM_PATTERN + r"\s*[章回節节折幕]")


def parse_lv1(line):
    m = LV1_A_REGEX.match(line)
    if m and m.group("arc") and _CHAPTER_IN_ARC.search(m.group("arc")):
        return None
    # 只收「第N卷／部／篇／集」：不帶「第」的「集三千寵愛於一身」會被當成卷，那種寫法是
    # 辨識章節的常用寫法（要求編號後面有分隔）。LV1_A_REGEX 的另一種語序給連續編號用。
    if m and m.group("number"):
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), fields["prefix"] or "第", chinese_to_arabic(fields["number"]),
                fields["unit"], fields["title"])

    m = LV1_B_REGEX.match(line)
    if m and not (m.group("arc") and _CHAPTER_IN_ARC.search(m.group("arc"))):
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), "", 0.0, fields["unit"], fields["title"])
    return None


# 預設的標題長度上限：整批 738 本、30 萬章裡 99.9% 在 34 字以內；更長的「第N章 章名」另外放寬（long_formal_title）
MAX_TITLE_LENGTH = 40
CLOSING_QUOTE_TAIL_REGEX = re.compile(r"[”’」』]\s*$")


# 標題結尾允許字元：每一組是（顯示用的符號, 這一組包含的全形／半形字, 名稱），各自是開關。
# 預設允許問號、驚嘆號、刪節號、波浪號；問句結尾的正文被當成章節時，使用者可以關掉問號。
TITLE_TAIL_GROUPS = (
    ("？", "？?", "問號"),
    ("！", "！!", "驚嘆號"),
    ("…", "…", "刪節號"),
    ("～", "～~", "波浪號"),
    ("，", "，,", "逗號"),
    ("。", "。.", "句號"),
    ("：", "：:", "冒號"),
    ("；", "；;", "分號"),
    ("、", "、", "頓號"),
    ("”", "”’", "收尾引號"),
)
DEFAULT_TITLE_TAIL_ALLOWED = "？?！!…～~"
_TITLE_TAIL_CHARS = "".join(chars for _symbol, chars, _name in TITLE_TAIL_GROUPS)


def title_tail_groups(extra_chars: str = "") -> list:
    """內建的幾組，加上使用者在「標題結尾」分頁自己新增的標點（每個字一組）。"""
    groups = list(TITLE_TAIL_GROUPS)
    for char in dict.fromkeys(extra_chars or ""):
        if char not in _TITLE_TAIL_CHARS:
            groups.append((char, char, "自訂"))
    return groups


def build_title_tail_regex(allowed_chars=DEFAULT_TITLE_TAIL_ALLOWED, extra_chars: str = ""):
    """「標題不可用這些標點結尾」的樣式：清單上（內建＋使用者新增）沒有被允許的字。
    正式標題用引號收尾（「第1章「起點」」）的例外照舊（見 is_valid_auto_title）。"""
    blocked = (set(_TITLE_TAIL_CHARS) | set(extra_chars or "")) - set(allowed_chars or "")
    if not blocked:
        return None
    ellipsis_allowed = "…" in (allowed_chars or "")
    parts = []
    if "." in blocked:
        # 半形的「......」（兩個點以上）是刪節號：刪節號允許時不擋，單獨一個「.」照舊算句號
        blocked.discard(".")
        parts.append(r"(?<!\.)(?<!\. )(?<!\.　)\.(?!\.)" if ellipsis_allowed else r"\.")
    if not ellipsis_allowed:
        parts.append(r"\.(?:[ 　]?\.)+")
    if blocked:
        parts.insert(0, f"[{re.escape(''.join(sorted(blocked)))}]")
    return re.compile(f"(?:{'|'.join(parts)})\\s*$")


class TitleCheck:
    """標題的基本限制：最長幾個字、結尾不能是哪些標點。可以直接當成「標題結尾」的正則傳下去
    （有 search），各處判斷長度時用 title_length_limit() 取上限。"""

    def __init__(self, tail_regex=None, max_length: int = MAX_TITLE_LENGTH):
        self.tail_regex = tail_regex
        self.max_length = max_length

    def search(self, text):
        # 樣式都錨在行尾、最多往前看到句點前兩個字：只拿最後三個字去比，整本十幾萬行不必每行從頭掃
        if self.tail_regex is None:
            return None
        return self.tail_regex.search(text.rstrip()[-3:])


def build_title_check(allowed_chars=DEFAULT_TITLE_TAIL_ALLOWED, extra_chars: str = "",
                      max_length: int = MAX_TITLE_LENGTH) -> TitleCheck:
    return TitleCheck(build_title_tail_regex(allowed_chars, extra_chars), max_length)


def title_length_limit(check) -> int:
    return getattr(check, "max_length", MAX_TITLE_LENGTH)


_SPACE_RUN = re.compile(r"([ 　\t\xa0])\1+")


def too_long_for_title(text: str, limit: int) -> bool:
    """比標題長度上限長。章號跟章名之間塞了一大串空白（排版對齊用）時空白只算一個字，
    短的行不用多做這一步。"""
    if len(text) <= limit:
        return False
    # 正文行幾乎都比上限長、也沒有連續的空白：直接算太長，不用做取代（每一行都會問）
    if "  " not in text and "　　" not in text and "\t\t" not in text and "\xa0\xa0" not in text:
        return True
    return len(text) - sum(match.end() - match.start() - 1 for match in _SPACE_RUN.finditer(text)) > limit


LONG_FORMAL_TITLE_LENGTH = 100
_FORMAL_HEAD = re.compile(rf"第\s*{CN_NUM_PATTERN}\s*[章回節节][ 　\t:：\-—·、]+(?=\S)")


def long_formal_title(text: str, limit: int) -> bool:
    """超過標題長度、但是「第N章＋分隔＋章名」的正式寫法：有些書的章名就是一整段（六七十字），
    放寬到 LONG_FORMAL_TITLE_LENGTH。章名裡有句號、引號開了沒關的是章名跟正文第一句黏在
    同一行，照樣擋。使用者把標題長度調得比預設短，就照設定、不放寬。"""
    if limit < MAX_TITLE_LENGTH or not text.startswith("第"):
        return False
    head = _FORMAL_HEAD.match(text)
    if not head or too_long_for_title(text, max(limit, LONG_FORMAL_TITLE_LENGTH)):
        return False
    body = text[head.end():]
    return ("。" not in body and body.count("“") <= body.count("”") and body.count("「") <= body.count("」")
            and body.count("『") <= body.count("』"))


def is_valid_title(text, invalid_tail_regex):
    """標題的基本限制：不可過長，且不可用句末標點結尾。"""
    if too_long_for_title(text, title_length_limit(invalid_tail_regex)):
        return False
    return not (invalid_tail_regex and invalid_tail_regex.search(text))


def is_valid_auto_title(text, invalid_tail_regex):
    """自動辨識用的標題判斷，比 is_valid_title 多兩種例外。

    例外一：正式表頭以閉合引號結尾，例如『第1章 「起點」』。
    例外二：只有章號、章名寫在下一行的格式，例如『第6章：』。結尾的分隔符
            不是句末標點，而章號前綴本身就是夠強的結構證據。
    """
    # 長度先看（整本每一行都會問，只算一次），沒超過再照 is_valid_title 看結尾
    limit = title_length_limit(invalid_tail_regex)
    if too_long_for_title(text, limit) and not long_formal_title(text, limit):
        return False
    if not (invalid_tail_regex and invalid_tail_regex.search(text)):
        return True

    lv2 = parse_lv2(text)
    strong_lv2 = bool(lv2 and not is_weak_numbered_title(text))
    lv1 = parse_lv1(text)
    special = parse_special(text)

    if CLOSING_QUOTE_TAIL_REGEX.search(text) and (lv1 or special or strong_lv2):
        return True
    if strong_lv2 and not strip_title_body(lv2[5]):
        return True
    if lv1 and not strip_title_body(lv1[4]):
        return True
    if special and not strip_title_body(special[3]):
        return True
    return False


def looks_like_heading(text: str, max_length: int = MAX_TITLE_LENGTH) -> bool:
    """有章號（第N章，不含弱格式）、卷號或序章這類開頭，而且不太長：看起來像標題。
    「辨識格式」各頁數「未收錄」用（快取在 heading_word，長度上限可以改）。"""
    if too_long_for_title(text, max_length) and not long_formal_title(text, max_length):
        return False
    return heading_word(text) is not None and not not_a_heading(text)


# 自動辨識認得的字：（代號, 顯示, 這一組包含的寫法）。使用者可以在「辨識章節」
# 關掉其中幾個，例如關掉「節」「部」，正文裡的「第一節課」「第一部手機」就不會被當成章節。
CHAPTER_WORDS = (("章", "第N章", ("章",)), ("回", "第N回", ("回",)), ("節", "第N節", ("節", "节")),
                 ("折", "第N折", ("折",)), ("幕", "第N幕", ("幕",)), ("番外", "番外", ("番外",)))
VOLUME_WORDS = (("卷", "第N卷", ("卷",)), ("部", "第N部", ("部",)), ("篇", "第N篇", ("篇",)),
                ("集", "第N集", ("集",)), ("季", "第N季", ("季",)),
                ("外傳", "外傳", ("外傳", "外传")), ("終章", "終章", ("終章", "终章")))
SPECIAL_WORDS = (("序章", "序章", ("序章",)), ("序言", "序言", ("序言",)), ("序", "序", ("序",)),
                 ("前言", "前言", ("前言",)), ("楔子", "楔子", ("楔子",)), ("引子", "引子", ("引子",)),
                 ("簡介", "簡介", ("簡介", "简介", "內容簡介", "内容简介", "人物簡介", "人物简介")),
                 ("後記", "後記", ("後記", "后记")), ("尾聲", "尾聲", ("尾聲", "尾声")),
                 ("間章", "間章", ("間章", "间章")))
# 可以在「辨識章節 → 特殊標題」改成卷或章的字，與預設的層級（1＝卷、2＝章）。序章這類預設「2」的
# 是特殊標題（掛在目前的卷底下）；改成卷時後面的章會掛在它底下。
SPECIAL_LEVELS = {**{key: 2 for key, _label, _variants in SPECIAL_WORDS}, "番外": 2, "外傳": 1, "終章": 1}
_WORD_KEY = {variant: key for key, _label, variants in CHAPTER_WORDS + VOLUME_WORDS + SPECIAL_WORDS
             for variant in variants}
# heading_word 認得的標題一定有這些字：章、卷的單位前面要有「第」，不用「第」的只有番外、外傳、終章
# 與特殊標題。沒有的行不用跑整套標題正則（大檔的正文幾乎都是這種）。
_NUMBERED_UNITS = {"章", "回", "節", "折", "幕", "卷", "部", "篇", "集", "季"}
_WORD_HINT_REGEX = re.compile("|".join(sorted(
    map(re.escape, {"第"} | {variant for key, _label, variants in CHAPTER_WORDS + VOLUME_WORDS + SPECIAL_WORDS
                             if key not in _NUMBERED_UNITS for variant in variants}),
    key=len, reverse=True)))


# 不是標題的行：作者的話「PS：第五部總結…」、卷尾的一句話「第三卷……到此結束，下一章開始新的一卷」、
# 名稱是作者的話的卷「第一部總結兼請假」。自動辨識不收（人工收錄、自訂規則照舊）。
_PS_PREFIX = re.compile(r"^p\.?s\.?\s*[:：]", re.IGNORECASE)
_END_SENTENCE = re.compile(r"(?:到此|至此)(?:结束|結束|完结|完結|终|終|告一段落)|下一[章卷集部篇](?:开始|開始)")
_NOTE_TITLE_WORDS = re.compile(r"请假|請假|月票|求票|推荐票|推薦票|保底|感言|订阅|訂閱|^总结|^總結")


# 單位字跟後面的字合起來是一個詞：「第二部分，是…」「第一集團軍」「第三季度」不是卷，
# 「第三回合」「第一節課」「第二節自習課」「第一節晚自習」「第五節車廂」是正文的句子開頭，不是章節
_UNIT_WORD = re.compile(r"^[\s【\[(（]*第\s*[0-9０-９一二兩两三四五六七八九十百千萬万〇零" + CN_UPPER_DIGITS + r"]{1,8}\s*"
                        r"(?:部[分门門队隊长長落位]|集[团團中合体體]|篇幅|卷[入起子轴軸]|季[度节節末赛賽]|回合"
                        r"|[节節](?:[一-鿿]{0,2}[课課]|晚自[习習]|[车車][厢廂]))")


# 單位後面直接接只會出現在句子中間的詞、後面還有逗號：「第三章會晚一點，先去山路」「第一章就寫好了，…」
_SENTENCE_AFTER_UNIT = re.compile(r"^[\s【\[(（]*第\s*[0-9０-９一二兩两三四五六七八九十百千萬万〇零" + CN_UPPER_DIGITS + r"]{1,8}\s*[章回節节]"
                                  r"(?:會|会|就|的時候|的时候|已經|已经)[^，,]{0,15}[，,]")


# 季 is also a surname: 「第一季點頭，道：…」 is a character named 第一季. A season heading is written
# 「第一季」「第一季 山路」「第一季：山路」, never with the text glued to the unit
_GLUED_SEASON = re.compile(r"^[\s【\[(（]*第\s*[0-9０-９一二兩两三四五六七八九十百千萬万〇零" + CN_UPPER_DIGITS + r"]{1,8}\s*季"
                           r"(?![完終终結结])[一-鿿A-Za-z]")


def not_a_heading(text: str) -> bool:
    if _PS_PREFIX.match(text) or _END_SENTENCE.search(text) or _UNIT_WORD.match(text) \
            or _SENTENCE_AFTER_UNIT.match(text) or _GLUED_SEASON.match(text):
        return True
    volume = parse_lv1(text)
    if volume and not parse_lv2(text):
        return bool(_NOTE_TITLE_WORDS.search(strip_title_body(volume[4])))
    return False


def word_key(unit: str):
    """單位的寫法（节、外传…）對到「辨識格式」開關用的代號（節、外傳…）。"""
    return _WORD_KEY.get(unit)


def may_have_heading_word(text: str) -> bool:
    return _WORD_HINT_REGEX.search(text) is not None


@lru_cache(maxsize=1 << 18)
def heading_word(text: str):
    """自動辨識會照哪個字把這一行當成標題（照目錄辨識的順序：卷 → 章 → 特殊標題）；不是就回傳 None。"""
    if not may_have_heading_word(text):
        return None
    volume = parse_lv1(text)
    if volume:
        return _WORD_KEY.get(volume[3])
    chapter = parse_lv2(text)
    if chapter and not is_weak_numbered_title(text):
        return "番外" if chapter[2] == "番外" else _WORD_KEY.get(chapter[4])
    special = parse_special(text)
    if special:
        return _WORD_KEY.get(special[2]) or _WORD_KEY.get(special[2].lower())
    return None


def heading_words(text: str) -> set:
    """這一行標題用到的所有單位（「第一卷 山路 第一章」有卷和章兩個）。
    「辨識格式」關掉其中任何一個，這一行就不自動當成標題。"""
    if not may_have_heading_word(text):
        return set()
    keys = set()
    volume = parse_lv1(text)
    if volume:
        keys.add(_WORD_KEY.get(volume[3]))
    chapter = parse_lv2(text)
    if chapter and not is_weak_numbered_title(text):
        keys.add("番外" if chapter[2] == "番外" else _WORD_KEY.get(chapter[4]))
    special = parse_special(text)
    if special:
        keys.add(_WORD_KEY.get(special[2]) or _WORD_KEY.get(special[2].lower()))
    keys.discard(None)
    return keys


@lru_cache(maxsize=1 << 18)
def heading_number(text: str):
    """正式章號（第N章／節…，不含弱格式）的號碼；不是就回傳 None。缺章檢查找「原文有、沒收錄」的章用。"""
    if "第" not in text or len(text) > 200:
        return None
    parsed = parse_lv2(text)
    if not parsed or parsed[2] != "第" or not parsed[3] or is_weak_numbered_title(text):
        return None
    return int(parsed[3]) if float(parsed[3]).is_integer() else None


def parse_special(line):
    m = COMBO_SPECIAL_REGEX.match(line)
    if m:
        fields = m.groupdict(default="")
        return (_clean_arc(fields["arc"]), fields["volume"], fields["tag"], fields["title"])
    return None


# 「連續編號」只信任這三種能明確標出 number 群組位置的標準格式；自訂規則、
# 弱格式、不含編號的番外等一律回傳 None，交由呼叫端略過，不冒然改寫。
_CHAPTER_NUMBER_REGEXES = (COMBO_LV2_REGEX, COMBO_LV2_EXTRA_REGEX, COMBO_LV2_NUM_REGEX)


def looks_like_auto_chapter(text, user_rules=()):
    """這一行沒有任何標記時，自動辨識會不會把它當成章節？

    用來決定「把某一行移出目錄」要不要真的寫 [::X]：如果它本來就不會被
    認成章節（例如使用者手動用 [::] 加進來的一段正文），那只要把標記拿掉
    就夠了，不必在檔案裡留一個沒有意義的 [::X]。

    弱格式（「6.標題」這種純數字開頭）在這裡算「不是章節」：它們要在
    「疑似章節」確認過才會收進目錄，光靠格式不會自動成章。"""
    from .user_rules import match_user_chapter_rule   # 延後匯入，避免循環相依

    clean = text.strip()
    if not clean:
        return False
    if user_rules and match_user_chapter_rule(clean, user_rules):
        return True
    if parse_lv1(clean) or parse_special(clean) or parse_mixed_volume_chapter_header(clean):
        return True
    if is_weak_numbered_title(clean):
        return False
    return bool(parse_lv2(clean))


def locate_chapter_number(text):
    """在標題文字中找出章節／卷編號子字串的確切範圍，供原地替換數字用。

    先試章節（章／回／節／折／幕／番外／Chapter N），再試卷層級（卷／集／
    篇／部）；卷層級有兩種語序：「第X卷」（number 群組）或「卷X」
    （trailing_number 群組），要分開處理。
    """
    for regex in _CHAPTER_NUMBER_REGEXES:
        match = regex.match(text)
        if not match:
            continue
        try:
            start, end = match.start("number"), match.end("number")
        except (IndexError, re.error):
            continue
        number_text = match.group("number")
        if not number_text:
            continue
        return start, end, number_text
    match = LV1_A_REGEX.match(text)
    if match:
        if match.group("number"):
            start, end, number_text = match.start("number"), match.end("number"), match.group("number")
        elif match.group("trailing_number"):
            start, end = match.start("trailing_number"), match.end("trailing_number")
            number_text = match.group("trailing_number")
        else:
            return None
        if not number_text:
            return None
        return start, end, number_text
    return None


ARABIC_NUMBER_REGEX = re.compile(r"[0-9０-９]+(?:[.．]\d+)?")
ARABIC_TITLE_REGEX = re.compile(r"第\s*[0-9０-９]+\s*[部卷篇集季章回節节折幕]")


def uses_arabic_numerals(text):
    """判斷一段標題文字採用的是阿拉伯數字還是中文數字。"""
    if not text:
        return False
    if ARABIC_NUMBER_REGEX.fullmatch(text):
        return True
    return bool(ARABIC_TITLE_REGEX.search(text))


def render_chapter_number_like(new_number, sample_text):
    """依照樣本採用的數字系統輸出新編號文字。"""
    number = int(new_number)
    return str(number) if uses_arabic_numerals(sample_text) else arabic_to_chinese(number)


_DIGIT_WISE = re.compile(r"[〇零一二三四五六七八九两兩]+")


def ten_as_zero_reading(number_text):
    """有的作者把「十」寫成「零」：一百一十一 → 一一零一、一百三十九 → 一三零九。
    逐位寫法、最後是「零＋一個數字」時回傳這種讀法的值，其他寫法回傳 None。
    單看號碼分不出是 1101 還是 111，要照前後章決定（resolve_chapter_number）。"""
    text = (number_text or "").strip()
    if len(text) < 3 or not _DIGIT_WISE.fullmatch(text) or text[-2] not in "零〇" or text[-1] in "零〇":
        return None
    return int(chinese_to_arabic(text[:-2])) * 10 + int(chinese_to_arabic(text[-1]))


_NOISE_DOTS = re.compile(r"[.．]")


def resolve_chapter_number(number, number_text, previous):
    """號碼有兩種讀法時，選跟前一章（previous）接得比較上的那個。

    網站防轉載會在章號裡插句點（「第1.817章」）：拿掉句點剛好接上前一章（第 1816 章）才當成干擾，
    其他的小數章（「第1.5章」）照舊。"""
    if previous and number_text and not float(number).is_integer():
        digits = _NOISE_DOTS.sub("", number_text.strip()).translate(_FULLWIDTH_DIGITS)
        if digits.isdigit() and int(digits) == previous + 1:
            return float(int(digits))
    alternative = ten_as_zero_reading(number_text)
    if alternative is None or not previous:
        return number
    expected = previous + 1
    return alternative if abs(alternative - expected) < abs(number - expected) else number


def original_number_text(text, unit):
    """取出標題中該單位對應的原始編號文字，供保留原本的數字系統。"""
    match = re.search(r"(?:第|番外)\s*(" + CN_NUM_FLOAT_PATTERN + r"(?:\s*" + RANGE_SEP + r"\s*" + CN_NUM_PATTERN
                      + r")?)\s*" + re.escape(unit), text)
    if match:
        return match.group(1)
    weak = parse_weak_numbered_title(text)
    return weak["number_text"] if weak else None


def compact_toc_label(text):
    """只精簡畫面文字，不改動原標題、輸出內容或跳轉位置。"""
    value = re.sub(r"\s+", " ", text).strip()
    mixed = parse_mixed_volume_chapter_header(value)
    if mixed:
        return f"第{mixed['chapter_number_text']}{mixed['chapter_unit']} {mixed['chapter_body']}".strip()
    # 移除章號之前重複的作品名／卷名，但保留章號本身。
    chapter = re.search(r"(第\s*" + CN_NUM_FLOAT_PATTERN + r"\s*[章回節节折幕].*)$", value)
    if chapter and chapter.start() > 0:
        return chapter.group(1).strip()
    return value


def compact_number_ranges(numbers):
    if not numbers:
        return ""
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return "、".join(ranges)


def suggest_number(previous, following):
    if previous and following:
        if previous["number"] + 1 < following["number"]:
            return previous["number"] + 1
        if following["number"] > 1:
            return following["number"] - 1
        return previous["number"] + 1
    if following:
        return max(1, following["number"] - 1)
    if previous:
        return previous["number"] + 1
    return 1


def chapter_unit_signature(text):
    """回傳編號所屬的「類型」簽章，例如 ("第","章")、("卷類","卷")、("卷類","集")。

    「連續編號」不能把不同類型混在一起重排——例如「第1集」跟「第1章」是
    兩套完全獨立的編號系統，「第1卷」跟「第1集」也是各自獨立，硬要接起來
    變成連續數字沒有意義。找不到就回傳 None；呼叫端會把 None 當成「跟任何
    簽章都不同」，保守地不允許混選。
    """
    match = COMBO_LV2_REGEX.match(text)
    if match:
        return ("第", match.group("unit"))
    match = COMBO_LV2_EXTRA_REGEX.match(text)
    if match:
        return ("番外", match.group("unit") or "")
    match = COMBO_LV2_NUM_REGEX.match(text)
    if match:
        return ("Chapter", "")
    match = LV1_A_REGEX.match(text)
    if match:
        unit = match.group("unit") or match.group("leading_unit")
        if unit:
            return ("卷類", unit)
    return None


# 標題清理與空白正規化共用的樣式，避免同一份規則散落在多處。
TITLE_LEAD_SEP_REGEX = re.compile(r"^[\s，,、:：．.\-—·]+")
TITLE_TAIL_SEP_REGEX = re.compile(r"[\s。、]+$")
INLINE_SPACE_REGEX = re.compile(r"[ \t　]+")


def strip_title_body(text):
    """統一既有的標題尾端清理規則，並一併去除開頭殘留的分隔符。

    章節正則的分隔字元類別沒有涵蓋「：」「.」「—」，這些符號會被吃進
    title 群組。若不在這裡清掉，「第一章：」會被判定為「已有章名」，
    導致「合併下行標題」與幽靈標題偵測都不會啟動。
    """
    text = TITLE_LEAD_SEP_REGEX.sub("", text.strip())
    return TITLE_TAIL_SEP_REGEX.sub("", text).strip()


def preserve_title_separator(title, original):
    """在章號轉換或合集去前綴之後，還原原本的章號／章名間隔。"""
    def parts(text):
        for regex in (COMBO_LV2_REGEX, COMBO_LV2_EXTRA_REGEX, COMBO_LV2_NUM_REGEX,
                      LV1_A_REGEX, LV1_B_REGEX, COMBO_SPECIAL_REGEX):
            match = regex.match(text)
            if not match:
                continue
            ends = [match.end(name) for name in ("number", "trailing_number", "unit", "leading_unit", "tag")
                    if name in match.groupdict() and match.group(name) is not None]
            end = max(ends)
            gap = re.match(r"[ \t　:：·、\-—]*", text[end:]).group()
            return text[:end], gap, text[end + len(gap):]
        return None
    new, old = parts(title), parts(original)
    if new is None or old is None or not new[2]:
        return title
    # 無同行章名時沒有可保留的間隔；合併下行章名沿用產生的間隔。
    return new[0] + (old[1] if old[2] else new[1]) + new[2]


def extract_author_from_intro(lines):
    """從第一個正式卷／章之前的簡介區擷取「作者：名稱」。"""
    author_regex = re.compile(r"(?:^|[《》【】\s])(?:作者|作\s*者)\s*[：:]\s*([^\r\n]{1,80})")
    for raw_line in lines[:500]:
        line = raw_line.strip()
        if parse_lv1(line) or parse_lv2(line):
            break
        match = author_regex.search(line)
        if not match:
            continue
        author = match.group(1)
        author = re.split(r"[|｜]", author, maxsplit=1)[0]
        author = re.split(r"\s+(?:書名|作品|類型|类型|狀態|状态)\s*[：:]", author, maxsplit=1)[0]
        author = author.strip(" \t　，,。；;【】[]（）()《》")
        if author:
            return author
    return ""


def clean_merged_subtitle(text):
    """以共用弱格式規則移除「一、」「1.」「#1 標題」等編號。"""
    weak = parse_weak_numbered_title(text)
    if not weak:
        return text.strip(), False
    return weak["body"], True


# 卷的寫法兩種：「第一卷」或「卷一」（每章標題前面都帶著卷，例如「卷一 山路 第一章 出發」）；
# 卷號後面可以有一段括號附註（「卷十二（终卷） 歸途 第一章 …」）。
# 「卷一」這種寫法只在「自動補齊卷號與卷名」開著時才拆（short_volume=True）；
# 平常整行當一章。卷名與章號之間可以沒有空白（「第四卷风流第729节」）。
MIXED_VOLUME_CHAPTER_REGEX = re.compile(
    r"^\s*(?P<vraw>第\s*(?P<vnum1>" + CN_NUM_PATTERN + r")\s*(?P<vunit1>[部卷篇集季])"
    r"|(?P<vunit2>[部卷篇])\s*(?P<vnum2>" + CN_NUM_PATTERN + r")(?=[\s（(]))"
    r"\s*(?P<vnote>[（(][^（）()]{1,6}[）)])?\s*"
    r"(?P<vbody>.{0,30}?)\s*"
    r"(?P<craw>第\s*(?P<cnum>" + CN_NUM_PATTERN + r")\s*(?P<cunit>[章回節节折幕])\s*(?P<cbody>.*?))\s*$",
    re.IGNORECASE,
)


_MIXED_PAREN_REGEX = re.compile(
    r"^\s*(?P<vraw>第\s*(?P<vnum>" + CN_NUM_PATTERN + r")\s*(?P<vunit>[部卷篇集季]))\s*"
    r"[（(]\s*(?P<cnum>" + CN_NUM_PATTERN + r")\s*[、.．]\s*(?P<cbody>[^（）()]{1,30})[）)]\s*$")
_WHOLE_BRACKET = re.compile(r"^\s*[【\[]\s*(.+?)\s*[】\]]\s*$")

# 混合表頭的正則有好幾段可以吃空白的地方，異常長的行（例如一行裡有幾千個
# 空白）會反覆回溯好幾秒；真正的表頭不會這麼長，超過就不解析。
MIXED_HEADER_MAX_LENGTH = 120


def parse_mixed_volume_chapter_header(text, short_volume=False):
    """解析「第一卷 青雲篇 第01章 初入山門（修）」式混合表頭。
    short_volume=True 時也認「卷一 青雲篇 第一章 …」。"""
    # 底下每一種寫法都有「第」：大部分正文行在這裡就結束，不必再看括號、外框
    if len(text) > MIXED_HEADER_MAX_LENGTH or "第" not in text:
        return None
    paren = _MIXED_PAREN_REGEX.match(text) if "（" in text or "(" in text else None
    if paren:
        # 「第一卷（一、山路）」：卷號後面括號裡是這一章的號碼與章名
        number_text, body = paren.group("cnum"), paren.group("cbody").strip()
        return {"volume_raw": re.sub(r"\s+", "", paren.group("vraw")), "volume_note": "",
                "volume_number_text": paren.group("vnum"), "volume_number": int(chinese_to_arabic(paren.group("vnum"))),
                "volume_unit": paren.group("vunit"), "volume_body": "",
                "chapter_raw": f"第{number_text}章 {body}", "chapter_number_text": number_text,
                "chapter_number": int(chinese_to_arabic(number_text)), "chapter_unit": "章", "chapter_body": body}
    # 整行包在【】裡（「【第三集·第一章】」）：拿掉外框再比對。
    # 每一行都會走到這裡：先看第一個字，也不用 sub(r"\1")（帶反斜線的取代字串每次都會觸發 import）
    if text.lstrip()[:1] in ("【", "["):
        whole = _WHOLE_BRACKET.match(text)
        if whole:
            text = whole.group(1)
    match = MIXED_VOLUME_CHAPTER_REGEX.match(text)
    if not match or (match.group("vunit2") and not short_volume):
        return None
    volume_raw = re.sub(r"\s+", "", match.group("vraw"))
    volume_number_text = match.group("vnum1") or match.group("vnum2")
    volume_unit = match.group("vunit1") or match.group("vunit2")
    volume_note = match.group("vnote") or ""
    volume_body = match.group("vbody").strip(" \t　:：-—·・•▪")
    chapter_raw, chapter_number_text = match.group("craw"), match.group("cnum")
    chapter_unit, chapter_body = match.group("cunit"), match.group("cbody").strip()
    # 混合表頭常附下載站版本字樣；若下一行有正式表頭，後續仍會優先採用下一行。
    chapter_body = re.sub(r"\s*[（(]\s*(?:修|修改|校對|校对|補|补)\s*[）)]\s*$", "", chapter_body).strip()
    return {
        "volume_raw": volume_raw,
        "volume_note": volume_note,
        "volume_number_text": volume_number_text,
        "volume_number": int(chinese_to_arabic(volume_number_text)),
        "volume_unit": volume_unit,
        "volume_body": volume_body,
        "chapter_raw": chapter_raw,
        "chapter_number_text": chapter_number_text,
        "chapter_number": int(chinese_to_arabic(chapter_number_text)),
        "chapter_unit": chapter_unit,
        "chapter_body": chapter_body,
    }
