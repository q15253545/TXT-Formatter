"""中文數字與阿拉伯數字互轉。"""

CN_NUM = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    '百': 100, '千': 1000, '万': 10000, '萬': 10000, '兩': 2,
    '億': 10**8, '亿': 10**8, '兆': 10**12,
    # 大寫數字（「第壹章」）
    '壹': 1, '貳': 2, '贰': 2, '參': 3, '叁': 3, '肆': 4, '伍': 5, '陸': 6, '陆': 6,
    '柒': 7, '捌': 8, '玖': 9, '拾': 10, '佰': 100, '仟': 1000,
}

CN_DIGIT_TRANS = str.maketrans("０１２３４５６７８９點点", "0123456789..")
# 章號、卷號不會這麼大；更大的是正文裡一長串數字，當成沒有號碼（幾百位的數字轉 float 還會溢位）
MAX_NUMBER = 10 ** 9


def chinese_to_arabic(cn_str: str) -> float:
    if not cn_str: return 0.0
    cn_str = cn_str.translate(CN_DIGIT_TRANS).replace("．", ".")
    try:
        value = float(cn_str)
        return value if value <= MAX_NUMBER else 0.0
    except ValueError:
        pass
    if "." in cn_str:
        parts = cn_str.split(".")
        int_part, dec_part = parts[0], parts[1]
    else:
        int_part, dec_part = cn_str, ""
    def parse_integer(value):
        if not value:
            return 0
        for unit in ("兆", "億", "亿", "萬", "万"):
            if unit in value:
                left, right = value.split(unit, 1)
                return parse_integer(left or "一") * CN_NUM[unit] + parse_integer(right)
        result, pending = 0, 0
        for char in value:
            number = CN_NUM.get(char, int(char) if char.isdigit() else 0)
            if number >= 10:
                result += (pending or 1) * number
                pending = 0
            else:
                pending = pending * 10 + number
        return result + pending
    integer = parse_integer(int_part)
    if integer > MAX_NUMBER:
        return 0.0
    val = float(integer)
    if dec_part:
        # 「三點五」的小數部分仍是中文數字，需逐字轉成阿拉伯數字。
        digits = "".join(
            str(CN_NUM[char]) if char in CN_NUM and CN_NUM[char] < 10 else char
            for char in dec_part
        )
        if digits.isdigit():
            return val + float("0." + digits)
        return val
    return val if val > 0 else 0.0


def arabic_to_chinese(num: int) -> str:
    """以繁體中文輸出整數；大單位遞迴，避免四位分節溢位。"""
    num = int(num)
    if num < 0:
        return "負" + arabic_to_chinese(-num)
    if num == 0:
        return "零"
    def convert(n):
        for base, unit in ((10**12, "兆"), (10**8, "億"), (10**4, "萬")):
            if n >= base:
                upper, lower = divmod(n, base)
                return (convert(upper) + unit +
                        (("零" if lower < base // 10 else "") + convert(lower) if lower else ""))
        result, zero = "", False
        for index, char in enumerate(str(n)):
            digit = int(char)
            if digit:
                result += ("零" if zero else "") + "零一二三四五六七八九"[digit]
                result += ("", "十", "百", "千")[len(str(n)) - index - 1]
                zero = False
            elif result:
                zero = True
        return result
    result = convert(num)
    return result[1:] if result.startswith("一十") else result
