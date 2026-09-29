"""章節結構辨識：把整份文件解析成一棵與 UI 元件無關的目錄樹。

用 SimpleTree 取代真正的樹狀元件，讓辨識規則可以獨立測試；介面那一側
只需要寫一段「把 SimpleTree 畫進真正元件」的轉接程式。

呼叫方式：
    ctx = BuildContext(raw_lines=..., options=..., user_chapter_rules=...,
                        auto_titles=..., force_lv1_chapters=..., ...,
                        invalid_tail_regex=...)
    result = build_document_structure(ctx, apply_format=True)

呼叫端負責：套用 apply_format=True 時是否要把 result.processed_render_lines
寫回文件、更新 auto_titles、重新映射強制卷／章的集合，以及把
result.tree 畫進真正的目錄元件——這些都牽涉具體的 UI／文件狀態，
不屬於「辨識」本身。
"""

import re
from dataclasses import dataclass, field, replace

from .simple_tree import SimpleTree
from .format_options import FormatOptions
from .title_markers import strip_persistent_title_marker, END_MARK_REGEX, parse_end_mark
from .text_format import (
    PUNCT_TRANS, HALF_PUNCT_TRANS, FULLWIDTH_DIGIT_TRANS, HALFWIDTH_DIGIT_TRANS, QUOTE_KEEP, convert_quotes,
)
from .cn_numerals import chinese_to_arabic, arabic_to_chinese
from .chapter_parse import (
    CN_NUM_FLOAT_PATTERN, INLINE_SPACE_REGEX,
    CN_NUM_PATTERN, parse_lv1, parse_lv2, parse_special, parse_mixed_volume_chapter_header,
    is_weak_numbered_title, is_valid_auto_title, strip_title_body, volume_dash_number,
    preserve_title_separator, original_number_text, resolve_chapter_number, heading_word, heading_words, title_length_limit, too_long_for_title,
    word_key, not_a_heading,
)
from .collection import analyze_collection_structure
from .paragraph_split import SPLIT_OFF, split_long_paragraphs
from .reflow import collapse_inline_spaces, reflow_lines
from .user_rules import match_user_chapter_rule


def format_custom_title(options: FormatOptions, extra_prefix: str, prefix_tag: str, num_val: float,
                         unit_tag: str, body_title: str, apply_format: bool, number_text=None) -> str:
    num_style, sep_style = options.num_style, options.sep_style
    # num_val == 0 同時被當成「這個標題沒有編號」的哨兵值（例如外傳、
    # 不帶編號的番外），所以不能單純放寬成 >= 0；只有原始文字確實寫著
    # 0／零時，才視為真正的第 0 章，避免「第0章 序幕」被輸出成「第章 序幕」。
    has_number = num_val > 0 or (
        number_text is not None
        and str(number_text).strip() != ""
        and chinese_to_arabic(str(number_text)) == 0
        and re.search(r"[0０零〇]", str(number_text))
    )
    if has_number:
        if not float(num_val).is_integer():
            final_num = str(num_val)
        else:
            num_int = int(num_val)
            if num_style == "中文數字": final_num = arabic_to_chinese(num_int)
            elif num_style == "阿拉伯數字": final_num = str(num_int)
            else: final_num = number_text or str(num_int)
        tag = f"{prefix_tag}{final_num}{unit_tag}"
    else:
        tag = f"{prefix_tag}{unit_tag}".strip()

    tag = f"{extra_prefix}{tag}".strip()

    # 開頭分隔符的清理已統一由 strip_title_body 負責。
    body = strip_title_body(body_title)

    if apply_format and options.remove_extra_spaces:
        body = INLINE_SPACE_REGEX.sub(' ', body)

    if not body: return tag
    if not tag: return body

    if sep_style == "半形空格": return f"{tag} {body}"
    elif sep_style == "全形空格": return f"{tag}　{body}"
    elif sep_style == "冒號": return f"{tag}：{body}"
    else: return f"{tag} {body}"


def format_punctuation_and_dialogue(options: FormatOptions, text: str) -> str:
    """標點、對話引號、數字的全形半形（一行一行，不帶狀態到下一行）。"""
    text, marker = strip_persistent_title_marker(text)
    if options.normalize_punct:
        text = text.translate(PUNCT_TRANS)
    elif options.halfwidth_punct:
        text = text.translate(HALF_PUNCT_TRANS)
    if options.quote_style != QUOTE_KEEP:
        text = convert_quotes(text, options.quote_style)
    if options.fullwidth_digits:
        text = text.translate(FULLWIDTH_DIGIT_TRANS)
    elif options.halfwidth_digits:
        text = text.translate(HALFWIDTH_DIGIT_TRANS)
    return text + ({"include": "[::]", "exclude": "[::X]",
                    "auto_work": "[::W]", "auto_title": "[::T]"}.get(marker, ""))


def find_merge_subtitle(raw_lines, start_index, total, invalid_tail_regex):
    """尋找獨立章號後的拆行章名，回傳清理後章名與其原始行號。"""
    from .reflow import PARAGRAPH_INDENT, DIALOGUE_OR_SENTENCE_REGEX
    from .chapter_parse import clean_merged_subtitle, is_valid_title

    peek = start_index + 1
    while peek < total and not raw_lines[peek].strip():
        peek += 1
    if peek >= total:
        return "", None

    raw_candidate = raw_lines[peek]
    # 段落縮排是「這行是正文，不是標題」最強的證據，必須在 strip 之前判斷。
    if PARAGRAPH_INDENT.match(raw_candidate):
        return "", None

    candidate, candidate_marker = strip_persistent_title_marker(raw_candidate.strip())
    if candidate_marker:              # [::X] 不是標題；[::]、[::w]、[::t] 是使用者指定的標題，不能被併掉
        return "", None
    cleaned, has_number_prefix = clean_merged_subtitle(candidate)
    if not has_number_prefix and (parse_lv2(candidate) or parse_lv1(candidate) or parse_special(candidate)):
        return "", None
    if not cleaned:
        return "", None
    # 帶有對話引號或句末標點的行幾乎必定是正文；標題極少長這樣。
    if not has_number_prefix and DIALOGUE_OR_SENTENCE_REGEX.search(candidate):
        return "", None
    # A repost header ("author  2016-02-08 05:31:58 report views: 1200") or an author / word-count /
    # publish line right under a bare heading is not its chapter name.
    # Author chatter asking for tips / votes ("please tip", "monthly votes") isn't a chapter name either.
    from .ad_scan import SUPPORT_REQUEST_WORDS, looks_like_forum_header, meta_line_kind
    meta = meta_line_kind(candidate)
    if (looks_like_forum_header(candidate) or (meta is not None and meta[1] == "高")
            or any(word in candidate for word in SUPPORT_REQUEST_WORDS)):
        return "", None

    # 有明確小節編號的拆行章名可較長；其他格式用一般的保守判定。
    if has_number_prefix:
        if len(cleaned) > 180:
            return "", None
    elif not is_valid_title(candidate, invalid_tail_regex):
        return "", None

    cleaned = re.sub(r"^[\s，,、:：\-—]+", "", cleaned).strip()
    cleaned = strip_title_body(cleaned)
    return cleaned, peek


@dataclass
class BuildContext:
    """辨識一份文件所需的全部輸入，以及過程中累積的輸出。"""
    raw_lines: list
    options: FormatOptions
    user_chapter_rules: list = field(default_factory=list)
    auto_titles: dict = field(default_factory=dict)
    force_lv1_chapters: set = field(default_factory=set)
    force_lv2_chapters: set = field(default_factory=set)
    invalid_tail_regex: object = None
    # 自動補齊卷號與卷名：本文沒寫卷標題時，從卷結尾行、章號重新起算、每章前面帶的卷號（「卷一 山路 第一章」）
    # 推出缺少的卷，找得到卷名（卷結尾行「第一卷 山路 完」、章前面的「卷一 山路」）就一起寫上。
    # 介面上是「章節管理」的開關，預設關閉；核心保留預設開啟，方便單獨測試。
    infer_volumes: bool = True
    # 合併下行標題（「自動合併標題」開關的一半，預覽）：不排版時，只有章號的標題把下一行的章名接上來顯示在目錄上，
    # 本文不動；記在 StructureResult.merged_titles，按「套用到本文」才寫進去。
    merge_titles: bool = False
    # 自動辨識不認的字（「辨識章節」關掉的章節單位、特殊標題），見 chapter_parse.CHAPTER_WORDS 等
    disabled_words: frozenset = frozenset()
    # 特殊標題（序章、番外、外傳…）改當成卷（1）或章（2）：只記跟預設不同的，見 chapter_parse.SPECIAL_LEVELS
    special_levels: dict = field(default_factory=dict)
    # 合併重複標題（「自動合併標題」開關的另一半，預覽）：同一章的標題重複出現、中間不到 DUPLICATE_CONTENT_LIMIT 字（多半是作者的話）
    # 時，後面那個不算新的一章；記在 StructureResult.absorbed_titles，按「套用到本文」才刪掉那一行。
    skip_duplicate_titles: bool = False
    tree: SimpleTree = field(default_factory=SimpleTree)
    chapter_raw_map: dict = field(default_factory=dict)
    chapter_index_map: dict = field(default_factory=dict)
    chapter_records: dict = field(default_factory=dict)

    def match_custom_title(self, text):
        return match_user_chapter_rule(text, self.user_chapter_rules, self.invalid_tail_regex)

    def format_custom_title(self, extra_prefix, prefix_tag, num_val, unit_tag, body_title,
                             apply_format, number_text=None):
        return format_custom_title(self.options, extra_prefix, prefix_tag, num_val, unit_tag,
                                    body_title, apply_format, number_text)

    def format_punctuation_and_dialogue(self, text):
        return format_punctuation_and_dialogue(self.options, text)

    def find_merge_subtitle(self, start_index, total):
        title, row = find_merge_subtitle(self.raw_lines, start_index, total, self.invalid_tail_regex)
        if row is not None and self._protected_title(row):
            return "", None
        return title, row

    def _protected_title(self, row) -> bool:
        """使用者指定的標題（強制層級、自訂規則）：預覽合併不能把它吃掉。"""
        return (row in self.force_lv1_chapters or row in self.force_lv2_chapters
                or self.match_custom_title(strip_persistent_title_marker(self.raw_lines[row].strip())[0]) is not None)


