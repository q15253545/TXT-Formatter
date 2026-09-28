"""模糊掃描廣告候選：網址、發布頁、QQ／微信群、來源署名、重複段落、作品資訊、作者感言。"""

import bisect
import unicodedata
import re
from collections import Counter, defaultdict
from functools import lru_cache
from itertools import compress

from .chapter_parse import parse_lv1, parse_lv2
from .title_markers import strip_persistent_title_marker

# 重複段落：預設「去掉空白後至少 12 個字、整本出現 3 次以上」才列出。
# 掃描視窗的「重複段落」分頁可以調（太短的分段符號「……」「＊＊＊」會被大量列出）。
REPEAT_MIN_LENGTH = 12
REPEAT_MIN_COUNT = 3
REPEAT_MAX_LENGTH = 180
# 連續這麼多行（不算空行）都是重複的段落：整段內容重複放，不是廣告
DUPLICATED_RUN = 6

AD_CATEGORY_LABELS = {
    "url": "網址",
    "publish": "發布頁／下載頁",
    "qq": "QQ／QQ群",
    "wechat": "微信／微信群／公眾號",
    "source": "小說來源／網站名稱",
    "repeat": "重複廣告段落",
    # 論壇轉貼留下的樓層資訊（發表於、只看該作者）、使用者資料表（帖子、積分、金幣…）、評分記錄
    "forum": "論壇轉貼資訊",
    "meta": "作品資訊／分隔線",
    "author_note": "作者感言",
    # 網頁轉存時沒轉回來的字元碼（&#29368;、&nbsp;）：處理方式是換回原字，不是刪行
    "entity": "網頁字元碼、HTML 標籤",
    # 轉存時表情符號之類的字變成「?」（「??今天放學後…」、單獨一行「??」）：拿掉問號，正文留著
    "lost": "轉存遺失的字（??）",
}
# 換字（不是刪行）的類型：本文字色、排版前的廣告提醒都不算它們
FIX_CATEGORIES = frozenset({"entity", "lost"})
# 「作者感言與作品資訊」視窗只看這兩類；其餘（含重複段落）在「掃描無關連內容」視窗。
NOTE_CATEGORIES = ("author_note", "meta")
AD_ONLY_CATEGORIES = tuple(key for key in AD_CATEGORY_LABELS if key not in NOTE_CATEGORIES)

COMMON_TLDS = {
    "com", "cn", "net", "org", "cc", "vip", "top", "xyz", "info",
    "me", "tv", "io", "co", "site", "online", "club", "link", "pro",
}

PUBLISH_WORDS = (
    "發布頁", "发布页", "發佈頁", "下載頁", "下载页", "下載地址", "下载地址",
    "最新地址", "最新網址", "最新网址", "备用网址", "備用網址", "访问地址",
    "訪問地址", "手機閱讀", "手机阅读", "請記住", "请记住", "防失聯", "防失联",
    "網址", "网址", "網站", "网站",
)
QQ_WORDS = ("qq群", "qq 群", "q群", "群號", "群号", "加群", "扣扣群", "内群", "內群", "交流群",
            "进群", "進群", "粉丝群", "粉絲群")
WECHAT_WORDS = (
    "微信群", "微信號", "微信号", "微信", "公眾號", "公众号", "威信群",
)
# 下載站加在檔案開頭、結尾的版權聲明（「僅供個人學習…24小時內刪除」）：算來源，而且是高信心
DISCLAIMER_WORDS = (
    "仅供个人学习", "僅供個人學習", "仅供学习交流", "僅供學習交流", "24小时内删除", "24小時內刪除",
    "版权归原作者", "版權歸原作者", "非法及商业用途", "非法及商業用途", "与制作者无关", "與製作者無關",
    "视改动者为制作人", "視改動者為製作人", "支持订阅正版", "支持訂閱正版", "请支持正版", "請支持正版",
)
SOURCE_WORDS = (
    "本書來自", "本书来自", "本文來自", "本文来自", "本文件來自", "本文件来自",
    "小說下載", "小说下载", "電子書下載", "电子书下载", "由本站整理",
    "本站發布", "本站发布", "更多精彩", "求收藏", "求推薦", "求推荐",
    "资源共享", "資源共享", "免费找书", "免費找書", "全网小说", "全網小說",
) + DISCLAIMER_WORDS
SEPARATOR_CHARS = set("-—－─_=*＊~～·•。.")

# 對照表放在模組層級，避免每次呼叫都重建。
AD_NORMALIZE_TRANS = str.maketrans({
    "。": ".", "．": ".", "｡": ".", "點": ".", "点": ".",
    "／": "/", "：": ":", "＠": "@", "﹒": ".",
    "​": "", "‌": "", "‍": "", "﻿": "",
})
AD_WHITESPACE_REGEX = re.compile(r"\s+")


def normalize_ad_text(text):
    """統一全半形、零寬字元與常見網址混淆符號，供模糊掃描使用。"""
    return unicodedata.normalize("NFKC", text).lower().translate(AD_NORMALIZE_TRANS)


# 大檔有十幾萬行：快取要裝得下整本，改過本文重掃時才只需要算改到的行。
@lru_cache(maxsize=1 << 19)
def compact_ad_text(text):
    return AD_WHITESPACE_REGEX.sub("", normalize_ad_text(text))


# 段落縮排 + 對話引號 = 典型的小說正文特徵。
NARRATIVE_INDENT_REGEX = re.compile(r"^(?:　|[ \t]{2,}|\t)")
NARRATIVE_QUOTE_REGEX = re.compile(r"[「」『』“”\"]")


def _looks_like_narrative(lines, start, end):
    """判斷這個區段是否比較像小說正文，而不是獨立的廣告行。

    廣告行通常是短的、獨立的、沒有縮排也沒有對話；正文則相反。
    只要區段內有任何一行同時具備「段落縮排」與「夠長」，
    或者有對話引號且夠長，就視為正文。
    """
    for index in range(start, min(end + 1, len(lines))):
        line = lines[index]
        stripped = line.strip()
        if len(stripped) < 40:
            continue
        if NARRATIVE_INDENT_REGEX.match(line) or NARRATIVE_QUOTE_REGEX.search(stripped):
            return True
    return False


# --------------------------------------------------------------------------
# 作品資訊行：作者、字數、發表日期與平台、裝飾分隔線
#
# 一律要求「整行就是這個資訊」：行首就是關鍵字、長度不長、句末沒有標點，
# 才不會把正文裡提到作者或日期的句子掃進來。
# --------------------------------------------------------------------------

# 這類資訊行都很短；超過就當成正文。
META_MAX_LENGTH = 40
# 句末標點代表這是一句話，不是一行資訊。
META_SENTENCE_TAIL = re.compile(r"[。！？!?…」』”》\)）]$")

_META_AUTHOR = re.compile(r"^作\s*者\s*[:：]\s*\S.{0,20}$")
_META_WORDCOUNT = re.compile(r"^字\s*[数數]\s*[:：]?\s*[0-9]{2,9}\s*字?$")
# 「2015/07/27发表于：某某论坛」「2018-03-01首发某某网」「首發於 A、B、C」
_META_PLATFORM_WORDS = ("发表于", "發表於", "发表於", "首发", "首發", "发布于", "發佈於",
                        "發布於", "转载自", "轉載自", "原发", "原發")
# 整行就是一個日期：2022年2月20日、2015/07/27、2015-07-27
_META_DATE_PREFIX = re.compile(r"^[0-9]{4}\s*[-/年.]\s*[0-9]{1,2}\s*[-/月.]\s*[0-9]{1,2}\s*日?")
_META_DATE = re.compile(_META_DATE_PREFIX.pattern + r"$")


def _repeated_char_line(text):
    """整行都是同一個字元、而且重複十次以上（＊＊＊＊、──────）。"""
    compact = AD_WHITESPACE_REGEX.sub("", text)
    return len(compact) >= 10 and len(set(compact)) == 1


