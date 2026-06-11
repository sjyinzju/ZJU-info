"""
ZJU 统一认证 (ZJUAM) Session 管理器。

认证流程（基于 zjuam.py + Celechron zjuam.dart）：
1. GET /cas/login → 提取 execution token
2. GET /cas/v2/getPubKey → 获取 RSA modulus + exponent
3. RSA 无 padding 加密密码
4. POST /cas/login → 获得 iPlanetDirectoryPro Cookie

Session 生命周期：
- iPlanetDirectoryPro 有效期 ~2 小时
- 自动检测过期（探针请求 + 响应内容检测）
- pickle 持久化缓存，支持跨进程复用
"""

import logging
import pickle
import re
import ssl
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import urllib3

# 禁用 SSL 警告（ZJU 部分服务器使用旧版证书/DH key）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────
ZJUAM_URL = "https://zjuam.zju.edu.cn"
CAS_LOGIN_URL = f"{ZJUAM_URL}/cas/login"
PUBKEY_URL = f"{ZJUAM_URL}/cas/v2/getPubKey"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}

# TGT 有效期 2 小时，提前 10 分钟刷新
SESSION_TTL_SECONDS = 110 * 60  # 1h50min


# ── SSL workaround for legacy ZJU servers ──────────────────────

# Python 3.11+ 拒绝 ZJU 服务器的弱 DH key。需要自定义 SSL context。
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE
_SSL_CONTEXT.set_ciphers("DEFAULT:@SECLEVEL=0")


class _LegacySSLAdapter(requests.adapters.HTTPAdapter):
    """自定义 HTTPAdapter，使用降低安全级别的 SSL context。"""
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _SSL_CONTEXT
        return super().init_poolmanager(*args, **kwargs)


def _create_session() -> requests.Session:
    """创建兼容 ZJU 旧版 SSL 的 requests.Session。"""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    # 挂载自定义 SSL adapter（解决 DH_KEY_TOO_SMALL）
    adapter = _LegacySSLAdapter()
    session.mount("https://", adapter)
    return session


# ── RSA encryption (from zjuam.py, zero-dependency) ────────────

def rsa_encrypt(password: str, exponent_hex: str, modulus_hex: str) -> str:
    """
    RSA 无 padding 加密密码。

    算法：将密码字符串的每个字符 ASCII 值拼接为大整数，用公钥加密后返回 hex 字符串。
    """
    pwd_int = 0
    for ch in password:
        pwd_int = pwd_int * 256 + ord(ch)

    e = int(exponent_hex, 16)
    n = int(modulus_hex, 16)

    encrypted = pow(pwd_int, e, n)
    return hex(encrypted)[2:]


# ── Session manager ────────────────────────────────────────────

