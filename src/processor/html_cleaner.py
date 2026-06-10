"""HTML 清洗器 — 将 HTML 转为干净的纯文本 Markdown"""
import re
from typing import List
from html2text import HTML2Text
from src.models.raw_item import RawItem


# 配置 html2text 实例（复用，避免重复创建）
_converter = HTML2Text()
_converter.ignore_links = False
_converter.ignore_images = True
_converter.body_width = 0  # 不自动换行
_converter.unicode_snob = True
_converter.skip_internal_links = True
_converter.single_line_break = True


def clean_html(text: str) -> str:
    """将 HTML 文本转为干净的 Markdown"""
    if not text:
        return ""

    # 检测是否包含 HTML 标签
    if "<" not in text or ">" not in text:
        return _clean_whitespace(text)

    try:
        cleaned = _converter.handle(text)
    except Exception:
        # html2text 失败时回退到简单去标签
        cleaned = re.sub(r"<[^>]+>", "", text)

    return _clean_whitespace(cleaned)


def clean_items(items: List[RawItem]) -> List[RawItem]:
    """批量清洗 RawItem 的 raw_content"""
    for item in items:
        item.raw_content = clean_html(item.raw_content)
    return items


def _clean_whitespace(text: str) -> str:
    """去除多余空白"""
    text = re.sub(r"\n{3,}", "\n\n", text)  # 最多保留一个空行
    text = re.sub(r"[ \t]{2,}", " ", text)  # 多余空格合并
    return text.strip()
