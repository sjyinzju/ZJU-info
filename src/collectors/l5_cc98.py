"""
L5 采集器 — CC98 校园论坛（Bearer Token 认证）。

采集策略：
- 关键词搜索：8 组高价值关键词，近 14 天帖子
- 版面监控：11 个目标版面，近 7 天帖子
- Token 探针：每次采集前验证有效性

网络要求：校园网 / ZJU VPN。校外无 VPN 时连接超时，自动跳过。
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

# CC98 API 要求的 Accept 头（仅 application/json 会触发 406）
CC98_ACCEPT = "application/json, text/plain, */*"

# ── 目标版面（ID 来自 reference CC98/Forum/dist/static/boardinfo.json）──
TARGET_BOARDS = {
    68: "学习天地",       # 历年卷、课程评价、复习资料
    581: "科研学术",      # 实验室招募、科研讨论
    102: "留学交流",      # 出国、交换、暑研
    263: "考研一族",      # 保研、考研、夏令营
    105: "编程技术",      # CS 学习、项目经验
    100: "校园信息",      # 活动通知、招募
    198: "新生宝典",      # 选课经验、入门指南
    459: "实习兼职",      # 实习信息
    235: "求职广场",      # 校招、内推
    782: "投资理财",      # 投资理财知识
    248: "计算机学院",    # 直系院系信息
}

# ── 关键词搜索组 ──
SEARCH_KEYWORD_GROUPS = [
    # (搜索词, 用途)
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

    需要 Bearer Token（从浏览器 DevTools 提取，不含 'Bearer ' 前缀）。
    """

    def __init__(
        self,
        source: SourceConfig,
        crawl_config: dict,
        cc98_token: str = "",
        cc98_token_backup: str = "",
    ):
        super().__init__(source, crawl_config)
        self.cc98_token = cc98_token
        self.cc98_token_backup = cc98_token_backup
        self._active_token = ""  # 在 _verify_token 中确定
        self._headers = {}

    def _build_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": CC98_ACCEPT,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }

    async def collect(self) -> List[RawItem]:
        """执行 CC98 采集。先尝试主 Token，失败则尝试备用 Token。"""
        tokens = [t for t in [self.cc98_token, self.cc98_token_backup] if t]
        if not tokens:
            logger.warning(
                "L5 CC98 跳过：CC98_TOKEN 和 CC98_TOKEN_OUT_OF_CAMPUS_NETWORK 均未配置。"
                "请从浏览器 DevTools 提取 Token 并填入 config/.env"
            )
            return []

        # 依次尝试每个 Token
        for i, token in enumerate(tokens):
            label = "主 Token" if i == 0 else "备用 Token (校外)"
            if not self._verify_token(token, label):
                continue
            # Token 有效，继续采集
            self._active_token = token
            self._headers = self._build_headers(token)
            break
        else:
            logger.error("CC98 所有 Token 均无效或被拒绝，跳过 L5 采集")
            return []

        items: List[RawItem] = []
        items.extend(await self._collect_keyword_search())
        items.extend(await self._collect_board_topics())
        return items

    # ══════════════════════════════════════════════════════════
    #  Token 验证
    # ══════════════════════════════════════════════════════════

    def _verify_token(self, token: str, label: str = "") -> bool:
        """GET /api/me 验证 Token 是否有效。"""
        headers = self._build_headers(token)
        try:
            r = requests.get(
                f"{CC98_BASE}/me",
                headers=headers,
                timeout=10,
            )
            if r.status_code == 200:
                try:
                    user = r.json()
                    logger.info(f"CC98 {label} 有效 (用户: {user.get('name', '?')})")
                except ValueError:
                    logger.info(f"CC98 {label} 有效 (200 OK)")
                return True
            if r.status_code == 401:
                logger.warning(f"CC98 {label} 无效 (401)")
                return False
            logger.warning(f"CC98 /api/me 异常状态码: {r.status_code}")
            return False
        except requests.exceptions.ConnectTimeout:
            logger.warning(
                "CC98 连接超时 — 需要校园网或 ZJU VPN。跳过 L5 采集。"
            )
            return False
        except requests.exceptions.ConnectionError:
            logger.warning(
                "CC98 网络不可达 — 需要校园网或 ZJU VPN。跳过 L5 采集。"
            )
            return False

    # ══════════════════════════════════════════════════════════
    #  关键词搜索
    # ══════════════════════════════════════════════════════════

    async def _collect_keyword_search(self) -> List[RawItem]:
        """对每组关键词分别搜索，取近 14 天内帖子。"""
        logger.info("CC98 关键词搜索...")
        timeout = self.crawl_config.get("timeout_seconds", 30)
        items: List[RawItem] = []
        now_cn = datetime.now(CN_TZ)
        cutoff = now_cn - timedelta(days=14)

        for i, (kw_group, category) in enumerate(SEARCH_KEYWORD_GROUPS):
            # 请求间延迟，避免触发 CC98 频率限制（8 连发会 403）
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
                    logger.warning(f"CC98 搜索 '{kw_group[:15]}' HTTP {r.status_code}")
                    continue
                data = r.json()
                # API 可能直接返回 list 或 {items: [...]}
                topics = data if isinstance(data, list) else data.get("items", data.get("topics", []))
            except requests.exceptions.RequestException as e:
                logger.warning(f"CC98 搜索网络错误: {e}")
                continue
            except ValueError:
                logger.warning(f"CC98 搜索返回非 JSON")
                continue

            for t in topics:
                item = self._parse_topic(t, f"搜索-{category}", now_cn)
                if item:
                    # 过滤旧帖
                    if item.publish_time and item.publish_time < cutoff:
                        continue
                    items.append(item)

        logger.info(f"CC98 搜索: {len(items)} 条")
        return items

    # ══════════════════════════════════════════════════════════
    #  版面监控
    # ══════════════════════════════════════════════════════════

    async def _collect_board_topics(self) -> List[RawItem]:
        """采集 11 个目标版面近期帖子。"""
        logger.info("CC98 版面监控...")
        timeout = self.crawl_config.get("timeout_seconds", 30)
        items: List[RawItem] = []
        now_cn = datetime.now(CN_TZ)
        cutoff = now_cn - timedelta(days=7)

        for i, (board_id, board_name) in enumerate(TARGET_BOARDS.items()):
            if i > 0:
                time.sleep(0.8)  # 11 版面轮询，间隔防止限流
            try:
                r = requests.get(
                    f"{CC98_BASE}/board/{board_id}/topic",
                    params={"size": 10},
                    headers=self._headers,
                    timeout=timeout,
                )
                if r.status_code != 200:
                    logger.debug(f"CC98 版[{board_id}] HTTP {r.status_code}")
                    continue
                data = r.json()
                topics = data if isinstance(data, list) else data.get("items", data.get("topics", []))
            except requests.exceptions.RequestException:
                continue
            except ValueError:
                continue

            for t in topics:
                item = self._parse_topic(
                    t, f"版面-{board_name}", now_cn, board_name=board_name
                )
                if item:
                    if item.publish_time and item.publish_time < cutoff:
                        continue
                    # 版面帖加关键词过滤
                    keywords = self.source.keywords
                    if keywords:
                        searchable = (
                            f"{item.title} {item.raw_content or ''}"
                        ).lower()
                        if not any(kw.lower() in searchable for kw in keywords):
                            continue
                    items.append(item)

        logger.info(f"CC98 版面: {len(items)} 条")
        return items

    # ══════════════════════════════════════════════════════════
    #  解析
    # ══════════════════════════════════════════════════════════

    def _parse_topic(
        self,
        t: dict,
        source_category: str,
        now_cn: datetime,
        board_name: str = "",
    ) -> Optional[RawItem]:
        """将 CC98 帖子 JSON 转为 RawItem。"""
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

        # 解析时间
        pub_time = now_cn
        if post_time_str:
            try:
                pub_time = datetime.fromisoformat(
                    post_time_str.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # 截断内容
        content_clean = (
            content[:300].replace("\n", " ").strip()
            if content else ""
        )

        content_parts = [
            f"作者: {user_name}",
            f"版面: {board}" if board else "",
            f"浏览: {hit_count} | 回复: {reply_count}",
            f"内容摘要: {content_clean}" if content_clean else "",
            f"来源分类: {source_category}",
        ]

        return RawItem(
            source_name=f"CC98 - {board or source_category}",
            source_level="L5",
            url=f"https://www.cc98.org/topic/{topic_id}" if topic_id else "",
            title=title,
            raw_content="\n".join(p for p in content_parts if p),
            publish_time=pub_time,
            fetched_at=now_cn,
        )