@dataclass
class RenderState:
    """單次解析的狀態；由作品、卷、章與附錄處理函式共用。"""
    idx: int = 0
    total: int = 0
    current_lv1_node: str = ""
    current_work_node: str = ""
    current_work_name: str = ""
    last_found_vol: str = ""
    last_found_ch: str = ""
    collection_active: bool = False
    invalid_tail_regex: object = None
    volume_nodes: dict = field(default_factory=dict)
    # 從「卷一 卷名 第N章」拆出來的卷：本文還沒有這一行卷標題（套用前只是預覽）
    split_volumes: set = field(default_factory=set)
    latest_volume_node: str = None     # 最近一個（最上層的）卷節點：同名卷只跟它合併
    work_nodes: dict = field(default_factory=dict)
    work_volume_nodes: dict = field(default_factory=dict)
    collection_info: dict = field(default_factory=dict)
    collection_combined: dict = field(default_factory=dict)
    collection_work_lines: dict = field(default_factory=dict)
    processed_render_lines: list = field(default_factory=list)
    opts: FormatOptions = field(default_factory=FormatOptions)
    # 卷結尾行（第一卷終、【第二卷终】…）與推定卷需要的資料
    last_chapter_number: float = None
    current_volume_number: int = None
    real_volume_numbers: dict = field(default_factory=dict)
    volume_end_marks: list = field(default_factory=list)
    arabic_volume_numbers: bool = False
    # 合併下行標題的預覽：標題行號 → (章名所在行號, 接上去的章名)
    merged_titles: dict = field(default_factory=dict)
    # 重複標題的預覽：重複那一行的行號 → 保留的標題行號
    absorbed_titles: dict = field(default_factory=dict)


@dataclass
class StructureResult:
    tree: SimpleTree
    chapter_raw_map: dict
    chapter_index_map: dict
    chapter_records: dict
    processed_render_lines: list
    last_found_vol: str
    last_found_ch: str
    # 推定卷：本文沒有卷標題，但從卷結尾行／章號重新起算推得出來的卷。
    # 只存在目錄樹裡（不在 chapter_raw_map），值為 {"title", "number", "row"}，
    # row 是卷內第一個項目的原始行號——要寫回本文時卷標題就插在那一行前面。
    virtual_volumes: dict = field(default_factory=dict)
    # 從每章標題前面拆出來、本文還沒有卷標題的卷（跟推定卷一樣是預覽）
    split_volumes: set = field(default_factory=set)
    # 合併下行標題的預覽（只在不排版、開著 merge_titles 時有）：標題行號 → (章名所在行號, 章名)
    merged_titles: dict = field(default_factory=dict)
    # 重複標題的預覽：重複那一行的行號 → 保留的標題行號
    absorbed_titles: dict = field(default_factory=dict)


DUPLICATE_CONTENT_LIMIT = 100


def _same_title_body(first: str, second: str) -> bool:
    """章名一樣（不看空白、標點），或其中一個只有章號，才算同一章重複；
    「乙（上）」「乙（下）」這種分上下的不算。"""
    first, second = (re.sub(r"[\s\W_]+", "", body or "") for body in (first, second))
    return not first or not second or first == second


def _find_duplicate_heading(ctx: BuildContext, start: int, identity, body: str):
    """從 start 往下找：同一章的標題又出現一次、中間的正文不到 DUPLICATE_CONTENT_LIMIT 字，回傳那一行。
    中間遇到別的標題、或正文夠多了，就不是重複。"""
    from .word_count import char_count
    content = 0
    for row in range(start, min(len(ctx.raw_lines), start + 40)):
        text, marker = strip_persistent_title_marker(ctx.raw_lines[row].strip())
        if not text:
            continue
        if marker == 'exclude':
            content += char_count(text)
            continue
        if marker or ctx._protected_title(row) or parse_lv1(text) or parse_special(text):
            return None               # 中間隔著卷、特殊標題或使用者指定的標題：不是同一段
        parsed = parse_lv2(text)
        if parsed and not is_weak_numbered_title(text):
            return row if _same_chapter_identity(parsed, *identity) and _same_title_body(parsed[5], body) else None
        content += char_count(text)
        if content >= DUPLICATE_CONTENT_LIMIT:
            return None
    return None


def _next_named_chapter(ctx: BuildContext, state):
    """下一個非空行是有章名的章節標題（不是卷、特殊標題、使用者指定的標題）：回傳那一行，否則 None。"""
    for row in range(state.idx + 1, state.total):
        text, marker = strip_persistent_title_marker(ctx.raw_lines[row].strip())
        if not text:
            continue
        if marker or ctx._protected_title(row) or not is_valid_auto_title(text, state.invalid_tail_regex) \
                or parse_lv1(text) or parse_special(text) or is_weak_numbered_title(text):
            return None
        parsed = parse_lv2(text)
        return row if parsed and strip_title_body(parsed[5] or "") else None
    return None


_PERIOD_TAIL = re.compile(r"[。.．]\s*$")
_SENTENCE_MARK = re.compile(r"[。！？!?；;]")
_FIRST_HEAD = re.compile(r"^\s*第\s*[1１一]\s*[章回節节][ 　:：\-—·、]")


def _period_title_fits(state, line_str: str) -> bool:
    """句號結尾的「第N章 章名。」：句號結尾一般會擋（正文句子），但章號剛好接上一章（或是第 1 章，
    每卷重新數）、章名很短、中間沒有別的句子標點時，是作者習慣在章名後面加句號，照樣算一章。"""
    # 每一行都會問：先用最便宜的看（開頭附近有「第」、最後是句號），正文行在這裡就結束
    if "第" not in line_str[:4] or not _PERIOD_TAIL.search(line_str[-4:])             or too_long_for_title(line_str, title_length_limit(state.invalid_tail_regex)):
        return False
    parsed = parse_lv2(line_str)
    if not parsed or parsed[2] != "第" or not parsed[3] or is_weak_numbered_title(line_str):
        return False
    title = parsed[5].rstrip(" 　。.．")
    if len(title) > 20 or _SENTENCE_MARK.search(title):
        return False
    number, last = parsed[3], state.last_chapter_number
    if last is not None and number == last + 1:
        return True
    # 每卷重新數的第 1 章沒有上一章可以對：再嚴一點，章號後面要有分隔、章名裡沒有逗號
    return number == 1 and bool(_FIRST_HEAD.match(line_str)) and not re.search(r"[，,]", title)


_END_NOTE = re.compile(r"[（(【\[]?\s*本?[部卷篇集季章回節节折幕]\s*完本?\s*[）)】\]]?\s*$")


def _chapter_with_end_note(line_str: str) -> bool:
    """「第12章 過河（本卷完）」：章名後面註明這一卷完了，還是一章（不是單獨的卷尾那一行）。"""
    parsed = parse_lv2(line_str)
    if not parsed or parsed[2] != "第":
        return False
    title = _END_NOTE.sub("", parsed[5]).strip(" 　:：-—·")
    return len(title) >= 2 and title not in ("完結", "完结", "結束", "结束")


def _repeats_previous_heading(ctx: BuildContext, idx: int, line_str: str, title_check) -> bool:
    """超過標題長度、靠「長的正式章名」才算標題的行，開頭就是上一行的標題（「第5章 出發」下一行又是
    「第5章 出發“……”」）：是標題重複一次再接正文，不是另一章。"""
    if not too_long_for_title(line_str, title_length_limit(title_check)):
        return False
    previous = idx - 1
    while previous >= 0 and not ctx.raw_lines[previous].strip():
        previous -= 1
    if previous < 0:
        return False
    heading = re.sub(r"\s+", "", ctx.raw_lines[previous])
    return heading.startswith("第") and re.sub(r"\s+", "", line_str).startswith(heading)