@lru_cache(maxsize=1 << 19)
def meta_line_kind(text):
    """這一行是作品資訊嗎？回傳（種類, 信心）或 None。照行的內容快取（開檔後預先算好）：
    打開作者感言視窗時整本每一行都要問一次。

    單獨的日期只給「中」信心：日記體小說每一章開頭就是日期，如果給高信心
    而使用者順手按「全選高信心」，整本書的章節開頭就被刪光了。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > META_MAX_LENGTH:
        return None
    if _repeated_char_line(stripped):
        return ("separator", "中")
    if META_SENTENCE_TAIL.search(stripped):
        return None
    # 全形數字、全形冒號都先折成半形再比對。
    norm = unicodedata.normalize("NFKC", stripped)
    if _META_AUTHOR.match(norm):
        return ("author", "高")
    if _META_WORDCOUNT.match(norm):
        return ("wordcount", "高")
    if any(word in norm for word in _META_PLATFORM_WORDS):
        # 還要求這一行以日期或發表用語開頭，整行才真的是「發表資訊」；
        # 否則「他首發了一篇小說」這種正文句子也會被掃進來。
        if _META_DATE_PREFIX.match(norm) or norm.startswith(_META_PLATFORM_WORDS):
            return ("platform", "高")
        return None
    if _META_DATE.match(norm.replace(" ", "")):
        return ("date", "中")
    return None


def looks_like_separator(text):
    compact = compact_ad_text(text)
    return len(compact) >= 5 and all(char in SEPARATOR_CHARS for char in compact)


def find_domain_tokens(compact_text):
    """找出通用網域，不依賴任何特定小說網站名稱。"""
    tokens = []
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-._:/?=&%#@")
    start = None
    for index, char in enumerate(compact_text + " "):
        if char in allowed:
            if start is None:
                start = index
            continue
        if start is not None:
            token = compact_text[start:index].strip("-._:/")
            if "." in token:
                host_source = token.split("://", 1)[-1]
                host_part = host_source.split("/", 1)[0]
                host_part = host_part.rsplit("@", 1)[-1]
                pieces = [piece for piece in host_part.split(".") if piece]
                tld = pieces[-1] if pieces else ""
                is_domain = len(pieces) >= 2 and tld in COMMON_TLDS and any(c.isalpha() for c in pieces[-2])
                # 「10……9……8……7」這種倒數接起來也是四段數字：連續的點不算 IP
                is_ipv4 = (len(pieces) == 4 and ".." not in host_part
                           and all(piece.isdigit() and 0 <= int(piece) <= 255 for piece in pieces))
                if is_domain or is_ipv4:
                    tokens.append(token)
            start = None
    return tokens


_AD_WORD_REGEX = {}


def _words_regex(words):
    regex = _AD_WORD_REGEX.get(words)
    if regex is None:
        normalized = sorted({compact_ad_text(word) for word in words}, key=len, reverse=True)
        regex = _AD_WORD_REGEX[words] = re.compile("|".join(map(re.escape, normalized)))
    return regex


def _compact_contains_any(compact, words):
    return _words_regex(words).search(compact) is not None


def _url_window_end(lines, index):
    """回傳從指定行起、最多五行內組成網址時的最後行號。"""
    first = compact_ad_text(lines[index])
    if find_domain_tokens(first):
        return index
    # 跨行只拼接網址碎片，不能把前面的普通英文／數字正文帶入。
    fragment = re.compile(r"^[a-z0-9:/._?=&%#@+\-]+$")
    publish_context = (_compact_contains_any(first, PUBLISH_WORDS)
                       or _compact_contains_any(first, SOURCE_WORDS))
    if not fragment.fullmatch(first) and not publish_context:
        return None
    parts = "" if publish_context else first
    for end in range(index + 1, min(len(lines), index + 5)):
        next_part = compact_ad_text(lines[end])
        if not next_part or not fragment.fullmatch(next_part):
            break
        # 下一行已是完整網址時，前行須有發布語意才可合併。
        if find_domain_tokens(next_part) and not publish_context:
            return None
        parts += next_part
        if find_domain_tokens(parts):
            return end
    return None


_CIRCLED_DIGITS = set("⓪①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳❶❷❸❹❺❻❼❽❾➀➁➂➃➄➅➆➇➈㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉"
                      "⑴⑵⑶⑷⑸⑹⑺⑻⑼⒈⒉⒊⒋⒌⒍⒎⒏⒐")
_CJK_DIGITS = set("〇零一二三四五六七八九")


def _obfuscated_number(line: str) -> bool:
    """群號故意混用阿拉伯數字、圈圈數字、國字寫成一串（躲過關鍵字過濾）：
    短短一行裡至少 7 個「數字」，而且混了兩種以上寫法（其中一種要是圈圈數字或 〇）。"""
    text = line.strip()
    if not text or len(text) > 30:
        return False
    if "〇" not in text and not any(char in _CIRCLED_DIGITS for char in text):
        return False
    ascii_digits = sum(char.isascii() and char.isdigit() or char in "０１２３４５６７８９" for char in text)
    circled = sum(char in _CIRCLED_DIGITS for char in text)
    cjk = sum(char in _CJK_DIGITS for char in text)
    kinds = sum(1 for count in (ascii_digits, circled, cjk) if count)
    return (ascii_digits + circled + cjk) >= 7 and kinds >= 2 and (circled > 0 or "〇" in text)


_SPEAKER_LINE = re.compile(r"^[^:：“「『\"]{1,8}[:：][“「『\"]")
_SYSTEM_LINE = re.compile(r"^[【\[][^】\]]{1,40}[】\]]$")


def _is_dialogue_or_punct(compact: str) -> bool:
    """整段是一句對話（引號包住、或「名字：『……』」）、整行【系統訊息】、狀聲詞、或只有標點：
    故事裡常常重複出現，但不是廣告（真的廣告另外會被網址、關鍵字抓到）。"""
    if not any(char.isalnum() for char in compact):
        return True
    if _SPEAKER_LINE.match(compact) or _SYSTEM_LINE.match(compact):
        return True
    # 狀聲詞「啪啪啪！！！」「噗滋……噗滋……噗滋……」：去掉標點只剩三種字以內、而且有字重複三次以上
    # （「（本章完）」每個字只出現一次，照樣列）
    letters = Counter(char for char in compact if char.isalnum())
    if len(letters) <= 3 and max(letters.values()) >= 3:
        return True
    return compact[:1] in "“「『\"" and compact[-1:] in "”」』\""


# 只有關鍵字、沒有網址或帳號的類型：出現在長段正文裡就不算
_KEYWORD_ONLY = frozenset({"publish", "wechat", "source"})
# 只提到微信、網站（沒有網址、帳號）的行，要有這種叫人去做什麼的字才是廣告；
# 「手機響起微信提示音」「打開那個網站」是故事
_AD_ACTION = re.compile(r"關注|关注|搜索|搜尋|掃碼|扫码|掃一掃|扫一扫|領取|领取|下載|下载|訪問|访问|記住|记住|點擊|点击"
                        r"|免費|免费|首發|首发|最新章|全文閱讀|全文阅读|手機版|手机版|閱讀網址|阅读网址|網址|网址"
                        r"|加\s*(?:微信|vx|VX|群|qq|QQ|我)|(?:微信|公眾號|公众号|群)\s*[:：]")
_STORY_KEYWORD_TYPES = frozenset({"publish", "wechat"})
# 只看這一行本身就能決定的廣告類型（網址要看後面幾行，另外判斷）
LINE_AD_CATEGORIES = frozenset({"url", "publish", "qq", "wechat", "source"})
_TWO_ASCII = re.compile(r"[a-z0-9][^a-z0-9]*[a-z0-9]")
_LINE_PROFILES: dict = {}
_LINE_PROFILE_LIMIT = 1 << 19


def _line_profile(line):
    """（發布頁, 來源, 微信, QQ, 可能是網址）：只跟這一行的內容有關，照內容快取。"""
    profile = _LINE_PROFILES.get(line)
    if profile is None:
        if len(_LINE_PROFILES) > _LINE_PROFILE_LIMIT:
            _LINE_PROFILES.clear()
        compact = compact_ad_text(line)
        publish = _compact_contains_any(compact, PUBLISH_WORDS)
        source = _compact_contains_any(compact, SOURCE_WORDS)
        qq = ((("qq" in compact or _compact_contains_any(compact, QQ_WORDS))
               and sum(char.isdigit() for char in compact) >= 4) or _obfuscated_number(line))
        profile = (publish, source, _compact_contains_any(compact, WECHAT_WORDS), qq,
                   publish or source or _TWO_ASCII.search(compact) is not None)
        _LINE_PROFILES[line] = profile
    return profile


# 整本逐行的 map／Counter 分批做：一口氣在 C 裡跑完十幾萬行會一直握著 GIL，
# 本文標示在背景執行緒掃描時，畫面就被卡住；分批之間畫面才有機會跑
_CHUNK = 4096


def _map_in_chunks(function, items) -> list:
    result = []
    for start in range(0, len(items), _CHUNK):
        result.extend(map(function, items[start:start + _CHUNK]))
    return result


_NO_FEATURES = frozenset()


def _ad_line_features(lines, index, enabled_categories):
    """這一行的廣告類型；網址可跨最多三行並容許空白拆分。"""
    publish, source, wechat, qq, url_context = _line_profile(lines[index])
    if not (publish or source or wechat or qq or url_context):
        return _NO_FEATURES
    features = set()
    if "url" in enabled_categories and url_context and _url_window_end(lines, index) is not None:
        features.add("url")
    for key, hit in (("publish", publish), ("qq", qq), ("wechat", wechat), ("source", source)):
        if hit and key in enabled_categories:
            features.add(key)
    return features


# --------------------------------------------------------------------------
# 作者感言：章末的「作者有話說」、PS、上架感言、分隔線後面的閒聊、括號裡的附註
#
# 這類文字大多接在一章的最後面、下一章標題之前，所以從觸發的那一行一路收到
# 下一個章節標題為止。分隔線也常被當成場景切換用，只有後面那段帶著「更新、
# 訂閱、讀者、感謝…」這類詞才算；括號附註只收那幾行（常出現在章首），不往下延伸。
# --------------------------------------------------------------------------

# 比對前先做 NFKC＋小寫＋去空白，全形半形、大小寫都一樣看待。
_NOTE_HEADER_REGEX = re.compile(
    r"^(?:作者有(?:话|話)(?:说|說)|作者的(?:话|話)|作者(?:感言|按)|(?:上架|完本|完结|完結|单章|單章|新书|新書)感言"
    r"|感言[:：]|写在(?:后面|最后)|寫在(?:後面|最後)|题外话|題外話"
    r"|推(?:荐|薦)?一本|推(?:书|書)[:：]|今晚无更|今晚無更|今天无更|今天無更|[一二三四五六七八九十]{1,2}月(?:总结|總結))")
# 「※※※第二卷结束，老习惯，休息一天……」：分隔符號後面直接接作者的話
_NOTE_LEADING_SEPARATOR = re.compile(r"^[-—–_~=*※☆★◆◇●○]{2,}(?=[^-—–_~=*※☆★◆◇●○])")
_NOTE_PS_REGEX = re.compile(r"^p\.?s\.?[:：.,，、]")
_NOTE_SEPARATOR_REGEX = re.compile(
    r"^[-—–_~=*※☆★◆◇●○]{2,}(?:分割(?:线|線)|分隔(?:线|線)|分界(?:线|線))?[-—–_~=*※☆★◆◇●○]*$")
# 整行被括號包住（後面可以多一個句號之類：「（第5更送上，求订阅……）。」）
_NOTE_PAREN_REGEX = re.compile(r"^[(（【\[].*[)）】\]][。．.！!]?$")
# Asking readers for tips / votes: never a chapter name (also used by the merge-subtitle preview)
SUPPORT_REQUEST_WORDS = ("打赏", "打賞", "月票", "推荐票", "推薦票", "订阅", "訂閱", "求票", "加更")
_NOTE_WORDS = (
    "作者", "读者", "讀者", "书友", "書友", "感谢", "感謝", "谢谢", "謝謝", "订阅", "訂閱", "首订", "首訂",
    "月票", "推荐票", "推薦票", "打赏", "打賞", "收藏", "投票", "加更", "更新", "停更", "断更", "斷更",
    "请假", "請假", "上架", "新书", "新書", "本书", "本書", "这章", "這章", "本章", "下一章", "码字", "碼字",
    "卡文", "灵感", "靈感", "抱歉", "见谅", "見諒", "评论", "評論", "点赞", "點贊", "企鹅", "企鵝", "qq",
    "vip", "主页", "主頁", "私信", "私聊", "简介", "簡介", "后续", "後續", "约稿", "約稿", "金主", "购文",
    "購文", "包书", "包書", "读者群", "讀者群", "订阅群", "訂閱群", "or2", "orz", "大佬",
    # 章末常見的「票~~~」「求票票」「點個推薦」：只在分隔線／括號這種短區塊裡才會用到，
    # 單字「票」不會單獨把正文判成感言。
    "票", "求票", "推荐", "推薦",
    # 請假、斷更、月總結、推書
    "无更", "無更", "补上", "補上", "补更", "補更", "休息一天", "恢复更新", "恢復更新", "总结", "總結",
    "推一本", "推书", "推書",
)
_NOTE_SCAN_LIMIT = 60        # 從觸發行往下找章節標題，最多看幾行
# 章末一條分隔線、後面幾行短短的作者閒聊（「過渡一章」「見諒見諒」「昨晚喝了酒」），
# 沒有關鍵詞也算（中信心）：分隔線後面到下一章之間最多這麼多行、每行不超過這麼長。
_NOTE_TAIL_MAX_LINES = 10
_NOTE_TAIL_MAX_LENGTH = 60
_DIALOGUE_OPENERS = "“「『\"‘"
# 只認一長串破折號／減號／底線（網路小說章末「——————」後面接作者的話的慣例）；
# ＊＊＊、※※※ 這類多半是場景切換，後面接的是正文。
_TAIL_SEPARATOR = re.compile(r"^[—―─━\-－_＿]{4,}$")
_NOTE_MAX_BODY = 30          # 區塊超過這麼多行（不算空行）就不算高信心：可能吃到正文
_NOTE_SEPARATOR_MAX_BODY = 15


def _note_text(line: str) -> str:
    return AD_WHITESPACE_REGEX.sub("", unicodedata.normalize("NFKC", line).lower())


# 觸發行的第一個字只可能是這些（標題型、PS、分隔線、括號）；其他行直接跳過，
# 不必每一行都做全形半形轉換——大檔（十幾萬行）差好幾倍時間。
_NOTE_FIRST_CHARS = frozenset("作上完单單新感写寫题題推今一二三四五六七八九十pPｐＰ-—–_~=*※☆★◆◇●○－＿～＝＊(（【[［")


def _note_trigger(line: str):
    first = line.lstrip()[:1]
    if not first or first not in _NOTE_FIRST_CHARS:
        return None
    text = _note_text(line)
    if not text:
        return None
    if _NOTE_HEADER_REGEX.match(text):
        return "header"
    if _NOTE_PS_REGEX.match(text):
        return "ps"
    leading = _NOTE_LEADING_SEPARATOR.match(text)
    # 至少兩種關鍵詞：正文裡也有「————抱歉，扯远了」這種用破折號開頭的句子
    if leading and _note_word_count([text[leading.end():]]) >= 2:
        return "header"
    # 只認破折號、底線、星號、※ 這類；點（刪節號「……」會變成一串點）不算。
    if _NOTE_SEPARATOR_REGEX.match(text):
        return "separator"
    if _NOTE_PAREN_REGEX.match(text) and len(text) <= 200:
        return "paren"
    return None


def _note_word_count(lines) -> int:
    """出現了幾種關鍵詞。互相包含的只算一次（「月票」裡的「票」不另外算）。"""
    text = "".join(_note_text(line) for line in lines)
    found = [word for word in _NOTE_WORDS if word in text]
    return sum(1 for word in found if not any(word != other and word in other for other in found))


def _is_title_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and bool(parse_lv1(stripped) or parse_lv2(stripped))


def _title_checker(title_rows):
    """有目錄辨識出的標題行（含自訂規則、人工標記）就用它；沒有才自己判斷。
    自己判斷只認標準寫法，「第一百五十九章才不讓你……哼」這種章號後面沒空格的
    會漏掉，章末就找不到。"""
    if title_rows is None:
        return lambda lines, row: _is_title_line(lines[row])
    rows = set(title_rows)
    return lambda _lines, row: row in rows


def _last_before_title(lines, end: int, is_title) -> bool:
    """end 之後（跳過空行）緊接著章節標題或檔尾：這段是一章的最後一段。"""
    for row in range(end + 1, len(lines)):
        if lines[row].strip():
            return is_title(lines, row)
    return True


def _absorb_leading_marks(lines, index, is_title) -> int:
    """作者的話前面緊鄰的分隔線、卷／章結尾行（「※※※」「第八卷完」）一起收進來，最多往前 4 行。"""
    from .title_markers import parse_end_mark
    start = row = index
    while row > 0 and index - row < 4:
        previous = lines[row - 1]
        text = previous.strip()
        if not text:
            row -= 1
            continue
        # 卷／章結尾行（「第八卷完」）先認：它長得像卷標題，但其實是結尾
        if len(text) <= 20 and parse_end_mark(text):
            row -= 1
            start = row
            continue
        if is_title(lines, row - 1):
            break
        if _note_trigger(previous) == "separator":
            row -= 1
            start = row
            continue
        break
    return start


def _tail_chatter(content, boundary, total) -> bool:
    """章末分隔線後面的作者閒聊：分隔線後面到下一章標題之間只有幾行短句，
    而且沒有對話（場景切換後面接的正文常常是對話）、沒有別的分隔線。"""
    if not content or boundary >= total or len(content) > _NOTE_TAIL_MAX_LINES:
        return False
    for line in content:
        text = line.strip()
        if (len(text) > _NOTE_TAIL_MAX_LENGTH or text[0] in _DIALOGUE_OPENERS
                or _note_trigger(line) == "separator"):
            return False
    return True


def author_note_blocks(lines, title_rows=None):
    """找出作者感言，回傳 [(起始行, 結束行, 信心)]（0 起算、結束行含在內）。

    title_rows：目錄辨識出的章節標題行號（0 起算）；None 時自己判斷。"""
    is_title = _title_checker(title_rows)
    blocks = []
    total = len(lines)
    index = 0
    while index < total:
        kind = _note_trigger(lines[index])
        if kind is None or is_title(lines, index):
            index += 1
            continue
        if kind == "paren":
            # 括號附註：連續的括號行算一段，不往下延伸到章末。沒有關鍵詞時，
            # 只有「一章的最後一段」才列（中信心）：章末的括號幾乎都是作者在說話，
            # 正文中間的括號多半是旁白。
            end = index
            while end + 1 < total and _note_trigger(lines[end + 1]) == "paren":
                end += 1
            # 括號後面緊接的分隔線一起收（「（求票）」下一行「------」）
            while end + 1 < total and _note_trigger(lines[end + 1]) == "separator":
                end += 1
            words = _note_word_count(lines[index:end + 1])
            if words:
                blocks.append((index, end, "高" if words >= 2 else "中"))
            elif _last_before_title(lines, end, is_title):
                blocks.append((index, end, "中"))
            index = end + 1
            continue
        # 其他三種：一路收到下一個章節標題（或檔尾）為止。
        boundary = None
        for row in range(index + 1, min(total, index + 1 + _NOTE_SCAN_LIMIT)):
            if is_title(lines, row):
                boundary = row
                break
        if boundary is None and index + 1 + _NOTE_SCAN_LIMIT >= total:
            boundary = total
        if boundary is None:
            # 附近沒有章節標題：這不在章末，可能只是正文裡提到。
            # 標題型與 PS 只收那一行、給中信心；分隔線不算。
            if kind in ("header", "ps"):
                blocks.append((index, index, "中"))
            index += 1
            continue
        end = boundary - 1
        while end > index and not lines[end].strip():
            end -= 1
        body = [line for line in lines[index:end + 1] if line.strip()]
        words = _note_word_count(lines[index:end + 1])
        if kind == "separator":
            # 分隔線也是常見的場景切換：後面接的若是一大段正文，正文裡剛好出現
            # 「推薦」「收藏」「票」一兩個詞並不代表是作者在說話。內容超過 5 行時
            # 至少要 3 種關鍵詞才算。
            # 另外，分隔線後面第一段就要是作者在說話（有關鍵詞，或本身是 PS／括號附註）：
            # 場景切換後面第一段幾乎都是正文敘述。
            # 作者的話幾乎每行都在講更新、票、感謝；有關鍵詞的行不到一半時，
            # 比較像是正文剛好提到這些字。
            content = [line for line in body if _note_trigger(line) != "separator"]
            opens_as_note = bool(content) and (_note_word_count(content[:1]) > 0
                                               or _note_trigger(content[0]) in ("header", "ps", "paren"))
            noted_lines = sum(1 for line in content if _note_word_count([line]))
            # 頭尾都是分隔線、而且緊接著下一章（boundary 就是標題）：作者用分隔線把一大段話
            # 框起來（月總結、請假說明），長度與「每行都有關鍵詞」的限制放寬。
            framed = (len(body) >= 3 and _note_trigger(body[-1]) == "separator"
                      and boundary < total and opens_as_note and words >= 3)
            if framed and len(body) <= _NOTE_MAX_BODY * 2:
                blocks.append((index, end, "中" if len(body) > _NOTE_SEPARATOR_MAX_BODY else "高"))
                index = end + 1
                continue
            if (not words or len(body) > _NOTE_SEPARATOR_MAX_BODY or not opens_as_note
                    or noted_lines * 2 < len(content)
                    or (len(content) > 5 and words < 3)):
                if _TAIL_SEPARATOR.match(lines[index].strip()) and _tail_chatter(content, boundary, total):
                    blocks.append((_absorb_leading_marks(lines, index, is_title), end, "中"))
                    index = end + 1
                    continue
                index += 1
                continue
            confidence = "高" if words >= 3 else "中"
        elif kind == "header":
            confidence = "高" if len(body) <= _NOTE_MAX_BODY else "中"
        else:                                   # ps
            confidence = "高" if words or len(body) <= 8 else "中"
            if len(body) > _NOTE_MAX_BODY:
                confidence = "低"
        start = _absorb_leading_marks(lines, index, is_title)
        blocks.append((start, end, confidence))
        index = end + 1
    return blocks


# 論壇轉貼時夾在正文裡的帖子標頭：「書名 續章330」「作者名 2018-01-24 23:49:45 舉報 閱讀數：24116」
# 「大家好，我還是作者名」。有日期時間、又有舉報／閱讀數／回覆這類論壇用語的行才算。
_FORUM_DATETIME = re.compile(r"(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}日?\s+\d{1,2}:\d{2}(?::\d{2})?")
_FORUM_WORDS = ("举报", "舉報", "阅读数", "閱讀數", "阅读", "閱讀", "回复", "回覆", "楼主", "樓主", "只看该作者",
                "只看該作者", "发表于", "發表於", "点击", "點擊")


_FORUM_SENTENCE_TAIL = re.compile(r"[。！？!?…」』”]$")


_HAS_NUMBER = re.compile(r"[0-9０-９]|[一二三四五六七八九十百千]{1,6}[章回節节卷集]")


def looks_like_forum_header(line: str) -> bool:
    """A repost header line: date-time plus a forum word (report / view count / reply...)."""
    if len(line.strip()) > 80:
        return False
    return bool(_FORUM_DATETIME.search(line)) and any(word in line for word in _FORUM_WORDS)


def forum_header_blocks(lines, title_rows=None):
    """回傳 [(起始行, 結束行)]：日期時間＋論壇用語那一行，連同前一行（短、不是句子）
    與後面提到同一個名字的行。章節標題（含自訂規則、人工標記）不會被包進來。"""
    is_title = _title_checker(title_rows)
    blocks = []
    for row, line in enumerate(lines):
        if not looks_like_forum_header(line):
            continue
        match = _FORUM_DATETIME.search(line)
        start = end = row
        name = line[:match.start()].strip()
        previous = row - 1
        if previous >= 0 and lines[previous].strip():
            text = lines[previous].strip()
            # 只看真正的句末標點：標頭常以「（包月10）」這種括號結尾，不能因此排除。
            # A numbered line above the header ("title 52", "title sequel 330") is the post's chapter heading:
            # deleting it would lose the chapter boundary, so it stays (a custom special title can pick it up).
            if (len(text) <= 40 and not _FORUM_SENTENCE_TAIL.search(text)
                    and not is_title(lines, previous) and not _is_title_line(text)
                    and not _HAS_NUMBER.search(text)):
                start = previous
        for following in range(row + 1, min(len(lines), row + 3)):
            text = lines[following].strip()
            if not text:
                continue
            if is_title(lines, following) or _is_title_line(text):
                break
            # Only a line of nothing but lost characters ("??") belongs to the header; "??" followed by text
            # is the first paragraph of the post (the lost-character scan strips the marks instead).
            if ((name and name in text and len(text) <= 60) or not text.strip("?？")
                    or (text.startswith("大家好") and len(text) <= 60)):
                end = following
            else:
                break
        blocks.append((start, end))
    return blocks


_POST_NOTE_MAX_LENGTH = 80


def forum_post_notes(lines, header_blocks, title_rows=None):
    """帖子標頭（forum_header_blocks）後面第一行是作者的閒聊（「求打賞」「今天先更這本」），
    正文從下一行開始：[(起始行, 結束行, 信心)]。下一行開頭是轉存丟掉的字（「??」，作者貼文時
    放在正文開頭的表情符號）就是分界；沒有這個記號時要有兩種以上作者用語才算。"""
    is_title = _title_checker(title_rows)
    total = len(lines)

    def next_text(row):
        for following in range(row + 1, min(total, row + 4)):
            if lines[following].strip():
                return following
        return None

    notes = []
    for _start, end in header_blocks:
        row = next_text(end)
        if row is None or is_title(lines, row):
            continue
        text = lines[row].strip()
        if (len(text) > _POST_NOTE_MAX_LENGTH or text[0] in _DIALOGUE_OPENERS or text.startswith("?")
                or _is_title_line(text)):
            continue
        after = next_text(row)
        marked = after is not None and lines[after].lstrip().startswith("?")
        words = _note_word_count([text])
        if marked:
            notes.append((row, row, "高" if words else "中"))
        elif words >= 2:
            notes.append((row, row, "中"))
    return notes


# 論壇（Discuz 這類）轉貼時，每一樓前後夾著的介面文字：評分紀錄「某人 金币 +100 感谢…」、
# 「引用 使用道具 报告 回复」「TOP 放入宝箱」、使用者名稱、「LEVEL 7」「Rank: 6」、
# 帖子／精华／积分…一欄一行的資料表、「个人空间发短消息加为好友…」「2楼大中小发表于… 只看该作者」。
_FORUM_PROFILE_LABELS = frozenset(
    "帖子 精华 精華 积分 積分 金币 金幣 原创 原創 威望 支持 感谢 感謝 贡献 貢獻 赞助 贊助 推广 推廣 "
    "阅读权限 閱讀權限 注册时间 註冊時間 在线时间 在線時間 最后登录 最後登錄 主题 主題 好友 听众 聽眾 "
    "用户组 用戶組 经验 經驗 等级 等級 UID 性别 性別 来自 來自".split())
_FORUM_VALUE = re.compile(r"^(?:\d+(?:\.\d+)?\s*(?:枚|贴|帖|點|点|度|值|次|人|个|個|分|篇|小时|小時)?"
                          r"|\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)$")
_FORUM_UI = re.compile(
    r"^LEVEL\s*\d+$|^Rank:\s*\d+|个人空间|個人空間|发短消息|發短消息|加为好友|加為好友|当前离线|當前離線|当前在线|"
    r"當前在線|查看宝箱|查看寶箱|使用道具|放入宝箱|放入寶箱|只看该作者|只看該作者|楼大中小|樓大中小|本贴共获得感谢|"
    r"本帖共獲得感謝|点此感谢|點此感謝|(?:金币|金幣|贡献|貢獻|威望|积分|積分)\s*[+＋-]\s*\d+|"
    r"本帖最后由|本帖最後由|评分记录|評分記錄|作者的其他主题|作者的其他主題|^Super Moderator$|^Moderator$|"
    r"^Administrator$|^版主$", re.IGNORECASE)
# 勳章清單、「作者的其他主題」後面那一串作品連結：常常很長（超過一般的行長上限），但一看就是論壇介面
_FORUM_LONG = re.compile(r"(?:勋章|勳章).*(?:勋章|勳章).*(?:勋章|勳章)|【[^】]{1,30}】.{0,40}作者[:：].*作者[:：]")
FORUM_BLOCK_MIN_LINES = 6        # 至少這麼多行論壇用語（其中兩行以上不是單純的數字）才算一段


@lru_cache(maxsize=1 << 19)
def forum_line_strength(line: str) -> int:
    """2＝論壇介面用語（欄位名稱、按鈕列、評分），1＝資料表的值（數字、日期），0＝都不是。
    照行的內容快取（開檔後預先算好），打開作者感言視窗時整本每一行都要問一次。"""
    text = line.strip()
    if not text:
        return 0
    if len(text) > 80:
        return 2 if len(text) <= 1000 and _FORUM_LONG.search(text) else 0
    if text in _FORUM_PROFILE_LABELS or _FORUM_UI.search(text) or _FORUM_LONG.search(text):
        return 2
    return 1 if _FORUM_VALUE.match(text) else 0


def forum_profile_blocks(lines, title_rows=None):
    """回傳 [(起始行, 結束行)]：一段連續的論壇介面文字（中間可以隔空行，也可以夾著前後都是
    論壇用語的短行，例如使用者名稱）。單獨一個數字或日期不算，章節標題一律不包進去。"""
    toc = set(title_rows) if title_rows is not None else set()
    total = len(lines)
    # 整本先一次查好（map 在 C 裡跑），主迴圈只看有論壇用語的行：大檔十幾萬行，一行一行問太慢
    strengths = _map_in_chunks(forum_line_strength, lines)
    for row in toc:
        # 資料表的值（「3062 枚」「1 贴」）長得像「數字空格」的標題：這裡只排除真的在目錄裡的行。
        # 但打開「純數字獨立一行」時，「2071」「70」這種值也會被收進目錄：夾在論壇資料表裡、
        # 又不是使用者手動收錄（[::]）的純數字／日期，還是算資料表的一部分。
        if 0 <= row < total and strengths[row]:
            manual = strip_persistent_title_marker(lines[row].strip())[1] == "include"
            strengths[row] = 1 if strengths[row] == 1 and not manual else 0
    forum_rows = list(compress(range(total), strengths))
    forum_row = strengths.__getitem__

    def next_text_row(row):
        while row < len(lines) and not lines[row].strip():
            row += 1
        return row if row < len(lines) else None

    def sandwiched(row):
        """夾在論壇用語中間的短行（使用者名稱、頭銜，最多連續三行），後面還接著論壇用語。"""
        probe = row
        for _ in range(3):
            text = lines[probe].strip()
            if len(text) > 16 or probe in toc or _is_title_line(text):
                return False
            probe = next_text_row(probe + 1)
            if probe is None:
                return False
            if forum_row(probe):
                return True
        return False

    blocks = []
    row = 0
    while True:
        position = bisect.bisect_left(forum_rows, row)
        if position >= len(forum_rows):
            break
        row = forum_rows[position]
        strength = forum_row(row)
        start = end = row
        count, strong = 1, strength == 2
        cursor = next_text_row(row + 1)
        while cursor is not None:
            strength = forum_row(cursor)
            if not strength:
                if not sandwiched(cursor):
                    break
            else:
                count += 1
                strong += strength == 2
            end = cursor
            cursor = next_text_row(cursor + 1)
        if count >= FORUM_BLOCK_MIN_LINES and strong >= 2:
            blocks.append((start, end))
            row = end + 1
        else:
            row += 1
    return blocks


# 網頁轉存留下的 HTML 標籤（<br>、<p>、</div>、<span style=…>）：整個拿掉，字留著
_HTML_TAG = re.compile(r"</?(?:p|br|div|span|font|b|i|u|em|strong|a|img|hr|center|small|big|sup|sub|tr|td|table)"
                       r"(?:\s[^<>]{0,300})?\s*/?>", re.IGNORECASE)


def entity_candidates(lines) -> list:
    """網頁字元碼：每一行一個候選，帶著換回原字之後的樣子（fix）。章節標題裡的也算
    （「第七卷 我家住在&#32418;土高坡」），只換字、不動其他內容。"""
    from .quote_check import _HTML_ENTITY, _decode_entities
    candidates = []
    for row, line in enumerate(lines):
        has_entity = "&" in line and _HTML_ENTITY.search(line)
        has_tag = "<" in line and _HTML_TAG.search(line)
        if has_entity or has_tag:
            fixed = _decode_entities(_HTML_TAG.sub("", line))
            if fixed != line:
                candidates.append({"start": row, "end": row, "types": {"entity"}, "confidence": "高",
                                   "score": 5, "preview": line, "line": row + 1, "fix": fixed})
    return candidates


