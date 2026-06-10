"""L0 HTML 采集器 — requests + BeautifulSoup 解析静态网页
带有自动诊断：选择器匹配 0 条时自动 dump 页面结构并尝试通用回退模式。
"""
import asyncio
import random
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


# 通用新闻列表回退选择器（按优先级排序）
FALLBACK_SELECTORS = [
    "ul.news-list li",
    "ul.news_list li",
    "ul.newslist li",
    "div.news-list li",
    "div.news_list li",
    "div.news ul li",
    "div.news-con ul li",
    "div.news_con ul li",
    "div.list-con ul li",
    "div.list ul li",
    "ul.list li",
    "ul.list-unstyled li",
    "div.content ul li",
    "div.main ul li",
    "div.article-list li",
    "div.post-list li",
    "div.item",
    "div.card",
    "article",
    "table.table tr",
    "table tr",
    "li a",
]


class L0HtmlCollector(BaseCollector):
    """静态 HTML 页面采集器（含自动诊断与智能回退）"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30)
        self.max_retries = crawl_config.get("max_retries", 3)
        self.user_agent = crawl_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        delay_range = crawl_config.get("request_delay_seconds", [1, 3])
        self.delay_min = delay_range[0] if delay_range else 1
        self.delay_max = delay_range[1] if len(delay_range) > 1 else self.delay_min
        # 诊断信息保存目录
        self._diag_dir = Path(__file__).resolve().parent.parent.parent / "info" / "diagnose"

    async def collect(self) -> List[RawItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_and_parse)

    def _fetch_and_parse(self) -> List[RawItem]:
        url = self.source.url
        selector = self.source.selector
        logger.info(f"[HTML] {self.source.name} ({url})")

        html = self._fetch_with_retry(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")

        # ── 第一步：使用配置的选择器 ──
        if selector:
            elements = soup.select(selector)
            logger.info(f"[HTML] {self.source.name} 选择器 '{selector}' → {len(elements)} 个元素")

            if elements:
                return self._extract_items(elements, url)

        # ── 第二步：配置选择器失败 → 自动诊断 ──
        logger.warning(f"[HTML] {self.source.name} 选择器匹配 0 条，启动自动诊断...")
        self._save_diagnose_html(html)

        # ── 第三步：尝试通用回退模式 ──
        for fb_selector in FALLBACK_SELECTORS:
            elements = soup.select(fb_selector)
            if len(elements) >= 3:  # 至少3条才算有效
                logger.info(
                    f"[HTML] {self.source.name} 回退选择器 '{fb_selector}' → {len(elements)} 个元素"
                )
                return self._extract_items(elements, url)

        # ── 第四步：全完蛋，dump 所有链接帮助用户手动找 ──
        logger.error(f"[HTML] {self.source.name} 所有回退模式均失败，dump 页面链接结构")
        self._dump_link_structure(soup, url)
        return []

    def _extract_items(self, elements, base_url: str) -> List[RawItem]:
        """从 BeautifulSoup 元素列表中提取 RawItem"""
        items = []
        now = datetime.now()
        count = 0

        for el in elements:
            if count >= self.source.max_items:
                break

            # 找 <a> 标签：先直接找，再递归找子元素
            link_el = None
            if el.name == "a":
                link_el = el
            else:
                # 优先找直接子元素中的 <a>
                link_el = el.find("a", recursive=False)
                # 如果没找到，递归找所有后代 <a>
                if not link_el:
                    link_el = el.find("a")

            if not link_el:
                continue

            title = (link_el.get_text(strip=True) or link_el.get("title", "")).strip()
            href = link_el.get("href", "")
            if not title or not href or len(title) < 2:
                continue

            full_url = urljoin(base_url, href)
            date_text = self._extract_date(el)
            pub_time = self._parse_date(date_text)
            content_text = el.get_text(" ", strip=True)[:500]

            items.append(
                RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=full_url,
                    title=title,
                    raw_content=content_text,
                    publish_time=pub_time,
                    fetched_at=now,
                )
            )
            count += 1

        return items

    def _save_diagnose_html(self, html: str):
        """保存诊断用的 HTML 片段"""
        try:
            self._diag_dir.mkdir(parents=True, exist_ok=True)
            safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.source.name)
            filepath = self._diag_dir / f"{safe_name}.html"
            # 只保存 body 部分，截断到 50KB
            soup = BeautifulSoup(html, "lxml")
            body = soup.body
            snippet = str(body)[:50000] if body else html[:50000]
            filepath.write_text(snippet, encoding="utf-8")
            logger.info(f"[诊断] 页面 HTML 已保存: {filepath}")
        except Exception as e:
            logger.warning(f"[诊断] 保存 HTML 失败: {e}")

    def _dump_link_structure(self, soup, base_url: str):
        """当所有选择器都失败时，dump 页面中所有链接的结构帮助诊断"""
        links = soup.select("a[href]")
        if not links:
            logger.error(f"[诊断] 页面中没有任何 <a> 链接！可能需 Playwright 动态渲染")
            return

        # 过滤掉导航/页脚等噪音链接（长度 < 5 字的通常是导航）
        meaningful = []
        for a in links:
            text = a.get_text(strip=True)
            if len(text) >= 5:
                parent = a.parent
                parent_tag = parent.name if parent else "?"
                parent_class = " ".join(parent.get("class", [])) if parent else ""
                meaningful.append((text, a.get("href", ""), parent_tag, parent_class))

        logger.warning(f"[诊断] 页面共 {len(links)} 个链接，有意义 {len(meaningful)} 个:")
        logger.warning(f"[诊断] 前 15 个有意义链接的结构:")
        for i, (text, href, ptag, pclass) in enumerate(meaningful[:15]):
            logger.warning(f"  [{i+1}] <{ptag} class='{pclass}'> → {text[:60]}")
            logger.warning(f"       href={href[:80]}")

        # 建议选择器
        logger.warning(f"[诊断] 修正方法: 打开 info/diagnose/{self.source.name}.html")
        logger.warning(f"         找到新闻列表的容器元素，把它的 CSS 选择器填入 config.yaml")

    # ── 通用工具方法 ──

    def _fetch_with_retry(self, url: str) -> Optional[str]:
        last_error = None
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        for attempt in range(self.max_retries):
            try:
                delay = random.uniform(self.delay_min, self.delay_max)
                if attempt > 0:
                    delay *= 2 ** attempt
                time.sleep(delay)

                resp = requests.get(url, headers=headers, timeout=self.timeout, allow_redirects=True)
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text

            except requests.RequestException as e:
                last_error = e
                logger.warning(f"[HTML] {self.source.name} 第 {attempt+1}/{self.max_retries} 次失败: {e}")

        logger.error(f"[HTML] {self.source.name} 所有重试均失败: {last_error}")
        return None

    def _extract_date(self, element) -> str:
        for pat in [
            ".date", ".time", ".pub-date", ".post-date",
            "span.time", ".news-date", ".list-date",
            "[class*=date]", "[class*=time]",
        ]:
            try:
                found = element.select_one(pat)
                if found:
                    return found.get_text(strip=True)
            except Exception:
                pass
        return ""

    def _parse_date(self, date_text: str) -> Optional[datetime]:
        if not date_text:
            return None
        date_text = date_text.strip()
        for fmt in [
            "%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y/%m/%d",
            "%Y年%m月%d日", "%Y年%m月%d日 %H:%M",
            "%m月%d日", "%m-%d",
        ]:
            try:
                return datetime.strptime(date_text, fmt)
            except ValueError:
                continue
        return None
