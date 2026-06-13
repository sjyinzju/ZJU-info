"""L0 HTML 采集器 — requests + BeautifulSoup 解析静态网页

特性:
  - requests.Session 连接复用（TCP 长连接 + 头信息持久化）
  - 完整浏览器级请求头（Accept-Encoding, Sec-Fetch 等）
  - 域名级速率控制（同一域名串行化，避免触发反爬）
  - 细化异常分类（Timeout / ConnectionError / HTTPError / 429 限流）
  - 可溯源日志（每次请求记录耗时、状态码、重试次数）
  - 自动诊断：选择器失败时 dump 页面结构并尝试回退
"""
import asyncio
import random
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


# ══════════════════════════════════════════════════════════════
#  域名级速率控制（跨 Collector 实例共享）
# ══════════════════════════════════════════════════════════════

_domain_locks: Dict[str, threading.Lock] = {}
_domain_last_request: Dict[str, float] = {}
_global_lock = threading.Lock()


def _rate_limit(domain: str, min_interval: float = 1.0):
    """确保对同一域名的请求间隔 >= min_interval 秒"""
    now = time.time()
    with _global_lock:
        last = _domain_last_request.get(domain, 0)
        wait = min_interval - (now - last)
        if wait > 0:
            time.sleep(wait)
        _domain_last_request[domain] = time.time()


# ══════════════════════════════════════════════════════════════
#  回退选择器
# ══════════════════════════════════════════════════════════════

FALLBACK_SELECTORS = [
    "ul.news-list li", "ul.news_list li", "ul.newslist li",
    "div.news-list li", "div.news_list li", "div.news ul li",
    "div.news-con ul li", "div.list-con ul li", "div.list ul li",
    "ul.list li", "ul.list-unstyled li", "div.content ul li",
    "div.main ul li", "div.article-list li", "div.post-list li",
    "div.item", "div.card", "article",
    "table.table tr", "table tr", "li a",
]


# ══════════════════════════════════════════════════════════════
#  采集器
# ══════════════════════════════════════════════════════════════