# 行首的半形問號：中文裡的問號是全形，行首一串半形「?」後面直接接中文（或整行只有問號）是轉存丟掉的字
_LOST_LEAD = re.compile(r"^([ \t\u3000]*)\?+[ \t]*(?=[^\x00-\x7f]|$)")


def lost_char_candidates(lines) -> list:
    """轉存遺失的字：每一行一個候選，fix 是拿掉行首問號之後的樣子（整行只有問號的換成空行）。"""
    candidates = []
    for row, line in enumerate(lines):
        if line.lstrip(" \t\u3000")[:1] != "?":
            continue
        match = _LOST_LEAD.match(line)
        if not match:
            continue
        rest = line[match.end():]
        marks = match.group(0).count("?")
        # 單獨一個「?」的空行也可能是作者打的：中信心
        confidence = "中" if not rest.strip() and marks == 1 else "高"
        fixed = match.group(1) + rest if rest.strip() else ""
        candidates.append({"start": row, "end": row, "types": {"lost"}, "confidence": confidence,
                           "score": 5 if confidence == "高" else 3, "preview": line, "line": row + 1, "fix": fixed})
    return candidates


def scan_ad_candidates(lines, enabled_categories=None, line_ranges=None, title_rows=None,
                       repeat_min_length=REPEAT_MIN_LENGTH, repeat_min_count=REPEAT_MIN_COUNT):
    """模糊掃描廣告候選，只產生預覽資料，不直接刪除任何內容。

    line_ranges 是「只看這幾段」的行範圍（0 起算的半開區間），給「只掃描選取
    的章節」用。整份還是照掃（重複段落要看整本才數得準），只是最後把落在範圍
    外的候選濾掉，行號因此一律是整份文件裡的絕對行號。"""
    enabled = set(enabled_categories or AD_CATEGORY_LABELS)
    if not lines or not enabled:
        return []
    # compact_ad_text 是純函式，跨掃描保留快取是安全的：使用者常在同一個
    # 對話框裡切換分類重掃，命中快取可省下約一半時間。maxsize 已鎖住記憶體上限。
    candidates = _scan_ad_candidates(lines, enabled - FIX_CATEGORIES, title_rows,
                                     max(1, int(repeat_min_length)), max(2, int(repeat_min_count)))
    candidates = _drop_front_matter(candidates, lines, title_rows)
    if "entity" in enabled:
        # 換字的候選不跟要刪整段的候選合併：刪除與換字是兩種動作。同一行又有文中廣告片段（也是換字）的
        # 併成一個，兩個換字才不會互相蓋掉
        from .quote_check import _decode_entities
        entities = {candidate["start"]: candidate for candidate in entity_candidates(lines)}
        for candidate in candidates:
            if candidate.get("fix") is not None and candidate["start"] in entities:
                entities.pop(candidate["start"])
                candidate["types"] = candidate["types"] | {"entity"}
                candidate["fix"] = _decode_entities(_HTML_TAG.sub("", candidate["fix"]))
        candidates = sorted(candidates + list(entities.values()), key=lambda item: (item["start"], item["end"]))
    if "lost" in enabled:
        # 同一行已經有換字的候選（網頁字元碼、文中廣告片段）：併成一個，問號在它的結果上拿掉
        by_row = {candidate["start"]: candidate for candidate in candidates
                  if candidate.get("fix") is not None and candidate["start"] == candidate["end"]}
        extra = []
        for lost in lost_char_candidates(lines):
            existing = by_row.get(lost["start"])
            if existing is None:
                extra.append(lost)
                continue
            match = _LOST_LEAD.match(existing["fix"])
            if match:
                rest = existing["fix"][match.end():]
                existing["fix"] = match.group(1) + rest if rest.strip() else ""
            existing["types"] = existing["types"] | {"lost"}
        candidates = sorted(candidates + extra, key=lambda item: (item["start"], item["end"]))
    if line_ranges is None:
        return candidates
    return _clip_to_ranges(candidates, lines, line_ranges)


