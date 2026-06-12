"""
L5 采集器 — CC98 校园论坛。

Token 自动获取：Password Grant → 缓存 → refresh_token 刷新（30天）。
回退：手动配置的 CC98_TOKEN / CC98_TOKEN_OUT_OF_CAMPUS_NETWORK。

采集策略：
- 关键词搜索：8 组高价值关键词，近 14 天帖子
- 版面监控：11 个目标版面，近 7 天帖子
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from src.collectors.base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
CC98_BASE = "https://api.cc98.org"
CC98_ACCEPT = "application/json, text/plain, */*"

# ── 目标版面 ──
TARGET_BOARDS = {
    68: "学习天地", 581: "科研学术", 102: "留学交流",
    263: "考研一族", 105: "编程技术", 100: "校园信息",
    198: "新生宝典", 459: "实习兼职", 235: "求职广场",
    782: "投资理财", 248: "计算机学院",
}

# ── 关键词搜索组 ──
SEARCH_KEYWORD_GROUPS = [
    ("课程 推荐 给分 评价", "课程信息/经验"),
    ("科研 招募 实验室 招新", "科研信息"),
    ("经验 分享 避坑 总结", "经验分享"),
    ("历年卷 复习 资料", "历年卷与复习资料"),
    ("导师 实验室 评价", "导师/实验室评价"),
    ("转专业 辅修 经验", "转专业/辅修经验"),
    ("组队 内推 招募 实习", "组织招募+内推+组队"),
    ("报名 活动 竞赛", "活动报名"),
]


class L5CC98Collector(BaseCollector):
    """L5 CC98 论坛采集器。

    Token 获取优先级：
    1. 自动 Password Grant（需 cc98_username + cc98_password）
    2. 手动 CC98_TOKEN（备用）
    3. 手动 CC98_TOKEN_OUT_OF_CAMPUS_NETWORK（备用）
    """

    def __init__(
        self,
        source: SourceConfig,
        crawl_config: dict,
        cc98_username: str = "",
        cc98_password: str = "",
        cc98_token: str = "",
        cc98_token_backup: str = "",
    ):
        super().__init__(source, crawl_config)
        self.cc98_username = cc98_username
        self.cc98_password = cc98_password
        self.cc98_token = cc98_token
        self.cc98_token_backup = cc98_token_backup
        self._headers = {}

    # ══════════════════════════════════════════════════════════
    #  Token 获取
    # ══════════════════════════════════════════════════════════

    def _resolve_token(self) -> Optional[str]:
        """获取有效 Token：自动获取 → 缓存刷新 → 手动 Token 回退。"""
        # 1. 自动获取（Password Grant + 缓存刷新）
        if self.cc98_username and self.cc98_password:
            try:
                from src.auth.cc98_auth import get_valid_token
                token = get_valid_token(self.cc98_username, self.cc98_password)
                if token:
                    logger.info("CC98 Token 已就绪（自动获取/缓存/刷新）")
                    return token
            except Exception as e:
                logger.warning(f"CC98 自动 Token 获取异常: {e}")

        # 2. 回退：手动 Token
        for label, tok in [
            ("主 Token", self.cc98_token),
            ("备用 Token", self.cc98_token_backup),
        ]:
            if not tok:
                continue
            # 简单验证：非空即为有效
            if len(tok) > 100:
                logger.info(f"CC98 使用手动 {label}")
                return tok

        logger.warning(
            "CC98 Token 获取失败。请配置 CC98_PASSWORD 以启用自动获取，"
            "或手动填入 CC98_TOKEN"
        )
        return None

    # ══════════════════════════════════════════════════════════
    #  主采集
    # ══════════════════════════════════════════════════════════

    async def collect(self) -> List[RawItem]:
        token = self._resolve_token()
        if not token:
            return []

        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": CC98_ACCEPT,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

        items: List[RawItem] = []
        items.extend(await self._collect_keyword_search())
        items.extend(await self._collect_board_topics())
        return items

    # ══════════════════════════════════════════════════════════
    #  关键词搜索
    # ══════════════════════════════════════════════════════════

    async def _collect_keyword_search(self) -> List[RawItem]:
        logger.info("CC98 关键词搜索...")
        timeout = self.crawl_config.get("timeout_seconds", 30)
        items: List[RawItem] = []
        now_cn = datetime.now(CN_TZ)
        cutoff = now_cn - timedelta(days=14)

        for i, (kw_group, category) in enumerate(SEARCH_KEYWORD_GROUPS):
            if i > 0:
                time.sleep(1.5)
            try:
                r = requests.get(
                    f"{CC98_BASE}/topic/search",
                    params={"keyword": kw_group, "size": 10},
                    headers=self._headers,
                    timeout=timeout,
                )
                if r.status_code != 200:
                    logger.warning(
                        f"CC98 搜索 '{kw_group[:15]}' HTTP {r.status_code}"
                    )
                    continue
                topics = _parse_topic_list(r.json())
            except requests.exceptions.RequestException as e:
                logger.warning(f"CC98 搜索网络错误: {e}")
                continue
            except ValueError:
                continue

            for t in topics:
                item = self._parse_topic(t, f"搜索-{category}", now_cn)
                if item and (not item.publish_time or item.publish_time >= cutoff):
                    items.append(item)

        logger.info(f"CC98 搜索: {len(items)} 条")
        return items

    # ══════════════════════════════════════════════════════════
    #  版面监控
    # ══════════════════════════════════════════════════════════

    async def _collect_board_topics(self) -> List[RawItem]:
        logger.info("CC98 版面监控...")
        timeout = self.crawl_config.get("timeout_seconds", 30)
        items: List[RawItem] = []
        now_cn = datetime.now(CN_TZ)
        cutoff = now_cn - timedelta(days=7)

        for i, (board_id, board_name) in enumerate(TARGET_BOARDS.items()):
            if i > 0:
                time.sleep(0.8)
            try:
                r = requests.get(
                    f"{CC98_BASE}/board/{board_id}/topic",
                    params={"size": 10},
                    headers=self._headers,
                    timeout=timeout,
                )
                if r.status_code != 200:
                    continue
                topics = _parse_topic_list(r.json())
            except requests.exceptions.RequestException:
                continue
            except ValueError:
                continue

            for t in topics:
                item = self._parse_topic(
                    t, f"版面-{board_name}", now_cn, board_name=board_name
                )
                if item and (not item.publish_time or item.publish_time >= cutoff):
                    # 版面帖加关键词过滤
                    keywords = self.source.keywords
                    if keywords:
                        searchable = f"{item.title} {item.raw_content or ''}".lower()
                        if not any(kw.lower() in searchable for kw in keywords):
                            continue
                    items.append(item)

        logger.info(f"CC98 版面: {len(items)} 条")
        return items

    # ══════════════════════════════════════════════════════════
    #  解析
    # ══════════════════════════════════════════════════════════

    def _parse_topic(
        self, t: dict, source_category: str, now_cn: datetime,
        board_name: str = "",
    ) -> Optional[RawItem]:
        title = t.get("title", "")
        if not title:
            return None

        topic_id = t.get("id", "")
        user_name = t.get("userName", "")
        board = board_name or t.get("boardName", "")
        post_time_str = t.get("time", "")
        hit_count = t.get("hitCount", 0)
        reply_count = t.get("replyCount", 0)
        content = t.get("content", "")

        pub_time = now_cn
        if post_time_str:
            try:
                pub_time = datetime.fromisoformat(
                    post_time_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        content_clean = (
            content[:300].replace("\n", " ").strip() if content else ""
        )

        return RawItem(
            source_name=f"CC98 - {board or source_category}",
            source_level="L5",
            url=f"https://www.cc98.org/topic/{topic_id}" if topic_id else "",
            title=title,
            raw_content="\n".join(
                p for p in [
                    f"作者: {user_name}",
                    f"版面: {board}" if board else "",
                    f"浏览: {hit_count} | 回复: {reply_count}",
                    f"内容摘要: {content_clean}" if content_clean else "",
                    f"来源分类: {source_category}",
                ] if p
            ),
            publish_time=pub_time,
            fetched_at=now_cn,
        )


def _parse_topic_list(data) -> list:
    """统一解析 CC98 API 返回的帖子列表（list 或 dict wrapper）。"""
    if isinstance(data, list):
        return data
    return data.get("topics", data.get("items", data.get("data", [])))