class L0HtmlCollector(BaseCollector):
    """静态 HTML 页面采集器"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30)
        self.max_retries = crawl_config.get("max_retries", 3)
        delay_range = crawl_config.get("request_delay_seconds", [1, 3])
        self.delay_min = delay_range[0] if delay_range else 1.0
        self.delay_max = delay_range[1] if len(delay_range) > 1 else self.delay_min
        self._diag_dir = Path(__file__).resolve().parent.parent.parent / "info" / "diagnose"
        self._domain = urlparse(self.source.url).netloc

        # ── 构建 Session + 完整请求头 ──
        ua = crawl_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
        # 连接池配置
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=1,
            pool_maxsize=2,
            max_retries=0,           # 重试由我们自己的逻辑控制
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ── 公开接口 ─────────────────────────────────────────────

    async def collect(self) -> List[RawItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_and_parse)

    # ── 采集主流程 ───────────────────────────────────────────

    def _fetch_and_parse(self) -> List[RawItem]:
        url = self.source.url
        selector = self.source.selector
        t_start = time.time()

        logger.info(f"[HTML] {self.source.name} 开始采集 ({url})")

        html, meta = self._fetch_with_retry(url)
        if not html:
            return []

        soup = BeautifulSoup(html, "lxml")

        # ── Step 1: 使用配置的选择器 ──
        if selector:
            elements = soup.select(selector)
            if elements:
                items = self._extract_items(elements, url)
                elapsed = time.time() - t_start
                logger.info(
                    f"[HTML] {self.source.name} OK | "
                    f"选择器='{selector}' → {len(elements)}元素 → {len(items)}条 | "
                    f"HTTP={meta['status']} {meta['size']}B | "
                    f"耗时={elapsed:.1f}s 重试={meta['retries']}"
                )
                return items
            logger.warning(f"[HTML] {self.source.name} 选择器 '{selector}' 匹配 0 个元素")

        # ── Step 2: 自动诊断 + 回退 ──
        self._save_diagnose_html(html)

        for fb_selector in FALLBACK_SELECTORS:
            elements = soup.select(fb_selector)
            if len(elements) >= 3:
                items = self._extract_items(elements, url)
                elapsed = time.time() - t_start
                logger.info(
                    f"[HTML] {self.source.name} 回退OK | "
                    f"回退选择器='{fb_selector}' → {len(elements)}元素 → {len(items)}条 | "
                    f"耗时={elapsed:.1f}s"
                )
                return items

        # ── Step 3: 全失败，dump 诊断 ──
        self._dump_link_structure(soup, url)
        elapsed = time.time() - t_start
        logger.error(
            f"[HTML] {self.source.name} FAIL | "
            f"所有 {len(FALLBACK_SELECTORS)} 个回退选择器均失败 | "
            f"耗时={elapsed:.1f}s"
        )
        return []

    # ── 核心 HTTP 请求（重试 + 错误分类 + 速率控制）─────────

    def _fetch_with_retry(self, url: str) -> tuple:
        """
        带智能重试的 HTTP GET。

        Returns:
            (html_text, metadata_dict) 或 (None, {})
        """
        last_error = None
        meta = {"status": 0, "size": 0, "retries": 0, "error_type": ""}

        for attempt in range(self.max_retries):
            # 速率控制
            _rate_limit(self._domain, min_interval=random.uniform(self.delay_min, self.delay_max))

            try:
                resp = self._session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                )

                meta["status"] = resp.status_code
                meta["size"] = len(resp.content)
                meta["retries"] = attempt

                # ── 处理不同状态码 ──
                if resp.status_code == 200:
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    return resp.text, meta

                elif resp.status_code == 429:
                    # 被限流：等久一点再重试
                    retry_after = resp.headers.get("Retry-After", "10")
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = 10
                    logger.warning(
                        f"[HTML] {self.source.name} 429 Too Many Requests, 等待 {wait}s"
                    )
                    time.sleep(wait)
                    last_error = requests.HTTPError(f"429 Too Many Requests")
                    meta["error_type"] = "rate_limited"
                    continue  # 429 值得重试

                elif resp.status_code in (403, 404, 410):
                    # 不可重试的错误
                    logger.error(
                        f"[HTML] {self.source.name} HTTP {resp.status_code} — 不可重试"
                    )
                    meta["error_type"] = f"http_{resp.status_code}"
                    return None, meta

                elif resp.status_code >= 500:
                    # 服务端错误，可重试
                    logger.warning(
                        f"[HTML] {self.source.name} HTTP {resp.status_code} — 服务端错误，将重试"
                    )
                    last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                    meta["error_type"] = f"http_{resp.status_code}"
                    # 指数退避
                    time.sleep(2 ** attempt)
                    continue

                else:
                    # 其他状态码
                    logger.warning(f"[HTML] {self.source.name} HTTP {resp.status_code}")
                    last_error = requests.HTTPError(f"HTTP {resp.status_code}")
                    meta["error_type"] = f"http_{resp.status_code}"
                    continue

            except requests.Timeout as e:
                last_error = e
                meta["error_type"] = "timeout"
                logger.warning(
                    f"[HTML] {self.source.name} 超时 ({self.timeout}s) "
                    f"第 {attempt+1}/{self.max_retries} 次"
                )
                time.sleep(2 ** attempt)

            except requests.ConnectionError as e:
                last_error = e
                meta["error_type"] = "connection"
                err_str = str(e)[:80]
                logger.warning(
                    f"[HTML] {self.source.name} 连接失败: {err_str} "
                    f"第 {attempt+1}/{self.max_retries} 次"
                )
                time.sleep(2 ** attempt)

            except requests.TooManyRedirects as e:
                last_error = e
                meta["error_type"] = "too_many_redirects"
                logger.error(f"[HTML] {self.source.name} 重定向循环 — 放弃")
                return None, meta

            except requests.RequestException as e:
                last_error = e
                meta["error_type"] = type(e).__name__
                logger.warning(
                    f"[HTML] {self.source.name} {type(e).__name__}: {e} "
                    f"第 {attempt+1}/{self.max_retries} 次"
                )
                time.sleep(2 ** attempt)

        # 所有重试用尽
        logger.error(
            f"[HTML] {self.source.name} 所有 {self.max_retries} 次重试均失败 | "
            f"最后错误: {type(last_error).__name__}: {last_error}"
        )
        return None, meta

    # ── 内容提取 ─────────────────────────────────────────────

    def _extract_items(self, elements, base_url: str) -> List[RawItem]:
        items = []
        now = datetime.now()
        count = 0

        for el in elements:
            if count >= self.source.max_items:
                break

            link_el = None
            if el.name == "a":
                link_el = el
            else:
                link_el = el.find("a", recursive=False)
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

    # ── 诊断工具 ─────────────────────────────────────────────

    def _save_diagnose_html(self, html: str):
        try:
            self._diag_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.source.name)
            filepath = self._diag_dir / f"{safe}.html"
            soup = BeautifulSoup(html, "lxml")
            body = soup.body
            snippet = str(body)[:50000] if body else html[:50000]
            filepath.write_text(snippet, encoding="utf-8")
            logger.info(f"[诊断] HTML 已保存: {filepath}")
        except Exception as e:
            logger.warning(f"[诊断] 保存 HTML 失败: {e}")

    def _dump_link_structure(self, soup, base_url: str):
        links = soup.select("a[href]")
        if not links:
            logger.error(f"[诊断] 页面无链接 — 可能需 Playwright")
            return

        meaningful = []
        for a in links:
            text = a.get_text(strip=True)
            if len(text) >= 5:
                parent = a.parent
                ptag = parent.name if parent else "?"
                pclass = " ".join(parent.get("class", [])) if parent else ""
                meaningful.append((text, a.get("href", ""), ptag, pclass))

        logger.warning(f"[诊断] 页面共 {len(links)} 个链接，有意义 {len(meaningful)} 个:")
        for i, (text, href, ptag, pclass) in enumerate(meaningful[:15]):
            logger.warning(f"  [{i+1}] <{ptag} class='{pclass}'> → {text[:60]}")
            logger.warning(f"       href={href[:80]}")

    # ── 日期解析 ─────────────────────────────────────────────

    def _extract_date(self, element) -> str:
        patterns = [
            ".date", ".time", ".pub-date", ".post-date",
            "span.time", ".news-date", ".list-date",
            ".pull-right span",
            "[class*=date]", "[class*=time]",
        ]
        # 先从元素自身查找，再从父元素查找（日期可能在兄弟节点）
        for search_root in (element, element.parent if element.parent else None):
            if search_root is None:
                continue
            for pat in patterns:
                try:
                    found = search_root.select_one(pat)
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

    def close(self):
        """释放 Session 连接"""
        self._session.close()