def apply_candidates(lines, candidates):
    """把候選處理掉，回傳（新的整份行, 刪了幾行, 換了幾行）：有 fix 的（網頁字元碼、夾在正文裡的網址片段）
    那一行換成 fix，其餘刪掉 start～end 整段（重疊、相鄰的併成一段）。那一行跟掃描時不一樣（本文改過）的
    fix 不套用。掃描視窗的「處理已勾選項目」與內容檢查卡片的「刪除這筆」共用。"""
    result = list(lines)
    replaced = 0
    for candidate in candidates:
        if candidate.get("fix") is not None and result[candidate["start"]] == candidate["preview"]:
            result[candidate["start"]] = candidate["fix"]
            replaced += 1
    merged = []
    for start, end in sorted((c["start"], c["end"]) for c in candidates if c.get("fix") is None):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        del result[start:end + 1]
    return result, len(lines) - len(result), replaced


def _drop_front_matter(candidates, lines, title_rows):
    """第一章之前的書名、作者、簡介、又名……是 TXT 常見的開頭資訊，不是要刪的
    東西：只由「作品資訊／作者感言」組成、而且整段在第一個章節標題之前的候選
    拿掉。網址、QQ 這類真正的廣告即使在開頭也照樣列出。"""
    if title_rows is not None:
        first_title = min(title_rows) if title_rows else None
    else:
        first_title = next((row for row, line in enumerate(lines) if _is_title_line(line)), None)
    if first_title is None:
        return candidates
    informational = {"meta", "author_note"}
    return [candidate for candidate in candidates
            if not (candidate["end"] < first_title and set(candidate["types"]) <= informational)]


