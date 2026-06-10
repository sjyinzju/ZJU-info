#!/usr/bin/env python3
"""
URL 诊断工具 — 帮助你找到正确的 CSS 选择器

用法:
    python diagnose_url.py <url>                    诊断一个 URL
    python diagnose_url.py --source <name>           诊断 config.yaml 中的某个源
    python diagnose_url.py --all-html                诊断所有启用的 HTML 源

输出:
    1. 页面中所有有意义的链接及其父元素结构
    2. 推荐的 CSS 选择器
    3. 完整 HTML 保存到 info/diagnose/
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DIAG_DIR = PROJECT_ROOT / "info" / "diagnose"


def fetch_url(url: str) -> str:
    """获取 URL 的 HTML"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    print(f"\n  正在获取: {url}")
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    print(f"  HTTP {resp.status_code}, {len(resp.text)} 字节")
    return resp.text


def analyze_page(html: str, url: str, name: str = ""):
    """分析页面结构，输出诊断报告"""
    soup = BeautifulSoup(html, "lxml")

    # 保存完整 HTML
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or url))
    body = soup.body
    snippet = str(body)[:100000] if body else html[:100000]
    html_path = DIAG_DIR / f"{safe_name}.html"
    html_path.write_text(snippet, encoding="utf-8")
    print(f"\n  [诊断] 完整 HTML: {html_path}")

    # ── 分析页面结构 ──
    print(f"\n  {'='*56}")
    print(f"  页面结构分析: {name or url}")
    print(f"  {'='*56}")

    # 页面标题
    title_tag = soup.find("title")
    print(f"  页面标题: {title_tag.get_text(strip=True) if title_tag else '(无)'}")

    # 统计各种容器
    containers = {}
    for tag in ["ul", "ol", "div", "section", "article", "table"]:
        containers[tag] = len(soup.find_all(tag))

    print(f"\n  元素统计:")
    print(f"    <ul>: {containers['ul']}  |  <ol>: {containers['ol']}")
    print(f"    <div>: {containers['div']}  |  <section>: {containers['section']}")
    print(f"    <article>: {containers['article']}  |  <table>: {containers['table']}")
    print(f"    <a>: {len(soup.find_all('a'))} 个链接")

    # ── 寻找最可能的新闻列表容器 ──
    print(f"\n  {'─'*56}")
    print(f"  寻找新闻列表容器...")
    print(f"  {'─'*56}")

    candidates = []

    # 策略1: 包含最多 <a> 且文本 >5字 的元素
    for selector in [
        "ul", "ol", "div.list", "div.news", "div.content", "div.main",
        "div[class*=list]", "div[class*=news]", "div[class*=article]",
        "div[class*=content]", "div[class*=main]",
    ]:
        for container in soup.select(selector):
            links = container.select("a")
            meaningful = [a for a in links if len(a.get_text(strip=True)) >= 5]
            if len(meaningful) >= 3:
                # 计算选择器
                tag = container.name
                cls = container.get("class", [])
                cid = container.get("id", "")
                if cid:
                    css = f"#{cid}"
                elif cls:
                    css = f"{tag}.{'.'.join(cls)}"
                else:
                    css = tag

                candidates.append((len(meaningful), css, container, meaningful))

    # 按链接数量排序
    candidates.sort(key=lambda x: -x[0])

    if candidates:
        print(f"\n  找到 {len(candidates)} 个候选容器:\n")
        for i, (count, css, container, links) in enumerate(candidates[:8]):
            print(f"  [{i+1}] {count:>3} 条 | 选择器: {css}")
            # 显示前3个链接作为例子
            for j, a in enumerate(links[:3]):
                text = a.get_text(strip=True)[:60]
                href = a.get("href", "")[:70]
                print(f"        {text}")
                print(f"        → {href}")
            if len(links) > 3:
                print(f"        ... 还有 {len(links)-3} 条")
            print()
    else:
        print(f"\n  [WARNING] 未找到任何包含3条以上链接的容器！")
        print(f"  这个页面可能需要 JavaScript 渲染（应改用 L1 Playwright 采集）")

    # ── 推荐操作 ──
    print(f"  {'─'*56}")
    print(f"  推荐操作:")
    if candidates and candidates[0][0] >= 5:
        best = candidates[0]
        print(f"    在 config.yaml 中设置:")
        print(f"      selector: \"{best[1]} li\"")
        print(f"    预计可获取约 {best[0]} 条")
    else:
        print(f"    1. 打开 info/diagnose/{safe_name}.html 查看页面源码")
        print(f"    2. 找到新闻列表所在的容器元素")
        print(f"    3. 把 CSS 选择器填入 config.yaml 的 selector 字段")
        print(f"    4. 如果页面是 JS 渲染的，改为 L1 Playwright 源")

    print()


def diagnose_source(source_config):
    """诊断 config.yaml 中配置的单个源"""
    name = source_config.name
    url = source_config.url
    selector = source_config.selector

    try:
        html = fetch_url(url)
    except Exception as e:
        print(f"  [ERROR] 无法获取页面: {e}")
        return

    analyze_page(html, url, name)

    # 测试当前选择器
    if selector:
        soup = BeautifulSoup(html, "lxml")
        elements = soup.select(selector)
        print(f"  [当前选择器] '{selector}' → 匹配 {len(elements)} 个元素")
        if elements:
            print(f"  前3条:")
            for el in elements[:3]:
                a = el.find("a") if el.name != "a" else el
                if not a:
                    a = el.select_one("a")
                if a:
                    print(f"    - {a.get_text(strip=True)[:60]}")
        else:
            print(f"  [WARNING] 当前选择器匹配 0 条，需要修正！")
        print()


def main():
    parser = argparse.ArgumentParser(description="URL 诊断工具 — 找到正确的 CSS 选择器")
    parser.add_argument("url", nargs="?", help="要诊断的 URL")
    parser.add_argument("--source", "-s", help="诊断 config.yaml 中指定名称的源")
    parser.add_argument("--all-html", action="store_true", help="诊断所有启用的 HTML 源")
    parser.add_argument("--list", action="store_true", help="列出所有 HTML 源")
    args = parser.parse_args()

    if args.list:
        config = load_config()
        print("\n所有 L0 HTML 源:")
        for src in config.sources.l0_html:
            status = "[ON]" if src.enabled else "[OFF]"
            print(f"  {status} {src.name}")
            print(f"       URL: {src.url}")
            print(f"       selector: {src.selector}")
        return

    if args.all_html:
        config = load_config()
        sources = [s for s in config.sources.l0_html if s.enabled]
        if not sources:
            print("没有启用的 HTML 源。在 config.yaml 中设置 enabled: true")
            return
        print(f"\n诊断 {len(sources)} 个启用的 HTML 源...")
        for src in sources:
            diagnose_source(src)
        return

    if args.source:
        config = load_config()
        # 在所有层中搜索
        all_sources = (
            config.sources.l0_html + config.sources.l0_api +
            config.sources.l1_playwright + config.sources.l2_wechat +
            config.sources.l3_api + config.sources.l4_zjuam +
            config.sources.l5_internal
        )
        for src in all_sources:
            if args.source.lower() in src.name.lower():
                diagnose_source(src)
                return
        print(f"未找到名称包含 '{args.source}' 的源")
        return

    if args.url:
        try:
            html = fetch_url(args.url)
        except Exception as e:
            print(f"[ERROR] 无法获取页面: {e}")
            sys.exit(1)
        analyze_page(html, args.url)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
