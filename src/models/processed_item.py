"""LLM 处理后的条目模型 — Instructor 结构化输出的约束"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class ProcessedItem(BaseModel):
    """LLM 对单条信息处理后输出的结构化数据"""

    title: str = Field(..., description="信息标题（可改写，更清晰）")
    summary: str = Field(..., description="一句话摘要，≤50字")
    importance: Literal["高", "中", "低"] = Field(..., description="重要程度")
    category: Literal["科研", "竞赛", "出国", "就业", "校内通知", "其他"] = Field(
        ..., description="信息类别"
    )
    deadline: Optional[str] = Field(
        default=None, description="截止日期，YYYY-MM-DD 格式；无截止日期则为 null"
    )
    action_required: bool = Field(
        default=False, description="是否需要本人主动操作（报名、提交、联系等）"
    )
    action_hint: Optional[str] = Field(
        default=None, description="如需行动，给出一句话操作提示"
    )
    source_url: str = Field(..., description="原文链接")
    source_name: str = Field(default="", description="来源名称")
