#!/usr/bin/env python3
"""
手动触发脚本 — 单次运行完整流水线（开发 & 调试用）

用法:
    python run_once.py              # 使用默认配置运行一次
    python run_once.py --verbose    # 显示详细日志
    python run_once.py --dry-run    # 仅加载配置、列出启用的源，不实际采集

在 Phase 1 完成前，此脚本会正常加载配置并显示启用的信息源，
但流水线核心模块尚未实现时会给出明确提示。
"""

import os
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── 确保项目根目录在 sys.path ──────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── 加载 .env ──────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    env_file = PROJECT_ROOT / "config" / ".env"
    if env_file.exists():
        load_dotenv(env_file)
except ImportError:
    pass  # python-dotenv 未安装时忽略


# ══════════════════════════════════════════════════════════════
#  简易配置加载器（Phase 1.1 将迁移到 src/config_loader.py）
# ══════════════════════════════════════════════════════════════

def _resolve_env(value: str) -> str:
    """递归解析字符串中的 ${VAR} 和 ${VAR:-default} 占位符"""
    pattern = re.compile(r'\$\{(\w+)(?::-([^}]*))?\}')

    def _replace(match):
        var_name = match.group(1)
        default = match.group(2)
        return os.environ.get(var_name, default if default is not None else "")

    prev = None
    result = value
    while prev != result:
        prev = result
        result = pattern.sub(_replace, result)
    return result


def _resolve_dict(obj: Any) -> Any:
    """递归遍历 dict/list，解析所有字符串中的环境变量"""
    if isinstance(obj, dict):
        return {k: _resolve_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_dict(item) for item in obj]
    elif isinstance(obj, str):
        return _resolve_env(obj)
    return obj


