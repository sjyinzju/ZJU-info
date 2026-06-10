"""Playwright 冒烟测试 — 验证 headless Chromium 可正常启动"""
import asyncio
from playwright.async_api import async_playwright


async def test_browser_launch():
    """测试浏览器能否正常启动并访问页面"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://httpbin.org/get", timeout=15000)
        title = await page.title()
        content_preview = (await page.content())[:200]
        print(f"[OK] Browser launched, page title: '{title}', content: {content_preview}...")

        # 测试截图
        # Screenshot goes to logs/ (gitignored)
        import pathlib
        log_dir = pathlib.Path("logs")
        log_dir.mkdir(exist_ok=True)
        await page.screenshot(path=str(log_dir / "_test_screenshot.png"), full_page=False)
        print("[OK] Screenshot saved to logs/_test_screenshot.png")

        await browser.close()
        print("[OK] Playwright smoke test PASSED")


if __name__ == "__main__":
    asyncio.run(test_browser_launch())
