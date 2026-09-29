"""簡繁轉換引擎（檔名與正文共用）。

刻意不保留任何內建簡繁對照表：小型對照表只涵蓋少數常用字，轉出來的檔名
會是「半轉半不轉」的殘缺狀態，比完全不轉更容易誤導。沒有 OpenCC 時，
改為詢問使用者是否直接以原文檔名儲存。
（DEFAULT_VOCABULARY 是正文的「用詞對照」預設內容、使用者可以編輯，不是逐字的簡繁對照表。）
"""

import re
from functools import lru_cache

# 檔名簡繁轉換的目標選項；「保留原文」不做任何轉換。
SCRIPT_KEEP = "保留原文"
SCRIPT_TRAD = "繁體"
SCRIPT_SIMP = "簡體"
SCRIPT_CHOICES = (SCRIPT_TRAD, SCRIPT_SIMP, SCRIPT_KEEP)
SCRIPT_CONFIG = {SCRIPT_TRAD: "s2t", SCRIPT_SIMP: "t2s"}


@lru_cache(maxsize=4)
def get_opencc_converter(config="s2t"):
    """僅首次匯出時初始化，成功後重複使用。config 為 s2t（簡轉繁）或 t2s（繁轉簡）。"""
    try:
        import opencc
        return opencc.OpenCC(config)
    except Exception:
        return None


# 正文轉換的方向。跟檔名那組分開：正文多了「台灣用語」這個選項（會連詞彙
# 一起換，軟件→軟體、界面→介面），而且方向要講得更明確。
BODY_SCRIPT_MODES = {
    "簡體轉繁體": "s2t",
    "簡體轉繁體（台灣用語）": "s2twp",
    "繁體轉簡體": "t2s",
}
BODY_SCRIPT_CHOICES = tuple(BODY_SCRIPT_MODES)

BODY_SCRIPT_SAMPLES = {
    "簡體轉繁體": "這個軟件的界面設計，默認採用簡體中文。",
    "簡體轉繁體（台灣用語）": "這個軟體的介面設計，預設採用簡體中文。",
    "繁體轉簡體": "这个软件的界面设计，默认采用简体中文。",
}
BODY_SCRIPT_SAMPLE_SOURCE = "这个软件的界面设计，默认采用简体中文。"


def opencc_available() -> bool:
    """OpenCC 是選用套件；沒裝的時候按鈕要停用並說明原因。"""
    return get_opencc_converter("s2t") is not None


# 用詞對照的預設內容（繁體字寫；左邊是中國大陸的用詞、右邊是台灣的用詞）。使用者在繁簡轉換視窗的
# 「詞表」分頁直接編輯，不要的整行刪掉；存的是使用者編輯後的文字。
DEFAULT_VOCABULARY = "\n".join((
    "視頻 = 影片", "質量 = 品質", "信息 = 訊息", "網絡 = 網路", "默認 = 預設", "屏幕 = 螢幕",
    "軟件 = 軟體", "硬件 = 硬體", "程序 = 程式", "數據 = 資料", "服務器 = 伺服器", "內存 = 記憶體",
    "硬盤 = 硬碟", "U盤 = 隨身碟", "打印 = 列印", "鼠標 = 滑鼠", "短信 = 簡訊", "激光 = 雷射",
    "帶寬 = 頻寬", "網吧 = 網咖", "博客 = 部落格", "出租車 = 計程車", "公交車 = 公車",
    "摩托車 = 機車", "自行車 = 腳踏車", "空調 = 冷氣", "方便麵 = 泡麵", "酸奶 = 優格",
    "快餐 = 速食", "盒飯 = 便當", "土豆 = 馬鈴薯",
))
TO_TRADITIONAL = {"簡體轉繁體", "簡體轉繁體（台灣用語）"}
# 保護「不轉換的詞」用的佔位字：第 15 平面的私用區，OpenCC 不會動、正文裡也不會有
_PLACEHOLDER_BASE = 0xF0000


def parse_keep_words(text: str) -> list:
    """「不轉換的詞」：一行一個，空行不算。"""
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def parse_vocabulary(text: str) -> list:
    """「用詞對照」：一行一組「左邊 = 右邊」，兩邊都要有字；其他的行不算。"""
    pairs = []
    for line in (text or "").split("\n"):
        left, separator, right = line.partition("=")
        if separator and left.strip() and right.strip():
            pairs.append((left.strip(), right.strip()))
    return pairs


def _replace_all(text: str, mapping: dict) -> str:
    if not mapping:
        return text
    pattern = re.compile("|".join(map(re.escape, sorted(mapping, key=len, reverse=True))))
    return pattern.sub(lambda match: mapping[match.group()], text)


def convert_with_word_lists(text, mode, keep_words=(), vocabulary=()):
    """convert_body_text 加上使用者的詞表（行數不變，只換一行裡的字）：

    - 不轉換的詞：照使用者打的寫法留著；本文裡寫成繁體或簡體的都認得。
    - 用詞對照：轉繁體時轉完再把左邊換成右邊；轉簡體時先把右邊換回左邊再轉。"""
    if not text or mode not in BODY_SCRIPT_MODES:
        return text
    tokens, restore = {}, {}
    for index, word in enumerate(keep_words):
        token = chr(_PLACEHOLDER_BASE + index)
        restore[token] = word
        for variant in {word, convert_body_text(word, "簡體轉繁體"), convert_body_text(word, "繁體轉簡體")}:
            tokens.setdefault(variant, token)
    text = _replace_all(text, tokens)
    to_traditional = mode in TO_TRADITIONAL
    if not to_traditional:
        text = _replace_all(text, {right: left for left, right in vocabulary})
    text = convert_body_text(text, mode)
    if to_traditional:
        text = _replace_all(text, dict(vocabulary))
    return _replace_all(text, restore)


def convert_body_text(text, mode):
    """把整份（或一段）正文做簡繁轉換。

    OpenCC 是逐字轉換，行數與行的對應完全不變，所以章節狀態的行號是 1:1，
    不需要像搬移章節那樣另外算映射。行尾的 [::] 標記是 ASCII，不會被動到。

    沒有 OpenCC 時原樣回傳——半套轉換比不轉更糟（見模組開頭的說明）。
    """
    config = BODY_SCRIPT_MODES.get(mode)
    if not text or config is None:
        return text
    converter = get_opencc_converter(config)
    return converter.convert(text) if converter is not None else text


def convert_script(text, target=SCRIPT_TRAD):
    """依 target 將檔名轉為繁體或簡體；沒有 OpenCC 時原樣回傳、不做半套轉換。"""
    if not text or target == SCRIPT_KEEP:
        return text
    config = SCRIPT_CONFIG.get(target)
    if config is None:
        return text
    converter = get_opencc_converter(config)
    return converter.convert(text) if converter is not None else text
