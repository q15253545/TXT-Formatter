"""TXT 檔案編碼偵測。"""

import re

CJK_RANGE_REGEX = re.compile(r"[一-鿿]")
# 私用區、替換字元與 C1 控制碼都是「這個編碼解錯了」的訊號。
DECODE_NOISE_REGEX = re.compile(r"[-�-]")


def strip_stray_bom(text: str) -> tuple[str, int]:
    """移除夾在內文裡的 BOM（U+FEFF），回傳（清理後文字, 移除個數）。

    多個 TXT 直接串接成一個檔案時，後面每個檔案開頭的 BOM 會留在行首。
    它看不見，但 Python 的 strip() 不會把它當空白去掉，章節正則的「行首」
    也就對不上——「\ufeff第二章」整行被當成正文，目錄只剩沒有 BOM 的
    那幾章。BOM 在文字中間沒有任何意義，直接刪除。
    """
    count = text.count("\ufeff")
    return (text.replace("\ufeff", ""), count) if count else (text, 0)


# 從網頁複製的文字常夾著看不見的零寬空白（U+200B）、字詞連接符（U+2060）：跟 BOM 一樣，
# strip() 不會去掉，行首有它時章節標題就認不出來。U+200C、U+200D 在表情符號組合裡有用，
# 只拿掉夾在中文、全形字、行首行尾的。
_ZERO_WIDTH = re.compile("[\u200b\u2060]")
_JOINER_RUN = re.compile("[\u200c\u200d]+")
_CJK_EDGE = re.compile("[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef\n]")


def _strip_joiners(text: str) -> tuple[str, int]:
    """只看找到的那幾處（整份逐字比對很慢）：前後有一邊是中文、全形字、換行或頭尾才拿掉。"""
    parts, cursor, removed = [], 0, 0
    for match in _JOINER_RUN.finditer(text):
        start, end = match.span()
        before = text[start - 1] if start else "\n"
        after = text[end] if end < len(text) else "\n"
        if _CJK_EDGE.match(before) or _CJK_EDGE.match(after):
            parts.append(text[cursor:start])
            cursor = end
            removed += end - start
    if not removed:
        return text, 0
    parts.append(text[cursor:])
    return "".join(parts), removed


def strip_invisible_chars(text: str) -> tuple[str, int, int]:
    """移除 BOM（strip_stray_bom）與零寬字元，回傳（清理後文字, BOM 個數, 零寬字元個數）。"""
    text, boms = strip_stray_bom(text)
    zero_width = joiners = 0
    if "\u200b" in text or "\u2060" in text:
        text, zero_width = _ZERO_WIDTH.subn("", text)
    if "\u200c" in text or "\u200d" in text:
        text, joiners = _strip_joiners(text)
    return text, boms, zero_width + joiners


def detect_line_ending(file_path: str) -> str:
    """回傳 "CRLF" 或 "LF"。

    只能從原始位元組判斷：Python 以文字模式讀檔時會把 \\r\\n 統一換成 \\n，
    等拿到字串才看就永遠只會是 LF。
    """
    try:
        with open(file_path, "rb") as f:
            sample = f.read(64 * 1024)
    except OSError:
        return "LF"
    return "CRLF" if b"\r\n" in sample else "LF"


NUL_SCAN_BYTES = 4096
NUL_MIN_RATIO = 0.005         # 一般文字（UTF-8、Big5、GB18030）不會有 NUL；零星一兩個不算
ENDIAN_DOMINANCE = 8          # 沒有換行可看時，NUL 幾乎都落在同一種奇偶位才判得出位元組順序