def _mixed_header_allowed(ctx: BuildContext, mixed: dict, title_check) -> bool:
    """卷章同一行是自動辨識：卷、章的單位都沒被關掉，卷名、章名也各自不超過標題長度。"""
    if ctx.disabled_words and {word_key(mixed["volume_unit"]), word_key(mixed["chapter_unit"])} & ctx.disabled_words:
        return False
    limit = title_length_limit(title_check)
    return (not too_long_for_title(mixed.get("volume_body", ""), limit)
            and not too_long_for_title(mixed.get("chapter_body", ""), limit))


def build_chapter_records(ctx: BuildContext, collection_info):
    """從已確認目錄建立統一資料；缺章不再自行用另一套正則判斷。"""
    ctx.chapter_records = {}
    for node, row in ctx.chapter_raw_map.items():
        text, marker = strip_persistent_title_marker(ctx.raw_lines[row].strip())
        stored = ctx.auto_titles.get(row)
        combined = collection_info["combined"].get(row)
        source = combined["remainder"] if combined else text
        mixed = parse_mixed_volume_chapter_header(source, _split_short_volumes(ctx))
        custom = ctx.match_custom_title(source)
        chapter = parse_lv2(source)
        volume = parse_lv1(source)
        title = ctx.tree.item(node, "text")
        if row in ctx.force_lv1_chapters:
            kind = "volume"
        elif row in ctx.force_lv2_chapters:
            kind = "chapter"
        elif collection_info["active"] and title == collection_info["work_lines"].get(row):
            kind = "work"
        elif combined and title == combined["work"]:
            kind = "work"
        elif ctx.tree.get_children(node) or (volume and not mixed and not combined and not custom):
            kind = "volume"
        elif custom and custom["level"] == 1:
            kind = "volume"
        else:
            kind = "chapter"
        number, prefix, number_text, unit = 0, "", None, ""
        if kind == "chapter":
            if custom and custom.get("special"):
                # 自訂特殊標題：編號自成一組（續章1、續章2…），不跟「第N章」一起數
                number, prefix = custom["number"], custom["special"]
            elif custom:
                number = custom["number"]
                prefix = "第"
            elif mixed:
                number, prefix = mixed["chapter_number"], "第"
            elif chapter:
                number, prefix, unit = chapter[3], chapter[2], chapter[4]
                number_text = original_number_text(source, chapter[4])
            elif stored:
                number, prefix = stored.get("number", 0), stored.get("prefix", "")
            if chapter and chapter[2] == "番外":
                prefix = "番外"
        dash_volume = ((custom or {}).get("volume") or volume_dash_number(source)) if kind == "chapter" else None
        source_kind = ("manual" if marker == "include" or row in ctx.force_lv1_chapters
                       or row in ctx.force_lv2_chapters else "rule" if custom else "auto")
        ctx.chapter_records[node] = {"title": title, "kind": kind, "number": number, "prefix": prefix,
                                      "special": bool(custom and custom.get("special")),
                                      "source": source_kind, "number_text": number_text, "unit": unit,
                                      "dash_volume": dash_volume}
    _resolve_record_numbers(ctx)


def _resolve_record_numbers(ctx: BuildContext):
    """章號有兩種讀法的（一一零一＝1101 或 111），照同一層前一章選接得上的；缺章檢查、
    連續編號看的都是這裡的章號。"""
    previous = {}
    for node in sorted(ctx.chapter_records, key=ctx.chapter_raw_map.get):
        record = ctx.chapter_records[node]
        if record["kind"] != "chapter" or not record["number"]:
            continue
        parent = ctx.tree.parent(node)
        if record["prefix"] == "第":
            record["number"] = resolve_chapter_number(record["number"], record["number_text"], previous.get(parent))
        previous[parent] = record["number"]


def record_title(ctx: BuildContext, state: RenderState, item_id, title_text, processed_render_lines,
                  apply_format, raw_idx, manual_marked=False, auto_marker=""):
    options = state.opts
    ctx.chapter_raw_map[item_id] = raw_idx
    original_marker = strip_persistent_title_marker(ctx.raw_lines[raw_idx].strip())[1]
    if not auto_marker and original_marker in {"auto_work", "auto_title"}:
        auto_marker = original_marker
    suffix = "[::]" if manual_marked else {"auto_work": "[::W]", "auto_title": "[::T]"}.get(auto_marker, "")
    if apply_format:
        if options.keep_separator:
            source_title = strip_persistent_title_marker(ctx.raw_lines[raw_idx].strip())[0]
            title_text = preserve_title_separator(title_text, source_title)
        title_text = ctx.format_punctuation_and_dialogue(title_text)
        ctx.tree.item(item_id, text=title_text)
        manage_spacing = (
            options.remove_extra_empty
            or options.add_empty
            or options.format_title
        )
        if manage_spacing:
            while processed_render_lines and processed_render_lines[-1] == "":
                processed_render_lines.pop()

        empty_before = 0
        if options.add_empty:
            empty_before = 2
        elif options.format_title:
            empty_before = 1

        if processed_render_lines:
            processed_render_lines.extend([""] * empty_before)

        render_title = title_text + suffix
        processed_render_lines.append(render_title)

        empty_after = 1 if options.format_title else 0
        processed_render_lines.extend([""] * empty_after)

        ctx.chapter_index_map[item_id] = len(processed_render_lines) - empty_after
    else:
        render_title = title_text + suffix
        processed_render_lines.append(render_title)
        ctx.chapter_index_map[item_id] = len(processed_render_lines)


def render_collection_title(ctx: BuildContext, state: RenderState, apply_format, collection_record,
                             line_str, raw_line, title_raw_idx, manual_marked):
    """建立作品／卷父層，移除章標題中的重複作品前綴。"""
    state.current_work_name = collection_record['work']
    volume_title = collection_record['volume']
    display_row = len(state.processed_render_lines) + 1
    if state.current_work_name not in state.work_nodes:
        state.current_work_node = ctx.tree.insert('', 'end', text=state.current_work_name, open=True)
        state.work_nodes[state.current_work_name] = state.current_work_node
        if apply_format:
            record_title(ctx, state, state.current_work_node, state.current_work_name,
                          state.processed_render_lines, True, title_raw_idx, manual_marked,
                          auto_marker='auto_work')
        else:
            ctx.chapter_raw_map[state.current_work_node] = title_raw_idx
            ctx.chapter_index_map[state.current_work_node] = display_row
    else:
        state.current_work_node = state.work_nodes[state.current_work_name]
    volume_key = (state.current_work_name, re.sub(r'\s+', '', volume_title))
    if volume_key not in state.work_volume_nodes:
        state.current_lv1_node = ctx.tree.insert(state.current_work_node, 'end', text=volume_title, open=True)
        state.work_volume_nodes[volume_key] = state.current_lv1_node
        if apply_format:
            record_title(ctx, state, state.current_lv1_node, volume_title,
                          state.processed_render_lines, True, title_raw_idx, False)
        else:
            ctx.chapter_raw_map[state.current_lv1_node] = title_raw_idx
            ctx.chapter_index_map[state.current_lv1_node] = display_row
    else:
        state.current_lv1_node = state.work_volume_nodes[volume_key]
    data = collection_record['data']
    if collection_record['kind'] == 'chapter':
        _, _, prefix, number, unit, body_title = data
        body_title = strip_title_body(body_title)
        if state.opts.keep_number or not apply_format:     # only layout rewrites the number
            number_match = re.search(r'第\s*(' + CN_NUM_FLOAT_PATTERN + r')\s*' + re.escape(unit), line_str)
            number_text = number_match.group(1) if number_match else str(int(number))
            chapter_title = f'第{number_text}{unit} {body_title}'.strip()
        else:
            chapter_title = ctx.format_custom_title('', prefix, number, unit, body_title, apply_format)
        if prefix != '番外':
            state.last_found_ch = ctx.format_custom_title('', prefix, number, unit, '', apply_format).strip()
    elif collection_record['kind'] == 'special':
        _, _, special_tag, body_title = data
        body_title = re.sub(r'^[\s，,、:：\-—]+', '', body_title).strip()
        chapter_title = f'{special_tag} {body_title}'.strip()
    else:
        chapter_title = collection_record['remainder'].strip()
    chapter_node = ctx.tree.insert(state.current_lv1_node, 'end', text=chapter_title)
    if apply_format:
        record_title(ctx, state, chapter_node, chapter_title, state.processed_render_lines, True,
                      title_raw_idx, manual_marked,
                      auto_marker='auto_title' if collection_record['kind'] != 'chapter' else '')
    else:
        state.processed_render_lines.append(raw_line)
        ctx.chapter_raw_map[chapter_node] = title_raw_idx
        ctx.chapter_index_map[chapter_node] = display_row
    state.last_found_vol = volume_title
    state.idx += 1


