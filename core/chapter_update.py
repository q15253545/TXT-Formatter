"""接續更新章節：把新下載的同一本書跟本文比對，只加入本文沒有的章節。

使用者的本文多半整理過（排版、刪廣告、改標點、繁簡轉換），所以不比內容是否相同：
- 章節用標題對應：卷／章的種類、單位、章號、章名，章名抹掉空白、標點、尾巴的求票附註，
  兩邊都先轉成簡體再比（本文轉過繁體、新檔是簡體也對得上）。
- 兩份目錄當成兩串標題由上往下排齊（difflib，跟比對文件差異同一種方法）：章號每卷從 1 起算、
  重複章號、中間有番外都照順序對；排不齊的那一段裡，種類、單位、章號都一樣的也算同一章
  （使用者改過章名）。
- 對上的章只看新檔是不是明顯比較長（上次下載時還沒寫完）：排版、刪廣告、改標點、繁簡轉換
  字數都只差一點，而且通常是本文比較少。

新檔多出來的章：夾在對得上的章中間的是「本文缺少」，補在新檔裡前一章（在本文的那一章）後面；
在最後一個對得上的章後面的是「新章節」，接在本文最後。新檔開頭（書名、簡介）不加。
"""

import re
from difflib import SequenceMatcher

from .chapter_parse import parse_lv1, parse_lv2, parse_special, strip_title_body
from .duplicate_chapters import _strip_note
from .script_convert import convert_body_text, get_opencc_converter
from .title_markers import strip_persistent_title_marker

SAME, LONGER, MISSING, NEW = "same", "longer", "missing", "new"
# 新檔比本文多這麼多才算「可能補完了」：比例和字數都要超過
LONGER_RATIO = 1.2
LONGER_CHARS = 300

_HAN = re.compile(r"[㐀-䶿一-鿿\U00020000-\U0003134f]")
_NOT_HAN = re.compile(r"[^㐀-䶿一-鿿\U00020000-\U0003134f]+")
_NOT_WORD = re.compile(r"[\W_]+")


def han_count(lines) -> int:
    # 刪掉非漢字的片段再算長度：比 findall 逐字產生字串快三倍多（大檔整本都要算一次）
    return len(_NOT_HAN.sub("", "\n".join(lines)))


def _to_simplified(texts: list) -> list:
    """一次轉完（OpenCC 逐行轉很慢）；沒有 OpenCC 就照原樣比（繁簡不同的書就對不上）。"""
    converter = get_opencc_converter("t2s")
    if converter is None or not texts:
        return list(texts)
    converted = converter.convert("\n".join(texts)).split("\n")
    return converted if len(converted) == len(texts) else list(texts)


def _name_of(text: str) -> str:
    chapter = parse_lv2(text)
    if chapter:
        return chapter[5] or ""
    volume = parse_lv1(text)
    if volume:
        return volume[4] or ""
    special = parse_special(text)
    if special:
        return (special[2] or "") + (special[3] or "")
    return text


def toc_entries(lines, raw_map: dict, records: dict) -> list:
    """目錄上的每一項（照行號排好；同一行有卷和章的只算章）：row、kind、title（那一行的字）、
    key（對應用）、loose（章名不看的對應）。raw_map／records 用同一種鍵（目錄節點）。"""
    by_row = {}
    for node, row in raw_map.items():
        record = records.get(node)
        if record is None or not 0 <= row < len(lines):
            continue
        if row in by_row and by_row[row]["kind"] == "chapter":
            continue
        by_row[row] = {"row": row, "kind": record.get("kind", "chapter"), "number": record.get("number") or 0,
                       "unit": record.get("unit") or "", "prefix": record.get("prefix") or ""}
    entries = [by_row[row] for row in sorted(by_row)]
    texts = [strip_persistent_title_marker(lines[entry["row"]].strip())[0] for entry in entries]
    names = _to_simplified([_strip_note(strip_title_body(_name_of(text)))[0] for text in texts])
    units = _to_simplified([entry["unit"] for entry in entries])
    for entry, text, name, unit in zip(entries, texts, names, units):
        name = _NOT_WORD.sub("", name).lower()
        numbered = bool(entry["number"])
        entry["title"] = text
        entry["loose"] = (entry["kind"], unit, entry["prefix"], entry["number"]) if numbered else None
        entry["key"] = (entry["kind"], unit, entry["prefix"], entry["number"], name)
    return entries


