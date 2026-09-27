"""執行「使用者自己寫的」正則：尋找／全部取代、自訂章節規則。

內建的 re 一旦進入災難性回溯就停不下來，整個介面會卡死，事後計時也救不回來
（要等那一次比對自己結束）。有裝 regex 套件時改用它：語法跟 re 相容
（VERSION0），內部對常見的回溯寫法有優化，而且每次比對都能設逾時。
沒裝就退回 re，功能照舊、只是沒有逾時保護（pip install regex 即可）。

程式自己寫的固定正則不必經過這裡，繼續用 re。
"""

import re

try:
    import regex as _regex
except ImportError:          # pragma: no cover - 沒裝時退回 re
    _regex = None

HAS_TIMEOUT = _regex is not None
# 編譯失敗時兩種套件丟的例外不同，呼叫端一律攔這個。
errors = (re.error,) + ((_regex.error,) if _regex is not None else ())


class RegexTimeout(Exception):
    """正則在期限內沒有跑完（通常是寫法造成大量回溯）。"""


def compile(pattern: str, flags: int = 0):
    """flags 用 re 的旗標（re.IGNORECASE…），這裡會轉成 regex 的。"""
    if _regex is None:
        return re.compile(pattern, flags)
    converted = _regex.VERSION0
    for re_flag, regex_flag in ((re.IGNORECASE, _regex.IGNORECASE), (re.MULTILINE, _regex.MULTILINE),
                                (re.DOTALL, _regex.DOTALL)):
        if flags & re_flag:
            converted |= regex_flag
    return _regex.compile(pattern, converted)


def _is_regex(pattern) -> bool:
    return _regex is not None and isinstance(pattern, _regex.Pattern)


def finditer(pattern, text: str, pos: int = 0, timeout: float = 2.0):
    """逐筆產生命中；整次搜尋超過 timeout 秒就丟 RegexTimeout（已產生的照樣有效）。"""
    if not _is_regex(pattern):
        yield from pattern.finditer(text, pos)
        return
    try:
        yield from pattern.finditer(text, pos, timeout=timeout)
    except TimeoutError as error:
        raise RegexTimeout() from error


def subn(pattern, replacement, text: str, timeout: float = 5.0):
    if not _is_regex(pattern):
        return pattern.subn(replacement, text)
    try:
        return pattern.subn(replacement, text, timeout=timeout)
    except TimeoutError as error:
        raise RegexTimeout() from error


def fullmatch(pattern, text: str, timeout: float = 0.2):
    if not _is_regex(pattern):
        return pattern.fullmatch(text)
    try:
        return pattern.fullmatch(text, timeout=timeout)
    except TimeoutError as error:
        raise RegexTimeout() from error
