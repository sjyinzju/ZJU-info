"""日报整体结构模型"""

from typing import List
from pydantic import BaseModel, Field
from .processed_item import ProcessedItem


class ReportStats(BaseModel):
    """日报统计信息"""

    total_fetched: int = Field(default=0, description="本次采集到的原始条目总数")
    after_dedup: int = Field(default=0, description="去重后条目数")
    after_filter: int = Field(default=0, description="LLM 筛选后保留条目数")
    high_importance: int = Field(default=0, description="高重要度条目数")
    sources_succeeded: int = Field(default=0, description="成功采集的源数量")
    sources_failed: int = Field(default=0, description="失败的源数量")
    llm_tokens_used: int = Field(default=0, description="LLM 调用消耗 token 数")


class DailyReport(BaseModel):
    """日报整体结构"""

    date: str = Field(..., description="日报日期，YYYY-MM-DD 格式")
    generated_at: str = Field(..., description="生成时间，ISO 8601 格式")
    items: List[ProcessedItem] = Field(default_factory=list, description="筛选后的信息条目")
    editorial: str = Field(default="", description="编辑按：今日最值得关注的一件事")
    top_actions: List[str] = Field(default_factory=list, description="今日优先行动项（最多3条）")
    stats: ReportStats = Field(default_factory=ReportStats, description="统计信息")
