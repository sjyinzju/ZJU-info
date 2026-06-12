#!/usr/bin/env python3
"""
全源测试脚本 — 启用所有信息源 + 跳过去重。

用法:  python test_all_sources.py
特点:
  - 自动启用 config.yaml 中所有信息源（不限层级）
  - 跳过 SQLite URL 去重（每次运行都是全新视角）
  - 保留"每源至少 3 条"的来源覆盖兜底机制
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger
from src.config_loader import load_config, AppConfig, SourceConfig
from src.models.raw_item import RawItem
from src.models.daily_report import DailyReport, ReportStats
from src.collectors.l0_rss import L0RssCollector
from src.collectors.l0_html import L0HtmlCollector
from src.collectors.l0_jina import L0JinaCollector
from src.collectors.l1_playwright import L1PlaywrightCollector
from src.collectors.l4_zjuam import L4ZjuAmCollector
from src.collectors.l5_cc98 import L5CC98Collector
from src.processor.html_cleaner import clean_items as clean_html_items
from src.processor.llm_processor import run_llm_pipeline
from src.renderer.markdown_renderer import render_report, save_report


COLLECTOR_MAP = {
    "l0_rss": L0RssCollector,
    "l0_html": L0HtmlCollector,
    "l0_jina": L0JinaCollector,
    "l1_playwright": L1PlaywrightCollector,
    "l4_zjuam": L4ZjuAmCollector,
    "l5_internal": L5CC98Collector,
}


def enable_all_sources(config: AppConfig):
    """将所有信息源设置为 enabled=True"""
    sources = config.sources
    count = 0
    for cat in [
        "l0_rss", "l0_html", "l0_jina",
        "l1_api", "l1_playwright",
        "l2_wechat", "l4_zjuam", "l5_internal",
    ]:
        items = getattr(sources, cat, [])
        for src in items:
            if not src.enabled:
                src.enabled = True
                count += 1
    logger.info(f"[全源测试] 额外启用了 {count} 个源")


def simple_dedup(items: list) -> list:
    """仅做标题相似度去重（跳过 URL 数据库去重）"""
    import difflib
    final = []
    for item in items:
        is_dup = False
        for seen in final:
            ratio = difflib.SequenceMatcher(None, item.title, seen.title).ratio()
            if ratio > 0.85:
                is_dup = True
                break
        if not is_dup:
            final.append(item)
    logger.info(f"[全源测试] 标题去重: {len(items)} → {len(final)} (去除 {len(items)-len(final)})")
    return final


async def run_full_test(config: AppConfig):
    """执行全源采集流水线（无 URL 去重）"""
    crawl_cfg = config.crawl.model_dump()
    auth_cfg = config.auth.zjuam.model_dump()
    stats = ReportStats()
    all_items: list = []

    # ── 采集 ──
    logger.info("=" * 50)
    logger.info("[全源测试] 并发采集（所有源）")
    logger.info("=" * 50)

    enabled = config.sources.get_enabled()
    logger.info(f"[全源测试] 共 {len(enabled)} 个源")

    import random

    async def collect_one(cat, src):
        await asyncio.sleep(random.uniform(0.5, 2.0))
        collector_cls = COLLECTOR_MAP.get(cat)
        if collector_cls is None:
            logger.warning(f"[全源测试] 跳过不支持的类型: {cat} ({src.name})")
            return (src.name, None)
        try:
            if cat == "l4_zjuam":
                c = collector_cls(src, crawl_cfg, auth_config=auth_cfg)
            elif cat == "l5_internal":
                c = collector_cls(
                    src, crawl_cfg,
                    cc98_username=config.auth.cc98.username,
                    cc98_password=config.auth.cc98.password,
                    cc98_token=config.auth.cc98.token,
                    cc98_token_backup=config.auth.cc98.token_out_of_campus,
                )
            else:
                c = collector_cls(src, crawl_cfg)
            items = await c.collect()
            return (src.name, items)
        except Exception as e:
            logger.error(f"[全源测试] {src.name} 采集异常: {e}")
            return (src.name, None)

    sem = asyncio.Semaphore(crawl_cfg.get("max_concurrent", 5))

    async def bounded(coro):
        async with sem:
            return await coro

    tasks = [bounded(collect_one(cat, src)) for cat, src in enabled]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            stats.sources_failed += 1
            continue
        name, items = result
        if items is None:
            stats.sources_failed += 1
            logger.warning(f"[全源测试] {name}: 失败")
        elif isinstance(items, list):
            all_items.extend(items)
            stats.sources_succeeded += 1
            logger.info(f"[全源测试] {name}: {len(items)} 条")

    stats.total_fetched = len(all_items)
    logger.info(
        f"[全源测试] 采集完成: {stats.total_fetched} 条, "
        f"成功 {stats.sources_succeeded}, 失败 {stats.sources_failed}"
    )

    if not all_items:
        logger.warning("[全源测试] 无数据，退出")
        return

    # ── 去重（仅标题相似度）──
    all_items = simple_dedup(all_items)
    stats.after_dedup = len(all_items)

    # ── 清洗 ──
    all_items = clean_html_items(all_items)

    # ── LLM ──
    report = run_llm_pipeline(all_items, config, stats)

    # ── 渲染 ──
    md_content = render_report(report)
    filepath = save_report(
        md_content,
        report_dir=config.output.report_dir,
        name_format="{date}-full-{seq:02d}",
    )
    logger.info(f"[全源测试] 日报: {filepath}")

    # ── 汇总 ──
    print()
    print("=" * 62)
    print("  全源测试完成")
    print("=" * 62)
    print(f"  采集: {stats.total_fetched} 条")
    print(f"  去重后: {stats.after_dedup} 条")
    print(f"  LLM 筛选: {stats.after_filter} 条 (高重要度 {stats.high_importance})")
    print(f"  源: {stats.sources_succeeded} 成功 / {stats.sources_failed} 失败")
    print(f"  日报: {filepath}")
    print("=" * 62)


async def main():
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

    config = load_config()
    enable_all_sources(config)

    # 确保输出目录
    Path(config.output.report_dir).mkdir(parents=True, exist_ok=True)

    await run_full_test(config)

    # 清理 Playwright
    try:
        from src.collectors.l1_playwright import _shutdown_browser
        await _shutdown_browser()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
