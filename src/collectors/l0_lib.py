"""
L0 图书馆讲座展览采集器 — 列表页 → 详情页深度抓取。

libweb.zju.edu.cn 使用 WebPlus CMS。
列表页 .news_list li → 详情页 .wp_articlecontent。
支持 WeChat 链接的标题级抓取（不跟进详情）。
"""

import logging
import re
import time
from datetime import datetime, timezone
from typing import List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.collectors.base import BaseCollector
from src.models.raw_item import RawItem

logger = logging.getLogger(__name__)

CN_TZ = timezone(__import__("datetime").timedelta(hours=8))
BASE_URL = "https://libweb.zju.edu.cn"


class L0LibCollector(BaseCollector):
    """图书馆讲座展览采集器 — 列表 + 详情页双层抓取。"""

    async def collect(self) -> List[RawItem]:
        items: List[RawItem] = []
        seen_urls = set()
        timeout = self.crawl_config.get("timeout_seconds", 30)
        max_pages = self.source.max_items or 3
        headers = {
            "User-Agent": self.crawl_config.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ),
        }

        list_url = self.source.url
        if not list_url:
            return items

        now_cn = datetime.now(CN_TZ)

        # 遍历分页
        for page in range(1, max_pages + 1):
            if page == 1:
                page_url = list_url
            else:
                # list.htm → list2.htm, list3.htm ...
                page_url = re.sub(r'list\.htm', f'list{page}.htm', list_url)
                if page_url == list_url:
                    page_url = re.sub(r'\.htm$', f'{page}.htm', list_url)

            logger.debug(f"图书馆第{page}页: {page_url}")
            try:
                resp = requests.get(page_url, headers=headers, timeout=timeout)
                resp.encoding = "utf-8"
            except requests.exceptions.RequestException as e:
                logger.warning(f"图书馆列表页{page}网络错误: {e}")
                break

            if resp.status_code != 200:
                break

            soup = BeautifulSoup(resp.text, "html.parser")
            entries = soup.select(".news_list li")
            if not entries:
                break

            for li in entries:
                a_tag = li.find("a", href=True)
                if not a_tag:
                    continue

                title = a_tag.get_text(strip=True)
                href = a_tag["href"]
                full_url = urljoin(BASE_URL, href) if not href.startswith("http") else href

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # 提取日期
                date_str = ""
                date_match = re.search(r"(\d{4}-\d{2}-\d{2})", str(li))
                if date_match:
                    date_str = date_match.group(1)

                pub_time = now_cn
                if date_str:
                    try:
                        pub_time = datetime.strptime(date_str, "%Y-%m-%d")
                        pub_time = pub_time.replace(tzinfo=CN_TZ)
                    except ValueError:
                        pass

                # 关键词过滤
                keywords = self.source.keywords
                if keywords and not any(
                    kw.lower() in title.lower() for kw in keywords
                ):
                    continue

                is_wechat = "mp.weixin.qq.com" in full_url
                content = ""

                if not is_wechat:
                    # 抓取详情页
                    content = self._fetch_detail(full_url, headers, timeout)

                items.append(RawItem(
                    source_name="浙大图书馆",
                    source_level="L0",
                    url=full_url,
                    title=title,
                    raw_content=content if content else f"标题: {title}\n日期: {date_str}",
                    publish_time=pub_time if pub_time else now_cn,
                    fetched_at=now_cn,
                ))

                # 页面间短暂延迟
                time.sleep(0.5)

        logger.info(f"图书馆: {len(items)} 条（{len(seen_urls)} 条去重后）")
        return items

    def _fetch_detail(
        self, url: str, headers: dict, timeout: int
    ) -> str:
        """抓取详情页，提取 .wp_articlecontent 正文。"""
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.encoding = "utf-8"
        except requests.exceptions.RequestException:
            return ""

        if resp.status_code != 200:
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        content_el = soup.select_one(".wp_articlecontent")
        if not content_el:
            return ""

        # 提取纯文本，保留换行
        text = content_el.get_text(separator="\n", strip=True)
        # 清理多余空行
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 截断过长的内容
        if len(text) > 800:
            text = text[:800] + "…"
        return text
