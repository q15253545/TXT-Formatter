"""TXT 檔案的讀寫：寫入用原子替換，讀取先嚴格解碼。

寫入先寫進同一個資料夾裡的暫存檔，完整寫完（含 fsync）才用 os.replace 換掉目標檔：
直接 open(path, "w") 會先清空原檔，中途失敗就只剩半截。

讀取預設 errors="strict"：編碼猜錯時要讓呼叫端知道，不能默默把解不開的位元組換成 U+FFFD
再存回去。確定要容錯開啟時才呼叫 read_text_lossy。
"""

import errno
import os
import tempfile


def write_text_atomic(path: str, text: str, encoding: str = "utf-8"):
    """寫入暫存檔後原子替換；失敗時拋出例外，且目標檔維持原狀。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".txt-formatter-", suffix=".tmp")
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


def describe_file_error(error: Exception, writing: bool = False) -> str:
    """把讀寫檔的例外換成白話的原因和下一步（對話框用；原始例外由呼叫端寫進 app.log）。"""
    if isinstance(error, PermissionError):
        if writing:
            return "檔案可能正被其他程式開啟，或是唯讀、沒有權限。請關閉開著它的程式後再試，或換個位置存。"
        return "檔案可能正被其他程式開啟，或沒有讀取權限。請關閉開著它的程式後再試。"
    if isinstance(error, FileNotFoundError):
        if writing:
            return "找不到要存放的資料夾：可能已被移動或刪除。請換個位置存。"
        return "找不到這個檔案：可能已被移動、改名或刪除。請重新選擇檔案。"
    if isinstance(error, IsADirectoryError):
        return "這個名稱是資料夾，不是檔案。請換一個檔名。"
    if isinstance(error, UnicodeError):
        return "本文裡有無法存成 UTF-8 的字元（通常是亂碼）。請找出來刪掉後再存。"
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return "磁碟空間不足。請清出空間或換個位置存。"
    if writing:
        return "無法寫入這個位置。請確認磁碟或隨身碟還接著，或換個位置存。"
    return "無法讀取這個檔案。請確認檔案還在、沒有被其他程式鎖住後再試。"
