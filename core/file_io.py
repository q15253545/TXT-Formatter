"""TXT 檔案的讀寫：寫入用原子替換，讀取先嚴格解碼。

寫入先寫進同一個資料夾裡的暫存檔，完整寫完（含 fsync）才用 os.replace 換掉目標檔：
直接 open(path, "w") 會先清空原檔，中途失敗就只剩半截。

讀取預設 errors="strict"：編碼猜錯時要讓呼叫端知道，不能默默把解不開的位元組換成 U+FFFD
再存回去。確定要容錯開啟時才呼叫 read_text_lossy。
"""

import os
import tempfile


def write_text_atomic(path: str, text: str, encoding: str = "utf-8"):
    """寫入暫存檔後原子替換；失敗時拋出例外，且目標檔維持原狀。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".txt-tool-", suffix=".tmp")
    try:
        # 文字模式（newline 用預設值）：Windows 上換行仍然輸出成 CRLF，
        # 跟一般用 open(path, "w") 寫出來的檔案一樣。
        with os.fdopen(handle, "w", encoding=encoding) as target:
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_text(path: str, encoding: str) -> str:
    """嚴格解碼；編碼不符時拋出 UnicodeError。"""
    with open(path, "r", encoding=encoding, errors="strict") as source:
        return source.read()


def read_text_lossy(path: str, encoding: str) -> tuple[str, int]:
    """容錯解碼，回傳（內容, 解不開而被替換掉的字元數）。"""
    with open(path, "r", encoding=encoding, errors="replace") as source:
        content = source.read()
    return content, content.count("�")