def render_mixed_title(ctx: BuildContext, state: RenderState, apply_format, mixed_data, title_raw_idx,
                        manual_marked):
    """拆開同行卷章表頭，並與隨後的正式章標題去重。"""
    volume_key = (mixed_data['volume_unit'], mixed_data['volume_number'],
                  re.sub(r'\s+', '', mixed_data['volume_body']))
    # Only layout rewrites the numbers; a TOC rebuild shows the text as written (the last layout's number style
    # must not leak into the labels, e.g. after undoing that layout)
    keep_number = state.opts.keep_number or not apply_format
    if keep_number:
        volume_tag = mixed_data['volume_raw']
        if apply_format and not volume_tag.startswith('第'):
            # 拆出來的「卷一」寫成「第一卷」（數字照原文）：單獨一行的「卷一」預設不算卷，
            # 排版後重新辨識會認不出來（跟「套用到本文」同一種寫法）。
            volume_tag = f"第{volume_tag[1:].strip()}{mixed_data['volume_unit']}"
    else:
        volume_tag = ctx.format_custom_title('', '第', mixed_data['volume_number'],
                                              mixed_data['volume_unit'], '', apply_format)
    volume_tag += mixed_data.get('volume_note', '')
    volume_title = f"{volume_tag} {mixed_data['volume_body']}".strip()
    chapter_body = mixed_data['chapter_body']
    chosen_raw_idx = title_raw_idx
    consume_end = state.idx + 1
    peek = state.idx + 1
    while peek < state.total and (not ctx.raw_lines[peek].strip()):
        peek += 1
    if peek < state.total:
        next_clean, next_marker = strip_persistent_title_marker(ctx.raw_lines[peek].strip())
        next_data = parse_lv2(next_clean)
        # 必須是同一個編號、同一個單位才算重複：int() 會把「第1.5章」截成 1，
        # 把小數章、番外整個吞掉（第1.5章 插曲 → 第1章 插曲）。
        same_chapter = bool(next_data) and (
            next_data[3] == mixed_data['chapter_number']
            and next_data[4] == mixed_data['chapter_unit']
            and next_data[2] == '第')
        # Only a real repeat is folded in: same number and unit, the same name (or one side has none), no marker
        # and not a user-designated heading. A different name is a separate chapter (both stay; the duplicate
        # chapters check lists them), and [::] / forced levels are the user's call.
        if (not next_marker and next_data and (not is_weak_numbered_title(next_clean)) and same_chapter
                and _same_title_body(chapter_body, next_data[5]) and not ctx._protected_title(peek)):
            # 下一行只有章號（「第十章」）時，章名還是用這一行的，不然章名整個不見
            chapter_body = next_data[5].strip() or chapter_body
            chosen_raw_idx = peek
            consume_end = peek + 1
    if not chapter_body and consume_end == state.idx + 1 and _merging(ctx, state, apply_format):
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            chapter_body = merged_title
            _note_merge(state, apply_format, title_raw_idx, merged_index, merged_title)
            consume_end = merged_index + 1
    if keep_number:
        chapter_tag = f"第{mixed_data['chapter_number_text']}{mixed_data['chapter_unit']}"
    else:
        chapter_tag = ctx.format_custom_title('', '第', mixed_data['chapter_number'],
                                               mixed_data['chapter_unit'], '', apply_format)
    chapter_title = f'{chapter_tag} {chapter_body}'.strip()
    # 同一卷的混合表頭每章都會出現一次，要併在同一個卷底下；但中間隔了別的卷之後
    # 又出現同名的卷（轉貼時重複貼了卷名），就不能併回前面那一卷，不然後面的章節
    # 會跑到中間那一卷的前面，目錄順序整個亂掉。
    volume_is_new = state.volume_nodes.get(volume_key) != state.latest_volume_node or volume_key not in state.volume_nodes
    if volume_is_new:
        state.current_lv1_node = ctx.tree.insert('', 'end', text=volume_title)
        state.volume_nodes[volume_key] = state.current_lv1_node
        if not mixed_data['volume_raw'].startswith('第'):
            state.split_volumes.add(state.current_lv1_node)
        if apply_format:
            record_title(ctx, state, state.current_lv1_node, volume_title, state.processed_render_lines,
                          True, title_raw_idx, manual_marked)
        else:
            ctx.chapter_raw_map[state.current_lv1_node] = title_raw_idx
    else:
        state.current_lv1_node = state.volume_nodes[volume_key]
    state.latest_volume_node = state.current_lv1_node
    _note_volume(state, state.current_lv1_node, mixed_data['volume_number'], mixed_data['volume_number_text'])
    state.last_chapter_number = mixed_data['chapter_number']
    chapter_node = ctx.tree.insert(state.current_lv1_node, 'end', text=chapter_title)
    if apply_format:
        record_title(ctx, state, chapter_node, chapter_title, state.processed_render_lines, True,
                      chosen_raw_idx, manual_marked)
    else:
        first_render_row = len(state.processed_render_lines) + 1
        state.processed_render_lines.extend(ctx.raw_lines[state.idx:consume_end])
        ctx.chapter_raw_map[chapter_node] = chosen_raw_idx
        ctx.chapter_index_map[chapter_node] = first_render_row + (chosen_raw_idx - state.idx)
        if volume_is_new:
            ctx.chapter_index_map[state.current_lv1_node] = first_render_row
    state.last_found_vol = volume_tag
    state.last_found_ch = chapter_tag
    state.idx = consume_end


def render_volume_title(ctx: BuildContext, state: RenderState, apply_format, custom_title, forced_level,
                         m_lv1, m_lv2, line_str, raw_line, title_raw_idx, manual_marked):
    """處理卷級標題及人工層級，保留既有父層與卷去重規則。"""
    if custom_title and (not forced_level):
        arc, p_fix, v_num, v_unit, v_body = (
            '', '第' if custom_title['number'] else '', custom_title['number'],
            '卷' if custom_title['number'] else '', custom_title['title'])
    elif m_lv1:
        arc, p_fix, v_num, v_unit, v_body = m_lv1
    elif m_lv2:
        arc, vol, p_fix, v_num, v_unit, v_body = m_lv2
        arc = f'{arc} {vol}'.strip()
    else:
        arc, p_fix, v_num, v_unit, v_body = ('', '', 0, '', line_str)
    merged = False
    if not v_body and _merging(ctx, state, apply_format):
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            v_body = merged_title
            merged = True
            _note_merge(state, apply_format, title_raw_idx, merged_index, merged_title)
            state.idx = merged_index
    extra_prefix = f'{arc} ' if arc else ''
    if apply_format and (merged or not state.opts.keep_number or not state.opts.keep_separator):
        title = ctx.format_custom_title(extra_prefix, p_fix, v_num, v_unit, v_body, apply_format,
                                         original_number_text(line_str, v_unit))
    else:
        title = f'{line_str} {v_body}' if merged else line_str
    state.last_found_vol = ctx.format_custom_title(extra_prefix, p_fix, v_num, v_unit, '', apply_format).strip()
    if state.collection_active and state.current_work_node and (not arc):
        volume_key = (state.current_work_name, re.sub(r'\s+', '', title))
        if volume_key in state.work_volume_nodes:
            state.current_lv1_node = state.work_volume_nodes[volume_key]
            if not apply_format:
                state.processed_render_lines.append(raw_line)
        else:
            state.current_lv1_node = ctx.tree.insert(state.current_work_node, 'end', text=title, open=True)
            state.work_volume_nodes[volume_key] = state.current_lv1_node
            record_title(ctx, state, state.current_lv1_node, title, state.processed_render_lines,
                          apply_format, title_raw_idx, manual_marked)
    else:
        volume_key = (v_unit, v_num, re.sub(r'\s+', '', v_body))
        # 只跟上一個卷合併（同上：隔了別的卷又重複出現的卷名另外算一卷）
        if (v_num and volume_key in state.volume_nodes and (not forced_level)
                and state.volume_nodes[volume_key] == state.latest_volume_node):
            state.current_lv1_node = state.volume_nodes[volume_key]
            if not apply_format:
                state.processed_render_lines.append(raw_line)
        else:
            state.current_lv1_node = ctx.tree.insert('', 'end', text=title)
            if v_num:
                state.volume_nodes[volume_key] = state.current_lv1_node
            record_title(ctx, state, state.current_lv1_node, title, state.processed_render_lines,
                          apply_format, title_raw_idx, manual_marked)
        state.latest_volume_node = state.current_lv1_node
        _note_volume(state, state.current_lv1_node, v_num,
                     original_number_text(line_str, v_unit) if v_unit else '')
    state.idx += 1


def _same_chapter_identity(parsed, arc, vol, prefix, number, unit) -> bool:
    """parse_lv2 的結果跟目前這一章是不是「同一章」：篇名、卷名、前綴、編號、單位全部一致。"""
    p_arc, p_vol, p_prefix, p_num, p_unit = parsed[:5]
    return ((p_arc or "").strip(), (p_vol or "").strip(), p_prefix, p_num, p_unit) == \
        ((arc or "").strip(), (vol or "").strip(), prefix, number, unit)