def _segments(lines, entries) -> list:
    """每一項從標題那一行到下一項之前（最後一項到檔尾）：(start, end)，end 不含。"""
    rows = [entry["row"] for entry in entries] + [len(lines)]
    return [(rows[index], rows[index + 1]) for index in range(len(entries))]


def _align(body_entries, new_entries) -> dict:
    """新檔第幾項 → 本文第幾項（對得上的才有）。"""
    matcher = SequenceMatcher(None, [entry["key"] for entry in body_entries],
                              [entry["key"] for entry in new_entries], autojunk=False)
    pairs = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.update({j1 + offset: i1 + offset for offset in range(i2 - i1)})
        elif tag == "replace":
            # 改過章名：種類、單位、章號都一樣的照順序配對（不回頭，才不會交叉）
            next_body = i1
            for j in range(j1, j2):
                loose = new_entries[j]["loose"]
                if loose is None:
                    continue
                for i in range(next_body, i2):
                    if body_entries[i]["loose"] == loose:
                        pairs[j] = i
                        next_body = i + 1
                        break
    return pairs


def plan_update(body_lines, body_entries, new_lines, new_entries) -> list:
    """新檔每一項一筆：
      index       新檔第幾項
      title       新檔的標題那一行
      kind        chapter／volume／work
      status      same（本文已經有）、longer（本文已經有、新檔明顯比較長）、missing（本文缺少）、new（新章節）
      body_index  對上的本文第幾項（same／longer）
      position    加入時插在本文第幾行之前（missing／new；len(body_lines) 是接在最後）
      after       插在本文哪一項的標題後面（missing，顯示用；None 是插在本文最前面）
      body_count  本文那一章的漢字數（標題不算；沒有是 None）
      new_count   新檔那一章的漢字數
      suggested   預設勾不勾：本文缺少的有章號的章、新章節勾；本文已經有的不勾（新檔比較長的也不勾：
                  換掉會丟掉使用者在那一章的整理）；本文缺少、又沒有章號的（簡介、感言、公告）不勾，
                  多半是使用者整理時刪掉的
    """
    pairs = _align(body_entries, new_entries)
    body_segments = _segments(body_lines, body_entries)
    new_segments = _segments(new_lines, new_entries)
    matched = sorted(pairs)
    last_matched = matched[-1] if matched else -1
    plan = []
    previous = None
    for index, entry in enumerate(new_entries):
        start, end = new_segments[index]
        item = {"index": index, "title": entry["title"], "kind": entry["kind"], "body_index": None,
                "position": None, "after": None, "body_count": None, "suggested": False,
                "new_count": han_count(new_lines[start + 1:end])}
        if index in pairs:
            body_index = pairs[index]
            body_start, body_end = body_segments[body_index]
            body_count = han_count(body_lines[body_start + 1:body_end])
            longer = (item["new_count"] >= body_count * LONGER_RATIO
                      and item["new_count"] - body_count >= LONGER_CHARS)
            item.update(status=LONGER if longer else SAME, body_index=body_index, body_count=body_count)
            previous = body_index
        elif index > last_matched:
            item.update(status=NEW, position=len(body_lines), suggested=True)
        else:
            if previous is not None:
                item.update(position=body_segments[previous][1], after=body_entries[previous]["title"])
            else:
                following = pairs[next(j for j in matched if j > index)]
                item.update(position=body_segments[following][0])
            item.update(status=MISSING, suggested=entry["loose"] is not None)
        plan.append(item)
    return plan


def _convert_rows(lines, ranges, mode) -> list:
    """只把要加進本文的那幾段做繁簡轉換（OpenCC 很慢，整份新檔轉一次要好幾秒）。
    OpenCC 不增減換行：接起來轉完再切回去，行數一樣。"""
    rows = sorted({row for start, end in ranges for row in range(start, end)})
    if not rows:
        return lines
    converted = convert_body_text("\n".join(lines[row] for row in rows), mode).split("\n")
    if len(converted) != len(rows):
        return lines
    result = list(lines)
    for row, text in zip(rows, converted):
        result[row] = text
    return result


