"""文件層級的章節結構分析：多作品合集、弱格式章節候選、缺章檢查。"""

import bisect
import re
from collections import Counter, defaultdict

from .title_markers import strip_persistent_title_marker
from .title_blocks import TEMPLATES
from .user_rules import PRESET_RULES, preset_match
from .chapter_parse import (
    CN_NUM_PATTERN,
    chapter_range_end,
    MAX_TITLE_LENGTH,
    is_noise_prefix,
    parse_lv1,
    parse_lv2,
    parse_special,
    parse_weak_numbered_title,
    is_weak_numbered_title,
    parse_mixed_volume_chapter_header,
    too_long_for_title,
)

COLLECTION_COMBINED_REGEX = re.compile(
    r"^\s*(.{1,30}?)\s+(第\s*" + CN_NUM_PATTERN + r"\s*[部卷篇集季])\s+(.+?)\s*$",
    re.IGNORECASE,
)


def plausible_work_name(text):
    """書名推測採保守條件；正文句子不可只因位於卷前而升級。

    「正文 第一卷 …」的「正文」是網站加的雜訊前綴，不是作品名稱。"""
    return bool(text and len(text) <= 30 and not re.search(r"[，,。！？!?；;：:”“「」]", text)
                and not is_noise_prefix(text))


def analyze_collection_structure(lines, mode="自動判斷"):
    """第二階段分析多作品合集，不改動既有章節正則。"""
    if mode == "單本小說":
        return {"active": False, "combined": {}, "work_lines": {}, "works": []}

    combined = {}
    work_counts = Counter()
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not text:
            continue
        combined_match = COLLECTION_COMBINED_REGEX.match(text)
        if combined_match:
            work, volume, remainder = (
                combined_match.group(1).strip(), combined_match.group(2).strip(),
                combined_match.group(3).strip(),
            )
            if not plausible_work_name(work):
                continue
            lv2 = parse_lv2(remainder)
            special = parse_special(remainder)
            if lv2 and not is_weak_numbered_title(remainder):
                kind, data = "chapter", lv2
            elif special:
                kind, data = "special", special
            else:
                # 合集中的人物介紹、終章、篇外篇、書名號標題等仍屬第三層。
                kind, data = "generic", None
            combined[index] = {
                "work": work, "volume": volume, "kind": kind,
                "data": data, "remainder": remainder,
            }
            work_counts[work] += 1

    combined_work_names = set(work_counts)

    # 支援已排版為「作品名稱／第一集／第一章」的分行結構。
    # 作品名必須有 [::] 明確標記、已出現於完整合集表頭，
    # 或位於第一卷／集前。不再把第二卷前的任意正文當作書名。
    work_lines = {}
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not plausible_work_name(text):
            continue
        if marker != "include" and (parse_lv1(text) or parse_lv2(text) or parse_special(text)):
            continue
        peek = index + 1
        while peek < len(lines) and not lines[peek].strip():
            peek += 1
        if peek >= len(lines):
            continue
        next_text, next_marker = strip_persistent_title_marker(lines[peek].strip())
        # 各章重複的「第一卷…第04章」不是新作品起點。
        if parse_mixed_volume_chapter_header(next_text):
            continue
        if COLLECTION_COMBINED_REGEX.match(next_text):
            continue
        volume_data = parse_lv1(next_text)
        if next_marker != "exclude" and volume_data and volume_data[2] > 0:
            is_explicit = marker in {"include", "auto_work"}
            is_known = text in combined_work_names
            bracketed = ((text.startswith("《") and text.endswith("》"))
                         or (text.startswith("【") and text.endswith("】")))
            if is_explicit or is_known or bracketed or mode == "多作品合集":
                work_lines[index] = text

    repeated_works = {work for work, count in work_counts.items() if count >= 3}
    explicit_works = {
        work_lines[index] for index in work_lines
        if strip_persistent_title_marker(lines[index].strip())[1] in {"include", "auto_work"}
    }
    first_volume_works = set(work_lines.values())
    if mode == "多作品合集":
        active = bool(combined or work_lines)
    else:
        active = len(repeated_works) >= 2 or len(explicit_works) >= 2 or len(first_volume_works) >= 2
        active = active or any(strip_persistent_title_marker(lines[i].strip())[1] == "auto_work"
                               for i in work_lines)
    all_names = [item["work"] for item in combined.values()] + list(work_lines.values())
    works = list(dict.fromkeys(all_names)) if active else []
    return {"active": active, "combined": combined, "work_lines": work_lines, "works": works}


