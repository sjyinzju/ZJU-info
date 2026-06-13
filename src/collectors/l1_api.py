"""L1 API 采集器 — REST/JSON API 数据获取

支持 GET 和 POST，从 JSON 返回中映射字段到 RawItem。
配置中通过 extra_fields 指定 JSON 字段映射。
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests
from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


class L1ApiCollector(BaseCollector):
    """REST API 采集器（支持 GET/POST JSON）"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self.timeout = crawl_config.get("timeout_seconds", 30)
        self.max_retries = crawl_config.get("max_retries", 3)
        self.user_agent = crawl_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        )
        # 读取 extra 配置
        self.method = getattr(source, "method", "GET") or "GET"
        self.body = getattr(source, "body", None) or {}
        self.list_path = getattr(source, "list_path", None) or "result.records"
        self.title_field = getattr(source, "title_field", None) or "xwbt"
        self.date_field = getattr(source, "date_field", None) or "fbsj"
        self.url_field = getattr(source, "url_field", None) or "xwid"
        self.url_prefix = getattr(source, "url_prefix", None) or ""
        self.detail_url_template = getattr(source, "detail_url_template", None) or ""

    async def collect(self) -> List[RawItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_api)

    def _fetch_api(self) -> List[RawItem]:
        url = self.source.url
        t_start = time.time()

        logger.info(f"[API] {self.source.name} → {url}")

        # 请求
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        extra_h = getattr(self.source, "extra_headers", None)
        if extra_h:
            headers.update(extra_h)
        last_error = None

        for attempt in range(self.max_retries):
            try:
                if attempt > 0:
                    time.sleep(2 ** attempt)
                if self.method.upper() == "POST":
                    resp = requests.post(url, headers=headers, json=self.body, timeout=self.timeout)
                else:
                    resp = requests.get(url, headers=headers, timeout=self.timeout)

                if resp.status_code != 200:
                    logger.warning(f"[API] {self.source.name} HTTP {resp.status_code} 第{attempt+1}次")
                    last_error = f"HTTP {resp.status_code}"
                    continue

                data = resp.json()
                break
            except Exception as e:
                last_error = str(e)
                logger.warning(f"[API] {self.source.name} {e} 第{attempt+1}次")
        else:
            logger.error(f"[API] {self.source.name} 失败: {last_error}")
            return []

        # 导航到列表路径
        records = data
        for key in self.list_path.split("."):
            if isinstance(records, dict):
                records = records.get(key, [])
            else:
                break
        if not isinstance(records, list):
            records = []

        # 生成 RawItem
        items = []
        now = datetime.now()
        count = 0

        for rec in records:
            if count >= self.source.max_items:
                break

            title = self._get_field(rec, self.title_field)
            if not title:
                continue

            pub_time = None
            date_str = self._get_field(rec, self.date_field)
            if date_str:
                for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"]:
                    try:
                        pub_time = datetime.strptime(date_str, fmt)
                        break
                    except ValueError:
                        continue

            # 构建详情 URL + 噪音过滤
            tzljdz = self._get_field(rec, "tzljdz")
            xwid = self._get_field(rec, "xwid")
            xwnr = self._get_field(rec, "xwnr")

            # ── 噪音过滤 ──
            # 1. 跳过图片 URL
            if tzljdz and any(tzljdz.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".bmp")):
                continue
            # 2. 跳过内容为空的"平台首页"类条目
            if (not xwnr or xwnr == "null") and (not tzljdz):
                # 标题看起来像平台名而非活动通知
                if any(kw in title for kw in ["就业服务平台", "就业中心首页", "首页"]):
                    continue

            item_url = url
            # url_field + url_prefix: e.g. nnid → https://dasai.lanqiao.cn/notices/839
            if self.url_field and self.url_field != "xwid":
                id_val = self._get_field(rec, self.url_field)
                if id_val and self.url_prefix:
                    item_url = self.url_prefix + id_val
            if item_url == url and tzljdz:
                item_url = tzljdz
            if item_url == url and xwid and self.detail_url_template:
                item_url = self.detail_url_template.format(xwid=xwid)

            # 内容：通用字段提取（适配不同 API 的字段名）
            content_parts = []
            # 常见内容字段
            for field_name in ("xwnr", "brief", "description", "summary", "xwfbt"):
                val = self._get_field(rec, field_name)
                if val and val != "null" and val != title:
                    content_parts.append(val[:300])
                    break
            # 奖金
            bonus = self._get_field(rec, "bonus")
            if bonus and bonus != "0":
                content_parts.append(f"奖金: {bonus}")
            # 主办方
            org = self._get_field(rec, "organizers")
            if org:
                content_parts.append(f"主办: {org}")
            # 已报名人数
            reg = self._get_field(rec, "registered_person_num")
            if reg and reg != "0":
                content_parts.append(f"已报名: {reg}人")
            # 当前阶段
            stage = self._get_field(rec, "current_stage_Name")
            if stage:
                content_parts.append(f"当前阶段: {stage}")
            # 活动时间/地点
            for t_field in ("hdsj", "start_time"):
                t_val = self._get_field(rec, t_field)
                if t_val:
                    content_parts.append(f"时间: {t_val}")
                    break
            hddd = self._get_field(rec, "hddd")
            if hddd:
                content_parts.append(f"地点: {hddd}")
            if tzljdz and ("weixin" in tzljdz or "mp.weixin" in tzljdz):
                content_parts.append("(全文见微信公众号)")
            if not content_parts:
                content_parts.append(title)
            content = " | ".join(content_parts)[:500]

            items.append(RawItem(
                source_name=self.source.name,
                source_level="L1",
                url=item_url,
                title=title.strip(),
                raw_content=content,
                publish_time=pub_time,
                fetched_at=now,
            ))
            count += 1

        elapsed = time.time() - t_start
        logger.info(f"[API] {self.source.name} OK | {len(records)} records → {len(items)} items | 耗时={elapsed:.1f}s")
        return items

    def _get_field(self, record: dict, field: str) -> str:
        """安全获取字段值"""
        val = record.get(field, "")
        return str(val).strip() if val else ""
