"""验证 instructor + DeepSeek 兼容性

运行前请在 config/.env 中填入真实的 DEEPSEEK_API_KEY。
若 API key 未配置，测试会自动跳过。
"""
import os
import sys
import asyncio
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv("config/.env")

from pydantic import BaseModel, Field
from openai import OpenAI
import instructor


# ── 测试用的简单模型 ─────────────────────────────────
class ItemSummary(BaseModel):
    """单条信息摘要"""
    title: str = Field(..., description="标题")
    importance: str = Field(..., description="重要程度：高/中/低")
    one_line_summary: str = Field(..., description="一句话摘要")


class TestResult(BaseModel):
    """多条信息摘要结果"""
    items: list[ItemSummary] = Field(..., description="摘要列表")


async def test_instructor_with_deepseek():
    """验证 instructor 能否正常配合 DeepSeek API 输出结构化数据"""

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    if not api_key or "placeholder" in api_key:
        print("=" * 60)
        print("[SKIP] DEEPSEEK_API_KEY not configured")
        print("       Fill in your DeepSeek API Key in config/.env")
        print("       Get one at: https://platform.deepseek.com")
        print("=" * 60)
        return True  # Not a failure

    print("=" * 60)
    print("测试 instructor + DeepSeek 兼容性...")
    print(f"  Base URL: {base_url}")
    print(f"  Model: deepseek-chat")
    print("=" * 60)

    try:
        # 使用 instructor.patch 模式
        client = instructor.from_openai(
            OpenAI(api_key=api_key, base_url=base_url),
            mode=instructor.Mode.JSON,  # JSON mode，兼容性最好
        )

        test_content = """
        1. 浙江大学2026年"挑战杯"课外学术科技作品竞赛报名通知，截止日期2026年7月15日
        2. 计算机学院关于选拔优秀本科生参加2026年暑期剑桥大学交流项目的通知
        """

        result = client.chat.completions.create(
            model="deepseek-chat",
            response_model=TestResult,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个信息摘要助手。从给定文本中提取关键信息，输出结构化摘要。",
                },
                {"role": "user", "content": f"请摘要以下内容：\n{test_content}"},
            ],
            max_tokens=500,
            temperature=0.3,
        )

        print(f"\n[PASS] Instructor + DeepSeek works!")
        print(f"   Return type: {type(result).__name__}")
        print(f"   Items: {len(result.items)}")
        for i, item in enumerate(result.items):
            print(f"   [{i+1}] {item.title}")
            print(f"       importance: {item.importance}")
            print(f"       summary: {item.one_line_summary}")
        print(f"\n[PASS] instructor can constrain DeepSeek structured output")
        return True

    except Exception as e:
        print(f"\n[FAIL] {type(e).__name__}: {e}")
        print("\nPossible causes:")
        print("  1. Invalid or expired API key")
        print("  2. DeepSeek API doesn't support instructor JSON mode")
        print("  3. Network connection issue")
        print("\nFallback: if instructor incompatible, use raw prompt + Pydantic parse")
        return False


if __name__ == "__main__":
    success = asyncio.run(test_instructor_with_deepseek())
    sys.exit(0 if success else 1)
