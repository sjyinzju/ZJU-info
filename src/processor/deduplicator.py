"""去重器 — 基于 URL hash + 标题相似度"""
import difflib
from typing import List
from loguru import logger
from src.models.raw_item import RawItem
from src.storage.database import is_seen, mark_seen


def deduplicate(items: List[RawItem]) -> List[RawItem]:
    """去重：先用 URL hash 精确去重，再用标题相似度去重"""
    if not items:
        return []

    # 第一轮：URL hash 精确去重
    url_unique = []
    for item in items:
        if not is_seen(item.url):
            url_unique.append(item)

    dup_count = len(items) - len(url_unique)
    if dup_count > 0:
        logger.info(f"[去重] URL 精确去重: {len(items)} → {len(url_unique)} (去除 {dup_count})")

    # 第二轮：标题相似度去重
    final = []
    for item in url_unique:
        is_dup = False
        for seen in final:
            ratio = difflib.SequenceMatcher(None, item.title, seen.title).ratio()
            if ratio > 0.85:
                is_dup = True
                break
        if not is_dup:
            final.append(item)

    title_dup = len(url_unique) - len(final)
    if title_dup > 0:
        logger.info(f"[去重] 标题相似度去重: {len(url_unique)} → {len(final)} (去除 {title_dup})")

    # 标记已见
    for item in final:
        mark_seen(item.url, item.title, item.source_name)

    logger.info(f"[去重] 最终: {len(items)} → {len(final)} 条新内容")
    return final