def render_chapter_title(ctx: BuildContext, state: RenderState, apply_format, custom_title, forced_level,
                          m_lv1, m_lv2, line_str, title_raw_idx, manual_marked):
    """處理章級標題、下行合併與相鄰重複章名。"""
    if custom_title and (not forced_level):
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = (
            '', '', '第' if custom_title['number'] else '', custom_title['number'],
            '章' if custom_title['number'] else '', custom_title['title'])
    elif m_lv2:
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = m_lv2
    elif m_lv1:
        arc, ch_prefix, ch_num, ch_unit, ch_body = m_lv1
        vol = ''
    else:
        arc, vol, ch_prefix, ch_num, ch_unit, ch_body = ('', '', '', 0.0, '', line_str)
    ch_body = strip_title_body(ch_body)
    merged = False
    if not ch_body and _merging(ctx, state, apply_format):
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            ch_body = merged_title
            merged = True
            _note_merge(state, apply_format, title_raw_idx, merged_index, merged_title)
            state.idx = merged_index
    is_phantom = False
    # 幽靈標題＝同一個標題被拆成兩行（例如「第1章」單獨一行、下一行才是
    # 真正的完整標題）。「本章沒有標題文字」不足以證明該刪除——「第1章」
    # 後面直接接「第2章 開始」是完全合法的空章。判定為幽靈需同時滿足：
    #   1. 本行沒有被人工收錄（[::] 是使用者明確表達「這是章節」）
    #   2. 下一個標題的「編號」與本行相同——真正被拆行的標題編號會一致，
    #      而「第1章／第2章」這種相鄰空章編號不同，應予保留。
    # 下一行是卷或特殊標題不算幽靈：「第1章」後面直接接「第二卷」是合法的空章，刪掉會連正文一起消失。
    if not ch_body and not manual_marked:
        peek = state.idx + 1
        while peek < state.total:
            nxt = ctx.raw_lines[peek].strip()
            if not nxt:
                peek += 1
                continue
            next_clean, next_marker = strip_persistent_title_marker(nxt)
            if next_marker != 'exclude' and is_valid_auto_title(next_clean, state.invalid_tail_regex):
                next_lv2 = parse_lv2(next_clean)
                if next_lv2 and not is_weak_numbered_title(next_clean):
                    if _same_chapter_identity(next_lv2, arc, vol, ch_prefix, ch_num, ch_unit):
                        is_phantom = True
            break
    if is_phantom:
        state.idx += 1
        return
    # 只有章號、沒有正文，底下緊接著另一章有章名的標題：同一章的兩個標題（網站的貼文編號＋作者的章名，
    # 「第40章」「第32章 過河」）。「自動合併標題」開著時只留有章名的那個，跟重複標題一樣記在 absorbed_titles。
    # 有章名的空章（書裡自帶的目錄、正文遺失的章）不動：那不是多出來的標題。
    if (ctx.skip_duplicate_titles and not apply_format and m_lv2 and not custom_title and not ch_body
            and not merged and not manual_marked and not forced_level and not ctx._protected_title(title_raw_idx)):
        named = _next_named_chapter(ctx, state)
        if named is not None:
            state.absorbed_titles[title_raw_idx] = named
            state.idx += 1
            return
    dup_cands, peek = ([(line_str, ch_body, title_raw_idx)], state.idx + 1)
    while peek < state.total:
        nxt, nxt_marker = strip_persistent_title_marker(ctx.raw_lines[peek].strip())
        if not nxt:
            peek += 1
            continue
        if not nxt_marker and is_valid_auto_title(nxt, state.invalid_tail_regex) \
                and not ctx._protected_title(peek):
            p_data = parse_lv2(nxt)
            if p_data and (not is_weak_numbered_title(nxt)):
                p_body = p_data[5]
                # 只比數字不足以證明是同一章：「第1章 開始」與「第1節 插曲」
                # 是兩個不同的標題；「甲篇 第1章」與「乙篇 第1章」也是。
                # 單位、前綴、篇名、卷名都一致，而且章名一樣（或其中一個只有章號）才算重複；
                # 「第12章 風起」「第12章 雲湧」多半是作者編號打錯，兩章都留著，交給「合併重複章節」勾選。
                if ch_num > 0 and _same_chapter_identity(p_data, arc, vol, ch_prefix, ch_num, ch_unit) \
                        and _same_title_body(ch_body, strip_title_body(p_body)):
                    dup_cands.append((nxt, p_body, peek))
                    peek += 1
                    continue
        break
    if len(dup_cands) > 1:
        chosen_raw, ch_body, chosen_raw_idx = max(dup_cands, key=lambda x: len(x[1]))
        state.idx = peek
        best_data = parse_lv2(chosen_raw)
        if best_data:
            arc, vol, ch_prefix, ch_num, ch_unit, ch_body = best_data
            ch_body = strip_title_body(ch_body)
    else:
        chosen_raw = line_str
        chosen_raw_idx = title_raw_idx
        state.idx += 1
    extra_prefix = ''
    if arc:
        extra_prefix += arc.strip() + ' '
    if vol:
        extra_prefix += vol.strip() + ' '
    if ctx.skip_duplicate_titles and not apply_format and ch_num:
        duplicate = _find_duplicate_heading(ctx, state.idx, (arc, vol, ch_prefix, ch_num, ch_unit), ch_body)
        if duplicate is not None:
            state.absorbed_titles[duplicate] = chosen_raw_idx
    if ch_num and ch_prefix == '第':
        ch_num = resolve_chapter_number(ch_num, original_number_text(chosen_raw, ch_unit), state.last_chapter_number)
    # 「2-1 過河」＝第 2 卷（季）第 1 章：在第 2 卷底下才寫成「第1章 過河」；卷號對不上（本文沒寫那一卷）
    # 就照原樣留著，不然卷號會不見
    dash_volume = (custom_title.get('volume') if custom_title and not forced_level
                   else volume_dash_number(chosen_raw))
    keep_dash = bool(dash_volume) and dash_volume != state.current_volume_number
    if apply_format and not keep_dash and (merged or not state.opts.keep_number or not state.opts.keep_separator):
        chosen_title = ctx.format_custom_title(extra_prefix, ch_prefix, ch_num, ch_unit, ch_body,
                                                apply_format, original_number_text(chosen_raw, ch_unit))
    else:
        chosen_title = f'{chosen_raw} {ch_body}' if merged else chosen_raw
    if keep_dash:
        # 檔名的最新章照作者的寫法「第2-10章」，卷號已經在裡面了
        state.last_found_ch = f'第{dash_volume}-{int(ch_num)}章' if ch_num else chosen_raw
        state.last_found_vol = ''
    elif ch_prefix != '番外':
        # 「第0章」的 0 會被當成「沒有編號」，最新章變成「第章」。只有這種
        # 情況才把原始編號文字傳進去，其餘維持原本的阿拉伯數字寫法（最新章
        # 會用在建議檔名上，「第2章」比「第二章」好排序）。
        zero_number_text = original_number_text(chosen_raw, ch_unit) if not ch_num else None
        state.last_found_ch = ctx.format_custom_title(
            extra_prefix, ch_prefix, ch_num, ch_unit, '', apply_format, zero_number_text).strip()
    if ch_num:
        state.last_chapter_number = ch_num
    parent = state.current_lv1_node if state.current_lv1_node else ''
    item_id = ctx.tree.insert(parent, 'end', text=chosen_title)
    record_title(ctx, state, item_id, chosen_title, state.processed_render_lines, apply_format,
                  chosen_raw_idx, manual_marked)


def render_special_title(ctx: BuildContext, state: RenderState, apply_format, m_spec, line_str,
                          title_raw_idx, manual_marked):
    """處理簡介、序言及後記等沒有章號的標題。"""
    arc, vol, spec_tag, spec_body = m_spec
    spec_body = re.sub(r'^[\s，,、:：\-—]+', '', spec_body).strip()
    spec_body = strip_title_body(spec_body)
    if not spec_body and _merging(ctx, state, apply_format):
        merged_title, merged_index = ctx.find_merge_subtitle(state.idx, state.total)
        if merged_title:
            spec_body = merged_title
            _note_merge(state, apply_format, title_raw_idx, merged_index, merged_title)
            state.idx = merged_index
    # 沒有標題文字的序章／楔子照樣是一個章節（後面接「第1章」也不刪）；拆行的情況由合併下行標題處理。
    extra_prefix = ''
    if arc:
        extra_prefix += arc.strip() + ' '
    if vol:
        extra_prefix += vol.strip() + ' '
    if apply_format and not state.opts.keep_separator:
        title = ctx.format_custom_title(extra_prefix, '', 0.0, spec_tag, spec_body, apply_format)
    else:
        title = f'{extra_prefix}{spec_tag} {spec_body}'.strip()
    if state.collection_active and state.current_work_node:
        special_parent = state.current_lv1_node or state.current_work_node
    else:
        # 卷裡面的特殊標題（書中間的「简介：……」、卷末的後記）掛在那一卷底下，目錄才照閱讀順序；
        # 卷之前的（內容簡介、序章）放最上層。
        special_parent = state.current_lv1_node or ''
    item_id = ctx.tree.insert(special_parent, 'end', text=title)
    record_title(ctx, state, item_id, title, state.processed_render_lines, apply_format, title_raw_idx,
                 manual_marked)
    state.idx += 1