def _collect_weak_candidates(lines):
    """找出弱格式（純數字、井號、括號…）的章節候選，還沒算信心度。"""
    candidates = []
    for index, raw_line in enumerate(lines):
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if marker == "exclude" or not text:
            continue
        weak = parse_weak_numbered_title(text)
        if not weak:
            continue

        # 正式獨立章號後的弱格式行是次行章名，不是另一個候選章節。
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0:
            previous_text, previous_marker = strip_persistent_title_marker(lines[previous].strip())
            previous_lv2 = parse_lv2(previous_text)
            if (previous_marker != "exclude" and previous_lv2
                    and not is_weak_numbered_title(previous_text)
                    and not previous_lv2[5].strip()):
                continue

        candidates.append({"index": index, **weak, "text": text})
    return candidates


# 章名是一整句：有句號、分號（或句號結尾）幾乎一定是正文；只有逗號的也可能是章名（「先出虎穴，又入狼窩」）
_SENTENCE_BODY = re.compile(r"[。；;]|[．.]$")
_CLAUSE_BODY = re.compile(r"[，]|,(?!\d)")


def score_chapter_candidates(lines, candidates):
    """替候選章節打信心度：同格式前後編號連續、兩章之間有夠多正文、
    前後有空行，都是「真的是章節」的證據；編號連續但中間幾乎沒有正文，
    則比較像正文裡的條列。

    candidates 要依行號排序，每筆至少要有 index、number、style、style_key。"""
    style_counts = Counter(candidate["style_key"] for candidate in candidates)
    for position, candidate in enumerate(candidates):
        previous = candidates[position - 1] if position else None
        following = candidates[position + 1] if position + 1 < len(candidates) else None
        sequence_links = 0
        if previous and previous["style_key"] == candidate["style_key"] and previous["number"] + 1 == candidate["number"]:
            sequence_links += 1
        if following and following["style_key"] == candidate["style_key"] and candidate["number"] + 1 == following["number"]:
            sequence_links += 1

        neighbor_indices = []
        if previous and previous["style_key"] == candidate["style_key"]:
            neighbor_indices.append(previous["index"])
        if following and following["style_key"] == candidate["style_key"]:
            neighbor_indices.append(following["index"])
        body_chars = 0
        for neighbor_index in neighbor_indices:
            start, end = sorted((candidate["index"], neighbor_index))
            body_chars = max(body_chars, sum(len(line.strip()) for line in lines[start + 1:end]))

        blank_before = candidate["index"] == 0 or not lines[candidate["index"] - 1].strip()
        blank_after = candidate["index"] + 1 >= len(lines) or not lines[candidate["index"] + 1].strip()
        score = sequence_links * 2
        if style_counts[candidate["style_key"]] >= 3:
            score += 1
        if body_chars >= 200:
            score += 3
        elif body_chars >= 60:
            score += 2
        if blank_before or blank_after:
            score += 1
        if body_chars < 60 and sequence_links:
            score -= 3                  # 連號但中間只有一兩行：正文裡的條列（數值、條款、步驟）
        body = candidate.get("body") or ""
        if _SENTENCE_BODY.search(body):
            score -= 4                  # 章名是一整句：正文的條列
        elif _CLAUSE_BODY.search(body):
            score -= 2
        if candidate["style"] == "一般" and candidate["style_key"] == "一般:空格":
            score -= 1

        candidate["score"] = score
        candidate["confidence"] = "高" if score >= 5 else ("中" if score >= 3 else "低")
        candidate["selected"] = score >= 5
        start = max(0, candidate["index"] - 2)
        end = min(len(lines), candidate["index"] + 3)
        candidate["preview"] = "\n".join(
            ("▶ " if row == candidate["index"] else "  ") + lines[row]
            for row in range(start, end)
        )
    return candidates


# 常用格式沒涵蓋的弱格式，在清單上怎麼稱呼（style_key → 名稱）。
_WEAK_STYLE_LABELS = {
    "井號": "井號數字",
    "卷-章": "卷號-章號",
    "括號": "括號數字",
    "一般:.": "數字加點",
    "一般:、": "數字頓號",
    "一般:空格": "數字空格",
    "一般::": "數字加冒號",
    "一般:,": "數字加逗號",
    "一般:-": "數字加破折號",
}


def weak_style_label(style_key: str) -> str:
    if style_key in _WEAK_STYLE_LABELS:
        return _WEAK_STYLE_LABELS[style_key]
    separator = style_key.split(":", 1)[1] if ":" in style_key else style_key
    return f"數字＋「{separator}」"


