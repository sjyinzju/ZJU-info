"""L0 Kaggle 采集器 — 基于 Kaggle 官方 API

双引擎:
  - fetch_competitions: 精选竞赛列表（按 deadline 排序）
  - fetch_kernels: 高价值代码/讨论（按投票数排序）

鉴权: KAGGLE_API_TOKEN 环境变量（via config/.env）
"""
import asyncio
import os
import time
from datetime import datetime, timedelta
from typing import List, Optional

from loguru import logger

from .base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


class L0KaggleCollector(BaseCollector):
    """Kaggle 官方 API 采集器"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        super().__init__(source, crawl_config)
        self._api = None
        self.max_competitions = getattr(source, "max_competitions", None) or 10
        self.max_kernels = getattr(source, "max_kernels", None) or 8

    async def collect(self) -> List[RawItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch)

    def _get_api(self):
        """懒加载 Kaggle API 实例"""
        if self._api is None:
            token = os.environ.get("KAGGLE_API_TOKEN")
            if not token:
                logger.error("[Kaggle] KAGGLE_API_TOKEN 未在环境变量中设置")
                raise RuntimeError("KAGGLE_API_TOKEN not set. 请在 config/.env 中添加 KAGGLE_API_TOKEN=your_token")
            try:
                from kaggle.api.kaggle_api_extended import KaggleApi
                self._api = KaggleApi()
                self._api.authenticate()
                logger.info("[Kaggle] API 认证成功")
            except Exception as e:
                logger.error(f"[Kaggle] API 初始化失败: {e}")
                raise
        return self._api

    def _fetch(self) -> List[RawItem]:
        t_start = time.time()
        keywords = self.source.keywords or []
        max_items = self.source.max_items
        now = datetime.now()

        logger.info(f"[Kaggle] 开始采集 (max={max_items}, keywords={keywords})")

        try:
            api = self._get_api()
        except Exception:
            return []

        items: List[RawItem] = []

        # ── 引擎 1: 精选竞赛 ──
        try:
            items += self._fetch_competitions(api, keywords, now)
        except Exception as e:
            logger.error(f"[Kaggle] 竞赛列表拉取失败: {e}")

        # ── 引擎 2: 高价值 Kernels ──
        try:
            items += self._fetch_kernels(api, keywords, now)
        except Exception as e:
            logger.error(f"[Kaggle] Kernels 拉取失败: {e}")

        # ── 总量控制 ──
        items = items[:max_items]

        elapsed = time.time() - t_start
        logger.info(f"[Kaggle] 完成: {len(items)} 条 | 耗时={elapsed:.1f}s")
        return items

    def _fetch_competitions(self, api, keywords: List[str], now: datetime) -> List[RawItem]:
        """拉取活跃竞赛列表（group='general' 包含所有进行中的比赛）"""
        comps = []
        # 先拉 group=general（最多活跃竞赛）
        for group in ["general", "research", "featured"]:
            try:
                resp = api.competitions_list(group=group, sort_by="recentlyCreated")
                if hasattr(resp, "competitions") and resp.competitions:
                    comps.extend(resp.competitions)
            except Exception:
                continue
        if not comps:
            try:
                resp = api.competitions_list()
                if hasattr(resp, "competitions"):
                    comps = resp.competitions or []
            except Exception:
                pass

        # 去重
        seen = set()
        unique = []
        for c in comps:
            ref = getattr(c, "ref", "")
            if ref not in seen:
                seen.add(ref)
                unique.append(c)

        items = []
        count = 0

        for c in unique:
            try:
                title = getattr(c, "title", "") or ""
                ref = getattr(c, "ref", "") or ""
                description = getattr(c, "description", "") or getattr(c, "subtitle", "") or ""
                deadline_str = getattr(c, "deadline", "") or ""
                reward = getattr(c, "reward", "") or getattr(c, "prize", "") or ""
                team_count = getattr(c, "teamCount", 0) or 0
                is_featured = getattr(c, "isFeatured", False) or False
                category = getattr(c, "category", "") or ""

                # 竞赛全量获取（LLM 判断相关性），但过滤明确不相关的分类
                skip_categories = {"getting started", "tutorial", "beginner"}
                if category and category.lower() in skip_categories:
                    continue

                # 竞赛简介
                intro = (description or title)[:300]

                # 元数据拼接
                meta_parts = [f"竞赛: {title}"]
                if deadline_str:
                    meta_parts.append(f"截止日期: {deadline_str}")
                if reward:
                    meta_parts.append(f"奖金: {reward}")
                if team_count:
                    meta_parts.append(f"参赛队伍: {team_count}")
                if is_featured:
                    meta_parts.append("(精选)")
                content = " | ".join(meta_parts)

                url = f"https://www.kaggle.com/c/{ref}" if ref else f"https://www.kaggle.com/competitions"

                # 解析截止日期（可能是字符串或 datetime 对象）
                pub_time = None
                if deadline_str:
                    if isinstance(deadline_str, datetime):
                        pub_time = deadline_str
                    else:
                        for fmt in ["%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"]:
                            try:
                                pub_time = datetime.strptime(str(deadline_str), fmt)
                                break
                            except ValueError:
                                continue

                items.append(RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=url,
                    title=title,
                    raw_content=content,
                    publish_time=pub_time,
                    fetched_at=now,
                ))
                count += 1
                if count >= self.max_competitions:
                    break
            except Exception as e:
                logger.warning(f"[Kaggle] 竞赛条目解析异常: {e}")
                continue

        logger.info(f"[Kaggle] 竞赛: {count} 条")
        return items

    def _fetch_kernels(self, api, keywords: List[str], now: datetime) -> List[RawItem]:
        """拉取高价值 Kernels/讨论"""
        items = []
        count = 0
        one_month_ago = now - timedelta(days=30)

        # 拉取高价值 Kernel/Notebook（按热度或投票排序）
        kernels = []
        for sort_by in ["hotness", "voteCount", "scoreDescending"]:
            try:
                resp = api.kernels_list(sort_by=sort_by, page_size=20, language="all", output_type="all")
                if isinstance(resp, list):
                    kernels = resp
                elif hasattr(resp, "kernels"):
                    kernels = resp.kernels or []
                if kernels:
                    logger.info(f"[Kaggle] Kernels sort_by={sort_by}: {len(kernels)} 个")
                    break
            except Exception as e:
                logger.debug(f"[Kaggle] kernels_list({sort_by}): {e}")
                continue
        if not kernels:
            logger.warning("[Kaggle] Kernels 列表拉取失败（所有排序方式均无结果）")
            return items

        for k in kernels:
            try:
                title = getattr(k, "title", "") or ""
                ref = getattr(k, "ref", "") or ""
                upvotes = getattr(k, "total_votes", 0) or 0
                author = getattr(k, "author", "") or ""
                last_run = getattr(k, "last_run_time", None)

                # 过滤：至少 3 个赞
                if upvotes < 3:
                    continue

                # 关键词过滤
                search_text = f"{title} {ref}".lower()
                if keywords and not any(kw.lower() in search_text for kw in keywords):
                    continue

                url = f"https://www.kaggle.com/code/{ref}" if ref else ""
                meta_parts = [f"Kaggle Kernel: {title}"]
                if author:
                    meta_parts.append(f"作者: {author}")
                if upvotes:
                    meta_parts.append(f"点赞: {upvotes}")
                if last_run:
                    meta_parts.append(f"最近运行: {last_run}")
                content = " | ".join(meta_parts)

                items.append(RawItem(
                    source_name=self.source.name,
                    source_level="L0",
                    url=url,
                    title=title,
                    raw_content=content,
                    publish_time=None,
                    fetched_at=now,
                ))
                count += 1
                if count >= self.max_kernels:
                    break
            except Exception as e:
                logger.warning(f"[Kaggle] Kernel 条目解析异常: {e}")
                continue

        logger.info(f"[Kaggle] Kernels: {count} 条")
        return items