def _render_custom_special(ctx: BuildContext, state: RenderState, apply_format, custom_title, line_str, raw_line,
                           title_raw_idx, manual_marked):
    """「辨識章節 → 特殊標題」自己新增的字（續章12）：照原文顯示，不改寫成「第N章」。
    設成卷時後面的章掛在它底下；設成章時跟序章、番外一樣掛在目前的卷底下。"""
    if custom_title['level'] == 1:
        whole = ' '.join(part for part in (custom_title['arc'], custom_title['tag'], custom_title['title']) if part)
        render_volume_title(ctx, state, apply_format, dict(custom_title, number=0, title=whole), 0, None, None,
                            line_str, raw_line, title_raw_idx, manual_marked)
        return
    render_special_title(ctx, state, apply_format, (custom_title['arc'], '', custom_title['tag'], custom_title['title']),
                         line_str, title_raw_idx, manual_marked)
    if custom_title['number']:
        # 有編號的（續章12）是連載的進度：檔名的最新章寫它
        state.last_found_ch = custom_title['tag']


def _merging(ctx: BuildContext, state: RenderState, apply_format) -> bool:
    """只有章號的標題要不要把下一行的章名接上來：排版時看排版選項，
    不排版（重建目錄）時看「自動合併標題」的預覽開關。"""
    return state.opts.merge_title if apply_format else ctx.merge_titles


def _note_merge(state: RenderState, apply_format, title_row, subtitle_row, subtitle):
    """預覽模式記下合併了哪兩行：本文顯示預覽、「套用到本文」都靠這份資料。"""
    if not apply_format:
        state.merged_titles[title_row] = (subtitle_row, subtitle)


def _note_volume(state: RenderState, node, number, number_text):
    """記下真正的卷標題：卷號（推定卷避開重複卷號用）與編號寫法。"""
    number = int(number) if number and float(number).is_integer() else None
    state.current_volume_number = number
    if node and not state.collection_active:
        state.real_volume_numbers[node] = number
    if number_text and re.search(r'[0-9０-９]', str(number_text)):
        state.arabic_volume_numbers = True


def _is_end_line(state: RenderState, end_mark) -> bool:
    """卷結尾行一律視為結尾；章結尾行（第一章完）要跟上一章同號才算——
    「第100章 終」緊接在第99章後面，是一個叫「終」的真正章節。"""
    if end_mark['level'] == 'volume':
        return True
    number = end_mark['number']
    return number is None or state.last_chapter_number is None or number == state.last_chapter_number


def _close_volume(state: RenderState, row, end_mark, line_str):
    """卷結尾行：目前這一卷到此為止，後面的章節不再掛在它底下。"""
    number = end_mark['number'] if end_mark['number'] is not None else state.current_volume_number
    state.volume_end_marks.append((row, number, end_mark['unit'], end_mark.get('name') or ''))
    if re.search(r'[0-9０-９]', line_str):
        state.arabic_volume_numbers = True
    state.current_lv1_node = ''
    state.current_volume_number = None


def _split_short_volumes(ctx: BuildContext) -> bool:
    """「卷一 卷名 第N章」拆成卷＋章：自動補齊卷號與卷名開著才拆。"""
    return ctx.infer_volumes


# 章節標題前面帶著的卷號（「卷一 山路 第一章 出發」的「卷一」、「第二卷 城裡 第3章」的「第二卷」）
_PREFIX_VOLUME = re.compile(r"^(?:第\s*(?P<n1>" + CN_NUM_PATTERN + r")\s*(?P<u1>[部卷篇集季])"
                            r"|(?P<u2>[部卷篇])\s*(?P<n2>" + CN_NUM_PATTERN + r"))(?=[\s（(]|$)")


def _prefix_volume_groups(ctx: BuildContext, state: RenderState) -> dict:
    """每章標題前面都帶著卷號、本文卻沒有另外寫卷標題、整行也沒拆成卷＋章時（例如混合表頭不被允許），
    照前綴的卷號把相鄰的章節分成一卷一卷。"""
    tree, raw_map = ctx.tree, ctx.chapter_raw_map
    top = list(tree.get_children(''))
    numbers = {}
    for node in top:
        if node not in raw_map or tree.get_children(node):
            continue
        parsed = parse_lv2(strip_persistent_title_marker(ctx.raw_lines[raw_map[node]].strip())[0])
        match = _PREFIX_VOLUME.match((parsed[0] or '').strip()) if parsed else None
        if match:
            value = chinese_to_arabic(match.group("n1") or match.group("n2"))
            if value and float(value).is_integer():
                numbers[node] = (int(value), match.group("u1") or match.group("u2"))
    if len(numbers) < 2:
        return {}
    arabic = (state.opts.num_style == '阿拉伯數字'
              or (state.opts.num_style != '中文數字' and state.arabic_volume_numbers))
    virtual, current, current_node = {}, None, None
    for node in top:
        key = numbers.get(node)
        if key is None:
            # 前綴中斷（序章、後記、沒帶卷號的章）：不屬於任何一卷
            current = current_node = None
            continue
        if key != current:
            number, unit = key
            title = f"第{number if arabic else arabic_to_chinese(number)}{unit}"
            current_node = tree.insert('', 'end', text=title)
            virtual[current_node] = {'title': title, 'number': number, 'row': raw_map[node], 'unit': unit}
            current = key
        tree.reparent(node, current_node)
    return virtual


def _dash_volume_groups(ctx: BuildContext, state: RenderState) -> dict:
    """「2-1」「2-2」（卷號-章號）的章落在別的卷底下、本文也沒有第 2 卷：照卷號補一卷。
    單位與數字寫法跟前面真正的卷一樣（「第一季」後面補「第二季」）。"""
    tree, raw_map, real = ctx.tree, ctx.chapter_raw_map, state.real_volume_numbers
    written = set(real.values())
    virtual, current, current_node = {}, None, None
    for node in sorted(ctx.chapter_records, key=raw_map.get):
        record = ctx.chapter_records[node]
        if record['kind'] != 'chapter':
            continue
        volume, parent = record.get('dash_volume'), tree.parent(node)
        if not volume or volume in written:
            # 中間夾著一般的章、或本文已經寫了這一卷：接下來的另外算
            current = current_node = None
            continue
        if volume != current:
            written_parent = parse_lv1(tree.item(parent, 'text')) if parent in real else None
            unit = written_parent[3] if written_parent and written_parent[3] else '卷'
            arabic = (state.opts.num_style == '阿拉伯數字'
                      or (state.opts.num_style != '中文數字' and state.arabic_volume_numbers))
            title = f"第{volume if arabic else arabic_to_chinese(volume)}{unit}"
            current_node = tree.insert('', 'end', text=title)
            virtual[current_node] = {'title': title, 'number': volume, 'row': raw_map[node], 'unit': unit}
            current = volume
        tree.reparent(node, current_node)
    return virtual


