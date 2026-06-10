"""采集层输出模型 — 从各信息源抓取后的统一数据结构"""

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, HttpUrl, Field


SourceLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]


class RawItem(BaseModel):
    """采集器输出的原始条目，尚未经 LLM 处理"""

    source_name: str = Field(..., description="来源名称，如 'arXiv CS.AI'、'CC98 热门'")
    source_level: SourceLevel = Field(default="L0", description="信息源层级")

    url: str = Field(..., description="原文链接")
    title: str = Field(..., description="原始标题")
    raw_content: str = Field(default="", description="html2text 清洗后的纯文本内容")

    publish_time: Optional[datetime] = Field(
        default=None, description="原文发布时间（若能解析）"
    )
    fetched_at: datetime = Field(
        default_factory=datetime.now, description="采集时间"
    )

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
