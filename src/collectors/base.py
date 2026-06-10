"""采集器抽象基类"""
from abc import ABC, abstractmethod
from typing import List
from src.models.raw_item import RawItem
from src.config_loader import SourceConfig


class BaseCollector(ABC):
    """所有采集器的抽象基类"""

    def __init__(self, source: SourceConfig, crawl_config: dict):
        self.source = source
        self.crawl_config = crawl_config

    @abstractmethod
    async def collect(self) -> List[RawItem]:
        """执行采集，返回 RawItem 列表。失败抛异常，由调用方处理。"""
        ...
