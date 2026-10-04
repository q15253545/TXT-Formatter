"""EPUB 檔的純文字：照閱讀順序（spine）把每一章的 XHTML 轉成一行一段。

EPUB 是 zip：META-INF/container.xml 指到 OPF，OPF 的 manifest 列出檔案、spine 列出閱讀順序、
metadata 有書名與作者。用標準函式庫就讀得到，不需要另外裝套件。
標題（h1～h6）、段落（p、div、li…）各自一行，圖片、樣式、腳本不取；兩個檔案之間空一行。
有 DRM 的（META-INF/encryption.xml 加密了正文）讀不出字，直接報錯；只加密字型的照常讀。
"""

import posixpath
import re
import zipfile
import zlib
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from urllib.parse import unquote

_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF = "{http://www.idpf.org/2007/opf}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_ENC = "{http://www.w3.org/2001/04/xmlenc#}"
_FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
_DOCUMENT_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}

_BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr", "section",
           "article", "header", "footer", "dt", "dd", "figcaption", "caption", "hr", "table", "ul", "ol"}
_SKIPPED = {"script", "style", "head", "title", "svg", "math", "rt", "rp"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SPACES = re.compile(r"[ \t\r\n\f\v]+")


class EpubError(Exception):
    """不是 EPUB、檔案壞了，或正文加密（DRM）。"""


class _TextExtractor(HTMLParser):
    """區塊元素各自一行，br 換行；行內的空白、換行照 HTML 的規則縮成一個空格。
    標題（h1～h6）裡的 br 當成空格：「<h2>第一章<br/>山路</h2>」是一個標題，拆成兩行的話章名會變成正文、
    目錄只剩章號（要開自動合併標題才接得回來）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._current: list[str] = []
        self._skip_depth = 0
        self._heading_depth = 0

    def _flush(self):
        text = _SPACES.sub(" ", "".join(self._current)).strip()
        if text:
            self.lines.append(text)
        self._current = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIPPED:
            self._skip_depth += 1
        elif tag == "br":
            self._line_break()
        elif tag in _BLOCKS:
            self._flush()
            if tag in _HEADINGS:
                self._heading_depth += 1

    def _line_break(self):
        if self._heading_depth:
            self._current.append(" ")
        else:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag == "br":
            self._line_break()
        elif tag == "hr":
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIPPED:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCKS:
            self._flush()
            if tag in _HEADINGS:
                self._heading_depth = max(0, self._heading_depth - 1)

    def handle_data(self, data):
        if not self._skip_depth:
            self._current.append(data)

    def close(self):
        super().close()
        self._flush()


def _document_lines(markup: str) -> list:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.lines


def _decode(data: bytes, href: str) -> str:
    # 一律嚴格解碼：用「�」換掉解不開的字，匯入看起來成功、實際上字已經沒了（跟 TXT 不預設容錯同理）
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8")
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
    except UnicodeDecodeError as error:
        raise EpubError(f"章節檔 {href} 有解不開的字元") from error
    head = data[:200].decode("ascii", "replace")
    declared = re.search(r"encoding=[\"']([\w.-]+)", head)
    for codec in ((declared.group(1), "utf-8") if declared else ("utf-8",)):
        try:
            return data.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    raise EpubError(f"章節檔 {href} 有解不開的字元")


def _opf_path(archive: zipfile.ZipFile) -> str:
    try:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
    except KeyError:
        names = [name for name in archive.namelist() if name.lower().endswith(".opf")]
        if not names:
            raise EpubError("找不到 OPF（不是 EPUB 檔）")
        return names[0]
    rootfile = container.find(f".//{_CONTAINER}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise EpubError("container.xml 沒有指到 OPF")
    return rootfile.get("full-path")


def _encrypted_documents(archive: zipfile.ZipFile) -> set:
    """encryption.xml 裡加密的檔案（字型混淆不算，那是合法的字型保護）。"""
    try:
        root = ElementTree.fromstring(archive.read("META-INF/encryption.xml"))
    except (KeyError, ElementTree.ParseError):
        return set()
    found = set()
    for data in root.iter(f"{_ENC}EncryptedData"):
        method = data.find(f"{_ENC}EncryptionMethod")
        if method is not None and method.get("Algorithm") in _FONT_OBFUSCATION:
            continue
        reference = data.find(f".//{_ENC}CipherReference")
        if reference is not None and reference.get("URI"):
            found.add(unquote(reference.get("URI")))
    return found


def read_epub(path: str) -> tuple:
    """回傳（整本的文字（一行一段）, {"title", "author"}）。"""
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as error:
        raise EpubError(str(error)) from error
    with archive:
        opf_path = _opf_path(archive)
        try:
            opf = ElementTree.fromstring(archive.read(opf_path))
        except (KeyError, ElementTree.ParseError) as error:
            raise EpubError(str(error)) from error
        base = posixpath.dirname(opf_path)
        metadata = opf.find(f"{_OPF}metadata")
        info = {"title": "", "author": ""}
        if metadata is not None:
            title = metadata.find(f"{_DC}title")
            creator = metadata.find(f"{_DC}creator")
            info["title"] = (title.text or "").strip() if title is not None else ""
            info["author"] = (creator.text or "").strip() if creator is not None else ""
        manifest = {}
        for item in opf.iter(f"{_OPF}item"):
            href = item.get("href")
            if item.get("id") and href:
                manifest[item.get("id")] = (posixpath.normpath(posixpath.join(base, unquote(href))),
                                            item.get("media-type", ""))
        spine = opf.find(f"{_OPF}spine")
        order = [manifest[ref.get("idref")] for ref in (spine.iter(f"{_OPF}itemref") if spine is not None else ())
                 if ref.get("idref") in manifest]
        documents = [href for href, media in order if media in _DOCUMENT_TYPES or href.lower().endswith(
            (".xhtml", ".html", ".htm"))]
        if not documents:
            raise EpubError("書裡沒有可以讀的章節檔")
        encrypted = _encrypted_documents(archive)
        if any(href in encrypted for href in documents):
            raise EpubError("這本 EPUB 有加密（DRM），讀不出文字")
        lines = []
        for href in documents:
            try:
                data = archive.read(href)
            except KeyError as error:
                # 目錄（spine）列了卻不在檔案裡：略過的話會變成少章的書卻顯示匯入成功
                raise EpubError(f"缺少章節檔：{href}") from error
            except (zipfile.BadZipFile, zlib.error, OSError, NotImplementedError) as error:
                raise EpubError(f"章節檔 {href} 損壞：{error}") from error
            markup = _decode(data, href)
            chunk = _document_lines(markup)
            if chunk:
                if lines:
                    lines.append("")
                lines.extend(chunk)
    return "\n".join(lines), info


def is_epub(path: str) -> bool:
    return path.lower().endswith(".epub")