_HAS_CHAPTER = re.compile(r"第\s*\S{1,8}?\s*[章回節节]")


def missed_volumes(lines, known_rows):
    """目錄裡一個卷都沒有，本文卻有至少 3 行不帶「第」的卷（「卷二　山路」「卷一」），卷號連號：回傳行數，否則 0。
    只看開頭是卷／部／篇／集的短行（其他行一個字就跳過），整本也很快。"""
    numbers = []
    for row, line in enumerate(lines):
        text = line.strip()
        if not text or text[0] not in "卷部篇集" or len(text) > 30 or row in known_rows:
            continue
        match = preset_match(text)
        if match and match[0] == "leading_unit_volume" and not _HAS_CHAPTER.search(text):
            numbers.append(match[1]["number"])
    consecutive = sum(after == before + 1 for before, after in zip(numbers, numbers[1:]))
    return len(numbers) if len(numbers) >= 3 and consecutive >= 2 else 0


MIDDLE_SCAN_LIMIT = 60000
MIDDLE_MIN_SPACING = 15


def missed_middle_chapters(lines, chapters, max_length=MAX_TITLE_LENGTH):
    """目錄的章號中間缺一段（第 628 章後面就是第 700 章），缺的那幾章在本文裡是另一種常用寫法
    （「629.標題」「96章、標題」）：回傳 {"key": 內建組合, "count": 行數, "after": 缺口前一章的章號}，
    至少 3 行才算，否則 None。只掃缺口那幾段（總共最多 MIDDLE_SCAN_LIMIT 行，開檔時快取還沒算好也不會卡）。"""
    template_ids = {key for key, level, _blocks in TEMPLATES if level == 2}
    found, first_after, rows_of = Counter(), {}, defaultdict(list)
    scanned = 0
    for (row, number), (next_row, next_number) in zip(chapters, chapters[1:]):
        if not isinstance(number, (int, float)) or not isinstance(next_number, (int, float))                 or not 2 <= next_number - number <= 500:
            continue
        scanned += next_row - row
        if scanned > MIDDLE_SCAN_LIMIT:
            break
        for candidate in scan_chapter_candidates(lines[row + 1:next_row], set(), max_length):
            key = candidate["format"].split(":", 1)[1] if candidate["format"].startswith("preset:") else None
            if key in template_ids and candidate["confidence"] != "低" and number < candidate["number"] < next_number:
                found[key] += 1
                first_after.setdefault(key, number)
                rows_of[key].append(row + 1 + candidate["index"])
    if not found:
        return None
    key, count = found.most_common(1)[0]
    # 章跟章之間隔著一段正文；一項接一項、只隔一兩行的是故事裡的清單（「一、某某劍」「二、某某鼎」）
    spacing = sorted(after - before for before, after in zip(rows_of[key], rows_of[key][1:]))
    if count < 3 or spacing[len(spacing) // 2] < MIDDLE_MIN_SPACING:
        return None
    return {"key": key, "count": count, "after": first_after[key]}


_TAIL_CLAUSE = re.compile(r"[，,]")


def missed_tail_chapters(lines, chapters, last_row, max_length=MAX_TITLE_LENGTH):
    """最後一個目錄項目（last_row）之後漏掉的章節：後半本換了寫法。只看那之後的本文，平常只有最後一章。

    chapters：目錄裡的章 [(行號, 章號)]，照行號排好。回傳 None，或
    {"kind": "template", "key": 內建組合, "count": 行數}——常用寫法的章號接著最後一章往下數；
    {"kind": "candidates", "format": 可疑章節的格式, "count": 行數}——接不上號，但後面還有一大段
    （平均一章的五倍以上）、裡面有同一種沒有內建組合的寫法。"""
    rest = lines[last_row + 1:]
    if len(rest) < 3 or not chapters:
        return None
    last_number = chapters[-1][1]
    by_format = {}
    for candidate in scan_chapter_candidates(rest, set(), max_length):
        by_format.setdefault(candidate["format"], []).append(candidate)
    template_ids = {key for key, _level, _blocks in TEMPLATES}
    average = max(1, last_row // len(chapters))
    for fmt, items in sorted(by_format.items(), key=lambda pair: -len(pair[1])):
        key = fmt.split(":", 1)[1] if fmt.startswith("preset:") else None
        # 像一整句的（附錄的日記「5.今天……，……」）、每項只有幾行的（書末的人物表）不算接下去的章
        numbers = [candidate["number"] for candidate in items if candidate["confidence"] == "高"
                   and not (len(candidate["text"]) > 15 and _TAIL_CLAUSE.search(candidate["text"]))]
        # 「2-1」（卷號-章號）是新的一卷：章號從 1 重新起算
        starts = numbers and (numbers[0] == 1 if key == "volume_dash_chapter"
                              else isinstance(last_number, (int, float)) and last_number >= 1
                              and last_number < numbers[0] <= last_number + 3)
        if (key in template_ids and len(numbers) >= 3 and starts
                and sum(after == before + 1 for before, after in zip(numbers, numbers[1:])) >= 2
                and len(rest) / len(numbers) >= average / 5):
            return {"kind": "template", "key": key, "count": len(numbers)}
    if len(rest) >= max(200, average * 5):
        fmt, items = max(by_format.items(), key=lambda pair: len(pair[1]), default=(None, []))
        if len(items) >= 3:
            return {"kind": "candidates", "format": fmt, "count": len(items)}
    return None


def scan_chapter_candidates(lines, known_rows=frozenset(), max_length=MAX_TITLE_LENGTH):
    """「可疑章節」：看起來像章節、但目前不在目錄裡的行。
    max_length 照「標題長度」的設定：常用格式本身不限章名長度，不擋的話正文裡一整句的條列
    （「4、依照規定應當…」）也會被列成高信心的可疑章節。

    每一行先看符不符合某個常用格式（依 PRESET_RULES 的順序，第一個符合的
    算數）；都不符合時，再看是不是常用格式沒涵蓋的弱格式（例如「1: 標題」）。
    兩種來源一起算信心度，清單上才能用同一套標準排序、勾選。

    每筆候選多了這幾個欄位：
      format：常用格式是 "preset:<id>"，其他格式是 "weak:<style_key>"
      label：清單上顯示的格式名稱
      level：逐行加入目錄時該當成卷（1）還是章（2）
    """
    presets = {preset["preset"]: (preset["name"], preset.get("level", 2)) for preset in PRESET_RULES}
    weak_by_row = {candidate["index"]: candidate for candidate in _collect_weak_candidates(lines)}
    candidates = []
    for index, raw_line in enumerate(lines):
        if index in known_rows:
            continue
        text, marker = strip_persistent_title_marker(raw_line.strip())
        if not text or marker == "exclude" or too_long_for_title(text, max_length):
            continue
        found = preset_match(text)
        if found:
            preset_id, match = found
            name, level = presets[preset_id]
            candidates.append({
                "index": index, "text": text, "number": match["number"], "body": match["title"],
                "style": "preset", "style_key": f"preset:{preset_id}",
                "format": f"preset:{preset_id}", "label": name, "level": level,
            })
        else:
            weak = weak_by_row.get(index)
            if weak is not None:
                candidates.append({**weak, "format": f"weak:{weak['style_key']}",
                                   "label": weak_style_label(weak["style_key"]), "level": 2})
                continue
            # 章號被打成字母或符號（「第A章 二進宮」）：不能自動當章節（沒有章號可排），
            # 但列進可疑清單，讓使用者自己決定要不要加進目錄。
            odd = _ODD_NUMBER_TITLE.match(text)
            if odd:
                candidates.append({
                    "index": index, "text": text, "number": 0, "body": odd.group("body"),
                    "style": "odd", "style_key": "odd_number", "format": "weak:odd_number",
                    "label": "章號不是數字", "level": 1 if odd.group("unit") in "卷部篇集季" else 2,
                })
    candidates = _demote_subsections(score_chapter_candidates(lines, candidates), known_rows)
    return _demote_inner_lists(candidates, known_rows, lines)


def _demote_subsections(candidates, known_rows):
    """每一章裡面的小節（「第1章」底下又分「（一）（二）」，下一章從「（一）」重新開始）：
    同一種寫法一再從頭數、而且每次重來前面都隔著正式的章節標題，就不是漏掉的章，
    降成中信心、不預設勾選。"""
    rows = sorted(known_rows)
    if not rows:
        return candidates
    by_style = defaultdict(list)
    for candidate in candidates:
        by_style[candidate["style_key"]].append(candidate)
    for items in by_style.values():
        restarts = after_title = 0
        for previous, current in zip(items, items[1:]):
            if current["number"] <= previous["number"]:
                restarts += 1
                position = bisect.bisect_right(rows, previous["index"])
                if position < len(rows) and rows[position] < current["index"]:
                    after_title += 1
        if restarts >= 2 and after_title >= max(2, restarts * 0.4):
            for candidate in items:
                if candidate["confidence"] == "高":
                    candidate["confidence"] = "中"
                    candidate["selected"] = False
    return candidates


def _demote_inner_lists(candidates, known_rows, lines):
    """章裡面的清單（人物表「1. 主角：…」、附錄的「一、…」）：目錄是正常的（至少 10 章、比這種寫法多），同一種寫法
    有 3 行以上擠在兩個目錄標題之間、從 1 開始數，而且後面那個目錄標題的章號沒有接著它們，
    就不是漏掉的章，降成中信心、不預設勾選（接得上的「1.」…「10.」後面是「第11章」就是真的章）。"""
    rows = sorted(known_rows)
    if len(rows) < 10:
        return candidates
    sections = defaultdict(list)
    for candidate in candidates:
        sections[(candidate["style_key"], bisect.bisect_right(rows, candidate["index"]))].append(candidate)
    # 同一種寫法比目錄的章還多：目錄才是零星收到的，這種寫法是整本的章，不是清單
    style_totals = Counter(candidate["style_key"] for candidate in candidates)
    for (style, position), items in sections.items():
        if len(items) < 3 or not items[0]["number"] or items[0]["number"] > 2 or style_totals[style] >= len(rows):
            continue
        if position < len(rows):
            following = parse_lv2(lines[rows[position]].strip())
            if following and following[3] == items[-1]["number"] + 1:
                continue
        for candidate in items:
            if candidate["confidence"] == "高":
                candidate["confidence"] = "中"
                candidate["selected"] = False
    return candidates


_ODD_NUMBER_TITLE = re.compile(
    r"^第\s*[A-Za-zＡ-Ｚａ-ｚ?？○□]{1,3}\s*(?P<unit>[章回節节卷部篇集季])\s*(?P<body>\S.{0,40})$")


def _section_parents(records, parent_of) -> set:
    """哪些卷（或全書）裡「節」是章底下的小節：同一層章、節都至少 5 個，而且節的號碼
    一再從頭數（每章重新開始）。只有幾個「第16節」（或幾個「章」）的，多半是寫錯，一起數。"""
    sections, others = {}, Counter()
    for node, record in records.items():
        if record["kind"] != "chapter":
            continue
        parent = parent_of(node)
        if record.get("unit") in ("節", "节"):
            sections.setdefault(parent, []).append(record["number"])
        elif record.get("unit"):
            others[parent] += 1
    result = set()
    for parent, numbers in sections.items():
        restarts = sum(1 for previous, current in zip(numbers, numbers[1:]) if current <= previous)
        if others[parent] >= 5 and len(numbers) >= 5 and restarts >= 2:
            result.add(parent)
    return result


_TITLE_VOLUME = re.compile(r"^\s*(?:第\s*" + CN_NUM_PATTERN + r"\s*[部卷篇集季]|[部卷篇]\s*" + CN_NUM_PATTERN + r")(?=[\s（(])")


def group_formal_chapters(records, parent_of, label_of):
    """把章節記錄依父節點分成幾組，供缺章檢查使用。

    parent_of／label_of 由呼叫端提供，所以同一套邏輯可以用在 Qt 的目錄樹
    上，也可以直接用在 core 的 SimpleTree 上（例如排版前只想算一次結構、
    不想重畫整棵目錄）。番外、小數章與沒有編號的標題不列入。
    """
    groups: dict = {}
    section_parents = _section_parents(records, parent_of)
    for node, record in records.items():
        number = record["number"]
        if (record["kind"] != "chapter" or record["prefix"] == "番外"
                or number <= 0 or not float(number).is_integer()):
            continue
        ancestors, ancestor, work = [], parent_of(node), None
        while ancestor is not None:
            ancestors.insert(0, label_of(ancestor))
            if records.get(ancestor, {}).get("kind") == "work":
                work = ancestor
            ancestor = parent_of(ancestor)
        # 章名前面帶著卷號（「卷一 山路 第一章」，沒開自動補齊卷號與卷名時不拆成卷）：再照卷號分組，
        # 每卷重新數的章號才不會全擠在一起變成重複（目錄上的卷放錯位置時也一樣）
        prefix = _TITLE_VOLUME.match(label_of(node) or "")
        prefix_volume = re.sub(r"\s+", "", prefix.group(0)) if prefix else ""
        if prefix_volume:
            ancestors.append(prefix_volume)
        # 章底下再分節（「第1節」每章重新數）：節另外一組，不跟章混在一起數
        section = record.get("unit") in ("節", "节") and parent_of(node) in section_parents
        # 自訂特殊標題（續章1、續章2…）自己一組編號
        series = record["prefix"] if record.get("special") else ""
        label = (" / ".join(ancestors) or "全書") + ("（節）" if section else "") + (f"（{series}）" if series else "")
        entry = groups.setdefault((parent_of(node), prefix_volume, section, series), {
            "label": label, "work": work, "numbers": [], "nodes": [], "titles": []})
        entry["numbers"].append(int(number))
        entry["nodes"].append(node)
        entry["titles"].append(label_of(node))
    return list(groups.values())


# 同一章分成幾段：「（上）（下）」「(1)(2)」
_PART_SUFFIX = re.compile(r"[（(]\s*([上中下]|[一二三四五六七八九十]+|\d+)\s*[)）]\s*$")
# 章號後面緊接「（第2部分）」「(二)」
_PART_AFTER_NUMBER = re.compile(r"[章回節节]\s*[（(]\s*第?\s*([上中下]|[一二三四五六七八九十]+|\d+)\s*(?:部分)?\s*[)）]")


_BARE_PART = re.compile(r"^(?P<base>.*\S)\s*(?P<part>[上中下]|[一二三四五六七八九十]|\d{1,2})\s*$")


def _split_parts(titles) -> bool:
    """同一個章號的幾個標題都帶著不同的分段標記（結尾的「（上）」、章號後面的「（第2部分）」）：
    是一章分成幾段，不是重複。第一段常常不寫標記（「第766章」「第766章（第2部分）」），最多一個沒有。"""
    parts = [(_PART_SUFFIX.search(title or "") or _PART_AFTER_NUMBER.search(title or "")) for title in titles]
    marked = [part.group(1) for part in parts if part]
    if len(marked) >= len(parts) - 1 and bool(marked) and len(set(marked)) == len(marked):
        return True
    # 沒有括號的「山路」「山路 一」「過河1」「過河2」：其餘一字不差才算
    bases, marks = set(), []
    for title in titles:
        match = _BARE_PART.match(title or "")
        bases.add(re.sub(r"\s+", "", match.group("base") if match else (title or "")))
        if match:
            marks.append(match.group("part"))
    return len(bases) == 1 and len(marks) >= len(titles) - 1 and bool(marks) and len(set(marks)) == len(marks)


def _extra_digit(value, expected) -> bool:
    """value 是 expected 多打了一個數字（11407 ← 1407）。"""
    text, want = str(int(value)), str(int(expected))
    return len(text) == len(want) + 1 and any(text[:i] + text[i + 1:] == want for i in range(len(text)))


def number_anomalies(numbers) -> dict:
    """照本文順序的章號裡，明顯打錯或不是章節的：{索引: ("typo", 應該是幾) | ("stray", None)}。

    - 前一章 N、後一章 N+k+1，中間 k 個（最多 3 個、彼此連號）卻不是 N+1…N+k：作者打錯
      （「第两百十一五章」讀成 225、「第389九章」讀成 3899），照前後章當成 N+1…N+k。
    - 前後兩章正好連號，中間卻夾了幾個差很遠的號碼（正文裡的「第32470节车厢」）：不是章節。
    - 後面沒有章可以對照（一卷的最後幾章）時，多打了一個數字的（「第11407章」接在第 1406 章後面）也算打錯。
    - 一組最前面的號碼比後面大很多、後面從頭連號（卷首多一個上一卷的「第102章」）：也當成不是章節。
    這樣一個錯字就不會在缺章報告裡變成上千章的缺口。"""
    found = {}
    count = len(numbers)
    if count >= 3 and numbers[1] + 1 == numbers[2] and numbers[0] > numbers[1] + 10:
        found[0] = ("stray", None)
    index = 1
    previous = numbers[1] if 0 in found else numbers[0]
    if 0 in found:
        index = 2
    while index < count - 1:
        current = numbers[index]
        handled = False
        for size in (1, 2, 3):
            if index + size >= count:
                break
            run = numbers[index:index + size]
            if any(b != a + 1 for a, b in zip(run, run[1:])):
                break
            # 跟前一章同號的是重複（另外列），不當成打錯
            if run[0] not in (previous, previous + 1) and numbers[index + size] == previous + size + 1:
                for offset in range(size):
                    found[index + offset] = ("typo", previous + 1 + offset)
                previous += size
                index += size
                handled = True
                break
        if handled:
            continue
        stray = 0
        for size in (1, 2, 3):
            run = numbers[index:index + size]
            if index + size < count and numbers[index + size] == previous + 1 and \
                    all(abs(value - previous) > (1 if size == 1 else 10) for value in run):
                stray = size
                break
        if stray:
            for offset in range(stray):
                found[index + offset] = ("stray", None)
            index += stray
            continue
        previous = current
        index += 1
    # 最後幾章（後面沒有章可以對照）：只認「多打了一個數字」這種明顯的打錯
    for index in range(max(1, count - 3), count):
        if index in found:
            continue
        before = next((found[i][1] if i in found and found[i][0] == "typo" else numbers[i]
                       for i in range(index - 1, -1, -1) if found.get(i, ("",))[0] != "stray"), None)
        if before is not None and numbers[index] != before + 1 and _extra_digit(numbers[index], before + 1):
            found[index] = ("typo", before + 1)
    return found


def _restart_segments(numbers, ends=None) -> list:
    """同一卷裡章號又從 1（或 2）重新數起、後面也連號：當成新的一段各自算缺口
    （卷尾接了續寫、番外，卻沒有卷標題）。回傳每一段的號碼。
    ends：跟 numbers 對齊的多章合併標題的最後一章（「第1-5章」是 5，不是範圍就是 None）：
    「第1-5章」後面接「第6-10章」也算連號。"""
    segments, start = [], 0
    for index in range(1, len(numbers)):
        value = numbers[index]
        last = (ends[index] if ends and ends[index] else None) or value
        if value <= 2 and numbers[index - 1] >= last + 3 and \
                (index + 1 >= len(numbers) or numbers[index + 1] == last + 1):
            segments.append(numbers[start:index])
            start = index
    segments.append(numbers[start:])
    return [segment for segment in segments if segment]


def _increasing_run(numbers, indices) -> set:
    """indices 這幾個位置的章號裡，最長的嚴格遞增子序列（回傳位置）。"""
    tails, tail_positions, parent = [], [], {}
    for position in indices:
        value = numbers[position]
        slot = bisect.bisect_left(tails, value)
        if slot == len(tails):
            tails.append(value)
            tail_positions.append(position)
        else:
            tails[slot] = value
            tail_positions[slot] = position
        parent[position] = tail_positions[slot - 1] if slot else None
    run, position = set(), tail_positions[-1] if tail_positions else None
    while position is not None:
        run.add(position)
        position = parent[position]
    return run


def misplaced_chapters(numbers) -> dict:
    """照本文順序的章號裡，放錯位置的章：{索引: ("after"|"before", 應該放在哪一章的索引)}。

    每一段（_restart_segments：卷裡重新數起的算另一段）取最長的遞增章號當成「位置對的章」，
    其餘的章號剛好補得上那串的缺口（在中間缺的號碼、或比最小的少 1、比最大的多 1）才算放錯位置；
    補不上缺口的是打錯、重複或不是章節，交給 number_anomalies 與重複檢查。
    遞增的章不到一半時不判斷（亂得太厲害，多半是別的問題）。"""
    found = {}
    position = 0
    for segment in _restart_segments(list(numbers)):
        indices = list(range(position, position + len(segment)))
        position += len(segment)
        run = _increasing_run(numbers, indices)
        if len(run) * 2 < len(indices) or len(run) == len(indices):
            continue
        ordered = sorted(run)
        values = [numbers[index] for index in ordered]
        present = set(values)
        by_gap = {}
        for index in indices:
            value = numbers[index]
            if index in run or value in present:
                continue
            if values[0] < value < values[-1] or value in (values[0] - 1, values[-1] + 1):
                if value > 0 and float(value).is_integer():
                    by_gap.setdefault(bisect.bisect_left(values, value), []).append(index)
        # 搬過去要把那個缺口整個補滿才算放錯位置：只補上一部分（13 跟 22 之間缺 14～21，卻只有 19～21 能搬）
        # 多半是另一套編號交錯在正文裡（網站的貼文編號夾在書本身的章號之間），不是章放錯地方
        for slot, gap_indices in by_gap.items():
            supplied = {int(numbers[index]) for index in gap_indices}
            low = int(values[slot - 1]) + 1 if slot else min(supplied)
            high = int(values[slot]) - 1 if slot < len(values) else max(supplied)
            # supplied 不重複：區間裡的號碼個數等於區間長度就是補滿了（不要真的列出區間，章號可能差到上億）
            if sum(low <= value <= high for value in supplied) < high - low + 1:
                continue
            for index in gap_indices:
                found[index] = ("after", ordered[slot - 1]) if slot else ("before", ordered[0])
    return found


def move_chapter_blocks(lines, moves) -> list:
    """moves：[(開始行, 結束行（不含）, 放到哪一行前面)]，行號都是搬之前的。

    同一個位置有好幾章要放時照 moves 的順序放。搬走的那段原本沒有以空行結尾、
    放進去的位置後面還有內容時補一個空行，免得跟下一章的標題黏在一起。"""
    moving = {}
    skip = set()
    for start, end, destination in moves:
        block = list(lines[start:end])
        if destination < len(lines) and block and block[-1].strip():
            block.append("")
        moving.setdefault(destination, []).append(block)
        skip.update(range(start, end))
    result = []
    for row in range(len(lines) + 1):
        for block in moving.get(row, ()):
            result.extend(block)
        if row < len(lines) and row not in skip:
            result.append(lines[row])
    return result


# 前後兩個號碼都可以是全形（辨識章號時全形、半形一樣看待）
_MERGED_NUMBER = re.compile(r"第\s*[0-9０-９]+\s*[、，,]\s*([0-9０-９]{1,4})\s*[章回節节]")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def chapter_gap_report(numbers, label, mode="僅檢查中間缺口", previous_last=None, titles=None):
    """numbers、titles 照本文順序。放錯位置的章（misplaced_chapters）先搬到該在的位置再檢查，
    另外放在 misplaced：[(索引, 章號, "after"|"before", 應該放在哪一章的索引)]。
    打錯、不像章節的號碼（number_anomalies）照前後章修正或拿掉，
    另外放在 anomalies：[(索引, 原本的號碼, "typo"|"stray", 應該是幾)]；索引都是傳進來的 numbers 的索引。"""
    original = list(numbers)
    misplaced = misplaced_chapters(original)
    order = [index for index in range(len(original)) if index not in misplaced]
    # 放錯位置的章插回該在的位置（在那一章後面／前面），再照這個順序做其他檢查
    for index, (where, target) in sorted(misplaced.items()):
        slot = order.index(target) + (1 if where == "after" else 0)
        while where == "after" and slot < len(order) and order[slot] in misplaced \
                and original[order[slot]] < original[index]:
            slot += 1
        order.insert(slot, index)
    reordered = [original[index] for index in order]
    local = number_anomalies(reordered)
    anomalies = {order[position]: value for position, value in local.items()}
    kept = [(index, guess if kind == "typo" else original[index])
            for index in order
            for kind, guess in [anomalies.get(index, ("", None))] if kind != "stray"]
    anomaly_list = [(index, original[index], kind, guess) for index, (kind, guess) in sorted(anomalies.items())]
    misplaced_list = [(index, original[index], where, target) for index, (where, target) in sorted(misplaced.items())]
    if titles:
        titles = [titles[index] for index, _number in kept]
    numbers = [number for _index, number in kept] or original
    ordered = sorted(set(numbers))
    ranges, duplicates, position = [], set(), 0
    ends = [chapter_range_end(title) for title in titles] if titles else None
    for segment_index, segment in enumerate(_restart_segments(numbers, ends)):
        segment_titles = titles[position:position + len(segment)] if titles else None
        position += len(segment)
        covered = set(segment)
        # 「第62、3章」一章裡有兩個章號：後面那章也算有；「第38-40章」整段都算有
        for number, title in zip(segment, segment_titles or ()):
            range_end = chapter_range_end(title)
            if range_end:
                covered.update(range(int(number) + 1, range_end + 1))
            also = _MERGED_NUMBER.search(title or "")
            if also:
                tail, head = also.group(1).translate(_FULLWIDTH_DIGITS), str(int(number))
                second = int(head[:-len(tail)] + tail) if len(tail) < len(head) else int(tail)
                if number < second <= number + 9:
                    covered.add(second)
        values = sorted(covered)
        start = values[0]
        if mode == "每卷從第1章起算":
            start = 1
        elif (mode == "同作品跨卷接續" and segment_index == 0 and previous_last is not None
              and values[0] > previous_last):
            start = previous_last + 1
        cursor = start
        for number in values:
            if number > cursor:
                ranges.append((cursor, number - 1))
            cursor = number + 1
        duplicates.update(n for n, count in Counter(segment).items() if count > 1
                          and not (segment_titles and _split_parts(
                              [title for number, title in zip(segment, segment_titles) if number == n])))
    return {"label": label, "missing_ranges": ranges, "duplicates": sorted(duplicates),
            "first": ordered[0], "last": ordered[-1], "count": len(ordered),
            "start_unverified": mode == "僅檢查中間缺口" and ordered[0] > 1, "anomalies": anomaly_list,
            "misplaced": misplaced_list}
