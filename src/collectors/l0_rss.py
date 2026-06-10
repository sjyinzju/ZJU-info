"""L0 RSS 采集器 — 使用 feedparser 解析 RSS/Atom 订阅源"""
import asyncio
import hashlib
from datetime import datetime
from typing import List
from loguru import logger
import feedparser

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


class L0RssCollector(BaseCollector):
    """RSS/Atom 订阅源采集器"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30)

    async def collect(self) -> List[RawItem]:
        """异步采集 RSS 源（feedparser 是同步的，用线程池执行）"""
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, self._fetch_rss)
        return self._to_raw_items(entries)

    def _fetch_rss(self) -> list:
        """同步获取并解析 RSS feed"""
        logger.info(f"[RSS] 正在获取: {self.source.name} ({self.source.url})")
        feed = feedparser.parse(
            self.source.url,
            agent=self.crawl_config.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ),
        )

        if feed.bozo and not feed.entries:
            # bozo = feedparser 解析异常标志，但有时仍能解析出条目
            logger.warning(f"[RSS] {self.source.name} 解析警告: {feed.bozo_exception}")

        entries = feed.entries[: self.source.max_items]
        logger.info(f"[RSS] {self.source.name} 获取到 {len(entries)} 条 (限制 {self.source.max_items})")
        return entries

    def _to_raw_items(self, entries: list) -> List[RawItem]:
        """将 feedparser 条目转为 RawItem 列表"""
        items = []
        now = datetime.now()

        for entry in entries:
            # 提取文本内容（去掉 HTML 标签的摘要）
            title = entry.get("title", "").strip()
            summary = entry.get("summary", entry.get("description", ""))
            link = entry.get("link", "")

            # 解析发布时间
            pub_time = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                try:
                    pub_time = datetime(*entry.published_parsed[:6])
                except (TypeError, ValueError):
                    pass

            # 拼接内容：title + summary
            raw_content = f"{title}\n\n{summary}" if summary else title

            if not link or not title:
                continue

            items.append(
                RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=link,
                    title=title,
                    raw_content=raw_content,
                    publish_time=pub_time,
                    fetched_at=now,
                )
            )

        return items