def infer_virtual_volumes(ctx: BuildContext, state: RenderState) -> dict:
    """補上本文沒寫、但推得出來的卷（只加在目錄樹，不改本文）。

    有些作者卷首不寫「第二卷」，只在卷尾寫【第二卷终】；或者整本只有卷尾
    沒有卷首。以「卷標題」和「卷結尾行」當分界，把目錄切成一段一段：
      - 段落後面接著「第N卷終」→ 這段是第N卷；
      - 段落前面是「第M卷終」→ 這段是第M+1卷；
      - 全書開頭、後面接著真正的「第K卷」（K>1）→ 這段是第K-1卷（補第一卷）。
    同一段內章號又從第1章起算，代表中間換了卷，再往前（或往後）切開一卷。
    完全沒有卷標題也沒有卷結尾行的文件，不推定任何卷。
    """
    if not ctx.infer_volumes or state.collection_active:
        return {}
    dashed = _dash_volume_groups(ctx, state)
    if dashed:
        _finish_virtual_volumes(ctx, state, dashed, next(iter(dashed.values()))['unit'])
        if state.last_found_ch.startswith('第') and '-' in state.last_found_ch and state.last_found_vol:
            # 最新章在補出來的卷裡：卷號已經在最新卷，章只寫章號
            state.last_found_ch = '第' + state.last_found_ch.split('-', 1)[1]
        return dashed
    if not state.real_volume_numbers:
        # 每章前面都帶著卷號的寫法：照前綴分卷（比從卷結尾行、章號重算推測可靠）
        grouped = _prefix_volume_groups(ctx, state)
        if grouped:
            _finish_virtual_volumes(ctx, state, grouped, next(iter(grouped.values()))['unit'])
            return grouped
    if not (state.real_volume_numbers or state.volume_end_marks):
        return {}
    tree = ctx.tree
    real = state.real_volume_numbers
    raw_map = ctx.chapter_raw_map

    def formal_number(node):
        record = ctx.chapter_records.get(node)
        if not record or record['kind'] != 'chapter' or record['prefix'] == '番外' or record.get('special'):
            return None
        number = record['number']
        return int(number) if number and number > 0 and float(number).is_integer() else None

    events = []
    for node in tree.get_children(''):
        if node in real:
            events.append((raw_map.get(node, 0), 0, 'volume', node))
            events.extend((raw_map.get(child, 0), 1, 'item', child) for child in tree.get_children(node))
        elif node in raw_map:
            events.append((raw_map[node], 1, 'item', node))
    events.extend((row, 0, 'end', number) for row, number, _unit, _name in state.volume_end_marks)
    end_names = {number: name for _row, number, _unit, name in state.volume_end_marks if number and name}
    events.sort(key=lambda event: (event[0], event[1]))

    blocks = []
    block = {'container': None, 'items': [], 'prev': None}
    for _row, _order, kind, payload in events:
        if kind == 'item':
            block['items'].append(payload)
            continue
        boundary = ('volume', real[payload]) if kind == 'volume' else ('end', payload)
        block['next'] = boundary
        blocks.append(block)
        block = {'container': payload if kind == 'volume' else None, 'items': [], 'prev': boundary}
    block['next'] = None
    blocks.append(block)

    unit = state.volume_end_marks[0][2] if state.volume_end_marks else '卷'
    if state.opts.num_style == '阿拉伯數字':
        arabic = True
    elif state.opts.num_style == '中文數字':
        arabic = False
    else:
        arabic = state.arabic_volume_numbers
    used = {number for number in real.values() if number}
    virtual = {}

    def segments(items):
        # 章號從第1章重新起算（前面已經超過第1章）就是換卷的位置。
        result, last = [[]], None
        for node in items:
            number = formal_number(node)
            if number is not None:
                if number == 1 and last is not None and last > 1 and result[-1]:
                    result.append([])
                last = number
            result[-1].append(node)
        return result

    def assign(seg, number):
        title = f"第{number if arabic else arabic_to_chinese(number)}{unit}"
        if end_names.get(number):
            title += f" {end_names[number]}"
        node = tree.insert('', 'end', text=title)
        for child in seg:
            tree.reparent(child, node)
        used.add(number)
        virtual[node] = {'title': title, 'number': number, 'row': raw_map[seg[0]]}

    for block in blocks:
        items, prev, nxt, container = block['items'], block['prev'], block['next'], block['container']
        formal = [i for i, node in enumerate(items) if formal_number(node) is not None]
        if not formal:
            continue
        # 全書開頭的簡介／序章、全書結尾的後記不屬於任何一卷。
        start = formal[0] if prev is None else 0
        end = formal[-1] + 1 if nxt is None else len(items)
        segs = segments(items[start:end])
        if container is not None:
            # 真正的第V卷底下章號重新起算、後面接著第N卷終（N>V）：
            # 後半段其實是沒寫卷標題的下一卷。
            current = real.get(container)
            if not (current and nxt and nxt[0] == 'end' and nxt[1] and nxt[1] > current):
                continue
            for offset, seg in enumerate(reversed(segs[1:])):
                number = nxt[1] - offset
                if number <= current or number in used:
                    break
                assign(seg, number)
            continue
        if nxt and nxt[0] == 'end' and nxt[1]:
            numbers = [nxt[1] - offset for offset in range(len(segs))][::-1]
        elif prev and prev[0] == 'end' and prev[1]:
            numbers = [prev[1] + 1 + offset for offset in range(len(segs))]
        elif nxt and nxt[0] == 'volume' and nxt[1] and nxt[1] > 1:
            numbers = [nxt[1] - 1 - offset for offset in range(len(segs))][::-1]
        else:
            continue
        limit = nxt[1] if nxt and nxt[0] == 'volume' and nxt[1] else None
        for seg, number in zip(segs, numbers):
            if number < 1 or number in used or (limit is not None and number >= limit):
                continue
            assign(seg, number)

    _finish_virtual_volumes(ctx, state, virtual, unit)
    return virtual


def _finish_virtual_volumes(ctx: BuildContext, state: RenderState, virtual: dict, unit: str):
    """推定卷建好之後：最上層照本文順序排好，最後一章落在推定卷裡時更新「最新卷」。"""
    if not virtual:
        return
    tree, raw_map = ctx.tree, ctx.chapter_raw_map

    def formal_number(node):
        record = ctx.chapter_records.get(node)
        if not record or record['kind'] != 'chapter' or record['prefix'] == '番外' or record.get('special'):
            return None
        number = record['number']
        return int(number) if number and number > 0 and float(number).is_integer() else None

    order = dict(raw_map)
    order.update((node, info['row']) for node, info in virtual.items())
    tree.sort_children('', key=lambda node: order.get(node, 0))
    # 最後一個正式章節落在推定卷裡，檔名的「最新卷」就是這一卷。
    last_chapter = max((node for node in raw_map if formal_number(node) is not None),
                       key=lambda node: raw_map[node], default=None)
    if last_chapter is not None and tree.parent(last_chapter) in virtual:
        # 跟真正卷標題的「最新卷」同一種寫法（format_custom_title），檔名才一致。
        number = virtual[tree.parent(last_chapter)]['number']
        state.last_found_vol = ctx.format_custom_title('', '第', number, unit, '', False)


def _build_with_reflow(ctx: BuildContext, write_text: bool) -> StructureResult:
    """排版選項「整理段落換行」「長段落」：先認出章節標題，把標題之間正文的硬換行接回去、
    太長的段落拆開，再照常排版。

    回傳結果的 chapter_raw_map 換算回「整理前」的行號，呼叫端（整份排版、只排選取章節）
    拿來對照舊行號的方式不用改。"""
    plain = replace(ctx.options, reflow_paragraphs=False, long_paragraph=SPLIT_OFF)
    probe = build_document_structure(replace(ctx, options=plain), apply_format=False, write_text=False)
    protected = (set(probe.chapter_raw_map.values()) | set(ctx.force_lv1_chapters)
                 | set(ctx.force_lv2_chapters) | set(ctx.auto_titles))
    if ctx.options.reflow_paragraphs:
        new_lines, row_map = reflow_lines(ctx.raw_lines, protected)
    else:
        new_lines, row_map = list(ctx.raw_lines), {row: row for row in range(len(ctx.raw_lines))}
    if ctx.options.long_paragraph != SPLIT_OFF:
        split_protected = {row_map[row] for row in protected if row in row_map}
        new_lines, split_map = split_long_paragraphs(new_lines, split_protected, ctx.options.long_paragraph)
        row_map = {row: split_map[moved_row] for row, moved_row in row_map.items()}

    def moved(rows):
        return {row_map[row] for row in rows if row in row_map}

    target = replace(
        ctx, raw_lines=new_lines, options=plain,
        auto_titles={row_map[row]: record for row, record in ctx.auto_titles.items() if row in row_map},
        force_lv1_chapters=moved(ctx.force_lv1_chapters), force_lv2_chapters=moved(ctx.force_lv2_chapters))
    result = build_document_structure(target, apply_format=True, write_text=write_text)
    # 新行號 → 整理前的行號（接起來的幾行取第一行）。只換算原本就認得的標題不夠：
    # 接回去之後才認出來的標題也要換回舊行號，不然 chapter_raw_map 會新舊行號混在一起。
    original = {}
    for row in sorted(row_map):
        original.setdefault(row_map[row], row)
    result.chapter_raw_map = {node: original.get(row, row) for node, row in result.chapter_raw_map.items()}
    ctx.tree, ctx.chapter_raw_map = target.tree, result.chapter_raw_map
    ctx.chapter_index_map, ctx.chapter_records = target.chapter_index_map, target.chapter_records
    return result


