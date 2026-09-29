"""把好幾個檔案（每章一個檔）合併成一份本文。

檔名排序照裡面的數字（「第10章」排在「第9章」後面，不是「第1章」後面），中文數字也算；
每個檔各自偵測編碼、移除 BOM 與零寬字元（跟開檔一樣）。第一行不是章節標題時，可以用檔名
（去掉副檔名；只有號碼的寫成「第N章」）當章名補在最前面。檔案之間空一行。
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


def starts_with_heading(text: str) -> bool:
    line = first_line(text)
    return bool(line) and len(line) <= 60 and bool(parse_lv2(line) or parse_lv1(line) or parse_special(line))


def merge_texts(parts, title_from_name: bool) -> str:
    """parts：[(檔名, 文字)] 照合併的順序。title_from_name：第一行不是章節標題時用檔名當章名。"""
    chunks = []
    for name, text in parts:
        body = text.strip("\n")
        if title_from_name and not starts_with_heading(body):
            title = os.path.splitext(os.path.basename(name))[0].strip()
            # 檔名只有號碼（「12.txt」「〇一二.txt」）：寫成「第12章」，目錄才認得
            if _NUMBER_RUN.fullmatch(title):
                title = f"第{title}章"
            body = f"{title}\n{body}" if body else title
        chunks.append(body)
    return "\n\n".join(chunk for chunk in chunks if chunk)