def load_config(config_path: Optional[Path] = None) -> dict:
    """加载并解析配置文件"""
    import yaml

    if config_path is None:
        config_path = PROJECT_ROOT / "config" / "config.yaml"

    if not config_path.exists():
        sys.exit(f"[ERROR] 配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    config = _resolve_dict(raw)
    return config


# ══════════════════════════════════════════════════════════════
#  来源统计
# ══════════════════════════════════════════════════════════════

SOURCE_CATEGORIES = [
    "l0_rss", "l0_api", "l0_html",
    "l1_playwright",
    "l2_wechat",
    "l3_api",
    "l4_zjuam",
    "l5_internal",
]

CATEGORY_LABELS = {
    "l0_rss": "L0 RSS 订阅",
    "l0_api": "L0 公开 API",
    "l0_html": "L0 公开网页",
    "l1_playwright": "L1 动态渲染",
    "l2_wechat": "L2 微信公众号",
    "l3_api": "L3 第三方 API",
    "l4_zjuam": "L4 ZJU 统一认证",
    "l5_internal": "L5 校内网",
}


def count_sources(config: dict) -> Dict[str, Any]:
    """统计启用/禁用的信息源"""
    sources = config.get("sources", {})
    enabled_list = []
    disabled_list = []

    for cat in SOURCE_CATEGORIES:
        items = sources.get(cat, [])
        if items is None:
            items = []
        for item in items:
            entry = {**item, "category": cat}
            if item.get("enabled", False):
                enabled_list.append(entry)
            else:
                disabled_list.append(entry)

    return {
        "enabled": enabled_list,
        "disabled": disabled_list,
        "total": len(enabled_list) + len(disabled_list),
    }


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def ensure_output_dir(path_str: str) -> Path:
    """确保输出目录存在，不存在则创建"""
    p = Path(path_str)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
        print(f"[OK] 已创建输出目录: {p}")
    return p


def print_banner():
    print()
    print("=" * 62)
    print("   ZJU Info Agent — 校园信息自动采集")
    print("=" * 62)
    print(f"   时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()


def print_config_summary(config: dict):
    """打印当前配置摘要"""
    user = config.get("user", {})
    llm = config.get("llm", {})
    output = config.get("output", {})
    schedule = config.get("schedule", {})

    print("─" * 62)
    print("  [配置摘要]")
    print(f"  用户: {user.get('name', '未设置')} | {user.get('identity', '')}")
    print(f"  LLM:  {llm.get('provider')} / {llm.get('model')}")
    print(f"  输出: {output.get('report_dir', '???')}")
    print(f"  格式: {output.get('report_name_format', '{date}-{seq:02d}')}")
    print(f"  定时: {'开启' if schedule.get('enabled') else '关闭'} "
          f"| 时间: {schedule.get('times', [])}")
    print()


def print_sources(config: dict):
    """打印信息源列表"""
    stats = count_sources(config)
    enabled = stats["enabled"]
    disabled = stats["disabled"]

    print("─" * 62)
    print(f"  [信息源] 共 {stats['total']} 个 | 已启用 {len(enabled)} | 禁用 {len(disabled)}")
    print()

    if enabled:
        print("  [ON] 已启用的源:")
        for s in enabled:
            cat = CATEGORY_LABELS.get(s.get("category", ""), s.get("category", ""))
            kw = s.get("keywords", [])
            kw_str = f"关键词: {kw}" if kw else "全部信息"
            print(f"     [{cat}] {s['name']}")
            print(f"            {s.get('url', s.get('endpoint', '?'))}")
            print(f"            {kw_str}  |  最多 {s.get('max_items', '?')} 条")
        print()

    if disabled:
        print(f"  [OFF] 禁用的源 ({len(disabled)} 个):")
        for s in disabled:
            cat = CATEGORY_LABELS.get(s.get("category", ""), s.get("category", ""))
            print(f"     [{cat}] {s['name']}")
        print()


def print_phase_status():
    """打印各 Phase 实现状态"""
    print("─" * 62)
    print("  [实现状态]")
    phases = [
        ("Phase 0", "项目骨架 & 配置 & 数据模型", "[DONE]"),
        ("Phase 1", "最小流水线 (RSS→去重→LLM→日报)", "[TODO]"),
        ("Phase 2", "扩展公开源 (HTML/API 采集器)", "[TODO]"),
        ("Phase 3", "定时调度 & 推送通知", "[TODO]"),
        ("Phase 4", "动态页面 & 微信公众号", "[TODO]"),
        ("Phase 5", "ZJU 统一认证 & 内网源", "[TODO]"),
        ("Phase 6", "打磨 & 文档 & CLI", "[TODO]"),
    ]
    for tag, desc, status in phases:
        print(f"  {status}  {tag}: {desc}")
    print()


def run_pipeline(config: dict) -> bool:
    """
    执行主流水线（Phase 1 实现后会有实际逻辑）
    目前为骨架状态，展示将要执行的步骤。
    """
    output = config.get("output", {})
    report_dir = ensure_output_dir(output.get("report_dir", "E:/MyReports"))

    print("─" * 62)
    print("  [流水线执行]")
    print()

    # ── Step 1: 加载配置 ──
    print("  [1/6] 加载配置...", end=" ")
    try:
        # TODO: Phase 1.1 — Pydantic 校验
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    # ── Step 2: 采集 ──
    print("  [2/6] 并发采集信息源...", end=" ")
    try:
        # TODO: Phase 1.2 — 调用 collectors
        print("SKIP (采集器未实现)")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    # ── Step 3: 去重 ──
    print("  [3/6] 去重过滤...", end=" ")
    try:
        # TODO: Phase 1.3 — deduplicator
        print("SKIP (去重器未实现)")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    # ── Step 4: 清洗 ──
    print("  [4/6] 内容清洗...", end=" ")
    try:
        # TODO: Phase 1.4 — html_cleaner
        print("SKIP (清洗器未实现)")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    # ── Step 5: LLM 处理 ──
    print("  [5/6] LLM 筛选 & 摘要...", end=" ")
    try:
        # TODO: Phase 1.5 — llm_processor
        print("SKIP (LLM 处理器未实现)")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    # ── Step 6: 渲染输出 ──
    print("  [6/6] 生成日报...", end=" ")
    try:
        # TODO: Phase 1.6-1.7 — renderer + pipeline
        print("SKIP (渲染器未实现)")
    except Exception as e:
        print(f"FAILED: {e}")
        return False

    print()
    print(f"  日报目录: {report_dir}")
    print(f"  文件名格式: {output.get('report_name_format', '{date}-{seq:02d}')}.md")
    print()
    print("─" * 62)
    print("  [INFO] Phase 1 尚未实现，以上均为骨架流程。")
    print("  请继续执行 Phase 1 以启用实际采集和日报生成。")
    print("─" * 62)
    return True


# ══════════════════════════════════════════════════════════════
#  入口
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="ZJU Info Agent — 手动触发一次采集 & 日报生成"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅加载配置、列出启用的源，不实际采集"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="显示详细日志"
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="指定配置文件路径（默认 config/config.yaml）"
    )
    args = parser.parse_args()

    # 加载配置
    config_path = Path(args.config) if args.config else None
    try:
        config = load_config(config_path)
    except Exception as e:
        sys.exit(f"[ERROR] 配置加载失败: {e}")

    print_banner()
    print_config_summary(config)
    print_sources(config)

    if args.dry_run:
        print("─" * 62)
        print("  [dry-run 模式] 仅展示配置，不执行流水线。")
        print("─" * 62)
        return

    print_phase_status()

    # 运行流水线
    success = run_pipeline(config)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