def _clip_to_ranges(candidates, lines, line_ranges):
    """只留下跟選取範圍有交集的候選，並把範圍外的行切掉。

    不切的話，選了第三章卻刪掉跨到第四章的段落，等於偷偷改了沒選的地方。"""
    ranges = []
    for start, end in sorted((start, end) for start, end in line_ranges if start < end):
        # 重疊或相連的範圍（選了整卷又選了其中一章）先合併，同一個候選才不會出現兩次
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    kept = []
    for candidate in candidates:
        for start, end in ranges:
            new_start = max(candidate["start"], start)
            new_end = min(candidate["end"], end - 1)
            if new_start > new_end:
                continue
            clipped = dict(candidate)
            clipped["start"], clipped["end"] = new_start, new_end
            clipped["preview"] = "\n".join(lines[new_start:new_end + 1])
            clipped["line"] = new_start + 1
            kept.append(clipped)
            # 不停在第一段：同一個候選可能跟好幾個選取的章節都有交集
    return kept


_AD_TYPES = frozenset({"url", "publish", "qq", "wechat", "source"})
_AD_BLOCK_REACH = 10         # 往上／往下最多找幾行分隔線
_AD_BLOCK_MAX_LINES = 12     # 分隔線中間最多幾行文字（太長就不是一段廣告）


