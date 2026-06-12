"""L0 Jina Reader 采集器 — 通过 Jina Reader API 获取任意网页的干净 Markdown 文本

Jina Reader API: https://r.jina.ai/<目标URL>
无需 API Key，返回标准 Markdown（保留链接、标题、列表结构）。
适合公告/通知列表页——信息密度高、无需手动排查 CSS 选择器。
"""
import asyncio
import re
import time
from datetime import datetime
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


JINA_BASE = "https://r.jina.ai/"


class L0JinaCollector(BaseCollector):
    """通过 Jina Reader API 采集网页内容（零选择器维护）"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30)
        self.max_retries = crawl_config.get("max_retries", 3)
        self.user_agent = crawl_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        delay_range = crawl_config.get("request_delay_seconds", [1, 3])
        self.delay_min = delay_range[0] if delay_range else 1.0
        self.delay_max = delay_range[1] if len(delay_range) > 1 else self.delay_min

    async def collect(self) -> List[RawItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_and_parse)

    def _fetch_and_parse(self) -> List[RawItem]:
        url = self.source.url
        jina_url = JINA_BASE + url
        t_start = time.time()

        logger.info(f"[Jina] {self.source.name} → {url}")

        md_text = self._fetch_jina(jina_url)
        if not md_text:
            return []

        elapsed = time.time() - t_start
        logger.info(
            f"[Jina] {self.source.name} OK | "
            f"{len(md_text)} 字符 Markdown | 耗时={elapsed:.1f}s"
        )

        # 从 Markdown 中提取链接 → RawItem
        items = self._parse_markdown_links(md_text, url)

        if not items:
            # 如果没有链接，把整页作为一个条目
            title = self._extract_title(md_text)
            items = [
                RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=url,
                    title=title or self.source.name,
                    raw_content=md_text[:2000],
                    publish_time=None,
                    fetched_at=datetime.now(),
                )
            ]

        logger.info(f"[Jina] {self.source.name} 解析出 {len(items)} 条")
        return items

    def _fetch_jina(self, jina_url: str) -> Optional[str]:
        """调用 Jina Reader API，带重试"""
        headers = {
            "Accept": "text/markdown",
            "User-Agent": self.user_agent,
            "X-Return-Format": "markdown",
        }
        last_error = None

        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(2 ** attempt)
                resp = requests.get(jina_url, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp.text
                elif resp.status_code == 429:
                    logger.warning(f"[Jina] {self.source.name} 429 限流，等待后重试")
                    time.sleep(5)
                    continue
                else:
                    logger.warning(
                        f"[Jina] {self.source.name} HTTP {resp.status_code} "
                        f"第 {attempt+1}/{self.max_retries} 次"
                    )
                    last_error = f"HTTP {resp.status_code}"
            except requests.RequestException as e:
                last_error = str(e)
                logger.warning(
                    f"[Jina] {self.source.name} {e} "
                    f"第 {attempt+1}/{self.max_retries} 次"
                )

        logger.error(f"[Jina] {self.source.name} 失败: {last_error}")
        return None

    def _parse_markdown_links(self, md_text: str, base_url: str) -> List[RawItem]:
        """从 Markdown 中提取 [title](url) 格式的链接，生成 RawItem 列表"""
        # 匹配 Markdown 链接: [text](url)
        link_pattern = re.compile(r'\[([^\]]+?)\]\(((?:https?:)?//[^\)]+)\)')
        matches = link_pattern.findall(md_text)

        if not matches:
            return []

        now = datetime.now()
        items = []
        seen_urls = set()

        for i, (title, href) in enumerate(matches):
            if i >= self.source.max_items:
                break

            title = title.strip()
            href = href.strip()

            # 跳过太短的标题、导航类链接、锚点
            if len(title) < 4:
                continue
            if href.startswith("#"):
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)

            # 补全相对 URL
            full_url = urljoin(base_url, href) if not href.startswith("http") else href

            # 提取链接周围的上下文（前后各 100 字）
            try:
                idx = md_text.index(f"[{title}]({href})")
                start = max(0, idx - 100)
                end = min(len(md_text), idx + len(title) + len(href) + 100)
                context = md_text[start:end].strip()
            except ValueError:
                context = title

            items.append(
                RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=full_url,
                    title=title,
                    raw_content=context[:800],
                    publish_time=None,  # Markdown 中日期格式不一，由 LLM 判断
                    fetched_at=now,
                )
            )

        return items

    def _extract_title(self, md_text: str) -> Optional[str]:
        """从 Markdown 中提取第一个 H1 作为标题"""
        m = re.search(r'^#\s+(.+)$', md_text, re.MULTILINE)
        if m:
            return m.group(1).strip()
        # 取第一行非空文本
        for line in md_text.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped[:100]
        return None