def _trim_blank(lines) -> list:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def apply_update(body_lines, body_entries, new_lines, new_entries, plan, checked, convert_mode=None):
    """照勾選的項目改本文：本文缺少、新章節插進去；新檔比較長的把那一章的正文換成新檔的（標題留本文的）。
    convert_mode：加入的文字先做繁簡轉換（BODY_SCRIPT_MODES 的鍵）。
    回傳（新的本文, 加入或換掉的標題在新本文的行號）。"""
    new_segments = _segments(new_lines, new_entries)
    body_segments = _segments(body_lines, body_entries)
    if convert_mode:
        new_lines = _convert_rows(new_lines, [new_segments[item["index"]] for item in plan
                                              if item["index"] in checked and item["status"] != SAME],
                                  convert_mode)
    inserts, replaces, replaced_titles = {}, {}, set()
    for item in plan:
        if item["index"] not in checked:
            continue
        if item["status"] in (MISSING, NEW):
            inserts.setdefault(item["position"], []).append(item["index"])
        elif item["status"] == LONGER:
            # 標題留本文的（使用者可能改過），標題下面的空行也留著，只換有字的那一段
            start, end = body_segments[item["body_index"]]
            content_start = start + 1
            while content_start < end and not body_lines[content_start].strip():
                content_start += 1
            content_end = end
            while content_end > content_start and not body_lines[content_end - 1].strip():
                content_end -= 1
            if content_start == content_end:        # 本文這一章沒有正文：接在標題下面
                content_start = content_end = start + 1
            replaces[content_start] = (content_end, item["index"])
            replaced_titles.add(start)

    result, added = [], []

    def emit_block(indices, next_line):
        if result and result[-1].strip():
            result.append("")
        for number, index in enumerate(indices):
            if number:
                result.append("")
            start, end = new_segments[index]
            segment = _trim_blank(new_lines[start:end])
            added.append(len(result))
            result.extend(segment)
        if next_line is not None and next_line.strip():
            result.append("")

    row = 0
    total = len(body_lines)
    while row <= total:
        # 同一行先換正文（屬於上一章）再插新的章
        if row in replaces:
            end, index = replaces.pop(row)
            start, stop = new_segments[index]
            result.extend(_trim_blank(new_lines[start + 1:stop]))
            if end > row:
                row = end
                continue
        if row in inserts:
            emit_block(inserts.pop(row), body_lines[row] if row < total else None)
        if row == total:
            break
        if row in replaced_titles:
            added.append(len(result))
        result.append(body_lines[row])
        row += 1
    return result, sorted(set(added))


def append_all(body_lines, new_lines, convert_mode=None):
    """整份接到最後：回傳（新的本文, 接上去的第一行的行號）。"""
    if convert_mode:
        converted = convert_body_text("\n".join(new_lines), convert_mode).split("\n")
        if len(converted) == len(new_lines):
            new_lines = converted
    result = list(body_lines)
    while result and not result[-1].strip():
        result.pop()
    if result:
        result.append("")
    start = len(result)
    result.extend(_trim_blank(new_lines))
    return result, start


def dominant_script(lines, sample_chars: int = 4000):
    """本文大多是繁體字還是簡體字："trad"、"simp"；分不出來或沒有 OpenCC 是 None。
    只看前面一段（夠判斷，大檔也快）。"""
    to_trad, to_simp = get_opencc_converter("s2t"), get_opencc_converter("t2s")
    if to_trad is None or to_simp is None:
        return None
    sample, size = [], 0
    for line in lines:
        chars = _HAN.findall(line)
        if chars:
            sample.append("".join(chars))
            size += len(chars)
            if size >= sample_chars:
                break
    text = "".join(sample)
    if not text:
        return None
    # 簡體字轉繁體會變、繁體字轉簡體會變；兩邊共用的字都不變
    simplified = sum(1 for a, b in zip(text, to_trad.convert(text)) if a != b)
    traditional = sum(1 for a, b in zip(text, to_simp.convert(text)) if a != b)
    if simplified >= 20 and simplified > traditional * 2:
        return "simp"
    if traditional >= 20 and traditional > simplified * 2:
        return "trad"
    return None