class ZjuAmSession:
    """ZJU 统一认证 Session 管理器。

    用法:
        session = ZjuAmSession("学号", "密码")
        if session.login():
            sso_session = session.sso_to_subsystem(
                "https://courses.zju.edu.cn/user/index"
            )
            resp = sso_session.get("https://courses.zju.edu.cn/api/todos")
    """

    def __init__(
        self,
        username: str,
        password: str,
        cache_path: Optional[Path] = None,
    ):
        self.username = username
        self.password = password
        self.cache_path = cache_path or Path("config/cookies/zjuam.pkl")

        self._session: requests.Session = _create_session()
        self._login_time: float = 0.0  # epoch timestamp of last login
        self._iplanet_cookie: Optional[str] = None

        # 尝试从缓存恢复
        self._load_cache()

    # ── public API ──────────────────────────────────────────

    def login(self) -> bool:
        """
        执行完整登录流程。成功返回 True，失败返回 False。
        登录成功后 Session 中自动携带 iPlanetDirectoryPro Cookie。
        """
        logger.info("开始 ZJUAM 登录...")

        # Step 0: 全新 Session（避免旧 Cookie 污染，参考 Celechron）
        self._session = _create_session()

        try:
            # Step 1: 获取 execution token
            resp = self._session.get(CAS_LOGIN_URL, timeout=15)
            match = re.search(
                r'name="execution" value="(.*?)"', resp.text
            )
            if not match:
                logger.error("无法从 CAS 页面提取 execution token")
                return False
            execution = match.group(1)

            # Step 2: 获取 RSA 公钥
            resp = self._session.get(PUBKEY_URL, timeout=15)
            pubkey = resp.json()
            modulus = pubkey["modulus"]
            exponent = pubkey["exponent"]

            # Step 3: RSA 加密密码
            encrypted_pwd = rsa_encrypt(self.password, exponent, modulus)

            # Step 4: 提交登录
            data = {
                "username": self.username,
                "password": encrypted_pwd,
                "_eventId": "submit",
                "execution": execution,
                "authcode": "",
                "rememberMe": "true",
            }
            resp = self._session.post(
                CAS_LOGIN_URL, data=data, allow_redirects=False, timeout=15
            )

            # 成功标志：302 重定向 + Set-Cookie 含 iPlanetDirectoryPro
            cookies = self._session.cookies.get_dict()
            if "iPlanetDirectoryPro" in cookies:
                self._login_time = time.time()
                self._iplanet_cookie = cookies["iPlanetDirectoryPro"]
                self._save_cache()
                logger.info("ZJUAM 登录成功")
                return True

            # 失败：仍在 CAS 页面
            if "用户名或密码错误" in resp.text:
                logger.error("ZJUAM 登录失败：用户名或密码错误")
            elif "统一身份认证" in resp.text:
                logger.error("ZJUAM 登录失败：停留在 CAS 页面（未知原因）")
            else:
                logger.error(f"ZJUAM 登录失败：未获得 iPlanetDirectoryPro Cookie")
            return False

        except requests.exceptions.RequestException as e:
            logger.error(f"ZJUAM 登录网络错误: {e}")
            return False

    def ensure_auth(self) -> bool:
        """
        确保持有有效的 Session。自动检测过期并重登。
        返回 True 表示最终持有有效 Session。
        """
        if self.is_valid():
            return True
        logger.info("Session 已过期或无效，重新登录...")
        return self.login()

    def is_valid(self) -> bool:
        """
        检查 Session 是否仍有效。

        策略（参考 Celechron）：
        1. TTL 检查：距登录超过 SESSION_TTL_SECONDS → 过期
        2. 探针请求：访问 CAS 首页，检查是否仍在登录页
        """
        if not self._iplanet_cookie:
            return False

        # TTL 检查
        if time.time() - self._login_time > SESSION_TTL_SECONDS:
            return False

        # 探针检查：访问 CAS 首页
        try:
            resp = self._session.get(CAS_LOGIN_URL, timeout=10)
            if "统一身份认证平台" in resp.text:
                # 仍然显示登录页 → Session 已过期
                return False
            return True
        except requests.exceptions.RequestException:
            # 网络错误时不标记为无效（可能是暂时的）
            return True

    def sso_to_subsystem(self, service_url: str) -> Optional[requests.Session]:
        """
        CAS SSO 跳转：用 iPlanetDirectoryPro 换取子系统的 Session Cookie。

        参考 courses.dart:88-108 的手动跟随重定向方案：
        1. 直接访问目标 service_url（携带 iPlanetDirectoryPro）
        2. 服务器自动触发 CAS 重定向链：
            目标站 → 302 CAS login → 302 目标站?ticket=ST-xxxx → 302 目标站首页
        3. 手动跟随每一步，累积所有 Cookie
        4. 重定向停止于目标站首页时 → 返回携带子系统 Cookie 的 Session

        Args:
            service_url: 子系统登录入口 URL（如 https://courses.zju.edu.cn/user/index）

        Returns:
            携带子系统 Cookie 的 requests.Session，或 None（登录失败时）
        """
        if not self.ensure_auth():
            logger.error("SSO 跳转失败：ZJUAM Session 无效")
            return None

        sub_session = _create_session()
        # 注入 iPlanetDirectoryPro（绑定到 zjuam 域，CAS 跳转时需要）
        sub_session.cookies.set(
            "iPlanetDirectoryPro", self._iplanet_cookie,
            domain="zjuam.zju.edu.cn",
        )

        try:
            current_url = service_url
            max_redirects = 12
            prev_url = None

            for step in range(max_redirects):
                # 防止同 URL 连续重定向（而非全量 visited ——
                # 重定向链会正常地回到起始 URL，如 courses → identity → CAS → identity → courses）
                if current_url == prev_url:
                    logger.error(f"SSO 重定向死循环: {current_url}")
                    return None
                prev_url = current_url

                resp = sub_session.get(
                    current_url, allow_redirects=False, timeout=15
                )

                # 检查是否到达目标（非重定向）
                if resp.status_code not in (301, 302, 303, 307, 308):
                    # 检查是否为 CAS 登录页（认证失败）
                    if "统一身份认证" in resp.text or "cas/login" in resp.url:
                        logger.error(
                            f"SSO 失败：停在 CAS 登录页 "
                            f"(URL: {resp.url[:100]})"
                        )
                        return None
                    # 成功落在子系统页
                    cookies = sub_session.cookies.get_dict()
                    logger.info(
                        f"SSO 成功 → {resp.url[:80]} "
                        f"(cookies: {list(cookies.keys())})"
                    )
                    return sub_session

                # 跟随重定向
                location = resp.headers.get("Location", "")
                if not location:
                    logger.error("SSO 失败：302 但没有 Location header")
                    return None

                # 处理相对路径
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"

                logger.debug(f"  302 → {location[:80]}")
                current_url = location

            logger.error("SSO 失败：超过最大重定向次数")
            return None

        except requests.exceptions.RequestException as e:
            logger.error(f"SSO 跳转网络错误: {e}")
            return None

    # ── pickle 持久化 ───────────────────────────────────────

    def _save_cache(self):
        """保存 Session 到 pickle 文件"""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "cookies": self._session.cookies.get_dict(),
                "login_time": self._login_time,
                "iplanet_cookie": self._iplanet_cookie,
            }
            with open(self.cache_path, "wb") as f:
                pickle.dump(data, f)
            logger.debug(f"Session 缓存已保存: {self.cache_path}")
        except OSError as e:
            logger.warning(f"Session 缓存保存失败: {e}")

    def _load_cache(self):
        """从 pickle 文件恢复 Session"""
        if not self.cache_path.exists():
            return

        try:
            with open(self.cache_path, "rb") as f:
                data = pickle.load(f)

            # 检查 TTL
            login_time = data.get("login_time", 0)
            if time.time() - login_time > SESSION_TTL_SECONDS:
                logger.debug("缓存 Session 已过期，丢弃")
                return

            # 恢复 Cookie
            for name, value in data.get("cookies", {}).items():
                self._session.cookies.set(name, value)

            self._login_time = login_time
            self._iplanet_cookie = data.get("iplanet_cookie")
            logger.debug(
                f"从缓存恢复 Session（剩余有效期: "
                f"{max(0, SESSION_TTL_SECONDS - (time.time() - login_time)):.0f}s）"
            )
        except (pickle.UnpicklingError, KeyError, OSError) as e:
            logger.warning(f"Session 缓存恢复失败: {e}")

    # ── context manager ──────────────────────────────────────

    def close(self):
        """关闭 Session（释放连接池）"""
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
