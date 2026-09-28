"""Word（.docx）檔的純文字：每個段落一行。

.docx 是 zip 裡的 XML（word/document.xml），用標準函式庫就讀得到，不需要另外裝套件。
只取正文：頁首頁尾、註腳、批註都在別的檔案裡，不讀；修訂模式刪掉的字（w:del 裡的 w:delText）、
功能變數的指令（w:instrText，例如 PAGE、HYPERLINK "..."）不是看得到的字，也不取。
"""

import zipfile
import xml.etree.ElementTree as ElementTree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPH = _W + "p"
_TEXT = _W + "t"
_SKIPPED = {_W + "del", _W + "delText", _W + "instrText", _W + "moveFrom"}
_TAB = _W + "tab"
_BREAKS = {_W + "br", _W + "cr"}
_HYPHENS = {_W + "noBreakHyphen": "-", _W + "softHyphen": ""}


class DocxError(Exception):
    """不是 Word 檔、或檔案壞了。"""


def _paragraph_text(paragraph) -> str:
    parts = []

    def walk(node):
        for child in node:
            tag = child.tag
            if tag in _SKIPPED:
                continue
            if tag == _TEXT:
                parts.append(child.text or "")
            elif tag == _TAB:
                parts.append("\t")
            elif tag in _BREAKS:
                # 分頁、分欄的 w:br 在純文字裡就是換行
                parts.append("\n")
            elif tag in _HYPHENS:
                parts.append(_HYPHENS[tag])
            elif tag != _PARAGRAPH:     # 文字方塊裡的段落另外算一行
                walk(child)

    walk(paragraph)
    return "".join(parts)


def read_docx_text(path: str) -> str:
    """整份文件的文字，段落之間用 \\n 分開（表格的每一格也是段落）。"""
    try:
        with zipfile.ZipFile(path) as archive:
            data = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as error:
        raise DocxError(str(error)) from error
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise DocxError(str(error)) from error
    body = root.find(_W + "body")
    if body is None:
        return ""
    lines = []
    for paragraph in body.iter(_PARAGRAPH):
        lines.append(_paragraph_text(paragraph))
    return "\n".join(lines)


def is_docx(path: str) -> bool:
    return path.lower().endswith(".docx")
