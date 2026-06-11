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

    # CC98 论坛内容特殊提示（与其他来源共用 system prompt 时不冲突）
    parts.append(
        "## 关于 CC98 论坛来源\n"
        "CC98 是浙大校内论坛，信息来自学生真实分享，价值极高。\n"
        "筛选 CC98 帖子时，请放宽标准：即使标题未精确命中关键词，只要内容涉及以下主题就应保留：\n"
        "- 课程推荐/避坑、教师评价、给分情况\n"
        "- 实习/校招内推、面试经验、offer 比较\n"
        "- 实验室招募、导师评价、科研心得\n"
        "- 历年卷/复习资料分享、考试经验\n"
        "- 竞赛组队、项目招募、活动报名\n"
        "- 转专业/辅修经验、出国交流心得\n"
        "- 投资理财、编程技术等实用分享"
    )

    # 日报要求
    if prompts.summary:
        parts.append(f"## 日报格式要求\n{prompts.summary}")

    return "\n\n".join(parts)


def build_user_message(items: List[RawItem]) -> str:
    """将待筛选的 RawItem 列表格式化为 LLM 用户消息"""
    lines = ["以下是今日采集到的信息，请筛选、分类、摘要：", ""]

    for i, item in enumerate(items, 1):
        pub_str = item.publish_time.strftime("%Y-%m-%d") if item.publish_time else "未知日期"
        lines.append(f"### [{i}] {item.title}")
        lines.append(f"来源: {item.source_name} | 日期: {pub_str}")
        lines.append(f"链接: {item.url}")
        # 严格控制每条内容长度，避免超出 LLM 上下文窗口
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