_BLOCK_WORDS = QQ_WORDS + SOURCE_WORDS + PUBLISH_WORDS + WECHAT_WORDS


def _expand_ads_to_separator_blocks(lines, hits, title_rows):
    """廣告常用兩條分隔線框成一段（群號、網址夾在「全網小說資源共享」「已滿請換群號」
    這類說明中間）：候選的上下各找得到分隔線、中間沒有章節標題、而且不長，就整段收進來。"""
    is_title = _title_checker(title_rows)
    total = len(lines)

    def find(row, step):
        seen = 0
        while 0 <= row < total and seen < _AD_BLOCK_REACH:
            text = lines[row].strip()
            if text:
                if is_title(lines, row):
                    return None
                if looks_like_separator(text) or _note_trigger(text) == "separator":
                    return row
                seen += 1
            row += step
        return None

    def ad_worded(line):
        compact = compact_ad_text(line)
        return _compact_contains_any(compact, _BLOCK_WORDS) or bool(find_domain_tokens(compact))

    for hit in hits:
        if not (hit["types"] & _AD_TYPES):
            continue
        top = find(hit["start"] - 1, -1) if not looks_like_separator(lines[hit["start"]]) else hit["start"]
        bottom = find(hit["end"] + 1, 1) if not looks_like_separator(lines[hit["end"]]) else hit["end"]
        if top is None or bottom is None:
            continue
        inside = [line for line in lines[top + 1:bottom] if line.strip() and not looks_like_separator(line)]
        if len(inside) > _AD_BLOCK_MAX_LINES:
            continue
        # Every line the block would add must carry its own ad wording: a story paragraph that merely sits
        # between two separators next to an ad line is not part of the ad (ambiguous prose is kept).
        added = [row for row in range(top + 1, bottom)
                 if not hit["start"] <= row <= hit["end"] and lines[row].strip() and not looks_like_separator(lines[row])]
        if not all(ad_worded(lines[row]) for row in added):
            continue
        # 後面緊接著再一小段分隔線框住的廣告說明（「已满或搜不到请换个群号」）也一起收
        while True:
            following = find(bottom + 1, 1)
            if following is None:
                break
            extra = [line for line in lines[bottom + 1:following] if line.strip() and not looks_like_separator(line)]
            if not extra or len(extra) > 3 or not all(ad_worded(line) for line in extra):
                break
            bottom = following
        hit["start"], hit["end"] = min(hit["start"], top), max(hit["end"], bottom)


