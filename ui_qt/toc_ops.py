"""目錄章節範圍計算：合併／整理／連續編號／刪除共用的邊界邏輯。

範圍一律以目錄上的標題行為邊界：一個節點的範圍＝從它自己的標題行，到下一個
「不屬於它子樹」的目錄行為止。QTreeWidget 的根節點是 None。
"""

import bisect

from core.chapter_parse import parse_lv1




def tree_children(tree, item):
    if item is None:
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    return [item.child(i) for i in range(item.childCount())]


def part_separator_items(tree):
    """部／卷／章三層的書：「第N部」底下沒有章、後面接著卷時，目錄上畫成分隔列（還是兩層，使用者 2026-10-05）。
    部底下直接是章的（後面沒有卷）照舊是一般的卷。"""
    tops = tree_children(tree, None)
    found = []
    for item, following in zip(tops, tops[1:]):
        parsed = parse_lv1(item.text(0)) if not item.childCount() else None
        if parsed and parsed[3] == "部" and following.childCount():
            found.append(item)
    return found


def subtree_items(tree, item):
    """item 自己的所有子孫（不含自己）。"""
    own = set()
    for child in tree_children(tree, item):
        own.add(child)
        own |= subtree_items(tree, child)
    return own


def ordered_boundaries(chapter_index_map, toc_boundary_map):
    """排序好的邊界（行號、節點）。整批查詢共用一份，不要每個節點各排一次
    ；目錄或正文變了就要重建，所以只在一次操作裡面共用。"""
    boundaries = toc_boundary_map or chapter_index_map
    pairs = sorted(((row, position, node) for position, (node, row) in enumerate(boundaries.items())),
                   key=lambda triple: triple[:2])
    return [row for row, _position, _node in pairs], [node for _row, _position, node in pairs]


def toc_section_rows(tree, item, chapter_index_map, toc_boundary_map, ordered=None):
    """節點自己＋所有子層級涵蓋的行號範圍（1-based 起點，終點或 None）。

    終點＝下一個「不屬於自己子樹」的目錄行，用排序過的邊界行號二分搜尋（全選幾千章才不會變成
    平方次比較）。ordered：ordered_boundaries() 的結果，多個節點一起查時傳進來共用。"""
    start = chapter_index_map.get(item)
    if start is None:
        return None, None
    subtree = subtree_items(tree, item)
    rows, nodes = ordered if ordered is not None else ordered_boundaries(chapter_index_map, toc_boundary_map)
    position = bisect.bisect_right(rows, start)
    while position < len(rows):
        if nodes[position] not in subtree:
            return start, rows[position]
        position += 1
    return start, None


def toc_section_lines(tree, item, chapter_index_map, toc_boundary_map, total_lines, ordered=None):
    """同一範圍換成 0-based 半開行區間 [起, 迄)。"""
    start, end = toc_section_rows(tree, item, chapter_index_map, toc_boundary_map, ordered)
    if start is None:
        return None
    end = end if end is not None else total_lines + 1
    return start - 1, min(end - 1, total_lines)
