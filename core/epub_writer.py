"""匯出 EPUB 3（附 EPUB 2 的 NCX 目錄，舊閱讀器也看得到目錄）。

目錄照本程式的目錄：每個目錄項目（卷、章、特殊標題）一個 XHTML 檔，卷底下的章在導覽目錄裡
縮一層。正文一行一段（<p>），段首縮排交給 CSS（text-indent: 2em），原本行首的空白拿掉；
空行不另外輸出（段落之間的距離也交給 CSS）。第一個目錄項目之前的文字（書名、簡介）放在
目錄之前的一頁，不列在目錄裡。封面圖由呼叫端提供（PNG 位元組），沒有就不放。
用標準函式庫的 zipfile 組成，不需要另外裝套件。
"""

import html
import os
import tempfile
import uuid
from datetime import datetime, timezone
import zipfile
from dataclasses import dataclass, field

_INDENT_CHARS = " \t　"

_CSS = """body { margin: 0 5%; line-height: 1.8; }
h1, h2, h3 { text-align: center; margin: 2em 0 1.2em; line-height: 1.4; }
h1 { font-size: 1.5em; }
h2 { font-size: 1.3em; }
h3 { font-size: 1.15em; }
p { text-indent: 2em; margin: 0 0 0.6em; }
.front p { text-indent: 0; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-width: 100%; max-height: 100%; }
"""


@dataclass
class EpubSection:
    """一個目錄項目：depth 從 0 起（卷 0、卷裡的章 1…），lines 是標題底下的正文。"""
    title: str
    depth: int
    lines: list = field(default_factory=list)


def _xhtml(title: str, body: str, language: str, body_class: str = "") -> str:
    class_attr = f' class="{body_class}"' if body_class else ""
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{language}" lang="{language}">\n<head>\n<meta charset="utf-8"/>\n'
            f'<title>{html.escape(title)}</title>\n'
            '<link rel="stylesheet" type="text/css" href="../style.css"/>\n</head>\n'
            f'<body{class_attr}>\n{body}\n</body>\n</html>\n')


def _paragraphs(lines) -> str:
    out = []
    for line in lines:
        text = line.strip(_INDENT_CHARS).rstrip()
        if text.strip():
            out.append(f"<p>{html.escape(text)}</p>")
    return "\n".join(out)


def _nav_list(entries) -> str:
    """entries：[(depth, title, href)] 照順序；照 depth 巢狀成 <ol>。"""
    out = ["<ol>"]
    stack = [0]
    first = True
    for depth, title, href in entries:
        depth = max(0, depth)
        if first:
            first = False
        elif depth > stack[-1]:
            out.append("<ol>")
            stack.append(depth)
        else:
            out.append("</li>")
            while len(stack) > 1 and depth < stack[-1]:
                out.append("</ol></li>")
                stack.pop()
        out.append(f'<li><a href="{href}">{html.escape(title)}</a>')
    if entries:
        out.append("</li>")
    while len(stack) > 1:
        out.append("</ol></li>")
        stack.pop()
    out.append("</ol>")
    return "\n".join(out)


def _ncx_points(entries) -> str:
    out = []
    stack = []
    for order, (depth, title, href) in enumerate(entries, 1):
        while stack and stack[-1] >= depth:
            out.append("</navPoint>")
            stack.pop()
        out.append(f'<navPoint id="nav{order}" playOrder="{order}"><navLabel><text>{html.escape(title)}'
                   f'</text></navLabel><content src="{href}"/>')
        stack.append(depth)
    out.extend("</navPoint>" for _ in stack)
    return "\n".join(out)


