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
# mc:AlternateContent 的 mc:Fallback 是給舊版 Word 的備份（文字方塊用 VML 再存一次），讀了文字會重複
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_SKIPPED = {_W + "del", _W + "delText", _W + "instrText", _W + "moveFrom", _FALLBACK}
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
    except zipfile.BadZipFile as error:
        raise DocxError("檔案已損壞，或不是真正的 Word 檔（.docx）。舊版的 .doc 請先用 Word 另存成 .docx。") from error
    except KeyError as error:
        raise DocxError("檔案裡找不到正文，可能不是 Word 文件。請用 Word 打開後另存成 .docx 再試。") from error
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise DocxError("文件內容損壞，讀不出文字。請用 Word 打開後另存一份再試。") from error
    body = root.find(_W + "body")
    if body is None:
        return ""
    return "\n".join(_paragraph_text(paragraph) for paragraph in _paragraphs(body))


def _paragraphs(node):
    """照文件順序列出段落（文字方塊裡的段落接在所在段落後面），不走進 mc:Fallback。"""
    for child in node:
        if child.tag == _FALLBACK:
            continue
        if child.tag == _PARAGRAPH:
            yield child
        yield from _paragraphs(child)


def is_docx(path: str) -> bool:
    return path.lower().endswith(".docx")
