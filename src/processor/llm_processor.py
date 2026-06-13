"""LLM 处理器 — DeepSeek + Instructor 结构化筛选 & 摘要生成"""
import sys
from typing import List, Optional
from datetime import datetime

from loguru import logger
from openai import OpenAI
import instructor
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config_loader import AppConfig
from src.models.raw_item import RawItem
from src.models.processed_item import ProcessedItem
from src.models.daily_report import DailyReport, ReportStats


# ══════════════════════════════════════════════════════════════
#  LLM 输出的 Pydantic 模型
# ══════════════════════════════════════════════════════════════

from pydantic import BaseModel, Field


class LLMReportOutput(BaseModel):
    """LLM 直接输出的日报内容（代码会补充 stats 等字段）"""
    items: List[ProcessedItem] = Field(
        default_factory=list,
        description="筛选并摘要后的信息条目（仅保留相关的）"
    )
    editorial: str = Field(
        default="",
        description="编辑按：今日最值得关注的一件事，用一句话概括"
    )
    top_actions: List[str] = Field(
        default_factory=list,
        description="今日优先行动项，最多3条，按紧急程度排序"
    )


# ══════════════════════════════════════════════════════════════
#  Prompt 构建
# ══════════════════════════════════════════════════════════════

def build_system_prompt(config: AppConfig) -> str:
    """根据用户配置构建完整的 system prompt"""
    user = config.user
    prompts = config.prompts

    parts = []

    # 用户身份
    parts.append(f"## 用户身份\n{user.identity}")

    # 关注方向
    if user.interests:
        interests = "\n".join(f"- {i}" for i in user.interests)
        parts.append(f"## 关注方向\n{interests}")

    # 筛选规则
    if prompts.filter:
        parts.append(f"## 筛选规则\n{prompts.filter}")

    # 管理学院内网内容（与 CC98 同等重要）
    sommis_rule = (
        "## !! 关于管理学院内网来源（硬性保留指令）\n"
        "管理学院内网是学院官方通知渠道，来源为「管理学院内网」的条目必须保留 40%-50%。\n"
        "学院通知标题本身就是核心信息，不要因为内容简短就丢弃。\n"
        "涉及以下主题的管理学院条目**必须全部保留**：\n"
        "- 教学：毕业论文、选课、辅修、考试、成绩、Office Time\n"
        "- 学工：评奖评优、社会实践、支教、志愿者、选拔\n"
        "- 科研：SRTP、国创省创、飞鹰计划、课题申报\n"
        "- 职业：实习、招聘、参访、企业活动\n"
        "- 院总办：暑期安排、重要通知、公示\n"
        "- 系所：讲座、学术活动、论坛\n"
        "只有在明显是纯行政事务（如经费报销、设备维护等与学生无关的内容）时才可过滤。"
    )
    parts.append(sommis_rule)

    # CC98 论坛内容（强制保留指令）
    cc98_rule = (
        "## !! 关于 CC98 论坛来源（最高优先级筛选指令）\n"
        "CC98 是浙大校内论坛，所有帖子来自学生真实分享，是日报的核心信息来源。\n"
        "**必须遵守以下规则：**\n"
        "1. CC98 帖子（来源包含 CC98 的条目）至少保留 50%，除非明显是灌水/广告/纯表情\n"
        "2. 带有 [CC98高价值] 前缀的 CC98 帖子是系统标注的高价值内容，**必须全部保留**\n"
        "3. 即使标题不精确匹配你的筛选关键词，只要内容涉及课程、科研、实习、经验、资源等实用话题就应保留\n"
        '4. CC98 帖子被过滤掉的唯一合理理由是：纯水帖（如打卡签到）、纯广告、或与浙大学生完全无关的内容'
    )
    parts.append(cc98_rule)

    # 图书馆讲座展览（高优先级）
    lib_rule = (
        "## !! 关于浙大图书馆来源（高优先级保留指令）\n"
        "浙大图书馆的讲座、展览、培训活动是学校官方组织的优质学术文化活动。\n"
        "来源为「浙大图书馆」「浙大图书馆 讲座展览」的条目必须遵守：\n"
        "1. 至少保留 60% 的图书馆条目\n"
        "2. 涉及以下主题的**必须全部保留**：\n"
        "   - AI、数据科学、编程、数字人文等技术类讲座\n"
        "   - 数据库使用、文献检索、科研工具培训\n"
        "   - 论文写作、学术发表指导\n"
        "   - 软件工具教学（Python、R、LaTeX 等）\n"
        "   - 艺术展览、音乐活动、文化体验\n"
        "   - 阅读分享、读书会\n"
        '3. 标记为「开卷有益」「求真一小时」「书香浙大」的活动通常质量较高，优先保留\n'
        "4. 仅当活动已明确过期时才可过滤"
    )
    parts.append(lib_rule)

    # 来源覆盖要求
    parts.append(
        "## 来源覆盖要求\n"
        f"每个信息源（source_name）在最终日报中至少保留 {MIN_ITEMS_PER_SOURCE} 条信息。\n"
        "即使某条信息与用户关注方向的匹配度不是最高，只要它来自该来源且有一定价值，就应保留。\n"
        "不要因为某个来源的信息偏少或有其他更重要的信息就完全忽略该来源。"
    )

    # 日报要求
    if prompts.summary:
        parts.append(f"## 日报格式要求\n{prompts.summary}")

    return "\n\n".join(parts)


