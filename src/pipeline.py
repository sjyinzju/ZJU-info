"""主流水线编排 — 串联采集→去重→清洗→过滤→LLM→渲染"""
import asyncio
import random
import traceback
from datetime import datetime
from typing import List
from loguru import logger

from src.config_loader import AppConfig
from src.models.raw_item import RawItem
from src.models.daily_report import DailyReport, ReportStats
from src.collectors.l0_rss import L0RssCollector
from src.collectors.l0_html import L0HtmlCollector
from src.collectors.l1_playwright import L1PlaywrightCollector
from src.collectors.l4_zjuam import L4ZjuAmCollector
from src.processor.deduplicator import deduplicate
from src.processor.html_cleaner import clean_items as clean_html_items
from src.processor.keyword_filter import pre_filter, merge_keywords
from src.processor.llm_processor import run_llm_pipeline
from src.renderer.markdown_renderer import render_report, save_report


# 采集器工厂
COLLECTOR_MAP = {
    "l0_rss": L0RssCollector,
    "l0_html": L0HtmlCollector,
    "l1_playwright": L1PlaywrightCollector,
    "l4_zjuam": L4ZjuAmCollector,
    # l0_api, l2_wechat, l3_api, l5_internal → 后续 Phase
}


async def run_pipeline(config: AppConfig) -> DailyReport:
    """
    执行完整流水线，返回 DailyReport。

    单个源失败不会阻断全局。
    """
    crawl_cfg = config.crawl.model_dump()
    auth_cfg = config.auth.zjuam.model_dump()
    stats = ReportStats()
    all_items: List[RawItem] = []

    # ══════════════════════════════════════════════════════════
    #  Step 1: 并发采集
    # ══════════════════════════════════════════════════════════
    logger.info("=" * 50)
    logger.info("[流水线] Step 1: 并发采集")
    logger.info("=" * 50)

    enabled = config.sources.get_enabled()
    logger.info(f"[流水线] 共 {len(enabled)} 个启用的源")

    # 创建采集任务
    tasks = []
    for cat, src in enabled:
        collector_cls = COLLECTOR_MAP.get(cat)
        if collector_cls is None:
            logger.warning(f"[流水线] 跳过不支持的类型: {cat} ({src.name})")
            stats.sources_failed += 1
            continue
        # L4 采集器需要 auth 参数
        if cat == "l4_zjuam":
            collector = collector_cls(src, crawl_cfg, auth_config=auth_cfg)
        else:
            collector = collector_cls(src, crawl_cfg)
        tasks.append(_collect_with_error_handling(collector, src.name))

    # 并发执行（受 max_concurrent 限制）
    sem = asyncio.Semaphore(crawl_cfg.get("max_concurrent", 5))

    async def bounded(task):
        async with sem:
            return await task

    results = await asyncio.gather(*[bounded(t) for t in tasks], return_exceptions=True)

    for i, (cat, src) in enumerate(enabled):
        if COLLECTOR_MAP.get(cat) is None:
            continue  # 已计入 failed
        result = results[i] if i < len(results) else None
        if isinstance(result, Exception):
            logger.error(f"[流水线] {src.name} 采集失败: {result}")
            stats.sources_failed += 1
        elif isinstance(result, list):
            all_items.extend(result)
            stats.sources_succeeded += 1
            logger.info(f"[流水线] {src.name}: {len(result)} 条")

    stats.total_fetched = len(all_items)
    logger.info(f"[流水线] 采集完成: {stats.total_fetched} 条, "
                f"成功 {stats.sources_succeeded}, 失败 {stats.sources_failed}")

    # ══════════════════════════════════════════════════════════
    #  Step 2: 去重
    # ══════════════════════════════════════════════════════════
    logger.info("[流水线] Step 2: 去重")
    all_items = deduplicate(all_items)
    stats.after_dedup = len(all_items)

    if not all_items:
        logger.info("[流水线] 去重后无新内容")
        return DailyReport(
            date=datetime.now().strftime("%Y-%m-%d"),
            generated_at=datetime.now().isoformat(),
            items=[],
            editorial="今日无新内容。",
            top_actions=[],
            stats=stats,
        )

    # ══════════════════════════════════════════════════════════
    #  Step 3: 内容清洗
    # ══════════════════════════════════════════════════════════
    logger.info("[流水线] Step 3: 内容清洗")
    all_items = clean_html_items(all_items)

    # ══════════════════════════════════════════════════════════
    #  Step 4: LLM 处理（筛选 + 摘要一步完成）
    #  ⚠️ 关键词预过滤暂不启用 — user.interests 已注入 LLM system prompt
    #     Phase 2 将实现 per-source 关键词精确预过滤
    # ══════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════
    #  Step 5: LLM 处理
    # ══════════════════════════════════════════════════════════
    logger.info("[流水线] Step 4: LLM 筛选 & 摘要")
    report = run_llm_pipeline(all_items, config, stats)

    # ══════════════════════════════════════════════════════════
    #  Step 6: 渲染 & 保存
    # ══════════════════════════════════════════════════════════
    logger.info("[流水线] Step 5: 渲染 & 保存")
    md_content = render_report(report)
    filepath = save_report(
        md_content,
        report_dir=config.output.report_dir,
        name_format=config.output.report_name_format,
    )

    logger.info(f"[流水线] 完成! 日报: {filepath}")
    return report


async def _collect_with_error_handling(collector, source_name: str) -> List[RawItem]:
    """包装采集器调用，捕获异常"""
    try:
        # 随机延迟避免同时发起请求
        await asyncio.sleep(random.uniform(0.5, 2.0))
        return await collector.collect()
    except Exception as e:
        logger.error(f"[采集] {source_name} 失败: {e}\n{traceback.format_exc()}")
        raise  # 抛给 gather 处理