def _utf16_without_bom(raw: bytes):
    """沒有 BOM 的 UTF-16（PowerShell、部分 Windows 程式存出來的）：英數字、換行的另一個位元組是 0。
    位元組順序先看換行：用 LE 解，LE 檔的換行是 \\n，BE 檔的換行會變成 U+0A00（反過來也一樣）。
    不能只看 NUL 落在奇數位還是偶數位：段首的全形空格（U+3000）的 0 落在另一邊，
    一般中文小說每段都有，兩邊的數量會差不多。一整段沒有換行時才退回看 NUL 的位置。
    最後再試解一次確認不是亂碼。判不出來回傳 None，交給下面的計分。"""
    head = raw[:NUL_SCAN_BYTES]
    head = head[:len(head) - len(head) % 2]
    even = head[0::2].count(0)
    odd = head[1::2].count(0)
    if not even + odd:
        return None
    as_le = head.decode("utf-16-le", errors="replace")
    le_breaks = as_le.count("\n") + as_le.count("\r")
    be_breaks = as_le.count("਀") + as_le.count("ഀ")
    if even + odd < max(4, len(head) * NUL_MIN_RATIO):
        # 很短的檔（一兩行，不到 800 位元組）NUL 可能不到 4 個：只在換行明確只落在一邊、長度是偶數、
        # 整份照那個順序解得開時才算，不然一般檔案零星的 0 會被誤判成 UTF-16。
        if (len(raw) * NUL_MIN_RATIO >= 4 or len(raw) % 2
                or bool(le_breaks) == bool(be_breaks)):
            return None
        candidate = "utf-16-le" if le_breaks else "utf-16-be"
        try:
            decoded = raw.decode(candidate)
        except UnicodeDecodeError:
            return None
        return candidate if not looks_misdecoded(decoded) else None
    if le_breaks != be_breaks:
        candidate = "utf-16-le" if le_breaks > be_breaks else "utf-16-be"
    else:
        candidate = ("utf-16-le" if odd > even * ENDIAN_DOMINANCE else
                     "utf-16-be" if even > odd * ENDIAN_DOMINANCE else None)
    if candidate is None:
        return None
    sample = raw[:len(raw) - len(raw) % 2][:128 * 1024]
    decoded = sample.decode(candidate, errors="replace")
    return candidate if decoded and not looks_misdecoded(decoded) else None


def looks_misdecoded(text: str, sample_chars: int = 200_000) -> bool:
    """解出來的文字有一大片替換字元、私用區字元或 C1 控制碼：編碼多半不對。"""
    sample = text[:sample_chars]
    if not sample:
        return False
    return len(DECODE_NOISE_REGEX.findall(sample)) / len(sample) > 0.01


def _strict_utf8_with_cjk(raw: bytes) -> bool:
    """整份是合法的 UTF-8、而且有中文字：Big5、GB18030 的位元組湊成一整份合法 UTF-8 幾乎不可能。
    下面的計分對很短的檔（一兩行）不準：GB18030 解出來的字數比較多，「中文字比例」反而比 UTF-8 高。
    取樣尾端可能切在一個字的中間，最多容忍最後 3 個位元組不完整。"""
    for cut in range(4):
        try:
            text = raw[:len(raw) - cut].decode("utf-8")
        except UnicodeDecodeError as error:
            if error.start < len(raw) - 3:
                return False
            continue
        return bool(CJK_RANGE_REGEX.search(text))
    return False


def smart_detect_encoding(file_path: str) -> str:
    """全部試解後計分取最佳。

    不能沿用「第一個解得過就採用」的寫法：GB18030 涵蓋 0x00–0xFF，
    幾乎不會拋 UnicodeDecodeError，Big5 檔會被它解成一堆私用區亂碼，
    導致 big5 這個候選永遠輪不到。
    """
    try:
        with open(file_path, "rb") as f:
            raw = f.read(128 * 1024)
    except OSError:
        return "utf-8"
    if not raw:
        return "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    utf16 = _utf16_without_bom(raw)
    if utf16:
        return utf16
    if _strict_utf8_with_cjk(raw):
        return "utf-8"

    best, best_score = "utf-8", float("-inf")
    for encoding in ("utf-8", "big5", "gb18030"):
        # 不能用嚴格解碼：取樣是固定讀 128 KB，尾端很可能剛好切在一個
        # 多位元組字元的中間，讓正確的編碼也拋出 UnicodeDecodeError，
        # 結果三個候選全部失敗、落回預設值。改用 errors="replace"：
        # 尾端截斷只會產生一兩個替換字元，編碼真的不符才會產生一大片。
        decoded = raw.decode(encoding, errors="replace")
        if not decoded:
            continue
        size = len(decoded)
        noise_ratio = len(DECODE_NOISE_REGEX.findall(decoded)) / size
        score = (len(CJK_RANGE_REGEX.findall(decoded)) / size * 100
                 - noise_ratio * 400)
        # UTF-8 幾乎不可能誤判成功，但這個加分只有在「幾乎沒有替換字元」
        # 時才給，否則會讓 UTF-8 硬是壓過正確的中文編碼。
        if encoding == "utf-8" and noise_ratio < 0.0005:
            score += 5
        if score > best_score:
            best, best_score = encoding, score
    return best