def build_user_message(items: List[RawItem]) -> str:
    """将待筛选的 RawItem 列表格式化为 LLM 用户消息。管理学院条目排在前面。"""
    lines = ["以下是今日采集到的信息，请筛选、分类、摘要：", ""]

    # 管理学院和 CC98 条目优先排列（引导 LLM 给予更多关注）
    priority_items = [it for it in items if "管理学院" in (it.source_name or "") or "CC98" in (it.source_name or "")]
    other_items = [it for it in items if it not in priority_items]
    sorted_items = priority_items + other_items

    # CC98 高价值关键词
    CC98_BOOST_KW = [
        "实习", "内推", "面经", "offer", "招募", "组队", "招人",
        "经验", "分享", "推荐", "避坑", "避雷", "评价", "导师",
        "历年卷", "复习", "资料", "考试", "课程",
        "项目", "竞赛", "报名", "比赛",
        "出国", "留学", "申请", "暑研", "交换",
        "转专业", "辅修", "微辅修",
    ]

    for i, item in enumerate(sorted_items, 1):
        pub_str = item.publish_time.strftime("%Y-%m-%d") if item.publish_time else "未知日期"

        # 管理学院 / CC98 帖子自动标注优先级：
        title_prefix = ""
        if "管理学院" in (item.source_name or ""):
            title_prefix = "[学院通知·必保留] "
            item.title = f"{title_prefix}{item.title}"
        elif "CC98" in (item.source_name or ""):
            searchable = f"{item.title} {item.raw_content or ''}".lower()
            boost = any(kw in searchable for kw in CC98_BOOST_KW)
            if boost:
                title_prefix = "[CC98高价值] "
                item.title = f"{title_prefix}{item.title}"

        lines.append(f"### [{i}] {item.title}")
        lines.append(f"来源: {item.source_name} | 日期: {pub_str}")
        lines.append(f"链接: {item.url}")
        content = (item.raw_content or "").strip()[:400]
        if len(item.raw_content or "") > 400:
            content += "…"
        lines.append(f"内容: {content}")
        lines.append("")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  LLM 调用
# ══════════════════════════════════════════════════════════════

def _create_client(config: AppConfig) -> instructor.Instructor:
    """创建 instructor 客户端"""
    llm = config.llm
    client = OpenAI(
        api_key=llm.api_key,
        base_url=llm.base_url,
    )
    return instructor.from_openai(client, mode=instructor.Mode.JSON)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    reraise=True,
)
def _call_llm(
    client: instructor.Instructor,
    model: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int,
    temperature: float,
) -> LLMReportOutput:
    """调用 LLM 并返回结构化输出"""
    logger.info(f"[LLM] 正在调用 {model}...")
    logger.debug(f"[LLM] System prompt 长度: {len(system_prompt)} chars")
    logger.debug(f"[LLM] User message 长度: {len(user_message)} chars")

    result = client.chat.completions.create(
        model=model,
        response_model=LLMReportOutput,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )

    logger.info(f"[LLM] 返回 {len(result.items)} 条摘要, {len(result.top_actions)} 条行动项")
    return result


