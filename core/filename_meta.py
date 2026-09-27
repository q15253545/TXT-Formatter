"""從常見 TXT 檔名擷取書名、作者與連載狀態；也負責反過來組出建議匯出檔名。"""

import os
import re

from core.cn_numerals import chinese_to_arabic

_FILENAME_UNSAFE_REGEX = re.compile(r'[\\/:*?"<>|]')


def extract_filename_metadata(filename):
    """從常見 TXT 檔名擷取書名、作者與連載狀態。"""
    # 只移除真正的 .txt 副檔名。呼叫端可能已經去過一次副檔名，
    # 不能再用 splitext 把 [example.org]後半部誤當成副檔名。
    base = re.sub(r"(?i)\.txt$", "", os.path.basename(filename).strip()).strip()
    author_match = re.search(r"作者\s*[：:]\s*(.+?)(?=(?:[◎（(【\[]|$))", base)
    author = author_match.group(1).strip(" _-，,。") if author_match else ""

    if re.search(r"未\s*完[結结]|連載中|连载中|連載|连载", base, re.IGNORECASE):
        status = "未完結"
    elif re.search(r"已\s*完[結结]|完[結结]|完本", base, re.IGNORECASE):
        status = "已完結"
    elif re.search(r"更新[至到]", base):
        status = "未完結"            # 「【更新至第72章】」：還在更新，就是連載中
    else:
        status = "未指定"

    title = ""
    # 《《書名》》這種重複的書名號也要剝乾淨，不能留一個「《」在書名裡。
    book_match = re.search(r"《+\s*([^\n《》]+?)\s*》", base)
    if book_match:
        title = book_match.group(1).strip()
    else:
        bracket_match = re.search(r"【+\s*([^\n【】]+?)\s*】", base)
        if bracket_match:
            title = bracket_match.group(1).strip()

    if not title:
        cleaned = re.sub(r"\[[^\]]*\]|【[^】]*】|《[^》]*》", " ", base)
        cleaned = re.split(r"作者\s*[：:]", cleaned, maxsplit=1)[0]
        cleaned = re.sub(r"\b\d+\s*[-~～—至]\s*\d+\s*(?:章|回)?", " ", cleaned)
        cleaned = re.sub(r"(?:未\s*完[結结]|已\s*完[結结]|完[結结]|完本|連載中|连载中|連載|连载)", " ", cleaned)
        title = re.sub(r"\s+", " ", cleaned).strip(" _-◎，,")
    return title, author, status


# 匯出檔名格式：{變數} 換成書籍資料；[ ] 括起來的是可省略的段落，裡面的變數有一個沒有值就整段不寫
# （例如沒有卷，「[第{volume}卷]」就不出現）。連載中（含未指定）與已完結各用一個格式。
FILENAME_VARIABLES = (("title", "書名"), ("author", "作者"), ("volume", "最新卷"), ("chapter", "最新章"),
                      ("extra", "番外"), ("extra_count", "番外章數"), ("volume_count", "卷數"),
                      ("status", "連載狀態"))
# 整段【更新至…】也包在 [ ] 裡：目錄還沒有章節時不會寫出「【更新至第章】」
DEFAULT_ONGOING_TEMPLATE = "《{title}》[【更新至[第{volume}卷]第{chapter}章[+{extra}]】]作者：{author}"
# 以前的預設格式：設定檔裡存的是這個就換成現在的預設（使用者自己改過的不動）
_OLD_DEFAULT_TEMPLATES = {"《{title}》【更新至[第{volume}卷]第{chapter}章[+{extra}]】作者：{author}":
                          DEFAULT_ONGOING_TEMPLATE}


def upgrade_template(template: str) -> str:
    return _OLD_DEFAULT_TEMPLATES.get(template, template)
DEFAULT_COMPLETED_TEMPLATE = "《{title}》（完結[+{extra}]）作者：{author}"

_NUMBER_REGEX = re.compile(r"[0-9０-９]+(?:[.．][0-9０-９]+)?|[零〇一二兩两三四五六七八九十百千万萬億亿兆]+")
_TEMPLATE_TOKEN = re.compile(r"\{([A-Za-z_]+)\}|\[|\]")


def filename_number(text: str) -> str:
    """最新卷／章欄位裡的號碼：「第2卷」→「2」、「第一百二十章」→「120」。
    沒有號碼（例如「上卷」）就去掉前面的「第」與後面的單位，照原字寫。"""
    text = (text or "").strip()
    match = _NUMBER_REGEX.search(text)
    if match:
        value = chinese_to_arabic(match.group(0))
        if value > 0:
            return str(int(value)) if float(value).is_integer() else f"{value:g}"
    return re.sub(r"[卷部篇集季章回節节折幕]$", "", re.sub(r"^第", "", text)).strip()


def filename_fields(title, author, status, last_vol, last_ch, extra_count=0, volume_count=0) -> dict:
    """檔名變數的值；沒有值的是空字串（數量是 0 也算沒有值）。"""
    return {
        "title": (title or "").strip(),
        "author": (author or "").strip(),
        "volume": filename_number(last_vol),
        "chapter": filename_number(last_ch),
        "extra": "番外" if extra_count else "",
        "extra_count": str(extra_count) if extra_count else "",
        "volume_count": str(volume_count) if volume_count else "",
        "status": {"已完結": "已完結", "未完結": "連載中"}.get(status, ""),
    }


def render_filename_template(template: str, fields: dict) -> str:
    """照格式代入變數。[ ] 可以一層包一層，各自判斷要不要寫；沒有配對的 [ ] 照原樣寫出，
    認不得的 {變數} 也照原樣留著，預覽時看得出打錯字。"""
    stack = [[[], False]]           # 每一層：（寫出的片段, 這一層有沒有空的變數）
    position = 0
    for match in _TEMPLATE_TOKEN.finditer(template or ""):
        stack[-1][0].append(template[position:match.start()])
        position = match.end()
        token = match.group(0)
        if token == "[":
            stack.append([[], False])
        elif token == "]":
            if len(stack) == 1:
                stack[-1][0].append(token)
            else:
                parts, empty = stack.pop()
                if not empty:
                    stack[-1][0].append("".join(parts))
        elif match.group(1) in fields:
            value = fields[match.group(1)]
            stack[-1][1] = stack[-1][1] or not value
            stack[-1][0].append(value)
        else:
            stack[-1][0].append(token)
    stack[-1][0].append((template or "")[position:])
    while len(stack) > 1:
        parts, _empty = stack.pop()
        stack[-1][0].append("[" + "".join(parts))
    return "".join(stack[0][0])


def filename_template_for(status: str, ongoing: str, completed: str) -> str:
    return (completed or DEFAULT_COMPLETED_TEMPLATE) if status == "已完結" else (ongoing or DEFAULT_ONGOING_TEMPLATE)


def build_smart_filename(fields: dict, template: str) -> str:
    """組出建議的匯出檔名（含 .txt），並把檔名不能用的字元換成底線。
    簡繁轉換是另一件事，呼叫端自己決定要不要再套 core.script_convert.convert_script。"""
    name = re.sub(r"\s+", " ", render_filename_template(template, fields)).strip()
    name = name or fields.get("title") or "未命名"
    return _FILENAME_UNSAFE_REGEX.sub("_", name) + ".txt"
