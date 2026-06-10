#!/usr/bin/env python3
"""
手动触发脚本 — 单次运行完整流水线

用法:
    python run_once.py              # 运行一次完整流水线
    python run_once.py --dry-run    # 仅加载配置、列出启用的源
    python run_once.py --verbose    # 显示 DEBUG 级别日志
"""

import sys
import asyncio
import argparse
from pathlib import Path
from datetime import datetime
from loguru import logger

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config, AppConfig


# ══════════════════════════════════════════════════════════════
#  显示函数
# ══════════════════════════════════════════════════════════════

CATEGORY_LABELS = {
    "l0_rss": "L0 RSS",
    "l0_api": "L0 API",
    "l0_html": "L0 网页",
    "l1_playwright": "L1 动态",
    "l2_wechat": "L2 微信",
    "l3_api": "L3 三方API",
    "l4_zjuam": "L4 认证",
    "l5_internal": "L5 内网",
}


def print_banner():
    print()
    print("=" * 62)
    print("   ZJU Info Agent — 校园信息自动采集")
    print("=" * 62)
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


def print_config_summary(config: AppConfig):
    print("-" * 62)
    print("  [配置]")
    print(f"  用户: {config.user.name} | {config.user.identity}")
    print(f"  LLM:  {config.llm.provider}/{config.llm.model}")
    print(f"  输出: {config.output.report_dir}")
    print(f"  文件名: {config.output.report_name_format}.md")
    print(f"  定时: {'ON' if config.schedule.enabled else 'OFF'} "
          f"| 时间: {config.schedule.times}")
    print()


def print_sources(config: AppConfig):
    enabled = config.sources.get_enabled()
    all_count = sum(
        len(getattr(config.sources, cat, []))
        for cat in CATEGORY_LABELS
    )
    print("-" * 62)
    print(f"  [信息源] 共 {all_count} 个 | 已启用 {len(enabled)}")
    print()

    if enabled:
        print("  [ON] 已启用:")
        for cat, src in enabled:
            cat_label = CATEGORY_LABELS.get(cat, cat)
            kw = src.keywords if src.keywords else "(全部)"
            print(f"     [{cat_label}] {src.name}")
            print(f"             {src.url}")
            print(f"             关键词: {kw}  |  最多 {src.max_items} 条")
        print()

    disabled_count = all_count - len(enabled)
    if disabled_count > 0:
        print(f"  [OFF] 禁用 {disabled_count} 个源（在 config.yaml 中设置 enabled: true 开启）")
        print()


def print_report_summary(report):
    """打印日报摘要到控制台"""
    stats = report.stats
    print()
    print("=" * 62)
    print("  日报生成完成")
    print("=" * 62)
    print(f"  日期: {report.date}")
    print(f"  采集: {stats.total_fetched} 条 | 去重后: {stats.after_dedup} | "
          f"LLM筛选: {stats.after_filter} 条")
    print(f"  高重要度: {stats.high_importance} 条")
    print(f"  源: {stats.sources_succeeded} 成功 / {stats.sources_failed} 失败")
    print(f"  编辑按: {report.editorial}")
    if report.top_actions:
        print(f"  优先行动项:")
        for i, action in enumerate(report.top_actions, 1):
            print(f"    {i}. {action}")
    print()


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════

async def main_async(config: AppConfig, dry_run: bool = False):
    """异步主流程"""
    print_banner()
    print_config_summary(config)
    print_sources(config)

    if dry_run:
        print("-" * 62)
        print("  [dry-run] 仅展示配置，不执行流水线。")
        print("-" * 62)
        return

    # 确保输出目录存在
    Path(config.output.report_dir).mkdir(parents=True, exist_ok=True)

    # 执行流水线
    from src.pipeline import run_pipeline

    print("-" * 62)
    print("  开始执行流水线...")
    print("-" * 62)
    print()

    try:
        report = await run_pipeline(config)
        print_report_summary(report)
    except Exception as e:
        logger.error(f"流水线执行失败: {e}")
        raise
    finally:
        # 确保 Playwright Browser 在 event loop 关闭前清理
        try:
            from src.collectors.l1_playwright import _shutdown_browser
            await _shutdown_browser()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="ZJU Info Agent — 手动触发")
    parser.add_argument("--dry-run", action="store_true", help="仅显示配置")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG 日志")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    args = parser.parse_args()

    # 日志级别
    logger.remove()
    if args.verbose:
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

    # 加载配置
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)

    # 运行
    asyncio.run(main_async(config, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