# ══════════════════════════════════════════════════════════════
#  来源覆盖兜底
# ══════════════════════════════════════════════════════════════

MIN_ITEMS_PER_SOURCE = 3


def _ensure_source_coverage(
    result: LLMReportOutput,
    all_items: List[RawItem],
) -> LLMReportOutput:
    """确保每个信息源在日报中至少出现 MIN_ITEMS_PER_SOURCE 条"""
    # 统计 LLM 已选中的各来源条目数
    source_counts: dict = {}
    selected_urls = {item.source_url for item in result.items}
    for item in result.items:
        src = item.source_name or "(未知)"
        source_counts[src] = source_counts.get(src, 0) + 1

    # 找出不足的来源
    all_source_names = {it.source_name for it in all_items}
    underrepresented = []
    for src_name in all_source_names:
        if source_counts.get(src_name, 0) < MIN_ITEMS_PER_SOURCE:
            underrepresented.append(src_name)

    if not underrepresented:
        return result

    logger.info(
        f"[LLM] 来源覆盖兜底: {len(underrepresented)} 个源不足 {MIN_ITEMS_PER_SOURCE} 条，补充中..."
    )

    # 从原始条目中补充
    for src_name in underrepresented:
        need = MIN_ITEMS_PER_SOURCE - source_counts.get(src_name, 0)
        # 从 all_items 中找该来源但未被 LLM 选中的条目
        candidates = [
            it for it in all_items
            if it.source_name == src_name and it.url not in selected_urls
        ]
        added = 0
        for raw in candidates:
            if added >= need:
                break
            result.items.append(ProcessedItem(
                title=raw.title,
                summary=raw.raw_content[:80] if raw.raw_content else raw.title,
                importance="低",
                category="其他",
                deadline=None,
                action_required=False,
                action_hint=None,
                source_url=raw.url,
                source_name=raw.source_name,
            ))
            selected_urls.add(raw.url)
            added += 1
        if added > 0:
            logger.info(f"  [兜底] {src_name}: +{added} 条 → 共 {source_counts.get(src_name, 0) + added} 条")

    return result


# ══════════════════════════════════════════════════════════════
#  处理器入口
# ══════════════════════════════════════════════════════════════

def run_llm_pipeline(
    items: List[RawItem],
    config: AppConfig,
    stats: ReportStats,
) -> DailyReport:
    """
    主入口：将 RawItem 列表送入 LLM，返回完整的 DailyReport。
    如果没有需要处理的内容，返回空日报。
    """
    if not items:
        logger.info("[LLM] 无内容需要处理，生成空日报")
        return DailyReport(
            date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=datetime.now().isoformat(),
            items=[],
            editorial="今日无新内容需要处理。",
            top_actions=[],
            stats=stats,
        )

    # 构建 prompt
    system_prompt = build_system_prompt(config)
    user_message = build_user_message(items)

    # 粗略估算 token（中文 ~1.5 chars/token）
    estimated_tokens = len(user_message) // 2
    logger.info(f"[LLM] 待处理 {len(items)} 条, 估算 ~{estimated_tokens} tokens")

    # 调用 LLM
    try:
        client = _create_client(config)
        result = _call_llm(
            client=client,
            model=config.llm.model,
            system_prompt=system_prompt,
            user_message=user_message,
            max_tokens=config.llm.max_tokens,
            temperature=config.llm.temperature,
        )
    except Exception as e:
        # 只记录日志，不把原始异常信息写入日报 editorial
        import traceback
        logger.error(f"[LLM] 调用失败（已重试3次）: {e}")
        logger.debug(traceback.format_exc())
        return DailyReport(
            date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=datetime.now().isoformat(),
            items=[],
            editorial="LLM 处理暂时不可用，请稍后重试。原始数据已保存。",
            top_actions=[],
            stats=stats,
        )

    # ── 来源覆盖兜底：每个源至少保留 3 条 ──
    result = _ensure_source_coverage(result, items)

    # 更新统计
    stats.after_filter = len(result.items)
    stats.high_importance = sum(1 for i in result.items if i.importance == "高")

    return DailyReport(
        date=datetime.now().strftime("%Y-%m-%d"),
        generated_at=datetime.now().isoformat(),
        items=result.items,
        editorial=result.editorial,
        top_actions=result.top_actions[:3],  # 最多3条
        stats=stats,
    )