def _repeated_compacts(lines, min_length, min_count) -> dict:
    """整本出現 min_count 次以上的段落（去掉空白後的樣子 → 出現幾次）。先數再篩：對話、純標點、
    章節標題只檢查數量夠的那幾種，不必每一行都做標題判斷。整本都用 map／Counter 數（在 C 裡跑）。"""
    compacts = _map_in_chunks(compact_ad_text, lines)
    everywhere = Counter()
    first_line = {}
    for start in range(0, len(lines), _CHUNK):
        everywhere.update(compacts[start:start + _CHUNK])
    # 從後面往前填：同一段落最後留下的是最前面那一行
    for start in range(((len(lines) - 1) // _CHUNK) * _CHUNK, -1, -_CHUNK):
        first_line.update(zip(reversed(compacts[start:start + _CHUNK]), reversed(lines[start:start + _CHUNK])))
    repeated = {}
    for compact, count in everywhere.items():
        if count < min_count or not min_length <= len(compact) <= REPEAT_MAX_LENGTH                 or _is_dialogue_or_punct(compact):
            continue
        stripped = first_line[compact].strip()
        if not parse_lv1(stripped) and not parse_lv2(stripped):
            repeated[compact] = count
    # 重複的段落不到 DUPLICATED_RUN 種，不可能連成那麼長的一段
    return _drop_duplicated_passages(compacts, repeated, everywhere) if len(repeated) >= DUPLICATED_RUN else repeated


def _drop_duplicated_passages(compacts, repeated, everywhere) -> dict:
    """整章、整本內容重複放了好幾次（加料版常把同一章放兩份）：連續 DUPLICATED_RUN 行以上（不算空行）
    都是重複的段落，就是內容重複，不是廣告。一個段落有一半以上的出現位置在這種區塊裡就不列；
    廣告是一兩行夾在每次都不一樣的正文中間，多行的廣告區塊也很少超過這個長度。
    整本只出現一次的行（不管長短）才算把區塊切斷：空行、重複出現但不列的行（對話、分隔線）不算。"""
    in_run, total = Counter(), Counter()
    run = []

    def close_run():
        if len(run) >= DUPLICATED_RUN:
            in_run.update(run)
        run.clear()

    for compact in compacts:
        if compact in repeated:
            run.append(compact)
            total[compact] += 1
        elif compact and everywhere[compact] == 1:
            close_run()
    close_run()
    return {compact: count for compact, count in repeated.items() if in_run[compact] * 2 < total[compact]}


# 文中的網址（大小寫混雜的「wwW.eXamPle.Cc」也算）
_INLINE_URL = re.compile(r"(?i)(?:https?://)?(?:www\.|wap\.|m\.)?[a-z0-9\-]{2,}(?:\.[a-z0-9\-]{2,})*"
                         r"\.(?:com|net|org|cc|info|cn|me|la|tw|xyz|top|vip|io)(?![a-z0-9])(?:/[\w\-./?=&%#]*)?")
INLINE_MIN_LINES = 3
# 片段的邊界：往前最多看這麼多字、往後只收裝飾符號（☆★【】…），碰到句子標點就停
_INLINE_BEFORE = 12
_SENTENCE_BOUNDARY = set("。！？!?，,；;：:”」』）)…—")
_INLINE_AFTER_CHARS = set("★☆】]〗」』）) 　・·の")
_INLINE_OPENERS = set("☆★【[〖「『（(")


def _common_suffix(texts):
    shortest = min(texts, key=len)
    length = 0
    while length < len(shortest) and all(text[len(text) - length - 1] == shortest[len(shortest) - length - 1]
                                         for text in texts):
        length += 1
    suffix = shortest[len(shortest) - length:]
    # 片段不從句子標點開始（「。山路書屋 www…」的句號是正文的）；有「☆【」這種開頭的裝飾就從那裡開始
    cut = max((index + 1 for index, char in enumerate(suffix) if char in _SENTENCE_BOUNDARY), default=0)
    suffix = suffix[cut:]
    opener = max((index for index, char in enumerate(suffix) if char in _INLINE_OPENERS), default=None)
    return suffix[opener:] if opener is not None else suffix


def _common_prefix(texts):
    shortest = min(texts, key=len)
    length = 0
    while length < len(shortest) and shortest[length] in _INLINE_AFTER_CHARS \
            and all(text[length] == shortest[length] for text in texts):
        length += 1
    return shortest[:length]


def _looks_injected(url: str) -> bool:
    """網站硬塞的網址：帶 www／http 開頭，或大小寫亂成一團（「wwW.eXamPle.Cc」「example.neT」：小寫後面接大寫）。
    只有開頭大寫的「Example.com」是正常寫法。"""
    if url.lower().startswith(("www.", "http", "wap.")):
        return True
    letters = [char for char in url if char.isalpha()]
    return any(before.islower() and after.isupper() for before, after in zip(letters, letters[1:]))


def _inline_ads(lines, rows):
    """夾在正文裡的廣告片段：同一個網址出現在至少 INLINE_MIN_LINES 行正文裡（「……那麼傷。山路書屋
    www.example.com」「……最後的☆城裡小說網のwww.example.net★落點……」）。每一行前後共同的那段
    （網站名、裝飾符號）連同網址一起當成片段，只刪片段（fix），正文留著。只出現一次的網址不動：
    多半是故事裡提到的網站。"""
    occurrences = defaultdict(list)
    for row in rows:
        line = lines[row]
        for match in _INLINE_URL.finditer(line):
            rest = (line[:match.start()] + line[match.end():]).strip()
            # 黏在英文字後面的（網站把英文單字的後半段換成自己的網址）刪了也還原不回原字，不動
            glued = match.start() > 0 and line[match.start() - 1].isascii() and line[match.start() - 1].isalpha()
            if len(rest) >= 20 and not glued:
                occurrences[match.group(0).lower()].append((row, match.start(), match.end(), match.group(0)))
    spans = defaultdict(list)
    counts = {}
    for items in occurrences.values():
        rows_with = {row for row, _start, _end, _text in items}
        if len(rows_with) < INLINE_MIN_LINES:
            continue
        before = _common_suffix([lines[row][max(0, start - _INLINE_BEFORE):start] for row, start, _end, _text in items])
        after = _common_prefix([lines[row][end:] + " " for row, _start, end, _text in items])
        if not (before or after.strip() or any(_looks_injected(text) for _row, _start, _end, text in items)
                or sum(not lines[row][end:].strip() for row, _start, end, _text in items) * 2 > len(items)):
            # 故事裡一再提到的網站（「買下 example.com 這個網域」）：沒有 www、大小寫正常、前後每次都不一樣、
            # 也不在段尾
            continue
        # Injected fragments sit at the end of the paragraph or are wrapped in decoration (☆…★, 【…】). One
        # repeated in the middle of a sentence with nothing around it may be the story's own text ("請收件者查看
        # https://… 並且讀完這封信"): list it, but never pre-select it.
        at_end = sum(not lines[row][end:].strip() for row, _start, end, _text in items) * 2 > len(items)
        # looked at per occurrence: one site may use several wrappings ("[www…]" in some lines, "の www…★" in others)
        decorated = sum(lines[row][start - 1:start] in _INLINE_OPENERS | {"の"}
                        or lines[row][end:end + 1] in _INLINE_AFTER_CHARS - {" ", "　"}
                        for row, start, end, _text in items) * 2 > len(items)
        for row, start, end, _text in items:
            spans[row].append((start - len(before), end + len(after)))
            counts[row] = max(counts.get(row, 0), len(rows_with) if at_end or decorated else min(len(rows_with), 4))
    candidates = []
    for row, ranges in spans.items():
        fixed = lines[row]
        for start, end in sorted(ranges, reverse=True):
            fixed = fixed[:start] + fixed[end:]
        fixed = fixed.rstrip()
        if fixed.strip() and fixed != lines[row]:
            confidence = "高" if counts[row] >= 5 else "中"
            candidates.append({"start": row, "end": row, "types": {"url"}, "confidence": confidence,
                               "score": 5 if confidence == "高" else 3, "preview": lines[row], "line": row + 1,
                               "fix": fixed})
    return candidates


def _scan_ad_candidates(lines, enabled, title_rows=None, repeat_min_length=REPEAT_MIN_LENGTH,
                        repeat_min_count=REPEAT_MIN_COUNT):
    repeated = (_repeated_compacts(lines, repeat_min_length, repeat_min_count)
                if "repeat" in enabled else {})

    hits = []
    total = len(lines)
    if enabled & LINE_AD_CATEGORIES:
        features_by_line = [_ad_line_features(lines, index, enabled) for index in range(total)]
    else:
        features_by_line = [_NO_FEATURES] * total
    # 作品資訊自己帶信心度（作者／字數／平台是高，日期與分隔線是中），
    # 不走下面那套「特徵愈多分數愈高」的算法。
    meta_by_line = (_map_in_chunks(meta_line_kind, lines) if "meta" in enabled
                    else [None] * total)
    # 論壇轉貼資訊（使用者資料表裡的註冊日期、數字）算無關連內容，不在作品資訊裡再列一次
    header_blocks = (forum_header_blocks(lines, title_rows)
                     if enabled & {"meta", "forum", "author_note"} else [])
    forum_blocks = (header_blocks + forum_profile_blocks(lines, title_rows)
                    if enabled & {"meta", "forum"} else [])
    if "meta" in enabled:
        for start, end in forum_blocks:
            for row in range(start, min(end + 1, total)):
                meta_by_line[row] = None
    # 併進 features_by_line，底下「把相鄰的廣告行合併成一段」才看得到它們：
    # 作者、字數、發表資訊通常連續好幾行，被切成好幾個候選很難勾。
    for row, kind in enumerate(meta_by_line):
        if kind is not None:
            features_by_line[row] = features_by_line[row] | {"meta"}
    # 文中夾的廣告片段（同一段網址在好幾行正文裡）：只刪片段（換字），不整段刪
    inline = _inline_ads(lines, [row for row, features in enumerate(features_by_line) if "url" in features])         if "url" in enabled else []
    # 章節標題不整行當廣告刪（「第9章 山路【求收藏】」刪了就少一章），文中片段那幾行也不整段刪
    for row in set(title_rows or ()) | {candidate["start"] for candidate in inline}:
        if 0 <= row < total:
            features_by_line[row] = _NO_FEATURES
    for index, line in enumerate(lines):
        is_repeat = bool(repeated) and compact_ad_text(line) in repeated
        if not features_by_line[index] and not is_repeat:
            continue
        features = set(features_by_line[index])
        if is_repeat:
            features.add("repeat")

        start = end = index
        if features != {"repeat"}:
            if "url" in features:
                end = max(end, _url_window_end(lines, index) or index)
            # 跨行網址或相鄰廣告關鍵字合併為一個候選段落，最多擴展五行。
            for next_index in range(index + 1, min(total, index + 5)):
                next_line = lines[next_index]
                next_features = set(features_by_line[next_index])
                if next_features or not next_line.strip() or looks_like_separator(next_line):
                    end = max(end, next_index)
                    features.update(next_features)
                    if "url" in next_features:
                        end = max(end, _url_window_end(lines, next_index) or next_index)
                else:
                    break
        if start > 0 and looks_like_separator(lines[start - 1]):
            start -= 1
        if end + 1 < total and looks_like_separator(lines[end + 1]):
            end += 1

        narrative = _looks_like_narrative(lines, start, end)
        if features <= _KEYWORD_ONLY and (narrative or (features <= _STORY_KEYWORD_TYPES and not any(
                _AD_ACTION.search(lines[row]) for row in range(start, end + 1)))):
            # 故事裡提到微信、網站、發布（「給丈夫發了一條微信」）：沒有網址、帳號，不是廣告
            continue
        edge = start < 200 or end >= max(0, total - 200)
        score = len(features) * 2 + (1 if edge else 0)
        if "source" in features and any(_compact_contains_any(compact_ad_text(lines[row]), DISCLAIMER_WORDS)
                                        for row in range(start, end + 1)):
            score += 2
        if "url" in features and ({"publish", "source"} & features):
            score += 2
        # 網址若出現在「看起來像正文」的行裡（有段落縮排、對話引號、而且夠長），
        # 通常是角色提到某個網站，不是廣告行。整段刪掉會傷到故事內容，
        # 所以調降信心度、不讓它落入預設勾選，改由使用者自行判斷。
        if narrative:
            score = min(score, 2)
        if "meta" in features:
            # 這一段裡只要有一行是高信心的作品資訊，整段就算高信心；
            # 只有日期或分隔線的話維持中信心。
            kinds = [meta_by_line[row] for row in range(start, min(end + 1, total))
                     if meta_by_line[row] is not None]
            score = max(score, 5 if any(kind[1] == "高" for kind in kinds) else 3)
        confidence = "高" if score >= 5 else "中" if score >= 3 else "低"
        hits.append({"start": start, "end": end, "types": set(features),
                     "confidence": confidence, "score": score})

    _expand_ads_to_separator_blocks(lines, hits, title_rows)

    if "forum" in enabled:
        for start, end in forum_blocks:
            hits.append({"start": start, "end": end, "types": {"forum"}, "confidence": "高", "score": 5})
    if "author_note" in enabled:
        scores = {"高": 5, "中": 3, "低": 1}
        for start, end, confidence in (author_note_blocks(lines, title_rows)
                                       + forum_post_notes(lines, header_blocks, title_rows)):
            hits.append({"start": start, "end": end, "types": {"author_note"},
                         "confidence": confidence, "score": scores[confidence]})

    merged = []
    for hit in sorted(hits, key=lambda item: (item["start"], item["end"])):
        if merged and hit["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], hit["end"])
            merged[-1]["types"].update(hit["types"])
            merged[-1]["score"] = max(merged[-1]["score"], hit["score"])
            merged[-1]["confidence"] = "高" if merged[-1]["score"] >= 5 else "中" if merged[-1]["score"] >= 3 else "低"
        else:
            merged.append(dict(hit))

    for candidate in merged:
        candidate["preview"] = "\n".join(lines[candidate["start"]:candidate["end"] + 1])
        candidate["line"] = candidate["start"] + 1
        if "repeat" in candidate["types"]:
            # 段落前後併進分隔線時也照那一行重複的次數顯示
            candidate["repeat_count"] = max(repeated.get(compact_ad_text(line), 0)
                                            for line in lines[candidate["start"]:candidate["end"] + 1])
    return sorted(merged + inline, key=lambda item: (item["start"], item["end"]))