def build_document_structure(ctx: BuildContext, apply_format: bool = False,
                              write_text: bool = True) -> StructureResult:
    """協調辨識與輸出；各類標題由獨立處理函式更新 RenderState。

    write_text 只影響 chapter_index_map 的意義：write_text 為 False（或
    apply_format 為 False）時不會真的產生格式化文字，此時 chapter_index_map
    改為直接對應原始行號（raw_index + 1），而不是迴圈過程中暫時算出的
    「假設要重新輸出」位置——呼叫端若真的要重新輸出格式化文字，也必須自行
    重新以 apply_format=True、write_text=True 呼叫一次，那一輪算出的位置
    才是實際輸出後的正確位置。
    """
    if apply_format and (ctx.options.reflow_paragraphs or ctx.options.long_paragraph != SPLIT_OFF):
        return _build_with_reflow(ctx, write_text)
    state = RenderState()
    ctx.tree = SimpleTree()
    ctx.chapter_index_map = {}
    ctx.chapter_raw_map = {}
    state.opts = ctx.options
    opts = state.opts
    state.total = len(ctx.raw_lines)
    state.idx = 0
    state.current_lv1_node, state.last_found_vol, state.last_found_ch = ('', '', '')
    state.volume_nodes = {}
    state.collection_info = analyze_collection_structure(ctx.raw_lines, opts.structure)
    if opts.structure != '單本小說':
        for row, record in ctx.auto_titles.items():
            if record.get('kind') == 'work':
                # 作品名稱照本文目前這一行：繁簡轉換、手動改過作品名之後，舊的記錄只說明
                # 「這一行是作品」，名稱不能拿舊的蓋回去。
                current = ''
                if 0 <= row < len(ctx.raw_lines):
                    current = strip_persistent_title_marker(ctx.raw_lines[row].strip())[0]
                state.collection_info['work_lines'][row] = current or record['title']
                state.collection_info['active'] = True
    state.collection_active = state.collection_info['active']
    state.collection_combined = state.collection_info['combined']
    state.collection_work_lines = state.collection_info['work_lines']
    state.work_nodes, state.work_volume_nodes = ({}, {})
    state.current_work_node, state.current_work_name = ('', '')
    state.processed_render_lines = []
    state.invalid_tail_regex = ctx.invalid_tail_regex
    while state.idx < state.total:
        title_raw_idx = state.idx
        raw_line = ctx.raw_lines[state.idx]
        line_str, persistent_marker = strip_persistent_title_marker(raw_line.strip())
        manual_marked = persistent_marker == 'include' and bool(line_str)
        excluded_marked = persistent_marker == 'exclude'
        mixed_data = (None if excluded_marked
                      else parse_mixed_volume_chapter_header(line_str, _split_short_volumes(ctx)))
        collection_record = state.collection_combined.get(state.idx) if not excluded_marked else None
        collection_work_name = state.collection_work_lines.get(state.idx) if not excluded_marked else None
        custom_title = None if excluded_marked else ctx.match_custom_title(line_str)
        stored_title = ctx.auto_titles.get(state.idx)
        if not stored_title and persistent_marker in {'auto_work', 'auto_title'}:
            stored_title = {'kind': 'work' if persistent_marker == 'auto_work' else 'chapter', 'title': line_str}
        forced_level = 1 if state.idx in ctx.force_lv1_chapters else 2 if state.idx in ctx.force_lv2_chapters else 0
        if forced_level:
            mixed_data = collection_record = collection_work_name = None
        if mixed_data is not None and not (manual_marked or custom_title or stored_title) \
                and not _mixed_header_allowed(ctx, mixed_data, state.invalid_tail_regex):
            mixed_data = None
        auto_title = is_valid_auto_title(line_str, state.invalid_tail_regex)
        if auto_title and _repeats_previous_heading(ctx, state.idx, line_str, state.invalid_tail_regex):
            auto_title = False
        if not auto_title and mixed_data is None and _period_title_fits(state, line_str):
            auto_title = True
        if auto_title and ctx.disabled_words and heading_words(line_str) & ctx.disabled_words:
            auto_title = False
        if (auto_title or mixed_data is not None) and not_a_heading(line_str):
            auto_title = False
            mixed_data = None
        special_level = 0
        if ctx.special_levels and (auto_title or manual_marked) and not (custom_title or stored_title or forced_level
                                                                          or mixed_data is not None):
            special_level = ctx.special_levels.get(heading_word(line_str), 0)
        is_title = not excluded_marked and (manual_marked or forced_level or stored_title or (mixed_data is not None) or (collection_record is not None) or (collection_work_name is not None) or (custom_title is not None) or auto_title)
        if state.idx in state.absorbed_titles and not (manual_marked or forced_level):
            is_title = False
        end_mark = None
        if line_str and len(line_str) <= 40 and not (manual_marked or forced_level or custom_title):
            end_mark = parse_end_mark(line_str)
        if end_mark:
            if _is_end_line(state, end_mark):
                is_title = False
                if end_mark['level'] == 'volume' and not state.collection_active:
                    _close_volume(state, title_raw_idx, end_mark, line_str)
        elif is_title and (not (manual_marked or forced_level or custom_title)) and END_MARK_REGEX.search(line_str) \
                and not _chapter_with_end_note(line_str):
            is_title = False
        if not line_str:
            if apply_format:
                if not opts.remove_extra_empty:
                    state.processed_render_lines.append('')
            else:
                state.processed_render_lines.append('')
            state.idx += 1
            continue
        if apply_format and opts.remove_indent:
            line_str = line_str.lstrip()
        if is_title:
            if state.collection_active and collection_work_name:
                state.current_work_name = collection_work_name
                if state.current_work_name not in state.work_nodes:
                    state.current_work_node = ctx.tree.insert('', 'end', text=state.current_work_name, open=True)
                    state.work_nodes[state.current_work_name] = state.current_work_node
                    record_title(ctx, state, state.current_work_node, state.current_work_name,
                                 state.processed_render_lines, apply_format, title_raw_idx,
                                 manual_marked, auto_marker='auto_work')
                else:
                    state.current_work_node = state.work_nodes[state.current_work_name]
                    if not apply_format:
                        state.processed_render_lines.append(raw_line)
                state.current_lv1_node = ''
                state.idx += 1
                continue
            if state.collection_active and collection_record:
                render_collection_title(ctx, state, apply_format, collection_record, line_str, raw_line,
                                        title_raw_idx, manual_marked)
                continue
            if mixed_data:
                render_mixed_title(ctx, state, apply_format, mixed_data, title_raw_idx, manual_marked)
                continue
            m_spec = parse_special(line_str)
            m_lv1 = parse_lv1(line_str)
            m_lv2 = parse_lv2(line_str)
            is_lv1 = False
            is_lv2 = False
            if title_raw_idx in ctx.force_lv1_chapters:
                is_lv1 = True
            elif title_raw_idx in ctx.force_lv2_chapters:
                is_lv2 = True
            elif custom_title:
                is_lv1 = custom_title['level'] == 1
                is_lv2 = custom_title['level'] == 2
            elif stored_title:
                is_lv1 = stored_title['kind'] == 'volume'
                is_lv2 = not is_lv1
            elif special_level:
                is_lv1 = special_level == 1
                is_lv2 = special_level == 2
            elif m_lv1:
                is_lv1 = True
            elif m_lv2 and (manual_marked or not is_weak_numbered_title(line_str)):
                is_lv2 = True
            elif manual_marked and (not m_spec):
                is_lv2 = True
            if custom_title and custom_title.get('special') and not forced_level:
                _render_custom_special(ctx, state, apply_format, custom_title, line_str, raw_line, title_raw_idx,
                                       manual_marked)
                continue
            if is_lv1:
                render_volume_title(ctx, state, apply_format, custom_title, forced_level, m_lv1, m_lv2,
                                    line_str, raw_line, title_raw_idx, manual_marked)
                continue
            if is_lv2:
                render_chapter_title(ctx, state, apply_format, custom_title, forced_level, m_lv1, m_lv2,
                                     line_str, title_raw_idx, manual_marked)
                continue
            if m_spec:
                render_special_title(ctx, state, apply_format, m_spec, line_str, title_raw_idx, manual_marked)
                continue
        body = raw_line
        if apply_format:
            body = ctx.format_punctuation_and_dialogue(body)
            if opts.remove_extra_spaces:
                # 段尾（半形／全形）空白全部刪掉；段落中間兩個中文字之間的空白
                # 直接刪掉，中英數之間收斂成一個半形空格（collapse_inline_spaces）。
                # 段首縮排不算「多餘」空格，要留著——要不要縮排交給「增加縮排」
                # ／「去除縮排」決定，不能因為勾了這項就把縮排也一併吃掉。
                content = body.strip()
                indent = body[:len(body) - len(body.lstrip())]
                body = indent + collapse_inline_spaces(content) if content else ''
            elif opts.auto_indent or opts.remove_indent:
                body = body.lstrip()
            if opts.auto_indent:
                body = ('    ' if opts.halfwidth_indent else '　　') + body.lstrip()
            elif opts.remove_indent:
                body = body.lstrip()
            state.processed_render_lines.append(body)
            if opts.add_paragraph_empty:
                state.processed_render_lines.append('')
        else:
            state.processed_render_lines.append(ctx.raw_lines[state.idx])
        state.idx += 1
    if apply_format:
        state.last_found_vol = ctx.format_punctuation_and_dialogue(state.last_found_vol)
        state.last_found_ch = ctx.format_punctuation_and_dialogue(state.last_found_ch)
    build_chapter_records(ctx, state.collection_info)
    virtual_volumes = infer_virtual_volumes(ctx, state)
    if not (write_text and apply_format):
        for item_id, raw_index in ctx.chapter_raw_map.items():
            ctx.chapter_index_map[item_id] = raw_index + 1
    return StructureResult(
        tree=ctx.tree,
        chapter_raw_map=ctx.chapter_raw_map,
        chapter_index_map=ctx.chapter_index_map,
        chapter_records=ctx.chapter_records,
        processed_render_lines=state.processed_render_lines,
        last_found_vol=state.last_found_vol,
        last_found_ch=state.last_found_ch,
        virtual_volumes=virtual_volumes,
        split_volumes=set(state.split_volumes),
        merged_titles=dict(state.merged_titles),
        absorbed_titles=dict(state.absorbed_titles),
    )
