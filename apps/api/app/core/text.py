"""文本归一化工具：解析入库与外部源清洗共用。

知网 PDF/页面大量使用全角数字（实测 `２０２５－０１－１８`），直接入库会导致
用半角 "2025" 检索时匹配失败。这里只转换数字、拉丁字母与常见标点，
中文本身是全角，不能动。
"""

_FULLWIDTH = {
    # 数字
    **{chr(0xFF10 + i): str(i) for i in range(10)},
    # 拉丁字母
    **{chr(0xFF21 + i): chr(0x41 + i) for i in range(26)},
    **{chr(0xFF41 + i): chr(0x61 + i) for i in range(26)},
    # 常见标点
    "－": "-", "．": ".", "：": ":", "（": "(", "）": ")",
    "，": ",", "％": "%", "／": "/", "　": " ", "＋": "+", "＝": "=",
    "＜": "<", "＞": ">", "［": "[", "］": "]",
}


def normalize_fullwidth(text: str) -> str:
    """全角 ASCII → 半角；保留中文与其他全角字符不变。"""
    if not text:
        return ""
    return "".join(_FULLWIDTH.get(ch, ch) for ch in text)
