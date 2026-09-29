"""把好幾個檔案（每章一個檔）合併成一份本文。

檔名排序照裡面的數字（「第10章」排在「第9章」後面，不是「第1章」後面），中文數字也算；
每個檔各自偵測編碼、移除 BOM 與零寬字元（跟開檔一樣）。開頭幾行都沒有章節標題時，可以用檔名
當章名補在最前面（title_from_filename）。檔案之間空一行。
逐章匯出（core/file_split.py）的檔案合併回來會得到原本的本文。
"""

import os
import re

from .chapter_parse import parse_lv1, parse_lv2, parse_special
from .cn_numerals import chinese_to_arabic

_NUMBER_RUN = re.compile(r"[0-9０-９]+|[零〇一二兩两三四五六七八九十百千萬万]+")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def natural_key(name: str):
    """檔名排序用：數字（阿拉伯、中文）照數值比，其餘照文字比。"""
    stem = os.path.splitext(os.path.basename(name))[0]
    key = []
    position = 0
    for match in _NUMBER_RUN.finditer(stem):
        key.append((0, stem[position:match.start()].lower()))
        text = match.group()
        value = int(text.translate(_FULLWIDTH_DIGITS)) if text[0] in "0123456789０１２３４５６７８９" \
            else chinese_to_arabic(text)
        key.append((1, value))
        position = match.end()
    key.append((0, stem[position:].lower()))
    return key


def first_line(text: str) -> str:
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return ""


HEADING_LOOKAHEAD = 5       # 開頭幾行（不算空行）裡有章節標題就不補檔名


def _is_heading(line: str) -> bool:
    return len(line) <= 60 and bool(parse_lv2(line) or parse_lv1(line) or parse_special(line))


def starts_with_heading(text: str) -> bool:
    """開頭幾行裡有章節標題（逐章匯出的第一個檔前面會有書名、簡介）。"""
    seen = 0
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_heading(line):
            return True
        seen += 1
        if seen >= HEADING_LOOKAHEAD:
            break
    return False


_ORDINAL = re.compile(r"^([0-9０-９]+)[\s._\-－、]+(.+)$")


def title_from_filename(name: str) -> str:
    """檔名當章名：只有號碼的寫成「第N章」；「001 山路」寫成「第1章 山路」；
    「0001 第1章 山路」（逐章匯出的檔名）去掉前面的序號。"""
    title = os.path.splitext(os.path.basename(name))[0].strip()
    if _NUMBER_RUN.fullmatch(title):
        return f"第{title}章"
    ordinal = _ORDINAL.match(title)
    if ordinal:
        rest = ordinal.group(2).strip()
        if _is_heading(rest):
            return rest
        number = int(ordinal.group(1).translate(_FULLWIDTH_DIGITS))
        return f"第{number}章 {rest}"
    return title


def merge_texts(parts, title_from_name: bool) -> str:
    """parts：[(檔名, 文字)] 照合併的順序。title_from_name：第一行不是章節標題時用檔名當章名。"""
    chunks = []
    for name, text in parts:
        body = text.strip("\n")
        if title_from_name and not starts_with_heading(body):
            title = title_from_filename(name)
            body = f"{title}\n{body}" if body else title
        chunks.append(body)
    return "\n\n".join(chunk for chunk in chunks if chunk)
