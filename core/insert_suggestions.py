"""手動插入標題時的編號建議：依目錄已確認的原始行推算下一個合理編號。"""

from .title_markers import strip_manual_title_marker
from .chapter_parse import parse_lv1, parse_lv2, parse_special, suggest_number, render_chapter_number_like

INSERTABLE_KINDS = ("章", "部", "卷", "篇", "集", "季", "前言", "序章", "楔子", "後記", "自訂標題")
NUMERIC_KINDS = {"章", "部", "卷", "篇", "集", "季"}


def get_structure_records(raw_lines, recognized_indices):
    """把已確認的章節行轉成推算編號用的資料：層級、編號、單位、同卷分組。"""
    records = []
    current_group = 0
    current_volume_key = None
    for raw_index in sorted(set(recognized_indices)):
        if not 0 <= raw_index < len(raw_lines):
            continue
        line, _ = strip_manual_title_marker(raw_lines[raw_index].strip())
        lv2 = parse_lv2(line)
        lv1 = parse_lv1(line)
        special = parse_special(line)

        # 「第二集 第五章」同時提供集與章兩種可能的插入順位。
        embedded_volume = ""
        if lv2:
            if lv2[1]:
                embedded_volume = lv2[1]
            elif lv2[0] and parse_lv1(lv2[0]):
                embedded_volume = lv2[0]
        if embedded_volume:
            volume_data = parse_lv1(embedded_volume)
            if volume_data and volume_data[2] > 0:
                volume_key = (volume_data[3], int(volume_data[2]))
                if volume_key != current_volume_key:
                    current_group += 1
                    current_volume_key = volume_key
                    records.append({"index": raw_index, "level": 1, "number": int(volume_data[2]),
                                    "unit": volume_data[3], "group": current_group,
                                    "reference": embedded_volume})

        if lv2 and lv2[2] != "番外" and lv2[3] > 0 and float(lv2[3]).is_integer():
            records.append({"index": raw_index, "level": 2, "number": int(lv2[3]),
                            "unit": "章", "group": current_group,
                            "reference": line})
        elif lv1 and lv1[2] > 0:
            volume_key = (lv1[3], int(lv1[2]))
            if volume_key != current_volume_key:
                current_group += 1
                current_volume_key = volume_key
                records.append({"index": raw_index, "level": 1, "number": int(lv1[2]),
                                "unit": lv1[3], "group": current_group,
                                "reference": line})
        elif special:
            records.append({"index": raw_index, "level": 0, "number": 0,
                            "unit": special[2], "group": current_group,
                            "reference": line})
    return records


def get_insert_suggestions(raw_lines, recognized_indices, insert_index):
    records = get_structure_records(raw_lines, recognized_indices)
    suggestions = {}

    chapters = [record for record in records if record["level"] == 2]
    prev_ch = next((record for record in reversed(chapters) if record["index"] < insert_index), None)
    next_ch = next((record for record in chapters if record["index"] >= insert_index), None)
    same_group_prev = prev_ch
    if next_ch and prev_ch and next_ch["group"] != prev_ch["group"]:
        same_group_prev = None
    chapter_number = suggest_number(same_group_prev, next_ch)
    chapter_reference = (next_ch or same_group_prev or {}).get("reference", "第1章")
    suggestions["章"] = {"number": chapter_number, "reference": chapter_reference}

    for unit in ("部", "卷", "篇", "集", "季"):
        volumes = [record for record in records if record["level"] == 1 and record["unit"] == unit]
        prev_vol = next((record for record in reversed(volumes) if record["index"] < insert_index), None)
        next_vol = next((record for record in volumes if record["index"] >= insert_index), None)
        if prev_vol or next_vol:
            suggestions[unit] = {
                "number": suggest_number(prev_vol, next_vol),
                "reference": (next_vol or prev_vol)["reference"]
            }

    if insert_index == 0:
        default_kind = "前言"
    elif next_ch or prev_ch:
        default_kind = "章"
    elif any(unit in suggestions for unit in ("部", "卷", "篇", "集", "季")):
        default_kind = next(unit for unit in ("部", "卷", "篇", "集", "季") if unit in suggestions)
    else:
        default_kind = "章"
    return suggestions, default_kind


def build_inserted_title(kind, number_text, title_text, reference=""):
    title_text = title_text.strip()
    if kind in NUMERIC_KINDS:
        try:
            number = int(number_text)
        except ValueError:
            raise ValueError("編號必須是大於 0 的整數。")
        if number <= 0:
            raise ValueError("編號必須是大於 0 的整數。")
        rendered_number = render_chapter_number_like(number, reference)
        base = f"第{rendered_number}{kind}"
    elif kind == "自訂標題":
        if not title_text:
            raise ValueError("請輸入自訂標題。")
        return f"{title_text}[::]"
    else:
        base = kind
    return f"{base} {title_text}".strip()
