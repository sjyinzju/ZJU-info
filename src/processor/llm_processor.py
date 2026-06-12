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

    # 日报要求
    if prompts.summary:
        parts.append(f"## 日报格式要求\n{prompts.summary}")

    return "\n\n".join(parts)


def build_user_message(items: List[RawItem]) -> str:
    """将待筛选的 RawItem 列表格式化为 LLM 用户消息，CC98 帖自动标注优先级。"""
    lines = ["以下是今日采集到的信息，请筛选、分类、摘要：", ""]

    # CC98 高价值关键词（用于预标注，引导 LLM 保留）
    CC98_BOOST_KW = [
        "实习", "内推", "面经", "offer", "招募", "组队", "招人",
        "经验", "分享", "推荐", "避坑", "避雷", "评价", "导师",
        "历年卷", "复习", "资料", "考试", "课程",
        "项目", "竞赛", "报名", "比赛",
        "出国", "留学", "申请", "暑研", "交换",
        "转专业", "辅修", "微辅修",
    ]

    for i, item in enumerate(items, 1):
        pub_str = item.publish_time.strftime("%Y-%m-%d") if item.publish_time else "未知日期"

        # CC98 帖子自动标注优先级：
        title_prefix = ""
        if "CC98" in (item.source_name or ""):
            searchable = f"{item.title} {item.raw_content or ''}".lower()
            boost = any(kw in searchable for kw in CC98_BOOST_KW)
            if boost:
                title_prefix = "[CC98高价值] "
                item.title = f"{title_prefix}{item.title}"  # 就地修改，传给后续

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
