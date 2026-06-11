"""
L4 采集器 — ZJU 统一认证后的校内资源。

子采集器：
- 学在浙大：近期 deadline（详细紧急度分级）+ 课程信息
- 教务系统：通知公告（精准关键词）+ 考试安排（60天）+ 成绩变化检测
- 对外交流：报名中项目、名额信息
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

from src.collectors.base import BaseCollector
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig
from src.auth.zjuam_manager import ZjuAmSession

logger = logging.getLogger(__name__)

# ── 学在浙大 ──────────────────────────────────────────────────
COURSES_SERVICE_URL = "https://courses.zju.edu.cn/user/index"
COURSES_TODOS_URL = "https://courses.zju.edu.cn/api/todos"

# ── 教务系统 ZDBK ─────────────────────────────────────────────
ZDBK_SERVICE_URL = "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html"
ZDBK_BASE = "https://zdbk.zju.edu.cn/jwglxt"

ZDBK_NOTICE_URL = (
    f"{ZDBK_BASE}/xtgl/xwgl_cxXwListGyxtzgg.html"
    "?gnmkdm=index&doType=query&queryModel.showCount=50"
)
ZDBK_NOTICE_DETAIL = f"{ZDBK_BASE}/xtgl/xwgl_cxXwViewGyxtzgg.html?gnmkdm=index"

ZDBK_GRADES_URL = (
    f"{ZDBK_BASE}/cxdy/xscjcx_cxXscjIndex.html"
    "?doType=query&queryModel.showCount=5000"
)
ZDBK_EXAMS_URL = (
    f"{ZDBK_BASE}/xskscx/kscx_cxXsgrksIndex.html"
    "?doType=query&queryModel.showCount=5000"
)
# 对外交流申请
ZDBK_EXCHANGE_URL = (
    f"{ZDBK_BASE}/jlsgl/xjlssq_cxJlssqIndex.html"
    "?gnmkdm=N10653005&layout=default"
)

GRADES_CACHE_DIR = Path("config/cookies")

ZDBK_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": f"{ZDBK_BASE}/xtgl/index_initMenu.html",
}

CN_TZ = timezone(timedelta(hours=8))

# ── 通知公告精准关键词 ──────────────────────────────────────
# 用户指定的高优先级关键词（精确匹配即分类标注）
PRIORITY_KEYWORDS = [
    "选课", "辅修", "微辅修", "双学位", "跨专业",
    "交流", "交换", "出国", "出境", "出入境", "境外", "访学", "暑校", "国际",
    "四六级", "CET", "英语等级", "外语等级",
    "保研", "推免", "考研", "直博", "硕博连读", "夏令营", "复试",
    "奖学金", "评优", "评奖", "国奖", "省政府", "荣誉称号",
    "补考", "缓考",
    "新课", "新开课", "课程",
    "注册", "报到", "开学", "校历", "放假",
    "安排", "通知",
]

# 分类→关键词映射（用于在 raw_content 中标注标签）
NOTICE_CATEGORIES = {
    "选课/辅修": ["选课", "辅修", "微辅修", "双学位", "跨专业", "课程补选", "退课", "扩容", "新课", "新开课"],
    "对外交流": ["交流", "交换", "出国", "出境", "出入境", "境外", "国际", "访学", "暑校", "summer"],
    "四六级/语言": ["四六级", "CET", "六级", "四级", "英语等级", "外语等级", "普通话"],
    "保研/考研": ["保研", "推免", "考研", "直博", "硕博连读", "硕博", "夏令营", "招生", "复试"],
    "学期安排": ["校历", "放假", "开学", "注册", "报到", "教学周", "调课", "停课", "安排"],
    "奖学金/评优": ["奖学金", "评优", "评奖", "荣誉称号", "三好", "国奖", "省政府"],
    "考试/补考": ["考试", "补考", "缓考", "期中", "期末"],
    "竞赛/科研": ["竞赛", "大赛", "科创", "科研", "创新", "项目申报", "SRTP"],
    "毕业论文": ["毕业论文", "毕业设计", "答辩", "开题", "中期检查"],
}


class L4ZjuAmCollector(BaseCollector):
    """L4 ZJUAM 认证资源采集器。"""

    def __init__(
        self,
        source: SourceConfig,
        crawl_config: dict,
        auth_config: Optional[dict] = None,
    ):
        super().__init__(source, crawl_config)
        self.auth_config = auth_config or {}
        self._zjuam: Optional[ZjuAmSession] = None
        self._zdbk_session: Optional[requests.Session] = None

    async def collect(self) -> List[RawItem]:
        username = self.auth_config.get("username", "")
        password = self.auth_config.get("password", "")

        if not username or not password:
            logger.warning("L4 采集跳过：ZJU_USERNAME / ZJU_PASSWORD 未配置")
            return []

        src_name = self.source.name
        src_url = self.source.url or ""

        self._zjuam = ZjuAmSession(username, password)
        items: List[RawItem] = []

        try:
            if "学在浙大" in src_name or "courses" in src_url:
                items.extend(await self._collect_courses_todos())

            if "教务" in src_name or "zdbk" in src_url:
                items.extend(await self._collect_zdbk())
        finally:
            self._zjuam.close()
            if self._zdbk_session:
                self._zdbk_session.close()

        return items

    # ══════════════════════════════════════════════════════════════
    #  学在浙大 — 详细 Deadline 采集
    # ══════════════════════════════════════════════════════════════

    async def _collect_courses_todos(self) -> List[RawItem]:
        """采集学在浙大待办，按紧急度详细分级。"""
        logger.info("采集学在浙大待办事项...")

        sub = self._zjuam.sso_to_subsystem(COURSES_SERVICE_URL)
        if sub is None:
            logger.error("学在浙大 SSO 失败")
            return []

        try:
            resp = sub.get(
                COURSES_TODOS_URL,
                timeout=self.crawl_config.get("timeout_seconds", 30),
            )
            if resp.status_code != 200:
                logger.error(f"学在浙大 API HTTP {resp.status_code}")
                return []
            data = resp.json()
        except Exception as e:
            logger.error(f"学在浙大 API 错误: {e}")
            return []
        finally:
            sub.close()

        todo_list = data.get("todo_list", [])
        if not todo_list:
            logger.info("学在浙大：无待办")
            return []

        now_cn = datetime.now(CN_TZ)
        items = []

        for todo in todo_list:
            item = self._parse_todo_detailed(todo, now_cn)
            if item:
                items.append(item)

        # 按紧急度排序：已过期 → 今天 → 本周 → 本月 → 远期 → 无截止
        def urgency_key(item: RawItem) -> tuple:
            content = item.raw_content or ""
            pt = item.publish_time
            if "已过期" in content:
                return (0, pt or datetime.max.replace(tzinfo=CN_TZ))
            if "今天" in content or "小时" in content:
                return (1, pt or datetime.max.replace(tzinfo=CN_TZ))
            if "距截止" in content:
                days_match = re.search(r"距截止 (\d+) 天", content)
                if days_match:
                    days = int(days_match.group(1))
                    if days <= 3:
                        return (2, pt or datetime.max.replace(tzinfo=CN_TZ))
                    if days <= 7:
                        return (3, pt or datetime.max.replace(tzinfo=CN_TZ))
                    if days <= 30:
                        return (4, pt or datetime.max.replace(tzinfo=CN_TZ))
                    return (5, pt or datetime.max.replace(tzinfo=CN_TZ))
            return (6, datetime.max.replace(tzinfo=CN_TZ))

        items.sort(key=urgency_key)

        # 统计
        expired = sum(1 for i in items if "已过期" in (i.raw_content or ""))
        urgent = sum(1 for i in items if "今天" in (i.raw_content or "") or
                     ("距截止" in (i.raw_content or "") and
                      any(f"距截止 {d} 天" in (i.raw_content or "") for d in range(1, 4))))
        week = sum(1 for i in items if "距截止" in (i.raw_content or "") and
                   any(f"距截止 {d} 天" in (i.raw_content or "") for d in range(4, 8)))

        logger.info(
            f"学在浙大: {len(items)} 条 "
            f"（已过期 {expired}，紧急 {urgent}，本周 {week}）"
        )
        return items

    def _parse_todo_detailed(
        self, todo: dict, now_cn: datetime
    ) -> Optional[RawItem]:
        """详细解析 todo，包含更多上下文和行动建议。"""
        course_name = todo.get("course_name", "未知课程")
        title = todo.get("title", "无标题")
        todo_type = todo.get("type", "")
        is_locked = todo.get("is_locked", False)
        course_code = todo.get("course_code", "")

        # 截止时间
        end_time_str = todo.get("end_time", "")
        end_time: Optional[datetime] = None
        deadline_detail = ""
        urgency = "normal"
        suggestion = ""

        if end_time_str:
            try:
                end_time = datetime.fromisoformat(
                    end_time_str.replace("Z", "+00:00")
                )
                end_time_cn = end_time.astimezone(CN_TZ)
                remaining = end_time_cn - now_cn
                remaining_hours = remaining.total_seconds() / 3600

                if remaining.total_seconds() < 0:
                    deadline_detail = (
                        f"已过期（原截止: {end_time_cn.strftime('%m月%d日 %H:%M')}）"
                    )
                    urgency = "expired"
                    suggestion = "联系教师确认是否仍可补交"
                elif remaining_hours <= 1:
                    deadline_detail = f"⚠️⚠️ 今天 {end_time_cn.strftime('%H:%M')} 截止（仅剩 {int(remaining_hours * 60)} 分钟！）"
                    urgency = "critical"
                    suggestion = "立即完成并提交"
                elif remaining_hours <= 6:
                    deadline_detail = f"⚠️ 今天 {end_time_cn.strftime('%H:%M')} 截止（剩 {int(remaining_hours)} 小时）"
                    urgency = "critical"
                    suggestion = "今天内务必完成"
                elif remaining.days == 0:
                    deadline_detail = f"今天 {end_time_cn.strftime('%H:%M')} 截止"
                    urgency = "today"
                    suggestion = "今天完成提交"
                elif remaining.days == 1:
                    deadline_detail = (
                        f"明天 {end_time_cn.strftime('%H:%M')} 截止（⚠️ 仅剩 1 天）"
                    )
                    urgency = "high"
                    suggestion = "明天截止，今天开始准备"
                elif remaining.days <= 3:
                    deadline_detail = (
                        f"距截止 {remaining.days} 天（{end_time_cn.strftime('%m月%d日 %H:%M')}）"
                    )
                    urgency = "high"
                    suggestion = "近 3 天内截止，优先处理"
                elif remaining.days <= 7:
                    deadline_detail = (
                        f"距截止 {remaining.days} 天（{end_time_cn.strftime('%m月%d日')}）"
                    )
                    urgency = "medium"
                    suggestion = "合理安排时间，本周完成"
                elif remaining.days <= 30:
                    deadline_detail = (
                        f"距截止 {remaining.days} 天（{end_time_cn.strftime('%m月%d日')}）"
                    )
                    urgency = "low"
                    suggestion = "远期任务，但建议提前规划"
                else:
                    deadline_detail = (
                        f"距截止 {remaining.days} 天（{end_time_cn.strftime('%m月%d日')}）"
                    )
                    urgency = "info"
                    suggestion = "长期任务，可在日历中标记"
            except (ValueError, TypeError):
                deadline_detail = f"截止: {end_time_str}"

        type_map = {
            "homework": "作业", "exam": "考试",
            "discussion": "讨论", "notice": "通知",
        }
        type_cn = type_map.get(todo_type, todo_type)
        locked_tag = " [已锁定]" if is_locked else ""

        # 构建丰富的 raw_content
        content_parts = [
            f"课程: {course_name}",
            f"课号: {course_code}" if course_code else "",
            f"类型: {type_cn}{locked_tag}",
            f"状态: {deadline_detail}",
            f"紧急度: {urgency}",
            f"行动建议: {suggestion}" if suggestion else "",
        ]

        return RawItem(
            source_name=f"学在浙大 - {course_name}",
            source_level="L4",
            url=f"https://courses.zju.edu.cn/api/todos#todo-{todo.get('id', 'unknown')}",
            title=f"[{type_cn}] {title}",
            raw_content="\n".join(p for p in content_parts if p),
            publish_time=end_time,
            fetched_at=now_cn,
        )

    # ══════════════════════════════════════════════════════════════
    #  教务系统 — 总控
    # ══════════════════════════════════════════════════════════════

    async def _collect_zdbk(self) -> List[RawItem]:
        """教务系统全量采集：通知 + 考试 + 成绩 + 对外交流。"""
        if not self._zdbk_session:
            logger.info("登录教务系统...")
            self._zdbk_session = self._zjuam.sso_to_subsystem(ZDBK_SERVICE_URL)
            if self._zdbk_session is None:
                logger.error("教务系统 SSO 失败")
                return []
            logger.info("教务系统登录成功")

        items: List[RawItem] = []
        items.extend(await self._collect_zdbk_notices())
        items.extend(await self._collect_zdbk_exams())
        items.extend(await self._collect_zdbk_grades())
        items.extend(await self._collect_zdbk_exchange())
        return items

    # ══════════════════════════════════════════════════════════════
    #  通知公告（精准关键词过滤）
    # ══════════════════════════════════════════════════════════════

    async def _collect_zdbk_notices(self) -> List[RawItem]:
        """采集教务通知公告，精准匹配用户关注的关键词，时间范围扩大至 90 天。"""
        logger.info("采集教务系统通知公告（精准关键词）...")
        timeout = max(self.crawl_config.get("timeout_seconds", 30) * 2, 60)

        try:
            resp = self._zdbk_session.post(
                ZDBK_NOTICE_URL, headers=ZDBK_HEADERS, timeout=timeout,
            )
            if resp.status_code != 200:
                logger.error(f"通知公告 API HTTP {resp.status_code}")
                return []
            data = resp.json()
            notice_list = data.get("items", [])
        except requests.exceptions.Timeout:
            logger.warning("通知公告 API 超时")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"通知公告 API 网络错误: {e}")
            return []
        except ValueError as e:
            logger.error(f"通知公告 API 非 JSON: {e}")
            return []

        if not notice_list:
            logger.info("教务系统：无通知公告")
            return []

        items: List[RawItem] = []
        max_items = self.source.max_items or 50
        now_cn = datetime.now(CN_TZ)
        cutoff_date = now_cn - timedelta(days=90)  # 扩大到 90 天

        # 第一轮：按关键词匹配打分
        scored_notices = []
        for notice in notice_list:
            title = notice.get("xwbt", "")
            if not title:
                continue

            # 计算关键词匹配分数
            score = 0
            matched_kws = []
            for kw in PRIORITY_KEYWORDS:
                if kw in title:
                    score += 1
                    matched_kws.append(kw)

            # 置顶加分
            is_pinned = notice.get("sfzd", "0") == "1"
            if is_pinned:
                score += 10

            # 日期：越新越加分
            pub_date_str = notice.get("fbsj", "")
            try:
                pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d")
                pub_date = pub_date.replace(tzinfo=CN_TZ)
                days_ago = (now_cn - pub_date).days
                if days_ago <= 3:
                    score += 5
                elif days_ago <= 7:
                    score += 3
                elif days_ago <= 30:
                    score += 1
            except (ValueError, TypeError):
                pub_date = None
                days_ago = 999

            # 跳过来自太久远的（除非匹配了关键词或置顶）
            if score == 0 and pub_date and pub_date < cutoff_date:
                continue

            scored_notices.append((score, matched_kws, is_pinned, pub_date, pub_date_str, notice))

        # 按分数降序排列
        scored_notices.sort(key=lambda x: (-x[0], x[3] if x[3] else datetime.min.replace(tzinfo=CN_TZ)))

        for score, matched_kws, is_pinned, pub_date, pub_date_str, notice in scored_notices[:max_items]:
            title = notice.get("xwbt", "")
            notice_id = notice.get("xwbh", "")
            publisher = notice.get("fbr", "")
            category = notice.get("gglb", "")

            # 构建详情 URL
            detail_url = (
                f"{ZDBK_NOTICE_DETAIL}&xwbh={notice_id}" if notice_id else ""
            )

            # 分类标签
            matched_cats = []
            for cat, kws in NOTICE_CATEGORIES.items():
                if any(kw in title for kw in kws):
                    matched_cats.append(cat)

            pinned_tag = " [置顶]" if is_pinned else ""
            cat_tag = f" [{', '.join(matched_cats)}]" if matched_cats else ""
            kw_tag = f" 🔑{','.join(matched_kws[:5])}" if matched_kws else ""

            content_parts = [
                f"发布单位: {publisher}",
                f"发布日期: {pub_date_str}",
                f"类别: {category}",
                f"匹配关键词: {', '.join(matched_kws)}" if matched_kws else "",
                f"相关标签: {', '.join(matched_cats)}" if matched_cats else "",
                f"匹配分数: {score}",
            ]

            items.append(RawItem(
                source_name="教务系统通知",
                source_level="L4",
                url=detail_url,
                title=f"{title}{pinned_tag}{cat_tag}{kw_tag}",
                raw_content="\n".join(p for p in content_parts if p),
                publish_time=pub_date or now_cn,
                fetched_at=now_cn,
            ))

        matched_count = sum(1 for i in items if "匹配关键词" in (i.raw_content or ""))
        pinned_count = sum(1 for i in items if "置顶" in (i.title or ""))
        recent_count = sum(
            1 for i in items
            if i.publish_time and (now_cn - i.publish_time).days <= 7
        )

        logger.info(
            f"教务通知: {len(items)} 条 "
            f"（关键词匹配 {matched_count}，置顶 {pinned_count}，近7天 {recent_count}）"
        )
        return items

    # ══════════════════════════════════════════════════════════════
    #  考试安排（扩大到 60 天）
    # ══════════════════════════════════════════════════════════════

    async def _collect_zdbk_exams(self) -> List[RawItem]:
        """采集考试安排，扩大时间范围至 60 天，分紧急度。"""
        logger.info("采集教务系统考试安排（60 天范围）...")
        timeout = max(self.crawl_config.get("timeout_seconds", 30) * 2, 60)

        try:
            resp = self._zdbk_session.post(
                ZDBK_EXAMS_URL, headers=ZDBK_HEADERS, timeout=timeout,
            )
            if resp.status_code != 200:
                logger.error(f"考试 API HTTP {resp.status_code}")
                return []
            data = resp.json()
            exam_list = data.get("items", [])
        except requests.exceptions.Timeout:
            logger.warning("考试 API 超时")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"考试 API 网络错误: {e}")
            return []
        except ValueError:
            logger.error("考试 API 非 JSON")
            return []

        if not exam_list:
            logger.info("教务系统：无考试安排")
            return []

        now_cn = datetime.now(CN_TZ)
        cutoff_past = now_cn - timedelta(days=7)
        cutoff_future = now_cn + timedelta(days=60)
        items: List[RawItem] = []

        for exam in exam_list:
            course_name = exam.get("kcmc", "")
            time_str = exam.get("kssj", "")
            location = exam.get("jsmc", "")
            seat = exam.get("zwxh", "")

            if not course_name:
                continue

            exam_dt = self._parse_exam_time(time_str)
            if exam_dt is None:
                continue

            # 时区修正
            if exam_dt.tzinfo is None:
                exam_dt = exam_dt.replace(tzinfo=CN_TZ)

            days_left = (exam_dt - now_cn).days

            # 过滤：跳过太久远的过去（>7天前）和太遥远的未来（>60天）
            if days_left < -7 or days_left > 60:
                continue

            # 紧急度评级
            if days_left < 0:
                urgency = "past"
                urgency_label = "已结束"
                suggestion = ""
            elif days_left == 0:
                urgency = "critical"
                urgency_label = "⚠️⚠️ 今天考试！"
                suggestion = "确认考场位置和座位号，带好证件和文具"
            elif days_left <= 3:
                urgency = "high"
                urgency_label = f"⚠️ 距考试仅 {days_left} 天"
                suggestion = "重点复习，整理笔记和错题"
            elif days_left <= 7:
                urgency = "medium"
                urgency_label = f"距考试 {days_left} 天"
                suggestion = "按计划复习，查漏补缺"
            elif days_left <= 14:
                urgency = "low"
                urgency_label = f"距考试 {days_left} 天（2周内）"
                suggestion = "开始系统复习"
            elif days_left <= 30:
                urgency = "info"
                urgency_label = f"距考试 {days_left} 天（1月内）"
                suggestion = "可以开始做复习计划"
            else:
                urgency = "info"
                urgency_label = f"距考试 {days_left} 天"
                suggestion = "远期考试，建议关注授课进度"

            time_display = exam_dt.strftime("%m月%d日 %H:%M")

            content_parts = [
                f"考试时间: {time_display}（{urgency_label}）",
                f"地点: {location}" if location else "",
                f"座位号: {seat}" if seat else "",
                f"紧急度: {urgency}",
                f"建议: {suggestion}" if suggestion else "",
            ]

            exam_code = exam.get("xkkh", "").replace(" ", "_")[:50]
            exam_url = f"https://zdbk.zju.edu.cn/jwglxt/xskscx/#exam-{exam_code}-{exam_dt.strftime('%Y%m%d')}"
            items.append(RawItem(
                source_name="教务系统 - 考试安排",
                source_level="L4",
                url=exam_url,
                title=f"[考试·{urgency_label}] {course_name}",
                raw_content="\n".join(p for p in content_parts if p),
                publish_time=exam_dt,
                fetched_at=now_cn,
            ))

        items.sort(key=lambda x: x.publish_time if x.publish_time else datetime.max.replace(tzinfo=CN_TZ))

        upcoming = sum(1 for i in items if i.publish_time and i.publish_time > now_cn)
        urgent = sum(1 for i in items if "critical" in (i.raw_content or "") or "high" in (i.raw_content or ""))

        logger.info(
            f"教务考试: {len(items)} 条（即将到来 {upcoming}，紧急 {urgent}）"
        )
        return items

    # ══════════════════════════════════════════════════════════════
    #  成绩变化检测
    # ══════════════════════════════════════════════════════════════

    async def _collect_zdbk_grades(self) -> List[RawItem]:
        """成绩变化检测（首次建立基线，后续仅报新成绩）。"""
        logger.info("采集教务系统成绩（变化检测）...")
        timeout = max(self.crawl_config.get("timeout_seconds", 30) * 2, 60)

        try:
            resp = self._zdbk_session.post(
                ZDBK_GRADES_URL, headers=ZDBK_HEADERS, timeout=timeout,
            )
            if resp.status_code != 200:
                logger.error(f"成绩 API HTTP {resp.status_code}")
                return []
            data = resp.json()
            grade_list = data.get("items", [])
        except requests.exceptions.Timeout:
            logger.warning("成绩 API 超时")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"成绩 API 网络错误: {e}")
            return []
        except ValueError:
            logger.error("成绩 API 非 JSON")
            return []

        if not grade_list:
            return []

        grade_summary = sorted(
            [
                (g.get("xkkh", ""), g.get("kcmc", ""), g.get("cj", ""), g.get("jd", ""), g.get("xf", ""))
                for g in grade_list
            ],
            key=lambda x: x[0],
        )
        current_hash = hashlib.md5(
            json.dumps(grade_summary, ensure_ascii=False).encode()
        ).hexdigest()

        cache_file = GRADES_CACHE_DIR / "grades_hash.txt"
        previous_hash = ""
        if cache_file.exists():
            try:
                previous_hash = cache_file.read_text().strip()
            except OSError:
                pass

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(current_hash)
        except OSError as e:
            logger.warning(f"成绩哈希缓存写入失败: {e}")

        if current_hash == previous_hash:
            logger.info("成绩无变化")
            return []

        if not previous_hash:
            logger.info(f"成绩基线已建立（{len(grade_list)} 门课），后续有新成绩时会报告")
            return []

        logger.info("检测到成绩变化！")
        now_cn = datetime.now(CN_TZ)

        prev_grades: Dict[str, tuple] = {}
        snapshot_file = GRADES_CACHE_DIR / "grades_snapshot.json"
        if snapshot_file.exists():
            try:
                prev_data = json.loads(snapshot_file.read_text())
                prev_grades = {item[0]: tuple(item) for item in prev_data}
            except (json.JSONDecodeError, OSError):
                pass

        try:
            snapshot_file.write_text(json.dumps(grade_summary, ensure_ascii=False))
        except OSError:
            pass

        items = []
        for code, name, score, gp, credit in grade_summary:
            prev = prev_grades.get(code)
            if prev is None:
                items.append(RawItem(
                    source_name="教务系统 - 新成绩",
                    source_level="L4",
                    url=f"https://zdbk.zju.edu.cn/jwglxt/cxdy/#grade-{code}",
                    title=f"[新成绩] {name}",
                    raw_content=(
                        f"课程: {name}\n成绩: {score}\n绩点: {gp}\n学分: {credit}"
                    ),
                    publish_time=now_cn,
                    fetched_at=now_cn,
                ))
            elif prev[2] != score:
                items.append(RawItem(
                    source_name="教务系统 - 成绩变更",
                    source_level="L4",
                    url=f"https://zdbk.zju.edu.cn/jwglxt/cxdy/#grade-change-{code}",
                    title=f"[成绩变更] {name}",
                    raw_content=(
                        f"课程: {name}\n旧成绩: {prev[2]} → 新成绩: {score}\n绩点: {gp}"
                    ),
                    publish_time=now_cn,
                    fetched_at=now_cn,
                ))

        logger.info(f"成绩变化: {len(items)} 条新/变更")
        return items

    # ══════════════════════════════════════════════════════════════
    #  对外交流
    # ══════════════════════════════════════════════════════════════

    async def _collect_zdbk_exchange(self) -> List[RawItem]:
        """
        采集对外交流申请页面。

        查询是否有进行中的交流项目申请、可报名项目等。
        当前无数据时返回空（季节性问题），有数据时按名额/报名状态报告。
        """
        logger.info("采集对外交流信息...")
        timeout = max(self.crawl_config.get("timeout_seconds", 30) * 2, 60)
        username = self.auth_config.get("username", "")

        exchange_url = f"{ZDBK_EXCHANGE_URL}&su={username}&doType=query&queryModel.showCount=50"

        try:
            resp = self._zdbk_session.post(
                exchange_url, headers=ZDBK_HEADERS, timeout=timeout,
            )
            if resp.status_code != 200:
                logger.error(f"对外交流 API HTTP {resp.status_code}")
                return []
            data = resp.json()
            exchange_list = data.get("items", [])
        except requests.exceptions.Timeout:
            logger.warning("对外交流 API 超时")
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"对外交流 API 网络错误: {e}")
            return []
        except ValueError:
            logger.warning("对外交流 API 返回非 JSON（可能没有申请记录）")
            return []

        if not exchange_list:
            logger.info("对外交流：当前无申请记录或可报名项目")
            return []

        # 有数据：解析交流项目信息
        logger.info(f"对外交流: {len(exchange_list)} 条记录")
        now_cn = datetime.now(CN_TZ)
        items = []

        for ex in exchange_list[:20]:
            # 尝试解析常见字段（字段名可能因系统版本而异）
            prog_name = (
                ex.get("jlmc", "") or ex.get("xmmc", "") or
                ex.get("jhlxmc", "") or ""
            )
            school = ex.get("xxmc", "") or ex.get("dwmc", "") or ""
            country = ex.get("gb", "") or ex.get("gj", "") or ""
            status = ex.get("zt", "") or ex.get("shzt", "") or ""
            quota = ex.get("rs", "") or ex.get("zs", "") or ""
            apply_start = ex.get("sqksrq", "") or ex.get("kssj", "") or ""
            apply_end = ex.get("sqjsrq", "") or ex.get("jssj", "") or ""

            if not prog_name:
                continue

            content_parts = [
                f"项目名称: {prog_name}",
                f"学校/单位: {school}" if school else "",
                f"国家/地区: {country}" if country else "",
                f"名额: {quota}" if quota else "",
                f"报名时间: {apply_start} ~ {apply_end}" if apply_start else "",
                f"状态: {status}" if status else "",
            ]

            items.append(RawItem(
                source_name="教务系统 - 对外交流",
                source_level="L4",
                url=(
                    f"{ZDBK_EXCHANGE_URL}&su={username}" if username else ""
                ),
                title=f"[交流项目] {prog_name}",
                raw_content="\n".join(p for p in content_parts if p),
                publish_time=now_cn,
                fetched_at=now_cn,
            ))

        logger.info(f"对外交流: 解析出 {len(items)} 个项目")
        return items

    # ══════════════════════════════════════════════════════════════
    #  辅助方法
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _parse_exam_time(time_str: str) -> Optional[datetime]:
        """解析考试时间: "2025年08月23日(14:00-16:40)" """
        if not time_str:
            return None
        m = re.match(
            r"(\d{4})年(\d{1,2})月(\d{1,2})日\((\d{1,2}):(\d{2})-",
            time_str,
        )
        if not m:
            return None
        try:
            return datetime(
                year=int(m.group(1)), month=int(m.group(2)),
                day=int(m.group(3)), hour=int(m.group(4)),
                minute=int(m.group(5)), tzinfo=CN_TZ,
            )
        except ValueError:
            return None
