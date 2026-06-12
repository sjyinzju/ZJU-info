"""
CC98 Token 自动获取与刷新。

使用 OAuth2 Password Grant 从 openid.cc98.org 获取 Bearer Token。
refresh_token 有效期 30 天，过期前自动刷新。
Token 缓存到 config/cookies/cc98_token.json。
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ── OIDC 配置（来自 CC98 React 源码 LogOn.tsx）──
TOKEN_URL = "https://openid.cc98.org/connect/token"
CLIENT_ID = "9a1fd200-8687-44b1-4c20-08d50a96e5cd"
CLIENT_SECRET = "8b53f727-08e2-4509-8857-e34bf92b27f2"
SCOPE = "cc98-api openid offline_access"

# 缓存文件
CACHE_DIR = Path("config/cookies")
CACHE_FILE = CACHE_DIR / "cc98_token.json"


def _create_session():
    s = requests.Session()
    s.verify = False
    import urllib3
    urllib3.disable_warnings()
    return s


def acquire_token(username: str, password: str) -> Optional[dict]:
    """
    通过 Password Grant 获取 CC98 Token。

    Returns:
        dict with access_token, refresh_token, expires_in, token_type
        or None on failure.
    """
    if not username or not password:
        logger.warning("CC98 auto-login skipped: username or password not configured")
        return None

    logger.info("CC98 Password Grant 获取 Token...")
    s = _create_session()
    try:
        r = s.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            logger.info(
                "CC98 Token 获取成功 (expires_in=%ss, has_refresh=%s)",
                data.get("expires_in", "?"),
                "yes" if data.get("refresh_token") else "no",
            )
            return data
        else:
            err = r.json()
            logger.error(
                "CC98 Password Grant 失败: %s (%s)",
                err.get("error", "?"),
                err.get("error_description", ""),
            )
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"CC98 Token 获取网络错误: {e}")
        return None
    finally:
        s.close()


def refresh_token(refresh_token_str: str) -> Optional[dict]:
    """
    用 refresh_token 刷新 access_token。

    Returns:
        dict with access_token, refresh_token, expires_in
        or None on failure.
    """
    logger.info("CC98 刷新 Token...")
    s = _create_session()
    try:
        r = s.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token_str,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            logger.info(
                "CC98 Token 刷新成功 (expires_in=%ss)",
                data.get("expires_in", "?"),
            )
            return data
        else:
            logger.warning("CC98 refresh_token 过期，需要重新登录")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"CC98 Token 刷新网络错误: {e}")
        return None
    finally:
        s.close()


def load_cached_token() -> Optional[dict]:
    """从缓存文件加载 Token。"""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        # 检查 access_token 是否过期（给 60 秒缓冲）
        expires_at = data.get("expires_at", 0)
        if time.time() > expires_at - 60:
            logger.debug("缓存 Token 已过期")
            return None
        logger.debug(
            "缓存 Token 有效 (剩余 %ds)", int(expires_at - time.time())
        )
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_token_to_cache(token_data: dict):
    """保存 Token 到缓存文件（附带过期时间戳）。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = dict(token_data)
        # 记录绝对过期时间
        expires_in = data.get("expires_in", 3600)
        data["expires_at"] = time.time() + expires_in
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.debug("Token 已缓存到 %s", CACHE_FILE)
    except OSError as e:
        logger.warning("Token 缓存写入失败: %s", e)


def get_valid_token(username: str = "", password: str = "") -> Optional[str]:
    """
    获取有效的 CC98 access_token（自动缓存/刷新/获取）。

    优先级：
    1. 缓存的有效 access_token
    2. 缓存的 refresh_token → 刷新
    3. Password Grant → 获取新 Token
    """
    # 1. 检查缓存
    cached = load_cached_token()
    if cached and cached.get("access_token"):
        return cached["access_token"]

    # 2. 检查缓存的 refresh_token
    if cached and cached.get("refresh_token"):
        new_data = refresh_token(cached["refresh_token"])
        if new_data and new_data.get("access_token"):
            save_token_to_cache(new_data)
            return new_data["access_token"]

    # 3. Password Grant
    if username and password:
        new_data = acquire_token(username, password)
        if new_data and new_data.get("access_token"):
            save_token_to_cache(new_data)
            return new_data["access_token"]

    return None
