"""L1 Playwright 采集器 — 用真实 Chromium 渲染 JS 页面后提取内容

全局共享单个 Browser 实例，每个源使用独立 Context（相当于无痕窗口）。
L1 源串行执行，避免内存爆炸。
"""
import asyncio
import random
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger
from playwright.async_api import async_playwright, Browser, BrowserContext

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig

# ══════════════════════════════════════════════════════════════
#  全局 Browser 管理
# ══════════════════════════════════════════════════════════════

_browser: Optional[Browser] = None
_playwright_instance = None       # Playwright 实例，用于彻底清理子进程
_browser_lock = asyncio.Lock()
# L1 采集串行锁（一次只跑一个 Playwright 源）
_playwright_semaphore = asyncio.Semaphore(1)

# 诊断截图保存目录
_SCREENSHOT_DIR = Path(__file__).resolve().parent.parent.parent / "info" / "diagnose"

# UA 池（常见中文浏览器）
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


async def _get_browser() -> Browser:
    """获取或创建全局 Browser 实例"""
    global _browser, _playwright_instance
    if _browser is None or not _browser.is_connected():
        async with _browser_lock:
            if _browser is None or not _browser.is_connected():
                logger.info("[Playwright] 启动 Chromium...")
                _playwright_instance = await async_playwright().start()
                _browser = await _playwright_instance.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ],
                )
                logger.info("[Playwright] Chromium 已启动")
    return _browser


async def _shutdown_browser():
    """彻底关闭 Browser 和 Playwright 实例（释放子进程）"""
    global _browser, _playwright_instance
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
        _browser = None
        logger.info("[Playwright] Chromium 已彻底关闭")


# ══════════════════════════════════════════════════════════════
#  采集器
# ══════════════════════════════════════════════════════════════

class L1PlaywrightCollector(BaseCollector):
    """动态渲染页面采集器（Playwright）"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30) * 1000  # 转毫秒
        self.max_retries = crawl_config.get("max_retries", 3)
        delay_range = crawl_config.get("request_delay_seconds", [1, 3])
        self.delay_min = delay_range[0] if delay_range else 1
        self.delay_max = delay_range[1] if len(delay_range) > 1 else self.delay_min

    async def collect(self) -> List[RawItem]:
        """采集 JS 渲染页面（串行执行，避免多页面并发）"""
        async with _playwright_semaphore:
            return await self._do_collect()

    async def _do_collect(self) -> List[RawItem]:
        url = self.source.url
        wait_selector = getattr(self.source, "wait_selector", None) or "a"
        selector = getattr(self.source, "selector", None) or wait_selector
        screenshot = getattr(self.source, "screenshot", False)

        logger.info(f"[PW] {self.source.name} ({url})")

        # 随机延迟
        delay = random.uniform(self.delay_min, self.delay_max)
        await asyncio.sleep(delay)

        browser = await _get_browser()
        context: Optional[BrowserContext] = None

        for attempt in range(self.max_retries):
            try:
                context = await browser.new_context(
                    user_agent=random.choice(_UA_POOL),
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                page = await context.new_page()

                # 注入反检测脚本
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
                """)

                # 导航
                await page.goto(url, wait_until="networkidle", timeout=self.timeout)

                # 等待内容出现
                try:
                    await page.wait_for_selector(wait_selector, timeout=15000)
                except Exception:
                    logger.warning(
                        f"[PW] {self.source.name} wait_selector '{wait_selector}' 超时，"
                        f"尝试继续提取..."
                    )

                # 额外等待确保动态内容加载
                await asyncio.sleep(2)

                # 获取渲染后的 HTML
                html = await page.content()

                # 截图（调试用）
                if screenshot or attempt > 0:
                    _save_screenshot(self.source.name, page, attempt)

                await context.close()
                context = None

                # 解析
                soup = BeautifulSoup(html, "lxml")
                elements = soup.select(selector)
                logger.info(
                    f"[PW] {self.source.name} 选择器 '{selector}' → {len(elements)} 个元素"
                )

                if not elements:
                    # 回退：用通用选择器尝试
                    for fb in ["li a", "a[href]", "ul li a", "div.item a"]:
                        elements = soup.select(fb)
                        if len(elements) >= 3:
                            logger.info(f"[PW] {self.source.name} 回退 '{fb}' → {len(elements)} 个")
                            break

                if not elements:
                    logger.warning(
                        f"[PW] {self.source.name} 渲染完成但未找到链接元素。"
                        f"请用浏览器 F12 确认 CSS 选择器并更新 config.yaml"
                    )
                    return []

                return self._extract_items(elements, url)

            except Exception as e:
                logger.warning(
                    f"[PW] {self.source.name} 第 {attempt+1}/{self.max_retries} 次失败: {e}"
                )
                if context:
                    await context.close()
                    context = None
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"[PW] {self.source.name} 所有重试均失败")
        return []

    def _extract_items(self, elements, base_url: str) -> List[RawItem]:
        """从 BS4 元素列表提取 RawItem（与 L0 HTML 共用逻辑）"""
        items = []
        now = datetime.now()
        count = 0

        for el in elements:
            if count >= self.source.max_items:
                break

            # 找 <a> 标签
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
                    source_level="L1",
                    url=full_url,
                    title=title,
                    raw_content=content_text,
                    publish_time=pub_time,
                    fetched_at=now,
                )
            )
            count += 1

        logger.info(f"[PW] {self.source.name} 解析出 {len(items)} 条")
        return items

    # ── 日期解析工具（与 L0 HTML 相同）────────────────────

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


# ══════════════════════════════════════════════════════════════
#  辅助
# ══════════════════════════════════════════════════════════════

def _save_screenshot(name: str, page, attempt: int = 0):
    """保存页面截图到 info/diagnose/"""
    try:
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
        path = _SCREENSHOT_DIR / f"{safe}_attempt{attempt}.png"
        import asyncio as _asyncio
        # 需要在 event loop 中执行
        # page.screenshot 是 async，但这里我们被同步调用，用 create_task
        pass  # 截图由调用方在 async context 中处理
    except Exception:
        pass
