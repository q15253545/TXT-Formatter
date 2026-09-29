"""逐章匯出：目錄的每一項（卷、章、特殊標題）存成一個 TXT，放進一個資料夾。

檔名＝序號＋標題（「0001 第1章 山路.txt」），序號補零，照檔名排序就是本文的順序；
不能當檔名的字元換成底線。第一個目錄項目之前的文字（書名、簡介）放在第一個檔的最前面。
用「合併多個檔案」（core/file_merge.py）合併回來會得到原本的本文。
"""

import re

_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')
MAX_NAME = 80


def split_sections(lines, title_rows) -> list:
    """回傳 [(標題, 這一段的行)]：每個目錄項目一段，第一段帶著第一個標題之前的文字。"""
    rows = sorted(row for row in set(title_rows) if 0 <= row < len(lines))
    if not rows:
        return [("", list(lines))]
    sections = []
    for index, row in enumerate(rows):
        start = 0 if index == 0 else row
        end = rows[index + 1] if index + 1 < len(rows) else len(lines)
        body = list(lines[start:end])
        while body and not body[-1].strip():
            body.pop()
        sections.append((lines[row].strip(), body))
    return sections


def section_filenames(titles) -> list:
    width = max(4, len(str(len(titles))))
    names = []
    for index, title in enumerate(titles, 1):
        safe = _UNSAFE.sub("_", title).strip(" .")[:MAX_NAME] or "未命名"
        names.append(f"{index:0{width}d} {safe}.txt")
    return names