def build_epub(path: str, title: str, author: str, sections, front_lines=(), cover_png: bytes | None = None,
               language: str = "zh") -> None:
    """寫出 EPUB 到 path。sections：EpubSection 照本文順序；front_lines：第一個目錄項目之前的文字。"""
    book_id = f"urn:uuid:{uuid.uuid4()}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = title.strip() or "未命名"
    files = []            # (檔名（在 OEBPS 底下）, 內容, media-type, 在不在 spine, manifest id, 屬性)
    toc_entries = []
    if cover_png:
        files.append(("images/cover.png", cover_png, "image/png", False, "cover-image", "cover-image"))
        cover_body = f'<div class="cover"><img src="../images/cover.png" alt="{html.escape(title)}"/></div>'
        files.append(("text/cover.xhtml", _xhtml(title, cover_body, language, "cover"),
                      "application/xhtml+xml", True, "cover", ""))
    front = _paragraphs(front_lines)
    if front:
        files.append(("text/front.xhtml", _xhtml(title, front, language, "front"),
                      "application/xhtml+xml", True, "front", ""))
    min_depth = min((section.depth for section in sections), default=0)
    for number, section in enumerate(sections, 1):
        depth = section.depth - min_depth
        heading = f"h{min(depth + 1, 3)}"
        body = f"<{heading}>{html.escape(section.title)}</{heading}>\n{_paragraphs(section.lines)}"
        name = f"text/c{number:05d}.xhtml"
        files.append((name, _xhtml(section.title, body, language), "application/xhtml+xml", True,
                      f"c{number:05d}", ""))
        toc_entries.append((depth, section.title, name))

    nav_entries = [(depth, label, href.replace("text/", "", 1)) for depth, label, href in toc_entries]
    nav = ('<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
           f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
           f'xml:lang="{language}" lang="{language}">\n<head>\n<meta charset="utf-8"/>\n'
           f'<title>{html.escape(title)}</title>\n</head>\n<body>\n<nav epub:type="toc" id="toc">\n'
           f'<h1>目錄</h1>\n{_nav_list(nav_entries)}\n</nav>\n</body>\n</html>\n')
    files.append(("text/nav.xhtml", nav, "application/xhtml+xml", False, "nav", "nav"))

    ncx = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           f'<head><meta name="dtb:uid" content="{book_id}"/></head>\n'
           f'<docTitle><text>{html.escape(title)}</text></docTitle>\n'
           f'<navMap>\n{_ncx_points(toc_entries)}\n</navMap>\n</ncx>\n')
    files.append(("toc.ncx", ncx, "application/x-dtbncx+xml", False, "ncx", ""))
    files.append(("style.css", _CSS, "text/css", False, "css", ""))

    manifest = "\n".join(
        f'<item id="{item_id}" href="{name}" media-type="{media}"' + (f' properties="{props}"' if props else "")
        + "/>" for name, _content, media, _spine, item_id, props in files)
    spine = "\n".join(f'<itemref idref="{item_id}"/>' for _name, _content, _media, in_spine, item_id, _props
                      in files if in_spine)
    creator = f"<dc:creator>{html.escape(author.strip())}</dc:creator>\n" if author.strip() else ""
    cover_meta = '<meta name="cover" content="cover-image"/>\n' if cover_png else ""
    opf = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           f'<dc:identifier id="bookid">{book_id}</dc:identifier>\n'
           f'<dc:title>{html.escape(title)}</dc:title>\n{creator}'
           f'<dc:language>{language}</dc:language>\n'
           f'<meta property="dcterms:modified">{modified}</meta>\n'
           f'{cover_meta}</metadata>\n<manifest>\n{manifest}\n</manifest>\n'
           f'<spine toc="ncx">\n{spine}\n</spine>\n</package>\n')
    container = ('<?xml version="1.0" encoding="utf-8"?>\n'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles>\n</container>\n')

    # 寫暫存檔、成功才取代目標檔：中途失敗時原本的檔案不會壞掉
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".",
                                         prefix=".txt-formatter-", suffix=".tmp")
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            # mimetype 必須是第一個檔、而且不壓縮
            archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
            for name, content, _media, _spine, _item_id, _props in files:
                archive.writestr(f"OEBPS/{name}", content, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
