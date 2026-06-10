"""关键词预过滤器 — LLM 调用前先用 jieba 分词粗筛，减少 token 消耗"""
from typing import List
from loguru import logger
from src.models.raw_item import RawItem


def pre_filter(items: List[RawItem], keywords: List[str]) -> List[RawItem]:
    """
    基于关键词做粗筛。
    - 如果 keywords 为空 → 全部通过
    - 如果 keywords 非空 → 标题或内容包含任一关键词的通过
    """
    if not keywords:
        return items

    filtered = []
    for item in items:
        text = f"{item.title} {item.raw_content}".lower()
        for kw in keywords:
            if kw.lower() in text:
                filtered.append(item)
                break

    dropped = len(items) - len(filtered)
    if dropped > 0:
        logger.info(f"[关键词] {len(items)} → {len(filtered)} (丢弃 {dropped} 条不相关)")

    return filtered


def merge_keywords(source_keywords: List[str], global_keywords: List[str]) -> List[str]:
    """合并源级别关键词和全局关键词"""
    all_kw = list(set(source_keywords + global_keywords))
    return all_kw
