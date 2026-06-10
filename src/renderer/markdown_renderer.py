"""Markdown 日报渲染器 — Jinja2 模板 + 序号自动递增"""
import re
from pathlib import Path
from datetime import datetime
from typing import Optional
from loguru import logger

from jinja2 import Environment, FileSystemLoader
from src.models.daily_report import DailyReport


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)), autoescape=False)


def render_report(report: DailyReport) -> str:
    """将 DailyReport 渲染为 Markdown 字符串"""
    template = _env.get_template("report.md.j2")
    return template.render(report=report)


def save_report(
    markdown_content: str,
    report_dir: str,
    name_format: str = "{date}-{seq:02d}",
) -> Path:
    """
    保存日报到指定目录，自动递增序号。

    例如 name_format="{date}-{seq:02d}" → 2026-6-10-01.md, 2026-6-10-02.md, ...
    """
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    date_str = f"{now.year}-{now.month}-{now.day}"  # 2026-6-10 (cross-platform)

    # 查找当天已有序号
    existing = list(output_dir.glob(f"{date_str}-*.md"))
    max_seq = 0
    pattern = re.compile(rf"{re.escape(date_str)}-(\d+)\.md$")
    for f in existing:
        m = pattern.search(f.name)
        if m:
            max_seq = max(max_seq, int(m.group(1)))

    seq = max_seq + 1
    filename = name_format.format(date=date_str, seq=seq)
    if not filename.endswith(".md"):
        filename += ".md"

    filepath = output_dir / filename
    filepath.write_text(markdown_content, encoding="utf-8")
    logger.info(f"[渲染] 日报已保存: {filepath}")
    return filepath
