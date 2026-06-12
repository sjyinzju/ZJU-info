"""配置加载器 — YAML 读取 + Pydantic 校验 + 环境变量注入"""
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator
import yaml


# ══════════════════════════════════════════════════════════════
#  环境变量解析
# ══════════════════════════════════════════════════════════════

def resolve_env(value: str) -> str:
    """递归解析 ${VAR} 和 ${VAR:-default}"""
    pattern = re.compile(r'\$\{(\w+)(?::-([^}]*))?\}')

    def _replace(match):
        var_name = match.group(1)
        default = match.group(2)
        env_val = os.environ.get(var_name, "")
        if env_val:
            return env_val
        if default is not None:
            return default
        return ""  # 解析失败返回空，后续校验会报错

    prev = None
    result = value
    while prev != result:
        prev = result
        result = pattern.sub(_replace, result)
    return result


def resolve_dict(obj: Any) -> Any:
    """递归解析 dict/list 中所有字符串的环境变量"""
    if isinstance(obj, dict):
        return {k: resolve_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_dict(item) for item in obj]
    if isinstance(obj, str):
        return resolve_env(obj)
    return obj


# ══════════════════════════════════════════════════════════════
#  Pydantic 配置模型
# ══════════════════════════════════════════════════════════════

class UserConfig(BaseModel):
    name: str = "用户"
    identity: str = "浙江大学 在读本科生"
    interests: List[str] = Field(default_factory=list)


class LLMConfig(BaseModel):
    provider: Literal["deepseek", "openai", "anthropic"] = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    max_tokens: int = 2000
    temperature: float = 0.3

    @field_validator("api_key")
    @classmethod
    def api_key_required(cls, v: str) -> str:
        if not v or "placeholder" in v.lower():
            raise ValueError(
                "LLM API Key 未配置。请在 config/.env 中填入 DEEPSEEK_API_KEY=你的key\n"
                "获取 DeepSeek API Key: https://platform.deepseek.com"
            )
        return v


class OutputConfig(BaseModel):
    report_dir: str = "E:/MyReports"
    report_name_format: str = "{date}-{seq:02d}"
    save_raw: bool = True
    save_processed: bool = True


class ScheduleConfig(BaseModel):
    enabled: bool = False
    times: List[str] = Field(default_factory=lambda: ["08:00"])
    timezone: str = "Asia/Shanghai"
    collection_interval_minutes: int = 120


class CrawlConfig(BaseModel):
    max_concurrent: int = 5
    request_delay_seconds: List[float] = Field(default_factory=lambda: [1.0, 3.0])
    timeout_seconds: int = 30
    max_retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )


class FeishuNotifyConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class EmailNotifyConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.zju.edu.cn"
    smtp_port: int = 465
    from_addr: str = ""
    to_addr: str = ""
    password: str = ""


class NotifyConfig(BaseModel):
    feishu: FeishuNotifyConfig = Field(default_factory=FeishuNotifyConfig)
    email: EmailNotifyConfig = Field(default_factory=EmailNotifyConfig)


class ZjuamAuthConfig(BaseModel):
    username: str = ""
    password: str = ""


class CC98AuthConfig(BaseModel):
    token: str = ""
    token_out_of_campus: str = ""
    username: str = ""
    password: str = ""


class AuthConfig(BaseModel):
    zjuam: ZjuamAuthConfig = Field(default_factory=ZjuamAuthConfig)
    cc98: CC98AuthConfig = Field(default_factory=CC98AuthConfig)


class SourceConfig(BaseModel):
    name: str
    url: str = ""
    endpoint: Optional[str] = None  # CC98 用
    enabled: bool = False
    keywords: List[str] = Field(default_factory=list)
    max_items: int = 10
    selector: Optional[str] = None
    wait_selector: Optional[str] = None  # Playwright 用
    monitor: bool = False
    account_name: Optional[str] = None  # 微信公众号用


class SourcesConfig(BaseModel):
    l0_rss: List[SourceConfig] = Field(default_factory=list)
    l0_html: List[SourceConfig] = Field(default_factory=list)
    l1_api: List[SourceConfig] = Field(default_factory=list)
    l1_playwright: List[SourceConfig] = Field(default_factory=list)
    l2_wechat: List[SourceConfig] = Field(default_factory=list)
    l4_zjuam: List[SourceConfig] = Field(default_factory=list)
    l5_internal: List[SourceConfig] = Field(default_factory=list)

    def get_enabled(self) -> List[tuple]:
        """返回所有启用的源，格式: [(category, SourceConfig), ...]"""
        result = []
        for cat in [
            "l0_rss", "l0_html",
            "l1_api", "l1_playwright",
            "l2_wechat", "l4_zjuam", "l5_internal",
        ]:
            items = getattr(self, cat, [])
            for item in items:
                if item.enabled:
                    result.append((cat, item))
        return result


class PromptsConfig(BaseModel):
    filter: str = ""
    summary: str = ""


class AppConfig(BaseModel):
    """根配置"""
    user: UserConfig = Field(default_factory=UserConfig)
    llm: LLMConfig
    output: OutputConfig = Field(default_factory=OutputConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)

    class Config:
        extra = "forbid"  # 禁止未定义的字段，及时发现拼写错误


# ══════════════════════════════════════════════════════════════
#  加载入口
# ══════════════════════════════════════════════════════════════

def load_config(config_path: Optional[Path] = None) -> AppConfig:
    """加载、解析、校验配置，返回 AppConfig 实例"""
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"

    if not config_path.exists():
        sys.exit(f"[ERROR] 配置文件不存在: {config_path}")

    # 1. 加载 .env（如果存在）
    env_file = config_path.parent / ".env"
    if env_file.exists():
        try:
            from dotenv import load_dotenv as _load
            _load(env_file)
        except ImportError:
            pass

    # 2. 读取 YAML
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # 3. 解析环境变量
    resolved = resolve_dict(raw)

    # 4. Pydantic 校验
    try:
        config = AppConfig(**resolved)
    except Exception as e:
        print("\n" + "=" * 62)
        print("  [ERROR] 配置校验失败，请检查 config/config.yaml")
        print("=" * 62)
        print(f"  {e}")
        print("=" * 62 + "\n")
        sys.exit(1)

    return config
