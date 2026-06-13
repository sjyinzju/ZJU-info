"""L0 自动化提取采集器 — readability-lxml + html2text + BS4

零选择器配置：自动识别页面正文区域，提取其中所有链接作为列表条目。
原理：readability 算法去除导航/侧栏/页脚 → 在正文区域内找链接 → 提取为 RawItem。
"""
import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from readability import Document as ReadabilityDoc
import html2text
from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


# html2text 转换器（复用）
_md = html2text.HTML2Text()
_md.ignore_links = False
_md.ignore_images = True
_md.body_width = 0
_md.unicode_snob = True


class L0AutoCollector(BaseCollector):
    """自动化内容提取采集器（readability 算法 + 链接提取）"""

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
        return await loop.run_in_executor(None, self._fetch_and_extract)

    def _fetch_and_extract(self) -> List[RawItem]:
        url = self.source.url
        t_start = time.time()

        logger.info(f"[Auto] {self.source.name} → {url}")

        html = self._fetch(url)
        if not html:
            return []

        soup_full = BeautifulSoup(html, "lxml")

        # ── Step 1: readability 提取正文区域 ──
        try:
            doc = ReadabilityDoc(html)
            content_html = doc.summary(html_partial=True)
        except Exception as e:
            logger.warning(f"[Auto] {self.source.name} readability 失败: {e}，回退到原始 HTML")
            content_html = html

        soup_content = BeautifulSoup(content_html, "lxml")

        # ── Step 2: 从正文区域提取链接 ──
        links = soup_content.select("a[href]")
        meaningful = [
            a for a in links
            if len(a.get_text(strip=True)) >= 4
            and not a.get("href", "").startswith("#")
            and not a.get("href", "").startswith("javascript:")
        ]

        if len(meaningful) >= 3:
            items = self._extract_list_items(meaningful, url, soup_content)
        else:
            # ── 回退：readability 丢失了列表链接 → 在原始 HTML 中找链接密集区 ──
            logger.info(f"[Auto] {self.source.name} readability 仅 {len(meaningful)} 个链接，启用 BS4 回退...")
            items = self._fallback_extract(soup_full, url)

        # ── 仍不足：尝试从页面所有链接中补充 ──
        if len(items) < min(3, self.source.max_items):
            logger.info(f"[Auto] {self.source.name} 仅 {len(items)} 条，尝试全局链接补充...")
            items = self._extract_all_links(soup_full, url, existing_count=len(items))

        elapsed = time.time() - t_start
        logger.info(
            f"[Auto] {self.source.name} OK | "
            f"readability → {len(meaningful)} 个链接 → {len(items)} 条 | "
            f"耗时={elapsed:.1f}s"
        )
        return items

    def _fallback_extract(self, soup: BeautifulSoup, base_url: str) -> List[RawItem]:
        """BS4 回退：找链接密度最高且内容最像列表的 DOM 区域"""
        # 导航类关键词（用于排除）
        nav_keywords = [
            "首页", "返回", "部门简介", "联系我们", "单位职责", "机构设置",
            "登录", "注册", "English", "旧版", "管理", "入口",
        ]

        def _is_content_link(a) -> bool:
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if len(text) < 6:
                return False
            if href.startswith("#") or href.startswith("javascript:"):
                return False
            if any(kw in text for kw in nav_keywords):
                return False
            return True

        best_container = None
        best_score = 0

        # 优先检查列表类元素（ul/ol/table），再检查通用容器
        for tag in soup.find_all(["ul", "ol", "table", "tbody", "div", "section", "article"]):
            all_links = tag.select("a[href]")
            content_links = [a for a in all_links if _is_content_link(a)]
            if len(content_links) < 3:
                continue

            # 得分偏好：内容链接多 + 文本多样化（防导航栏）
            texts = [a.get_text(strip=True) for a in content_links]
            unique_ratio = len(set(texts)) / len(texts)  # 文本重复度（导航栏会很低）
            avg_len = sum(len(t) for t in texts) / len(texts)
            # ul/ol/table 加分（更可能是列表）
            tag_bonus = 2.0 if tag.name in ("ul", "ol", "table", "tbody") else 1.0
            score = len(content_links) * avg_len * unique_ratio * tag_bonus

            if score > best_score:
                best_score = score
                best_container = tag

        if best_container:
            all_links = best_container.select("a[href]")
            content_links = [a for a in all_links if _is_content_link(a)]
            logger.info(
                f"[Auto] BS4回退: <{best_container.name}> 中 {len(content_links)} 个内容链接 "
                f"(总 {len(all_links)}, score={best_score:.0f})"
            )
            return self._extract_list_items(content_links, base_url, best_container)

        logger.warning(f"[Auto] BS4回退未找到合适容器")
        return []

    def _extract_all_links(
        self, soup: BeautifulSoup, base_url: str, existing_count: int = 0
    ) -> List[RawItem]:
        """最后的回退：从整个页面找所有有意义的链接"""
        all_links = soup.select("a[href]")
        meaningful = [
            a for a in all_links
            if len(a.get_text(strip=True)) >= 6  # 更严格：至少 6 个字
            and not a.get("href", "").startswith("#")
            and not a.get("href", "").startswith("javascript:")
            and "首页" not in a.get_text()
            and "返回" not in a.get_text()
        ]
        return self._extract_list_items(meaningful, base_url, soup)

    def _extract_list_items(
        self, links: list, base_url: str, soup_content: BeautifulSoup
    ) -> List[RawItem]:
        """从链接列表提取 RawItem（列表页模式）"""
        items = []
        now = datetime.now()
        seen_urls = set()
        seen_titles = set()
        count = 0

        for a in links:
            if count >= self.source.max_items:
                break

            raw_text = a.get_text(strip=True)
            href = a.get("href", "")
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)

            full_url = urljoin(base_url, href)

            # ── 标题清洗：分离标题和正文 ──
            title, date_text = self._split_title_and_date(raw_text)

            # 标题去重（同一标题只保留一条）
            title_key = title[:40]
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            pub_time = self._parse_date(date_text)

            # ── 上下文隔离：沿 DOM 向上找到最近的独立块元素 ──
            container = a.parent
            while container and container.name not in ("li", "tr", "td", "p", "dt", "dd", "article"):
                # 如果 parent 是 div/span，继续往上找
                if container.parent and container.name in ("div", "span", "a"):
                    container = container.parent
                else:
                    break

            if container and container.name not in ("a",):
                context = container.get_text(" ", strip=True)
            else:
                context = raw_text

            # 截断上下文
            context = context[:500] if len(context) > 500 else context

            items.append(RawItem(
                source_name=self.source.name,
                source_level="L0",
                url=full_url,
                title=title,
                raw_content=context,
                publish_time=pub_time,
                fetched_at=now,
            ))
            count += 1

        return items

    def _split_title_and_date(self, raw_text: str) -> tuple:
        """从原始文本中分离标题和日期

        常见格式:
          - "重点通知与公示公告2026-04-01浙江大学人工智能学院关于..."
          - "2026-04-01通知内容标题..."
          - "[通知]2026-04-01标题..."

        Returns: (title, date_string)
        """
        # 匹配日期模式
        date_patterns = [
            (r'(\d{4}-\d{2}-\d{2})', "%Y-%m-%d"),
            (r'(\d{4}/\d{2}/\d{2})', "%Y/%m/%d"),
            (r'(\d{4}年\d{1,2}月\d{1,2}日)', "%Y年%m月%d日"),
        ]

        date_str = ""
        title = raw_text

        for pattern, _ in date_patterns:
            m = re.search(pattern, raw_text)
            if m:
                date_str = m.group(1)
                # 取日期后面的内容作为标题
                after_date = raw_text[m.end():].strip()
                if len(after_date) > 6:
                    title = after_date[:120]  # 标题最长 120 字
                else:
                    # 日期在末尾，标题在前
                    title = raw_text[:m.start()].strip()[:120]
                break

        # 如果没有日期，直接截断标题
        if not date_str:
            title = raw_text[:120]

        # 去掉标题中的多余符号
        title = title.strip("｜|【】[] ，,。.")

        return title, date_str

    def _extract_article(
        self, html: str, url: str, soup_full: BeautifulSoup
    ) -> List[RawItem]:
        """文章页模式：提取全文作为一个条目"""
        # 用 readability 提取正文
        try:
            doc = ReadabilityDoc(html)
            title = doc.title() or self.source.name
            content_html = doc.summary(html_partial=True)
            content_text = _md.handle(content_html)[:2000]
        except Exception:
            title = self.source.name
            body = soup_full.body
            content_text = body.get_text(" ", strip=True)[:2000] if body else ""

        return [RawItem(
            source_name=self.source.name,
            source_level="L0",
            url=url,
            title=title.strip(),
            raw_content=content_text.strip(),
            publish_time=None,
            fetched_at=datetime.now(),
        )]

    def _fetch(self, url: str) -> Optional[str]:
        """带重试的 HTTP GET"""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        last_error = None
        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(2 ** attempt)
                resp = requests.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code == 200:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text
                elif resp.status_code in (403, 404, 410):
                    logger.error(f"[Auto] {self.source.name} HTTP {resp.status_code}")
                    return None
                else:
                    last_error = f"HTTP {resp.status_code}"
                    logger.warning(f"[Auto] {self.source.name} {last_error} 第{attempt+1}次")
            except requests.RequestException as e:
                last_error = str(e)
                logger.warning(f"[Auto] {self.source.name} {last_error} 第{attempt+1}次")
        logger.error(f"[Auto] {self.source.name} 失败: {last_error}")
        return None

    def _extract_date_from_text(self, text: str) -> str:
        """从文本中匹配日期"""
        patterns = [
            r'(\d{4}-\d{2}-\d{2})',
            r'(\d{4}/\d{2}/\d{2})',
            r'(\d{4}年\d{1,2}月\d{1,2}日)',
            r'(\d{2}-\d{2})',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        return ""

    def _parse_date(self, date_text: str) -> Optional[datetime]:
        if not date_text:
            return None
        for fmt in [
            "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日",
            "%m-%d",
        ]:
            try:
                return datetime.strptime(date_text.strip(), fmt)
            except ValueError:
                continue
        return None
