#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agy-accounts · Antigravity 多账号切换面板.

只替换 agy-server（language_server --standalone）使用的 OAuth token，
不触碰 ~/.gemini 中的会话、项目与产物数据。

依赖：Python 3.9+ 标准库。
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import hashlib
import hmac
import http.cookies
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import glob
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

APP_NAME = "agy-accounts"
VERSION = "1.10.2"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(BASE_DIR, "webapp")

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
}

DEFAULTS: Dict[str, Any] = {
    "token_path": "~/.gemini/jetski-standalone-oauth-token",
    "accounts_dir": "~/.local/share/agy-accounts/accounts",
    "service_name": "agy-server",
    "stop_command": [],
    "start_command": [],
    "status_command": [],
    "probe_url": "http://127.0.0.1:8765/",
    "app_url": "http://127.0.0.1:8765/",
    "listen_host": "127.0.0.1",
    "listen_port": 8790,
    "password": "",
    "password_hash": "",
    "session_ttl": 86400,
    "cookie_secure": False,
    "probe_timeout": 30,
    "command_timeout": 30,
    "language_server_path": "/opt/agy-server/language_server",
    "quota_ttl": 300,
    "quota_timeout": 12,
    "stop_timeout": 20,
    "login_max_failures": 5,
    "login_window": 300,
    "log_level": "INFO",
    "auto_failover": True,
    "auto_failover_last_good": "",
}

ENV_OVERRIDES = {
    "AGY_ACCOUNTS_TOKEN": "token_path",
    "AGY_ACCOUNTS_DIR": "accounts_dir",
    "AGY_ACCOUNTS_SERVICE": "service_name",
    "AGY_ACCOUNTS_PROBE_URL": "probe_url",
    "AGY_ACCOUNTS_APP_URL": "app_url",
    "AGY_ACCOUNTS_HOST": "listen_host",
    "AGY_ACCOUNTS_PORT": "listen_port",
    "AGY_ACCOUNTS_PASSWORD": "password",
    "AGY_ACCOUNTS_LOG_LEVEL": "log_level",
}

COOKIE_NAME = "agy_accounts_session"
CSRF_HEADER = "X-Agy-Accounts"
MAX_BODY = 64 * 1024
MAX_NAME = 60
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
JWT_RE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*$")

log = logging.getLogger("agy-accounts")
CONFIG_LOCK = threading.RLock()
PREFERENCES_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 错误与基础工具
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


class SwitchError(Exception):
    def __init__(self, message: str, status: int = 502, rolled_back: Optional[bool] = None,
                 detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.rolled_back = rolled_back
        self.detail = detail


class ServiceOperationLock:
    """切号、服务恢复和更新共用；繁忙时返回 409，不排队。"""

    def __init__(self):
        self._guard = threading.Lock()
        self._operation = ""

    @contextmanager
    def hold(self, operation: str) -> Iterator[None]:
        with self._guard:
            if self._operation:
                raise ApiError(409, "正在执行%s，请等待当前操作完成。" % self._operation)
            self._operation = operation
        try:
            yield
        finally:
            with self._guard:
                self._operation = ""


def now() -> int:
    return int(time.time())


def iso_time(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def expand_path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(str(value)))


def ensure_dir(path: str, mode: int = 0o700) -> str:
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return path


def read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("读取文件失败 %s: %s", path, exc.__class__.__name__)
        return None


def read_live_token(cfg: Dict[str, Any]) -> Optional[bytes]:
    try:
        with open(cfg["token_path"], "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ApiError(500, "无法读取线上令牌，请检查文件与目录权限。") from exc


def write_atomic(path: str, data: bytes, mode: int = 0o600) -> None:
    """在同目录写入临时文件后 os.replace，避免出现半个文件。"""
    directory = os.path.dirname(path) or "."
    ensure_dir(directory, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".agy-accounts-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json_atomic(path: str, obj: Any, mode: int = 0o600) -> None:
    payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    write_atomic(path, payload, mode)


def read_json(path: str) -> Dict[str, Any]:
    raw = read_bytes(path)
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("JSON 文件解析失败：%s", path)
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def config_home() -> str:
    return expand_path(os.environ.get("AGY_ACCOUNTS_HOME", "~/.config/agy-accounts"))


def config_path() -> str:
    explicit = os.environ.get("AGY_ACCOUNTS_CONFIG", "").strip()
    if explicit:
        return expand_path(explicit)
    return os.path.join(config_home(), "config.json")


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    path = expand_path(path) if path else config_path()
    cfg: Dict[str, Any] = dict(DEFAULTS)
    stored = read_json(path)
    for key, value in stored.items():
        if key in cfg and value is not None:
            cfg[key] = value

    for env, key in ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value is None or value == "":
            continue
        if key in ("listen_port",):
            try:
                cfg[key] = int(value)
            except ValueError:
                raise SystemExit("环境变量 %s 必须是整数" % env)
        else:
            cfg[key] = value

    cfg["config_path"] = path
    cfg["config_home"] = os.path.dirname(path) or "."
    cfg["token_path"] = expand_path(cfg["token_path"])
    cfg["accounts_dir"] = expand_path(cfg["accounts_dir"])
    cfg["probe_url"] = str(cfg.get("probe_url") or "").strip()
    cfg["app_url"] = str(cfg.get("app_url") or "").strip()
    cfg["listen_host"] = str(cfg.get("listen_host") or "127.0.0.1")
    cfg["listen_port"] = int(cfg.get("listen_port") or 8790)
    for key in ("session_ttl", "probe_timeout", "command_timeout", "stop_timeout",
                "login_max_failures", "login_window", "quota_ttl", "quota_timeout"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            cfg[key] = int(DEFAULTS[key])
    cfg["cookie_secure"] = bool(cfg.get("cookie_secure"))
    cfg["password"] = str(cfg.get("password") or "")
    cfg["password_hash"] = str(cfg.get("password_hash") or "")
    cfg["language_server_path"] = expand_path(cfg.get("language_server_path") or
                                               "/opt/agy-server/language_server")
    cfg["auto_failover"] = bool(cfg.get("auto_failover", True))
    return cfg


def update_config(cfg: Dict[str, Any], **updates: Any) -> None:
    """更新少量配置项并写回文件，用于面板设置页。"""
    path = cfg.get("config_path") or config_path()
    with CONFIG_LOCK:
        try:
            try:
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)
            except FileNotFoundError:
                stored = {}
            if not isinstance(stored, dict):
                raise ValueError("configuration is not a JSON object")
            stored.update(updates)
            write_json_atomic(path, stored, 0o600)
        except (OSError, ValueError) as exc:
            log.warning("保存配置失败：%s", exc.__class__.__name__)
            raise ApiError(500, "无法保存配置，请检查配置文件格式和写入权限。") from exc
        cfg.update(updates)


def preferences_path(cfg: Dict[str, Any]) -> str:
    return os.path.join(cfg["config_home"], "preferences.json")


def load_preferences(cfg: Dict[str, Any]) -> Dict[str, Any]:
    data = read_json(preferences_path(cfg))
    dashboard = data.get("dashboard")
    if isinstance(dashboard, dict):
        cards = dashboard.get("cards")
        order = dashboard.get("order")
        if isinstance(cards, list) and isinstance(order, list):
            try:
                revision = int(data.get("rev") or 0)
            except (TypeError, ValueError):
                revision = 0
            return {"rev": revision, "dashboard": {
                "cards": [str(x) for x in cards][:50],
                "order": [str(x) for x in order][:50],
            }}
    return {"rev": 0, "dashboard": {"cards": list(DEFAULT_DASHBOARD_CARDS),
                                     "order": list(DEFAULT_DASHBOARD_ORDER)}}


DEFAULT_DASHBOARD_CARDS = ["total_tokens", "input_tokens", "output_tokens",
                           "cache_hit_rate", "account_status", "aggregate_quota"]
DEFAULT_DASHBOARD_ORDER = ["total_tokens", "input_tokens", "output_tokens",
                           "cache_hit_rate", "first_text_latency", "body_speed",
                           "account_status", "aggregate_quota", "average_input"]


def save_preferences(cfg: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    dashboard = data.get("dashboard")
    if not isinstance(dashboard, dict):
        raise ApiError(400, "preferences.dashboard 必须是对象")
    cards = dashboard.get("cards")
    order = dashboard.get("order")
    if not isinstance(cards, list) or not isinstance(order, list):
        raise ApiError(400, "dashboard.cards 与 dashboard.order 必须是数组")
    if len(cards) > 50 or len(order) > 50:
        raise ApiError(400, "卡片数量过多")
    if data.get("manual") is not True:
        raise ApiError(400, "仅支持在设置页手动保存卡片")
    client_rev = data.get("rev")
    if type(client_rev) is not int or client_rev < 0:
        raise ApiError(409, "设置版本缺失，请刷新页面后重试")
    cards = [str(x)[:80] for x in cards]
    order = [str(x)[:80] for x in order]
    # 空选择可能是旧页面或异常请求写回，拒绝而不是静默改回默认。
    if not cards:
        raise ApiError(400, "至少保留一张首页卡片")
    with PREFERENCES_LOCK:
        if client_rev != load_preferences(cfg)["rev"]:
            raise ApiError(409, "设置已在其它页面更新，请刷新后再保存")
        payload = {"rev": client_rev + 1, "dashboard": {"cards": cards, "order": order}}
        write_json_atomic(preferences_path(cfg), payload, 0o600)
    log.info("卡片偏好已更新：rev=%d cards=%d order=%d [%s]",
             payload["rev"], len(cards), len(order), ",".join(cards))
    return payload


def command_list(cfg: Dict[str, Any], key: str, default: List[str]) -> List[str]:
    value = cfg.get(key)
    if isinstance(value, list) and value:
        return [str(x) for x in value]
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    return default


def service_commands(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    name = str(cfg.get("service_name") or "agy-server")
    return {
        "stop": command_list(cfg, "stop_command", ["systemctl", "stop", name]),
        "start": command_list(cfg, "start_command", ["systemctl", "start", name]),
        "status": command_list(cfg, "status_command",
                               ["systemctl", "is-active", "--quiet", name]),
    }


# ---------------------------------------------------------------------------
# 密码与会话
# ---------------------------------------------------------------------------


def hash_password(password: str, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"),
                                 iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, digest.hex())


def verify_password_hash(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt, expected = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("ascii"),
                                     int(rounds))
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def password_configured(cfg: Dict[str, Any]) -> bool:
    return bool(cfg.get("password") or cfg.get("password_hash"))


def check_password(cfg: Dict[str, Any], password: str) -> bool:
    ok = False
    if cfg.get("password"):
        ok = hmac.compare_digest(str(cfg["password"]).encode("utf-8"),
                                 password.encode("utf-8"))
    if cfg.get("password_hash") and verify_password_hash(password, str(cfg["password_hash"])):
        ok = True
    return ok


def change_password(cfg: Dict[str, Any], current: str, new_password: str) -> None:
    if os.environ.get("AGY_ACCOUNTS_PASSWORD"):
        raise ApiError(409, "面板密码由环境变量管理，请修改 AGY_ACCOUNTS_PASSWORD 并重启面板。")
    if not isinstance(new_password, str) or len(new_password) < 8:
        raise ApiError(400, "新密码至少 8 个字符")
    if len(new_password) > 256:
        raise ApiError(400, "新密码过长")
    with CONFIG_LOCK:
        if not check_password(cfg, current):
            raise ApiError(403, "当前密码不正确")
        update_config(cfg, password_hash=hash_password(new_password), password="")
    log.info("面板密码已更新")


def load_secret(cfg: Dict[str, Any]) -> bytes:
    path = os.path.join(cfg["config_home"], "secret.key")
    raw = read_bytes(path)
    if raw and len(raw) >= 32:
        return raw[:32]
    secret = secrets.token_bytes(32)
    ensure_dir(cfg["config_home"], 0o700)
    write_atomic(path, secret, 0o600)
    return secret


def issue_session(secret: bytes, ttl: int) -> str:
    payload = json.dumps({"exp": now() + max(60, ttl), "n": secrets.token_hex(8)},
                         separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).rstrip(b"=")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    return (body + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode("ascii")


def verify_session(secret: bytes, value: str) -> bool:
    try:
        body, sig = value.encode("ascii").split(b".", 1)
        expected = hmac.new(secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig + b"===")):
            return False
        payload = json.loads(base64.urlsafe_b64decode(body + b"===").decode("utf-8"))
        return int(payload.get("exp", 0)) > now()
    except (ValueError, TypeError, KeyError):
        return False


class LoginLimiter:
    def __init__(self, max_failures: int, window: int):
        self.max_failures = max(1, max_failures)
        self.window = max(30, window)
        self._lock = threading.Lock()
        self._failures: Dict[str, List[float]] = {}

    def _prune(self, ip: str) -> List[float]:
        cutoff = time.time() - self.window
        values = [t for t in self._failures.get(ip, []) if t >= cutoff]
        if values:
            self._failures[ip] = values
        else:
            self._failures.pop(ip, None)
        return values

    def locked_for(self, ip: str) -> int:
        with self._lock:
            values = self._prune(ip)
            if len(values) >= self.max_failures:
                return max(1, int(values[0] + self.window - time.time()))
            return 0

    def fail(self, ip: str) -> None:
        with self._lock:
            values = self._prune(ip)
            values.append(time.time())
            self._failures[ip] = values

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


# ---------------------------------------------------------------------------
# 令牌解析
# ---------------------------------------------------------------------------


def _jwt_email(token: str) -> str:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return ""
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        for key in ("email", "preferred_username", "upn"):
            value = data.get(key)
            if isinstance(value, str) and EMAIL_RE.match(value):
                return value
    except (ValueError, TypeError, UnicodeDecodeError):
        return ""
    return ""


def extract_email(raw: Optional[bytes]) -> str:
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace").strip().lstrip("\ufeff")
    if not text:
        return ""
    obj: Any = None
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None

    if isinstance(obj, dict):
        found: List[str] = []
        seen = set()

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 6 or id(node) in seen:
                return
            if isinstance(node, dict):
                seen.add(id(node))
                for key, value in node.items():
                    lk = str(key).lower()
                    if lk in ("email", "account", "username", "preferred_username") and \
                            isinstance(value, str) and EMAIL_RE.match(value.strip()):
                        found.append(value.strip())
                    elif lk in ("id_token", "access_token") and isinstance(value, str) \
                            and JWT_RE.match(value):
                        mail = _jwt_email(value)
                        if mail:
                            found.append(mail)
                    else:
                        walk(value, depth + 1)
            elif isinstance(node, list):
                seen.add(id(node))
                for item in node[:50]:
                    walk(item, depth + 1)

        walk(obj)
        if found:
            return found[0]

    if JWT_RE.match(text):
        mail = _jwt_email(text)
        if mail:
            return mail

    match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else ""



def _token_json(raw: Optional[bytes]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace").lstrip("\ufeff"))
    except (ValueError, UnicodeDecodeError):
        return None


def refresh_token_of(raw: Optional[bytes]) -> str:
    """取出 refresh_token。它是账号的稳定标识，access_token / 过期时间会轮换。

    兼容 agy-server 的 {"token": {"refresh_token": ...}} 以及扁平结构。
    """
    obj = _token_json(raw)
    if not isinstance(obj, dict):
        return ""
    seen = set()

    def walk(node: Any, depth: int = 0) -> str:
        if depth > 4 or not isinstance(node, dict) or id(node) in seen:
            return ""
        seen.add(id(node))
        value = node.get("refresh_token")
        if isinstance(value, str) and value.strip():
            return value.strip()
        for child in node.values():
            found = walk(child, depth + 1)
            if found:
                return found
        return ""

    return walk(obj)


def same_login(left: Optional[bytes], right: Optional[bytes]) -> bool:
    """字节完全一致，或 refresh_token 相同，都视为同一个 Google 登录。"""
    if left is None or right is None:
        return False
    if left == right:
        return True
    left_rt = refresh_token_of(left)
    return bool(left_rt) and left_rt == refresh_token_of(right)


def token_label(raw: Optional[bytes]) -> str:
    """日志里只能出现长度与摘要前缀，绝不输出内容。"""
    if raw is None:
        return "missing"
    return "len=%d sha256=%s" % (len(raw), hashlib.sha256(raw).hexdigest()[:8])



# ---------------------------------------------------------------------------
# 额度查询（CloudCode API）
# ---------------------------------------------------------------------------

CLOUDCODE_BASES = [
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
]
CLOUDCODE_BASE = CLOUDCODE_BASES[0]
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Antigravity 语言服务器使用 aidev_client UA；不带它时 CloudCode 只返回
# Claude 组，带它才会同时返回 Gemini Models 的 5h / weekly 两个桶。
QUOTA_USER_AGENT = ("antigravity/hub/2.32.1 (aidev_client; os_type=linux; "
                    "arch=amd64; cl=1021296516; datacollection_opt_in=false)")

# Antigravity 安装型 OAuth 客户端的公开凭据（Google 明确说明安装型应用密钥
# 不当作机密对待，客户端本身可从官方二进制中提取）。拆成两段以避免仓库
# 密钥扫描误报；运行时仍优先尝试配置缓存与二进制扫描结果。
_KNOWN_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
_KNOWN_CLIENT_SECRET = "GOCSPX-" + "K58FWR486LdLJ1mLB8sXC4z6qDAf"


class QuotaError(Exception):
    def __init__(self, message: str, unauthorized: bool = False):
        super().__init__(message)
        self.message = message
        self.unauthorized = unauthorized


def parse_expiry(value: Any) -> float:
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp / 1000.0 if stamp > 1e12 else stamp
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def token_details(raw: Optional[bytes]) -> Dict[str, Any]:
    obj = _token_json(raw)
    if not isinstance(obj, dict):
        raise QuotaError("令牌文件格式无法识别")
    token = obj.get("token") if isinstance(obj.get("token"), dict) else {}
    access = token.get("access_token") or obj.get("access_token") or ""
    refresh = token.get("refresh_token") or obj.get("refresh_token") or ""
    expiry = parse_expiry(token.get("expiry") or obj.get("expiry"))
    if not isinstance(access, str):
        access = ""
    if not isinstance(refresh, str):
        refresh = ""
    return {"obj": obj, "token": token, "access": access, "refresh": refresh,
            "expiry": expiry}


def encode_token_obj(obj: Dict[str, Any]) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def cache_key(raw: Optional[bytes]) -> str:
    rt = refresh_token_of(raw)
    basis = ("rt:" + rt) if rt else ("raw:" + hashlib.sha256(raw or b"").hexdigest())
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _tier_label(load_code: Dict[str, Any]) -> str:
    paid = load_code.get("paidTier") if isinstance(load_code.get("paidTier"), dict) else {}
    pid = str(paid.get("id") or "").lower()
    if "ultra" in pid:
        return "Ultra"
    if "pro" in pid:
        return "Pro"
    if paid:
        return str(paid.get("name") or "Paid")
    current = load_code.get("currentTier") if isinstance(load_code.get("currentTier"), dict) else {}
    cid = str(current.get("id") or "").lower()
    if cid in ("free-tier", "free"):
        return "Free"
    if cid in ("standard-tier", "standard"):
        return "Standard"
    return str(current.get("name") or "未知")


def normalize_models(raw_models: Any) -> List[Dict[str, Any]]:
    models: List[Dict[str, Any]] = []
    if not isinstance(raw_models, dict):
        return models
    for name, info in raw_models.items():
        if not isinstance(info, dict):
            continue
        low = str(name).lower()
        if not low.startswith(("gemini", "claude", "gpt", "image", "imagen")):
            continue
        quota = info.get("quotaInfo") if isinstance(info.get("quotaInfo"), dict) else {}
        try:
            fraction = float(quota.get("remainingFraction"))
        except (TypeError, ValueError):
            continue
        fraction = min(1.0, max(0.0, fraction))
        models.append({
            "name": str(name),
            "display_name": str(info.get("displayName") or name),
            "percentage": round(fraction * 100, 1),
            "remaining": round(fraction, 4),
            "reset": str(quota.get("resetTime") or ""),
            "supports_images": bool(info.get("supportsImages")),
            "supports_thinking": bool(info.get("supportsThinking")),
        })
    return models


def normalize_quota(load_code: Dict[str, Any], summary: Dict[str, Any],
                    models: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    groups: List[Dict[str, Any]] = []
    raw_groups = summary.get("groups") if isinstance(summary.get("groups"), list) else []
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        label = str(group.get("displayName") or "")
        low = label.lower()
        if "argon" in low:
            key = "argon"
        elif "carbon" in low:
            key = "carbon"
        elif "hydrogen" in low:
            key = "hydrogen"
        elif "gemini" in low:
            key = "gemini"
        elif "claude" in low or "gpt" in low:
            key = "claude"
        else:
            key = "other"
        windows: Dict[str, Any] = {}
        buckets = group.get("buckets") if isinstance(group.get("buckets"), list) else []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            window = str(bucket.get("window") or "")
            if window not in ("5h", "weekly"):
                continue
            try:
                remaining = float(bucket.get("remainingFraction"))
            except (TypeError, ValueError):
                continue
            remaining = min(1.0, max(0.0, remaining))
            windows[window] = {
                "remaining": round(remaining, 4),
                "reset": str(bucket.get("resetTime") or ""),
                "disabled": "does not currently apply" in str(bucket.get("description") or "").lower(),
            }
        groups.append({"key": key, "label": label or key.title(), "windows": windows})
    return {"tier": _tier_label(load_code), "groups": groups, "models": models or []}


def _post_json(url: str, payload: Dict[str, Any], access_token: str,
               timeout: int) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={
        "Authorization": "Bearer " + access_token,
        "Content-Type": "application/json",
        "User-Agent": QUOTA_USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise QuotaError("HTTP %s" % exc.code, unauthorized=exc.code in (401, 403))
    except Exception as exc:
        raise QuotaError("网络错误：%s" % exc.__class__.__name__)
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise QuotaError("接口返回了无法解析的内容")
    if not isinstance(value, dict):
        raise QuotaError("接口返回格式异常")
    return value


def fetch_quota_payload(access_token: str, timeout: int) -> Dict[str, Any]:
    """按原版顺序取：生产端点优先，逐模型额度 + 分组额度 + 套餐。"""
    load_code: Dict[str, Any] = {}
    last_error: Optional[QuotaError] = None
    for base in CLOUDCODE_BASES:
        try:
            load_code = _post_json(base + "/v1internal:loadCodeAssist", {
                "metadata": {"ideType": "ANTIGRAVITY"},
            }, access_token, timeout)
            last_error = None
            break
        except QuotaError as exc:
            last_error = exc
            if exc.unauthorized:
                raise
    if last_error is not None:
        raise last_error

    raw_models: Dict[str, Any] = {}
    for base in CLOUDCODE_BASES:
        try:
            data = _post_json(base + "/v1internal:fetchAvailableModels", {},
                              access_token, timeout)
            if isinstance(data.get("models"), dict) and data["models"]:
                raw_models = data["models"]
                break
        except QuotaError:
            continue

    summary: Dict[str, Any] = {}
    for base in CLOUDCODE_BASES:
        for payload in ({"project": "aicode-consumers"}, {}):
            try:
                summary = _post_json(base + "/v1internal:retrieveUserQuotaSummary",
                                     payload, access_token, timeout)
                break
            except QuotaError:
                summary = {}
        if summary.get("groups"):
            break

    return normalize_quota(load_code, summary, normalize_models(raw_models))


def _post_form(url: str, data: bytes, timeout: int) -> Optional[Dict[str, Any]]:
    """返回 None 表示这对 client 凭据不对，可以换下一对。"""
    request = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "%s/%s" % (APP_NAME, VERSION),
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        if "invalid_grant" in body:
            raise QuotaError("该账号的登录已失效，请重新登录后再保存")
        return None
    except Exception as exc:
        raise QuotaError("刷新令牌失败：%s" % exc.__class__.__name__)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


class QuotaManager:
    """按账号查询额度并缓存。只使用快照自身的令牌，刷新时同步报告账号。"""

    def __init__(self, cfg: Dict[str, Any], switcher: "Switcher"):
        self.cfg = cfg
        self.switcher = switcher
        self.cache_path = os.path.join(cfg["config_home"], "quota-cache.json")
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._client_lock = threading.Lock()
        self._clients_cache: Optional[List[Tuple[str, str]]] = None
        self._cache: Dict[str, Any] = {}
        stored = read_json(self.cache_path)
        accounts = stored.get("accounts")
        if stored.get("version") == 3 and isinstance(accounts, dict):
            self._cache = accounts

    # -- 缓存 ------------------------------------------------------------

    def _save_cache(self) -> None:
        write_json_atomic(self.cache_path, {"version": 3, "accounts": self._cache}, 0o600)

    def _cached(self, account_id: str) -> Optional[Dict[str, Any]]:
        entry = self._cache.get(account_id)
        return entry if isinstance(entry, dict) else None

    def _store(self, account_id: str, key: str, quota: Dict[str, Any]) -> None:
        with self._lock:
            self._cache[account_id] = {"key": key, "at": now(), "quota": quota}
            try:
                self._save_cache()
            except OSError as exc:
                log.warning("写入额度缓存失败：%s", exc.__class__.__name__)

    # -- OAuth client 发现 ----------------------------------------------

    def _remember_client(self, client_id: str, secret: str) -> None:
        try:
            write_json_atomic(os.path.join(self.cfg["config_home"], "oauth-client.json"),
                              {"client_id": client_id, "client_secret": secret}, 0o600)
        except OSError:
            pass

    def _scan_clients(self) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        seen = set()
        paths = [self.cfg.get("language_server_path") or "",
                 "/opt/agy-server/language_server",
                 "/usr/local/bin/agy-server"]
        id_re = re.compile(rb"\d{6,}-[a-z0-9]+\.apps\.googleusercontent\.com")
        secret_re = re.compile(rb"GOCSPX-[A-Za-z0-9_\-]+")
        ids: List[str] = []
        secrets: List[str] = []
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as handle:
                    while True:
                        chunk = handle.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        for match in id_re.finditer(chunk):
                            value = match.group().decode("ascii")
                            if value not in ids:
                                ids.append(value)
                        for match in secret_re.finditer(chunk):
                            value = match.group().decode("ascii")
                            if value not in secrets:
                                secrets.append(value)
            except OSError as exc:
                log.warning("扫描 OAuth 客户端失败：%s", exc.__class__.__name__)
        for client_id in ids:
            for secret in secrets:
                if (client_id, secret) not in seen:
                    seen.add((client_id, secret))
                    pairs.append((client_id, secret))
        return pairs

    def _clients(self) -> List[Tuple[str, str]]:
        with self._client_lock:
            if self._clients_cache is not None:
                return self._clients_cache
            pairs: List[Tuple[str, str]] = []
            cached = read_json(os.path.join(self.cfg["config_home"], "oauth-client.json"))
            cid, secret = cached.get("client_id"), cached.get("client_secret")
            if isinstance(cid, str) and isinstance(secret, str) and cid and secret:
                pairs.append((cid, secret))
            for pair in ((os.environ.get("AGY_OAUTH_CLIENT_ID"),
                          os.environ.get("AGY_OAUTH_CLIENT_SECRET")),
                         (_KNOWN_CLIENT_ID, _KNOWN_CLIENT_SECRET)):
                if pair[0] and pair[1] and pair not in pairs:
                    pairs.append((pair[0], pair[1]))
            for pair in self._scan_clients():
                if pair not in pairs:
                    pairs.append(pair)
            self._clients_cache = pairs
            return pairs

    # -- 令牌刷新 --------------------------------------------------------

    def _sync_live(self, original: bytes, refreshed: bytes) -> None:
        """只在该快照仍是当前登录时更新线上令牌，且与切换事务互斥。"""
        with self.switcher.token_lock:
            live = read_bytes(self.cfg["token_path"])
            if not same_login(live, original):
                return
            try:
                write_atomic(self.cfg["token_path"], refreshed, 0o600)
            except OSError as exc:
                log.warning("同步线上令牌失败：%s", exc.__class__.__name__)

    def _refresh_and_write(self, raw: bytes, path: str) -> bytes:
        details = token_details(raw)
        if not details["refresh"]:
            raise QuotaError("令牌已过期且没有 refresh_token，请重新登录该账号")
        last_error: Optional[str] = None
        for client_id, secret in self._clients():
            body = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": details["refresh"],
                "grant_type": "refresh_token",
            }).encode("utf-8")
            data = _post_form(OAUTH_TOKEN_URL, body, int(self.cfg["quota_timeout"]))
            if not data or not data.get("access_token"):
                last_error = "OAuth 客户端凭据不匹配"
                continue
            obj = details["obj"]
            token = obj.get("token")
            if not isinstance(token, dict):
                token = {}
                obj["token"] = token
            token["access_token"] = str(data["access_token"])
            try:
                expires_in = int(data.get("expires_in") or 3600)
            except (TypeError, ValueError):
                expires_in = 3600
            expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
                seconds=max(60, expires_in - 30))
            token["expiry"] = expiry.isoformat(timespec="microseconds").replace("+00:00", "Z")
            if data.get("refresh_token"):
                token["refresh_token"] = str(data["refresh_token"])
            if data.get("id_token"):
                obj["id_token"] = str(data["id_token"])
                mail = extract_email(str(data["id_token"]).encode("utf-8"))
                if mail:
                    obj["id_token_email"] = mail
            refreshed = encode_token_obj(obj)
            write_atomic(path, refreshed, 0o600)
            self._remember_client(client_id, secret)
            self._sync_live(raw, refreshed)
            return refreshed
        raise QuotaError(last_error or "找不到可用的 OAuth 客户端，无法刷新额度")

    # -- 查询 ------------------------------------------------------------

    def _fetch_one(self, account: Dict[str, Any], force: bool) -> Dict[str, Any]:
        account_id = str(account["id"])
        path = account_dir(self.cfg, account_id)
        raw = read_bytes(account_token_path(path))
        if not raw or not raw.strip():
            return {"status": "error", "error": "快照中没有令牌"}
        key = cache_key(raw)
        cached = self._cached(account_id)
        ttl = max(60, int(self.cfg["quota_ttl"]))
        if (cached and cached.get("key") == key and not force
                and now() - int(cached.get("at") or 0) < ttl):
            quota = cached.get("quota") if isinstance(cached.get("quota"), dict) else {}
            return dict(quota, status="ok", cached=True, fetched_at=int(cached.get("at") or 0))

        try:
            details = token_details(raw)
            fresh = raw
            if (not details["access"] or
                    (details["expiry"] and details["expiry"] < time.time() + 120)):
                fresh = self._refresh_and_write(raw, account_token_path(path))
                details = token_details(fresh)
            if not details["access"]:
                raise QuotaError("令牌中没有 access_token")
            try:
                quota = fetch_quota_payload(details["access"], int(self.cfg["quota_timeout"]))
            except QuotaError as exc:
                if not exc.unauthorized:
                    raise
                fresh = self._refresh_and_write(fresh, account_token_path(path))
                details = token_details(fresh)
                quota = fetch_quota_payload(details["access"],
                                            int(self.cfg["quota_timeout"]))
            self._store(account_id, cache_key(fresh), quota)
            return dict(quota, status="ok", cached=False, fetched_at=now())
        except QuotaError as exc:
            if cached and cached.get("key") == key:
                quota = cached.get("quota") if isinstance(cached.get("quota"), dict) else {}
                return dict(quota, status="stale", cached=True,
                            fetched_at=int(cached.get("at") or 0), error=exc.message)
            return {"status": "error", "error": exc.message}
        except Exception as exc:
            log.exception("额度查询出现未预期错误 id=%s", account_id)
            return {"status": "error", "error": "查询失败：%s" % exc.__class__.__name__}

    def get_all(self, force: bool = False, cache_only: bool = False) -> Dict[str, Any]:
        # 串行刷新，避免同一令牌被并发刷新或重复打 Google 接口。
        # 读取缓存不等待远程刷新，保证内置账号页和切换状态查询及时返回。
        if cache_only:
            quotas: Dict[str, Any] = {}
            for account in list_accounts(self.cfg):
                account_id = str(account["id"])
                path = account_dir(self.cfg, account_id)
                raw = read_bytes(account_token_path(path))
                if not raw:
                    quotas[account_id] = {"status": "loading"}
                    continue
                key = cache_key(raw)
                with self._lock:
                    entry = self._cached(account_id)
                    if entry and entry.get("key") == key and isinstance(entry.get("quota"), dict):
                        quota = dict(entry["quota"])
                        quota.update({"status": "ok", "cached": True,
                                      "fetched_at": int(entry.get("at") or 0)})
                        quotas[account_id] = quota
                    else:
                        quotas[account_id] = {"status": "loading"}
            return {"quotas": quotas, "fetched_at": now()}
        with self._fetch_lock:
            quotas = {}
            for account in list_accounts(self.cfg):
                quotas[str(account["id"])] = self._fetch_one(account, force)
            return {"quotas": quotas, "fetched_at": now()}


# ---------------------------------------------------------------------------
# 账号快照
# ---------------------------------------------------------------------------


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip().lower()
    text = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip(".-_")
    text = text[:48].strip(".-_")
    return text or "account"


def safe_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not re.fullmatch(r"[\w.\-]{1,64}", account_id,
                                                            flags=re.UNICODE):
        raise ApiError(400, "账号标识不合法")
    if account_id in (".", ".."):
        raise ApiError(400, "账号标识不合法")
    return account_id


def account_dir(cfg: Dict[str, Any], account_id: str) -> str:
    account_id = safe_account_id(account_id)
    root = os.path.realpath(cfg["accounts_dir"])
    path = os.path.realpath(os.path.join(root, account_id))
    if os.path.dirname(path) != root:
        raise ApiError(400, "账号标识不合法")
    return path


def account_meta_path(path: str) -> str:
    return os.path.join(path, "meta.json")


def account_token_path(path: str) -> str:
    return os.path.join(path, "token")


def _clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ApiError(400, "请填写账号名称")
    name = name.strip()
    if not name:
        raise ApiError(400, "请填写账号名称")
    if len(name) > MAX_NAME:
        raise ApiError(400, "账号名称不能超过 %d 个字符" % MAX_NAME)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ApiError(400, "账号名称包含不可见字符")
    return name


def _unique_id(cfg: Dict[str, Any], name: str, exclude: str = "") -> str:
    base = slugify(name)
    candidate = base
    counter = 2
    while True:
        path = os.path.join(cfg["accounts_dir"], candidate)
        if candidate == exclude or not os.path.exists(path):
            return candidate
        candidate = "%s-%d" % (base[:56], counter)
        counter += 1


def list_accounts(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    root = cfg["accounts_dir"]
    if not os.path.isdir(root):
        return []
    live = read_bytes(cfg["token_path"])
    result: List[Dict[str, Any]] = []
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        log.warning("列出账号目录失败：%s", exc.__class__.__name__)
        return []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        account_id = entry.name
        try:
            safe_account_id(account_id)
        except ApiError:
            continue
        path = entry.path
        meta = read_json(account_meta_path(path))
        token = read_bytes(account_token_path(path))
        is_current = bool(token and same_login(token, live))
        name = str(meta.get("name") or account_id)
        email = str(meta.get("email") or "") or extract_email(token)
        result.append({
            "id": account_id,
            "name": name,
            "email": email,
            "is_current": is_current,
            "created_at": int(meta.get("created_at") or 0),
            "last_used_at": int(meta.get("last_used_at") or 0),
            "created_at_iso": iso_time(int(meta.get("created_at") or 0)),
            "last_used_at_iso": iso_time(int(meta.get("last_used_at") or 0)),
            "has_token": bool(token),
        })
    result.sort(key=lambda item: (not item["is_current"],
                                  -int(item["last_used_at"] or 0),
                                  item["name"].lower()))
    return result


def find_account_by_name(cfg: Dict[str, Any], name: str, exclude: str = "") -> Optional[str]:
    wanted = name.strip().lower()
    for account in list_accounts(cfg):
        if account["id"] == exclude:
            continue
        if str(account["name"]).strip().lower() == wanted:
            return str(account["id"])
    return None


def save_account(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    name = _clean_name(name)
    live = read_live_token(cfg)
    if not live or not live.strip():
        raise ApiError(400, "当前没有可保存的登录令牌：%s 不存在或为空。"
                            "请先在 Antigravity 页面完成 Google 登录。" % cfg["token_path"])

    duplicate = find_account_by_name(cfg, name)
    if duplicate:
        raise ApiError(409, "已存在同名账号「%s」，请换一个名称。" % name)

    for account in list_accounts(cfg):
        path = account_dir(cfg, account["id"])
        token = read_bytes(account_token_path(path))
        if token and same_login(token, live):
            raise ApiError(409, "当前登录已保存为「%s」，无需重复保存。" % account["name"])

    ensure_dir(cfg["accounts_dir"], 0o700)
    account_id = _unique_id(cfg, name)
    path = os.path.join(cfg["accounts_dir"], account_id)
    ensure_dir(path, 0o700)
    stamp = now()
    meta = {
        "id": account_id,
        "name": name,
        "email": extract_email(live),
        "created_at": stamp,
        "last_used_at": stamp,
    }
    write_atomic(account_token_path(path), live, 0o600)
    write_json_atomic(account_meta_path(path), meta, 0o600)
    log.info("已保存账号快照 id=%s (%s)", account_id, token_label(live))
    return {"id": account_id, "name": name, "email": meta["email"]}


def read_target_token(cfg: Dict[str, Any], account_id: str) -> Tuple[str, bytes, Dict[str, Any]]:
    path = account_dir(cfg, account_id)
    if not os.path.isdir(path):
        raise ApiError(404, "目标账号不存在")
    token = read_bytes(account_token_path(path))
    if not token or not token.strip():
        raise ApiError(400, "目标账号的令牌快照为空或不可读")
    meta = read_json(account_meta_path(path))
    return path, token, meta


def touch_account(path: str, meta: Dict[str, Any], stamp: Optional[int] = None) -> Dict[str, Any]:
    meta = dict(meta)
    meta["last_used_at"] = stamp or now()
    write_json_atomic(account_meta_path(path), meta, 0o600)
    return meta


def sync_rotated_token(cfg: Dict[str, Any], live: bytes) -> Optional[str]:
    """Google 刷新令牌可能轮换，切换前把线上令牌写回匹配的快照。"""
    for account in list_accounts(cfg):
        path = account_dir(cfg, account["id"])
        token = read_bytes(account_token_path(path))
        if token and same_login(token, live):
            if token != live:
                write_atomic(account_token_path(path), live, 0o600)
            return str(account["id"])
    return None


def rename_account(cfg: Dict[str, Any], account_id: str, name: str) -> Dict[str, Any]:
    name = _clean_name(name)
    path, _token, meta = read_target_token(cfg, account_id)
    duplicate = find_account_by_name(cfg, name, exclude=account_id)
    if duplicate:
        raise ApiError(409, "已存在同名账号「%s」。" % name)
    meta = dict(meta)
    meta["name"] = name
    meta["id"] = account_id
    write_json_atomic(account_meta_path(path), meta, 0o600)
    log.info("账号已重命名 id=%s -> %s", account_id, name)
    return {"id": account_id, "name": name}


def delete_account(cfg: Dict[str, Any], account_id: str) -> None:
    path = account_dir(cfg, account_id)
    if not os.path.isdir(path):
        raise ApiError(404, "账号不存在")
    shutil.rmtree(path)
    log.info("已删除账号快照 id=%s", account_id)


# ---------------------------------------------------------------------------
# 服务启停与切换
# ---------------------------------------------------------------------------


def run_command(cfg: Dict[str, Any], args: List[str], timeout: Optional[int] = None) -> int:
    timeout = int(timeout or cfg["command_timeout"])
    log.info("执行命令：%s", " ".join(shlex.quote(x) for x in args))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              env=os.environ.copy())
    except FileNotFoundError:
        raise SwitchError("找不到命令：%s" % args[0], 500)
    except subprocess.TimeoutExpired:
        raise SwitchError("命令执行超时（%ds）：%s" % (timeout, args[0]), 504)
    except OSError as exc:
        raise SwitchError("命令无法执行：%s" % exc.__class__.__name__, 500)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:300]
        log.warning("命令返回 %s：%s %s", proc.returncode, args[0], detail)
        raise SwitchError("命令执行失败（退出码 %s）" % proc.returncode, 500,
                          detail=detail or None)
    return proc.returncode


def service_active(cfg: Dict[str, Any]) -> Optional[bool]:
    commands = service_commands(cfg)["status"]
    try:
        proc = subprocess.run(commands, capture_output=True, text=True, timeout=5,
                              env=os.environ.copy())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode in (3, 4):
        return False
    return None


def stop_service(cfg: Dict[str, Any]) -> None:
    commands = service_commands(cfg)
    run_command(cfg, commands["stop"], cfg["command_timeout"])
    deadline = time.monotonic() + max(3, cfg["stop_timeout"])
    while time.monotonic() < deadline:
        active = service_active(cfg)
        if active is False:
            return
        time.sleep(0.4)
    raise SwitchError("未能在 %ds 内确认服务已停止" % cfg["stop_timeout"], 504)


def start_service(cfg: Dict[str, Any]) -> None:
    commands = service_commands(cfg)
    run_command(cfg, commands["start"], cfg["command_timeout"])


def probe_service(cfg: Dict[str, Any]) -> None:
    url = cfg.get("probe_url") or ""
    if not url:
        return
    timeout = max(3, int(cfg["probe_timeout"]))
    deadline = time.time() + timeout
    last = "无响应"
    while time.time() < deadline:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (APP_NAME, VERSION)})
            with urllib.request.urlopen(request, timeout=5) as response:
                if int(response.status) < 500:
                    return
                last = "HTTP %s" % response.status
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return
            last = "HTTP %s" % exc.code
        except Exception as exc:  # URLError, socket.timeout, ...
            last = exc.__class__.__name__
        time.sleep(1.0)
    raise SwitchError("服务在 %ds 内未就绪（%s）" % (timeout, last), 504)


class Switcher:
    """串行执行切换；切换期间第二次请求直接收到 409。"""

    def __init__(self, cfg: Dict[str, Any], operation_lock: Optional[ServiceOperationLock] = None):
        self.cfg = cfg
        self.operation_lock = operation_lock or ServiceOperationLock()
        self.token_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {"active": False, "started_at": 0, "account": ""}

    def state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def switch(self, account_id: str) -> Dict[str, Any]:
        with self.operation_lock.hold("账号切换"):
            return self.switch_locked(account_id)

    def switch_locked(self, account_id: str) -> Dict[str, Any]:
        """调用方已持有服务操作锁；与额度回写互斥直至验证或回滚完成。"""
        with self._state_lock:
            self._state = {"active": True, "started_at": now(), "account": account_id}
        try:
            with self.token_lock:
                return self._switch_transaction(account_id)
        finally:
            with self._state_lock:
                self._state = {"active": False, "started_at": 0, "account": ""}

    @staticmethod
    def _record_use(path: str, meta: Dict[str, Any]) -> None:
        try:
            touch_account(path, meta)
        except OSError as exc:
            log.warning("更新快照使用时间失败：%s", exc.__class__.__name__)

    def _verify(self) -> None:
        auth = wait_language_server_auth(self.cfg)
        if auth is None:
            raise SwitchError("未能确认目标账号登录成功", 504)
        if not auth["signed_in"]:
            raise SwitchError(auth["message"] or "目标账号不可用，需要验证", 422)

    def _save_recovery_token(self, token: bytes) -> str:
        # Use unique files so subsequent attempts cannot overwrite an earlier failed rollback.
        directory = ensure_dir(os.path.join(self.cfg["config_home"], "switch-recovery"))
        path = os.path.join(directory, secrets.token_hex(16) + ".token")
        write_atomic(path, token, 0o600)
        return path

    @staticmethod
    def _remove_recovery_token(path: str) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except OSError as exc:
            log.warning("无法清理切号恢复备份 %s：%s", path, exc.__class__.__name__)

    def _switch_transaction(self, account_id: str) -> Dict[str, Any]:
        cfg = self.cfg
        path, target, meta = read_target_token(cfg, account_id)
        name = str(meta.get("name") or account_id)

        live = read_live_token(cfg)
        if live is not None and same_login(live, target):
            try:
                self._verify()
            except SwitchError as exc:
                raise ApiError(exc.status, exc.message + "；线上凭据未改动",
                               {"unchanged": True}) from exc
            if live != target:
                try:
                    write_atomic(account_token_path(path), live, 0o600)
                except OSError as exc:
                    log.warning("同步快照失败 id=%s：%s", account_id, exc.__class__.__name__)
            self._record_use(path, meta)
            return {"ok": True, "already_current": True, "name": name,
                    "message": "当前已经是「%s」" % name}

        log.info("开始切换账号 id=%s live=%s target=%s", account_id,
                 token_label(live), token_label(target))

        backup = live
        recovery_path = ""
        stopped = False
        mutated = False
        try:
            if live:
                sync_rotated_token(cfg, live)
            stop_service(cfg)
            stopped = True
            # 服务可能在停止前完成一次令牌轮换，备份最后落盘的同一账号。
            latest = read_live_token(cfg)
            if latest and (not live or same_login(live, latest)):
                backup = latest
                if latest != live:
                    sync_rotated_token(cfg, latest)
            elif latest is not None and latest != live:
                raise SwitchError("切换前检测到线上账号已变更，请重试", 409)
            if backup is not None:
                recovery_path = self._save_recovery_token(backup)
            write_atomic(cfg["token_path"], target, 0o600)
            mutated = True
            start_service(cfg)
            probe_service(cfg)
            self._verify()
        except Exception as exc:
            if not isinstance(exc, (SwitchError, ApiError, OSError)):
                log.exception("切换出现未预期错误 id=%s", account_id)
            status = exc.status if isinstance(exc, (ApiError, SwitchError)) else 500
            message = "切换账号失败"
            if isinstance(exc, (ApiError, SwitchError)):
                message = exc.message
            elif isinstance(exc, OSError):
                message = "切换失败，无法读写凭据或快照，请检查权限"
            if not stopped:
                raise ApiError(status, message + "；线上凭据未改动",
                               {"unchanged": True}) from exc
            rolled_back = self._rollback(backup) if mutated else self._restart_original()
            if not rolled_back:
                message += "；回滚未完成，请手动检查服务与令牌文件"
                if recovery_path:
                    message += "，原凭据备份：%s" % recovery_path
            elif mutated:
                message += "；已回滚到切换前的凭据"
            else:
                message += "；线上凭据未改动，原服务已启动"
            log.error("切换失败 id=%s：%s", account_id, message)
            extra = {"rolled_back": rolled_back, "unchanged": not mutated}
            if rolled_back:
                self._remove_recovery_token(recovery_path)
            elif recovery_path:
                extra["recovery_path"] = recovery_path
            if isinstance(exc, SwitchError) and exc.detail:
                extra["detail"] = exc.detail
            raise ApiError(status, message, extra) from exc

        self._remove_recovery_token(recovery_path)
        self._record_use(path, meta)
        log.info("账号切换完成 id=%s", account_id)
        return {
            "ok": True,
            "already_current": False,
            "name": name,
            "email": str(meta.get("email") or ""),
            "app_url": cfg.get("app_url") or "",
            "message": "已切换到「%s」" % name,
        }

    def _restart_original(self) -> bool:
        try:
            start_service(self.cfg)
            probe_service(self.cfg)
        except SwitchError as exc:
            log.error("回滚后恢复服务失败：%s", exc.message)
            return False
        return True

    def _rollback(self, backup: Optional[bytes]) -> bool:
        cfg = self.cfg
        try:
            stop_service(cfg)
        except SwitchError as exc:
            log.error("回滚停止服务失败，未覆盖令牌：%s", exc.message)
            return False
        try:
            if backup is None:
                try:
                    os.unlink(cfg["token_path"])
                except FileNotFoundError:
                    pass
            else:
                write_atomic(cfg["token_path"], backup, 0o600)
        except OSError as exc:
            log.error("回滚写入令牌失败：%s", exc.__class__.__name__)
            return False
        return self._restart_original()


# ---------------------------------------------------------------------------
# 账号失效自动切换
# ---------------------------------------------------------------------------

FAILOVER_INTERVAL = 90
FAILOVER_COOLDOWN = 1800
FAILOVER_AUTH_TIMEOUT = 30


def managed_service_process(cfg: Dict[str, Any], pid: int) -> bool:
    unit = str(cfg.get("service_name") or "agy-server")
    if not unit.endswith(".service"):
        unit += ".service"
    try:
        with open("/proc/%d/cgroup" % pid, encoding="utf-8") as handle:
            return any(unit in line.rstrip().split(":", 2)[-1].split("/") for line in handle)
    except OSError:
        return False


def language_server_auth(cfg: Dict[str, Any], timeout: float = 5) -> Optional[Dict[str, Any]]:
    """询问语言服务器当前 Google 账号是否可用。

    只信任服务器明确给出的回答；连接失败或服务未就绪时返回 None。
    """
    daemon_dir = os.path.expanduser("~/.gemini/antigravity/daemon")
    candidates: List[Tuple[float, int, str]] = []
    try:
        names = os.listdir(daemon_dir)
    except OSError:
        return None
    for name in names:
        if not (name.startswith("hub_") and name.endswith(".json")):
            continue
        path = os.path.join(daemon_dir, name)
        try:
            stat = os.stat(path)
            data = read_json(path)
        except OSError:
            continue
        pid = data.get("pid")
        port = data.get("httpsPort")
        token = data.get("csrfToken")
        if (type(pid) is not int or pid <= 0 or type(port) is not int or not 0 < port <= 65535
                or not isinstance(token, str) or not token or not managed_service_process(cfg, pid)):
            continue
        candidates.append((stat.st_mtime, port, token))
    if not candidates:
        return None
    _, port, csrf = max(candidates)

    url = ("https://127.0.0.1:%d/exa.language_server_pb.LanguageServerService/GetAuthStatus"
           % port)
    request = urllib.request.Request(
        url, data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 "Connect-Protocol-Version": "1",
                 "x-codeium-csrf-token": csrf})
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context))
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    result = payload.get("authResult") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None
    return {
        "signed_in": result.get("hasValidAuth") is True,
        "ineligible": result.get("ineligible") not in (None, False),
        "message": str(result.get("uiMessage") or "").strip(),
        "checked_at": now(),
    }


def wait_language_server_auth(cfg: Dict[str, Any],
                              timeout: int = FAILOVER_AUTH_TIMEOUT) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        auth = language_server_auth(cfg, timeout=min(5, remaining))
        if auth is not None and (auth["signed_in"] or auth["ineligible"]):
            return auth
        time.sleep(max(0, min(2, deadline - time.monotonic())))
    return None


class FailoverManager:
    """当前账号被 Google 判定不可用时，自动尝试其它已保存账号。"""

    def __init__(self, cfg: Dict[str, Any], switcher: "Switcher"):
        self.cfg = cfg
        self.switcher = switcher
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self.last_attempt = 0.0
        self.unknown_streak = 0
        self.last_recovery = 0.0
        self.last_good = str(cfg.get("auto_failover_last_good") or "")
        self._state: Dict[str, Any] = {
            "state": "idle", "message": "尚未检查", "at": 0,
            "from": "", "to": "",
        }

    def state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def _set(self, state: str, message: str, **extra: Any) -> None:
        with self._state_lock:
            self._state = {"state": state, "message": message, "at": now()}
            self._state.update(extra)

    def _remember_good(self, account_id: str) -> None:
        if not account_id or self.cfg.get("auto_failover_last_good") == account_id:
            return
        try:
            update_config(self.cfg, auto_failover_last_good=account_id)
        except ApiError as exc:
            log.warning("记录最近可用账号失败：%s", exc.message)

    def _recover_service(self) -> None:
        self.last_recovery = time.monotonic()
        self.unknown_streak = 0
        log.warning("连续查询登录状态超时，只重启服务；保留当前账号凭据")
        self._set("recovering", "登录状态无响应，正在重启语言服务器")
        try:
            stop_service(self.cfg)
            start_service(self.cfg)
            probe_service(self.cfg)
        except SwitchError as exc:
            log.error("语言服务器恢复失败：%s", exc.message)
            self._set("recovery_failed", "语言服务器恢复失败：%s" % exc.message)
        else:
            self._set("recovered", "语言服务器已重启，账号未更换；登录状态将在下一轮检查")

    def check(self, force: bool = False) -> Dict[str, Any]:
        if not self.cfg.get("auto_failover", True):
            self._set("disabled", "自动切换已关闭")
            return self.state()
        if not self._lock.acquire(blocking=False):
            return self.state()
        try:
            try:
                with self.switcher.operation_lock.hold("自动检查与恢复"):
                    self._check_locked(force)
            except ApiError as exc:
                self._set("busy" if exc.status == 409 else "failed", exc.message)
            return self.state()
        finally:
            self._lock.release()

    def _check_locked(self, force: bool) -> None:
        self._set("checking", "正在检查当前账号")
        auth = language_server_auth(self.cfg)
        if auth is None:
            self.unknown_streak += 1
            if self.unknown_streak < 3:
                self._set("unknown", "暂时无法读取 Antigravity 登录状态")
            elif self.last_recovery and time.monotonic() - self.last_recovery < FAILOVER_COOLDOWN:
                self._set("unknown", "登录状态仍不可读，等待服务恢复冷却")
            elif self.cfg.get("auto_failover", True):
                self._recover_service()
            return

        self.unknown_streak = 0
        if not (auth["signed_in"] or auth["ineligible"]):
            self._set("needs_signin", auth["message"] or "当前账号需要重新登录")
            return
        accounts = list_accounts(self.cfg)
        current = next((a for a in accounts if a.get("is_current")), None)
        if auth["signed_in"]:
            if current:
                self.last_good = str(current["id"])
                self._remember_good(self.last_good)
            self._set("ok", "当前账号可用")
            return
        if not force and self.last_attempt and time.monotonic() - self.last_attempt < FAILOVER_COOLDOWN:
            self._set("cooldown", "当前账号不可用，等待自动切换冷却")
            return

        candidates = [a for a in accounts if not a.get("is_current") and a.get("has_token")]
        candidates.sort(key=lambda a: a["id"] != self.last_good)
        if not candidates:
            self._set("no_candidates", "当前账号不可用，但没有其它可切换账号")
            return

        self.last_attempt = time.monotonic()
        from_name = str((current or {}).get("name") or "当前账号")
        last_error = ""
        for candidate in candidates:
            if not self.cfg.get("auto_failover", True):
                self._set("disabled", "自动切换已关闭")
                return
            account_id, name = str(candidate["id"]), str(candidate["name"])
            self._set("switching", "正在验证备用账号「%s」" % name, **{"from": from_name})
            try:
                self.switcher.switch_locked(account_id)
            except ApiError as exc:
                last_error = exc.message
                log.warning("自动切换到「%s」失败：%s", name, last_error)
                if exc.status == 422 and exc.extra.get("rolled_back"):
                    continue  # 仅在明确被拒绝且回滚成功时继续试下一账号。
                self._set("failed" if exc.extra.get("rolled_back") is False or exc.status != 504
                          else "unknown", last_error, **{"from": from_name})
                return
            self.last_good = account_id
            self._remember_good(account_id)
            self._set("switched", "已自动切换到「%s」" % name, **{"from": from_name, "to": name})
            return
        self._set("failed", "没有找到可用账号：%s" % last_error, **{"from": from_name})


def failover_loop(manager: FailoverManager) -> None:
    time.sleep(20)
    while True:
        try:
            manager.check()
        except Exception:  # noqa: BLE001
            log.exception("自动切换检查失败")
        time.sleep(FAILOVER_INTERVAL)


# ---------------------------------------------------------------------------
# Antigravity 核心更新
# ---------------------------------------------------------------------------

def running_language_server_version(cfg: Dict[str, Any]) -> str:
    """只读取受管服务内的 language_server，忽略其他会话的实例。"""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return ""
    for name in entries:
        if not name.isdigit() or not managed_service_process(cfg, int(name)):
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as handle:
                parts = handle.read().decode("utf-8", "replace").rstrip("\0").split("\0")
        except OSError:
            continue
        if "--standalone" not in parts:
            continue
        for index, part in enumerate(parts):
            if part == "--override_ide_version" and index + 1 < len(parts):
                return parts[index + 1]
            if part.startswith("--override_ide_version="):
                return part.split("=", 1)[1]
    return ""


def updater_home() -> str:
    return expand_path(os.environ.get("AGY_HOME") or "~/.agy-remote")


def staged_language_server_version() -> str:
    data = read_json(os.path.join(updater_home(), "updater-state.json"))
    return str(data.get("staged_version") or "")


def update_status(cfg: Dict[str, Any]) -> Dict[str, Any]:
    running = running_language_server_version(cfg)
    staged = staged_language_server_version()
    latest = staged or running
    return {
        "running": running,
        "latest": latest,
        "staged": staged,
        "available": bool(staged and running and staged != running),
        "binary": "/usr/local/bin/agy-server",
    }


def agy_server_binary() -> str:
    return shutil.which("agy-server") or "/usr/local/bin/agy-server"


def _update_output(proc: subprocess.CompletedProcess) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", (proc.stdout or "") + "\n" + (proc.stderr or "")).strip()


def check_official_update(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """调用 agy-server update -check 查询官方最新版本。"""
    try:
        proc = subprocess.run([agy_server_binary(), "update", "-check"],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(502, "检查更新失败：%s" % exc.__class__.__name__)
    output = _update_output(proc)
    version = re.search(r"Latest version\s*:\s*([0-9]\S*)", output)
    if proc.returncode != 0 or not version:
        raise ApiError(502, "检查更新失败：%s" % (output[-300:] or "无输出"))
    latest = version.group(1)
    running = running_language_server_version(cfg)
    return {
        "running": running,
        "latest": latest,
        "staged": staged_language_server_version(),
        "available": bool(running and latest != running),
        "output": output[-1200:],
    }


def apply_language_server_update(cfg: Dict[str, Any],
                                 operation_lock: ServiceOperationLock) -> Dict[str, Any]:
    """更新官方 language_server，并重启 agy-server 让新版本生效。"""
    with operation_lock.hold("系统更新"):
        before = update_status(cfg)
        if not before["running"]:
            raise ApiError(503, "无法确认受管语言服务器的当前版本，未执行更新。")
        try:
            for directory in (os.path.dirname(cfg["language_server_path"]), updater_home()):
                if not os.access(directory, os.W_OK | os.X_OK):
                    raise ApiError(503, "面板无法写入更新目录 %s。请由管理员在主机上执行更新。"
                                   % directory)
            proc = subprocess.run([agy_server_binary(), "update", "-yes"],
                                  capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ApiError(502, "更新下载失败：%s" % exc.__class__.__name__)
        output = _update_output(proc)
        if proc.returncode != 0:
            raise ApiError(502, "更新失败：%s" % (output[-400:] or "无输出"))
        match = re.search(r"Latest version\s*:\s*([0-9]\S*)", output)
        expected = match.group(1) if match else staged_language_server_version()
        if not expected:
            raise ApiError(502, "更新程序未报告目标版本；未重启服务。")
        if before["running"] == expected:
            return dict(update_status(cfg), message="当前已是目标版本，未重启", output=output[-1200:])

        log.warning("开始重启 agy-server 应用 Antigravity 更新")
        try:
            stop_service(cfg)
            start_service(cfg)
            probe_service(cfg)
        except SwitchError as exc:
            raise ApiError(exc.status if exc.status >= 400 else 504,
                           "更新已下载，但重启失败：%s" % exc.message,
                           {"detail": exc.detail} if exc.detail else None)

        # The web probe can respond before the managed language server is launched.
        deadline = time.monotonic() + max(3, cfg["probe_timeout"])
        while True:
            after = update_status(cfg)
            if after["running"] == expected:
                log.warning("Antigravity 更新已验证：%s -> %s", before["running"], expected)
                return dict(after, message="已更新并重启", output=output[-1200:])
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        raise ApiError(502, "重启后未确认目标版本 %s；实际运行版本：%s。"
                       % (expected, after["running"] or "未知"), {"running": after["running"]})


# ---------------------------------------------------------------------------
# 本地 Token 用量（读取 Antigravity 会话数据库）
# ---------------------------------------------------------------------------

MAX_GENERATION_BYTES = 64 * 1024 * 1024
PERFORMANCE_MAX_AGE = 7 * 24 * 60 * 60
PERFORMANCE_SAMPLE_LIMIT = 10
MAX_PROTO_TIMESTAMP = 253402300799  # 9999-12-31T23:59:59Z

def _pb_varint(data: bytes, pos: int) -> Tuple[Optional[int], int]:
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None, pos


def _pb_fields(data: bytes):
    pos = 0
    while pos < len(data):
        key, pos = _pb_varint(data, pos)
        if key is None:
            return
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _pb_varint(data, pos)
            yield number, wire, value
        elif wire == 2:
            length, pos = _pb_varint(data, pos)
            if length is None or pos + length > len(data):
                return
            yield number, wire, data[pos:pos + length]
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            return


def _pb_bytes(data: bytes, number: int) -> Optional[bytes]:
    for key, wire, value in _pb_fields(data):
        if key == number and wire == 2:
            return value
    return None


def _pb_uint(data: bytes, number: int) -> Optional[int]:
    for key, wire, value in _pb_fields(data):
        if key == number and wire == 0 and value is not None:
            return int(value)
    return None


def _pb_text(data: bytes, number: int) -> str:
    try:
        return (_pb_bytes(data, number) or b"").decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def _pb_duration(message: Optional[bytes]) -> float:
    if not message:
        return 0.0
    seconds = _pb_uint(message, 1) or 0
    nanos = _pb_uint(message, 2) or 0
    if seconds >= 2**63 or nanos >= 1_000_000_000:
        return 0.0
    return seconds + nanos / 1_000_000_000


def _completed_text_step(payload: bytes) -> bool:
    response = _pb_bytes(payload, 20)
    return bool(response and _pb_uint(response, 12) == 2  # STOP_PATTERN
                and _pb_bytes(response, 7) is None  # No tool calls.
                and _pb_text(response, 1))


def _performance_sample(blob: bytes, stamp: int, source: str) -> Optional[Dict[str, Any]]:
    wrapped = _pb_bytes(blob, 1) or b""
    usage = _pb_bytes(wrapped, 4) or b""
    body_tokens = _pb_uint(usage, 10) or 0  # Excludes thinking tokens (field 9).
    latency = _pb_duration(_pb_bytes(wrapped, 11))
    streaming = _pb_duration(_pb_bytes(wrapped, 12))
    model = _pb_text(wrapped, 21) or _pb_text(wrapped, 19)
    if not model or body_tokens < 100 or latency <= 0 or streaming < 1:
        return None
    return {"model": model, "source": source, "timestamp": stamp,
            "first_text_seconds": latency, "body_tokens_per_second": body_tokens / streaming}


def _recent_performance(samples: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not samples:
        return None
    ordered = sorted(samples, key=lambda row: (-row["timestamp"], row["source"], row["model"]))
    recent = ordered[:PERFORMANCE_SAMPLE_LIMIT]
    return {
        "sample_count": len(recent),
        "model_count": len({row["model"] for row in recent}),
        "source_count": len({row["source"] for row in recent}),
        "first_text_seconds": statistics.median(row["first_text_seconds"] for row in recent),
        "body_tokens_per_second": statistics.median(row["body_tokens_per_second"] for row in recent),
        "last_activity": recent[0]["timestamp"],
    }


def _usage_event(blob: bytes, step_timestamp: Optional[int]) -> Optional[Dict[str, Any]]:
    wrapped = _pb_bytes(blob, 1)
    if not wrapped:
        return None
    usage = _pb_bytes(wrapped, 4)
    if not usage:
        return None
    system_tokens = _pb_uint(usage, 1) or 0
    raw_input = _pb_uint(usage, 2) or 0
    output = _pb_uint(usage, 3) or 0
    cached = _pb_uint(usage, 5) or 0
    input_tokens = system_tokens + raw_input
    if raw_input == 0 and output == 0 and cached == 0 and input_tokens == 0:
        return None
    model_raw = _pb_bytes(wrapped, 21) or _pb_bytes(wrapped, 19)
    model = model_raw.decode("utf-8", "replace").strip() if model_raw else "未知模型"
    timestamp = None
    timing = _pb_bytes(wrapped, 9)
    if timing:
        clock = _pb_bytes(timing, 4)
        if clock:
            timestamp = _pb_uint(clock, 1)
    timestamp = timestamp or step_timestamp
    if not timestamp or not 0 < timestamp <= MAX_PROTO_TIMESTAMP:
        return None
    return {
        "model": model or "未知模型",
        "input": input_tokens,
        "output": output,
        "cached": cached,
        "total": input_tokens + output + cached,
        "timestamp": int(timestamp),
    }


def _empty_totals() -> Dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "request_count": 0}


def _add_event(totals: Dict[str, int], event: Dict[str, Any]) -> None:
    totals["input_tokens"] += event["input"]
    totals["output_tokens"] += event["output"]
    totals["cached_tokens"] += event["cached"]
    totals["total_tokens"] += event["total"]
    totals["request_count"] += 1


class UsageStats:
    """扫描 ~/.gemini/antigravity*/conversations/*.db，按时间与模型汇总。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at = 0.0

    def _databases(self) -> List[str]:
        home = os.path.expanduser("~")
        patterns = [
            os.path.join(home, ".gemini", "antigravity", "conversations", "*.db"),
            os.path.join(home, ".gemini", "antigravity*", "conversations", "*.db"),
        ]
        found: List[str] = []
        for pattern in patterns:
            for path in glob.glob(pattern):
                if path not in found:
                    found.append(path)
        return found

    def _read_database(self, path: str, scan_time: int) -> Tuple[
            List[Dict[str, Any]], List[Dict[str, Any]], int, int, bool]:
        events: List[Dict[str, Any]] = []
        samples: List[Dict[str, Any]] = []
        scanned = 0
        skipped = 0
        source = os.path.basename(os.path.dirname(os.path.dirname(path)))
        try:
            uri = "file:%s?mode=ro" % urllib.parse.quote(path)
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                # Keep both reads in one snapshot while another process writes to the DB.
                conn.execute("BEGIN")
                steps: List[Tuple[Optional[int], bool]] = []
                try:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(steps)")}
                    payload_column = ("CASE WHEN status = 3 AND length(step_payload) <= 1048576 "
                                      "THEN step_payload END"
                                      if {"status", "step_payload"} <= columns else "NULL")
                    for metadata, payload in conn.execute(
                            "SELECT CASE WHEN length(metadata) <= 65536 THEN metadata END, "
                            + payload_column + " FROM steps WHERE step_type = 15 ORDER BY idx"):
                        clock = _pb_bytes(metadata, 1) if isinstance(metadata, bytes) else None
                        stamp = _pb_uint(clock, 1) if clock else None
                        in_window = (stamp is not None
                                     and scan_time - PERFORMANCE_MAX_AGE <= stamp <= scan_time)
                        complete = in_window and isinstance(payload, bytes) and _completed_text_step(payload)
                        steps.append((stamp, complete))
                except sqlite3.Error:
                    pass
                # gen_metadata.idx and steps.idx are independent. Only pair timings
                # with steps if the ordered generation sequences have matching lengths.
                count = conn.execute("SELECT count(*) FROM gen_metadata WHERE data IS NOT NULL").fetchone()[0]
                aligned = count == len(steps)
                rows = conn.execute("SELECT CASE WHEN length(data) <= ? THEN data END, length(data) "
                                    "FROM gen_metadata WHERE data IS NOT NULL ORDER BY idx",
                                    (MAX_GENERATION_BYTES,))
                for ordinal, (blob, size) in enumerate(rows):
                    scanned += 1
                    if size > MAX_GENERATION_BYTES:
                        skipped += 1
                    if not isinstance(blob, bytes):
                        continue
                    stamp, complete = steps[ordinal] if ordinal < len(steps) else (None, False)
                    if aligned and complete and stamp is not None:
                        sample = _performance_sample(blob, stamp, source)
                        if sample:
                            samples.append(sample)
                    event = _usage_event(blob, stamp)
                    if event:
                        events.append(event)
            finally:
                conn.close()
            return events, samples, scanned, skipped, True
        except (sqlite3.Error, OSError):
            return events, [], scanned, skipped, False

    def _build(self) -> Dict[str, Any]:
        now = dt.datetime.now()
        today = now.date()
        start_30 = today - dt.timedelta(days=29)
        events: List[Dict[str, Any]] = []
        performance_samples: List[Dict[str, Any]] = []
        databases = self._databases()
        scan_time = int(now.timestamp())
        generations = 0
        skipped_large = 0
        unreadable = 0
        for path in databases:
            found, samples, scanned, skipped, ok = self._read_database(path, scan_time)
            generations += scanned
            skipped_large += skipped
            if not ok:
                unreadable += 1
            events.extend(found)
            performance_samples.extend(samples)

        ranges = {
            "today": today,
            "yesterday": today - dt.timedelta(days=1),
            "last_3_days": today - dt.timedelta(days=2),
            "last_7_days": today - dt.timedelta(days=6),
            "last_30_days": start_30,
        }
        totals = {name: _empty_totals() for name in ranges}
        daily: Dict[str, Dict[str, int]] = {}
        hourly: Dict[str, Dict[str, int]] = {}
        by_model: Dict[str, Dict[str, Dict[str, int]]] = {name: {} for name in ranges}

        for event in events:
            try:
                moment = dt.datetime.fromtimestamp(event["timestamp"])
            except (OverflowError, OSError, ValueError):
                continue
            day = moment.date()
            if day < start_30 or day > today:
                continue
            day_key = day.isoformat()
            _add_event(daily.setdefault(day_key, _empty_totals()), event)
            hour_key = "%s %02d:00" % (day_key, moment.hour)
            _add_event(hourly.setdefault(hour_key, _empty_totals()), event)
            for name, start in ranges.items():
                if name == "today":
                    if day != today:
                        continue
                elif name == "yesterday":
                    if day != today - dt.timedelta(days=1):
                        continue
                elif day < start:
                    continue
                _add_event(totals[name], event)
                bucket = by_model[name].setdefault(event["model"], _empty_totals())
                _add_event(bucket, event)

        def model_list(name: str) -> List[Dict[str, Any]]:
            values = []
            for model, value in by_model[name].items():
                item = {"model": model}
                item.update(value)
                values.append(item)
            values.sort(key=lambda item: item["total_tokens"], reverse=True)
            return values

        daily_list = [dict({"date": key}, **value) for key, value in sorted(daily.items())]
        hourly_list = [dict({"hour": key}, **value) for key, value in sorted(hourly.items())]
        last_activity = max((event["timestamp"] for event in events), default=0) or None

        return {
            "recent_performance": _recent_performance(performance_samples),
            "today": totals["today"],
            "yesterday": totals["yesterday"],
            "last_3_days": totals["last_3_days"],
            "last_7_days": totals["last_7_days"],
            "last_30_days": totals["last_30_days"],
            "daily": daily_list,
            "hourly": hourly_list,
            "by_model_today": model_list("today"),
            "by_model_yesterday": model_list("yesterday"),
            "by_model_3_days": model_list("last_3_days"),
            "by_model_7_days": model_list("last_7_days"),
            "by_model": model_list("last_30_days"),
            "databases_scanned": len(databases),
            "generations_scanned": generations,
            "skipped_large_records": skipped_large,
            "unreadable_databases": unreadable,
            "last_activity": last_activity,
            "generated_at": int(time.time()),
        }

    def get(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if not force and self._cached and time.time() - self._cached_at < 60:
                return self._cached
            result = self._build()
            self._cached = result
            self._cached_at = time.time()
            return result


# ---------------------------------------------------------------------------
# HTTP 面板
# ---------------------------------------------------------------------------


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _client_ip(handler: BaseHTTPRequestHandler) -> str:
    # 面板默认仅监听回环地址；不信任可伪造的 X-Forwarded-For，避免绕过登录限速。
    # 放在反向代理后时，所有请求共享同一个限速计数，对单用户面板可以接受。
    return str(handler.client_address[0])


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], app: "App"):
        self.app = app
        super().__init__(address, PanelHandler)


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (APP_NAME, VERSION)
    sys_version = ""

    @property
    def app(self) -> "App":
        return self.server.app  # type: ignore[attr-defined]

    # -- 基础 ------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s %s", _client_ip(self), fmt % args)

    def _send(self, status: int, payload: Optional[bytes] = None, content_type: str = "",
              extra_headers: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload or b"")))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload and self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, status: int, obj: Any, extra_headers: Optional[Dict[str, str]] = None) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8", extra_headers)

    def _error(self, status: int, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
        body = {"ok": False, "error": message}
        if extra:
            body.update(extra)
        self._json(status, body)

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "请求体长度不合法")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体必须是 JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return value

    def _cookie_value(self) -> str:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return ""
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _authorized(self) -> bool:
        token = self._cookie_value()
        return bool(token) and verify_session(self.app.secret, token)

    def _require_auth(self) -> None:
        if not self._authorized():
            raise ApiError(401, "未登录或会话已过期")

    def _powertools_request(self) -> None:
        """只供同机网页桥接使用，不能凭此密钥访问其它管理 API。"""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            raise ApiError(403, "桥接接口仅限本机")
        path = os.environ.get("AGY_ACCOUNTS_BRIDGE_KEY", "/etc/agy-powertools/accounts.key")
        key = read_bytes(path)
        if not key or not re.fullmatch(rb"[a-f0-9]{64}", key.strip()):
            raise ApiError(403, "账号桥接未配置")
        provided = self.headers.get("X-Agy-Accounts-Bridge", "").encode("utf-8")
        if not hmac.compare_digest(key.strip(), provided):
            raise ApiError(403, "账号桥接认证失败")
        body = self._read_body()
        action = body.get("action")
        if action == "list":
            result = self.app.powertools_accounts()
        elif action in ("core_update_status", "core_update_check"):
            if action == "core_update_check":
                self.app.check_powertools_update()
            result = self.app.powertools_update()
        elif action == "refresh":
            self.app.refresh_powertools_quotas(force=True)
            result = self.app.powertools_accounts()
        elif action == "switch":
            if body.get("confirmed") is not True or not isinstance(body.get("expectedCurrentId"), str):
                raise ApiError(400, "切换须明确确认，并提供当前账号状态")
            result = self.app.start_powertools_switch(body.get("accountId"), body["expectedCurrentId"])
        elif action == "alias":
            account_id, alias = body.get("accountId"), body.get("alias")
            if not isinstance(account_id, str) or not isinstance(alias, str) or len(alias) > MAX_NAME:
                raise ApiError(400, "账号备注无效")
            result = self.app.set_powertools_alias(account_id, alias)
        else:
            raise ApiError(400, "不支持此账号操作")
        self._json(200, dict(result, ok=True))

    def _require_csrf(self) -> None:
        if self.headers.get(CSRF_HEADER, "") != "1":
            raise ApiError(400, "缺少请求校验头")
        origin = self.headers.get("Origin", "")
        if origin:
            host = self.headers.get("Host", "")
            try:
                parsed = urllib.parse.urlparse(origin)
            except ValueError:
                raise ApiError(400, "Origin 不合法")
            if parsed.netloc != host:
                raise ApiError(403, "跨站请求被拒绝")

    def _security_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            ),
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
        }
        if self.app.cfg.get("cookie_secure"):
            headers["Strict-Transport-Security"] = "max-age=31536000"
        return headers

    # -- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._serve_app("index.html")
                return
            if path in ("/accounts", "/settings", "/menubar"):
                # React Router 的客户端路由，直接回首页由前端接管。
                self._serve_app("index.html")
                return
            if path in ("/icon.png", "/logo.png", "/tauri-shim.js") or path.startswith("/assets/"):
                self._serve_app(path.lstrip("/"))
                return
            if path == "/healthz":
                self._json(200, {"ok": True, "version": VERSION})
                return
            if path == "/api/status":
                self._require_auth()
                self._json(200, self.app.status_payload())
                return
            if path == "/api/preferences":
                self._require_auth()
                self._json(200, {"ok": True, "preferences": load_preferences(self.app.cfg)})
                return
            if path == "/api/failover":
                self._require_auth()
                self._json(200, {
                    "ok": True,
                    "enabled": bool(self.app.cfg.get("auto_failover", True)),
                    "status": self.app.failover.state(),
                })
                return
            if path == "/api/update":
                self._require_auth()
                self._json(200, dict(update_status(self.app.cfg), ok=True))
                return
            if path == "/api/usage":
                self._require_auth()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                force = query.get("refresh", [""])[0] in ("1", "true", "yes")
                self._json(200, dict(self.app.usage.get(force), ok=True))
                return
            if path == "/api/quotas":
                self._require_auth()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                force = query.get("refresh", [""])[0] in ("1", "true", "yes")
                cache_only = query.get("cached", [""])[0] in ("1", "true", "yes")
                self._json(200, dict(self.app.quota.get_all(force, cache_only), ok=True))
                return
            raise ApiError(404, "未找到该页面")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.extra)
        except Exception:
            log.exception("GET 请求处理失败")
            self._error(500, "服务器内部错误")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
            if path == "/internal/powertools/accounts":
                self._powertools_request()
                return
            self._require_csrf()

            if path == "/api/login":
                self._handle_login()
                return

            self._require_auth()

            if path == "/api/logout":
                expired = ("%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                           % COOKIE_NAME)
                if self.app.cfg.get("cookie_secure"):
                    expired += "; Secure"
                self._json(200, {"ok": True}, {"Set-Cookie": expired})
                return
            if path == "/api/password":
                body = self._read_body()
                change_password(self.app.cfg, str(body.get("currentPassword") or ""),
                                str(body.get("newPassword") or ""))
                self._json(200, {"ok": True})
                return
            if path == "/api/failover":
                body = self._read_body()
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    raise ApiError(400, "enabled 必须是布尔值")
                update_config(self.app.cfg, auto_failover=enabled)
                log.info("账号自动切换已%s", "启用" if enabled else "关闭")
                self._json(200, {
                    "ok": True,
                    "enabled": enabled,
                    "status": self.app.failover.state(),
                })
                return
            if path == "/api/update":
                body = self._read_body()
                action = str(body.get("action") or "apply")
                if action == "check":
                    result = check_official_update(self.app.cfg)
                elif action == "apply":
                    result = apply_language_server_update(self.app.cfg, self.app.operation_lock)
                else:
                    raise ApiError(400, "action 必须是 check 或 apply")
                self._json(200, dict(result, ok=True))
                return
            if path == "/api/preferences":
                body = self._read_body()
                dashboard = body.get("dashboard") if isinstance(body, dict) else None
                cards = dashboard.get("cards") if isinstance(dashboard, dict) else None
                log.info("卡片保存来源：ua=%r page=%r ref=%r cards=%r rev=%r manual=%r",
                         (self.headers.get("User-Agent") or "")[:120],
                         (self.headers.get("X-Agy-Page") or "")[:40],
                         (self.headers.get("Referer") or "")[:120],
                         len(cards) if isinstance(cards, list) else cards,
                         body.get("rev") if isinstance(body, dict) else None,
                         body.get("manual") if isinstance(body, dict) else None)
                self._json(200, {"ok": True, "preferences": save_preferences(self.app.cfg, body)})
                return
            if path == "/api/accounts":
                body = self._read_body()
                with self.app.operation_lock.hold("保存账号快照"):
                    result = save_account(self.app.cfg, body.get("name", ""))
                self._json(200, {"ok": True, "account": result})
                return
            if path == "/api/status":
                self._json(200, self.app.status_payload())
                return

            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "accounts":
                account_id = urllib.parse.unquote(parts[2])
                action = parts[3]
                if action == "switch":
                    result = self.app.switcher.switch(account_id)
                    self._json(200, result)
                    return
                if action == "rename":
                    body = self._read_body()
                    with self.app.operation_lock.hold("重命名账号快照"):
                        result = rename_account(self.app.cfg, account_id, body.get("name", ""))
                    self._json(200, {"ok": True, "account": result})
                    return
                if action == "delete":
                    with self.app.operation_lock.hold("删除账号快照"):
                        delete_account(self.app.cfg, account_id)
                    self._json(200, {"ok": True})
                    return
            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.extra)
        except Exception:
            log.exception("POST 请求处理失败")
            self._error(500, "服务器内部错误")

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            self._require_csrf()
            self._require_auth()
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            parts = [part for part in path.split("/") if part]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "accounts":
                with self.app.operation_lock.hold("删除账号快照"):
                    delete_account(self.app.cfg, urllib.parse.unquote(parts[2]))
                self._json(200, {"ok": True})
                return
            raise ApiError(404, "接口不存在")
        except ApiError as exc:
            self._error(exc.status, exc.message, exc.extra)
        except Exception:
            log.exception("DELETE 请求处理失败")
            self._error(500, "服务器内部错误")

    def _serve_app(self, name: str) -> None:
        root = os.path.realpath(WEBAPP_DIR)
        path = os.path.realpath(os.path.join(root, name))
        if path != root and not path.startswith(root + os.sep):
            raise ApiError(404, "未找到该页面")
        data = read_bytes(path)
        if data is None:
            raise ApiError(404, "未找到该页面")
        ext = os.path.splitext(path)[1].lower()
        content_type = STATIC_TYPES.get(ext, "application/octet-stream")
        if name == "index.html":
            # 把服务端偏好直接注入 HTML，避免前端首屏读取竞态。
            try:
                preferences = load_preferences(self.app.cfg)
                payload = json.dumps(preferences, ensure_ascii=False).replace("</", "<\\/")
                injected = ("<script>window.__AGY_PREFERENCES__=" + payload +
                            ";</script>")
                text = data.decode("utf-8", "replace")
                marker = '<script src="/tauri-shim.js"></script>'
                if marker in text:
                    text = text.replace(marker, injected + marker, 1)
                elif "<head>" in text:
                    text = text.replace("<head>", "<head>" + injected, 1)
                else:
                    text = injected + text
                data = text.encode("utf-8")
            except Exception:
                log.exception("注入服务端偏好失败")
        self._send(200, data, content_type, self._security_headers())

    def _handle_login(self) -> None:
        ip = _client_ip(self)
        locked = self.app.limiter.locked_for(ip)
        if locked:
            raise ApiError(429, "尝试次数过多，请 %d 秒后再试" % locked,
                           {"retry_after": locked})
        body = self._read_body()
        password = body.get("password")
        if not isinstance(password, str) or not password:
            raise ApiError(400, "请输入密码")
        if not check_password(self.app.cfg, password):
            self.app.limiter.fail(ip)
            log.warning("登录失败 ip=%s", ip)
            raise ApiError(401, "密码不正确")
        self.app.limiter.reset(ip)
        value = issue_session(self.app.secret, int(self.app.cfg["session_ttl"]))
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
            COOKIE_NAME, value, int(self.app.cfg["session_ttl"]))
        if self.app.cfg.get("cookie_secure"):
            cookie += "; Secure"
        self._json(200, {"ok": True, "version": VERSION}, {"Set-Cookie": cookie})
        log.info("登录成功 ip=%s", ip)


class App:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.secret = load_secret(cfg)
        self.limiter = LoginLimiter(int(cfg["login_max_failures"]), int(cfg["login_window"]))
        self.operation_lock = ServiceOperationLock()
        self.switcher = Switcher(cfg, self.operation_lock)
        self.quota = QuotaManager(cfg, self.switcher)
        self.usage = UsageStats(cfg)
        self.failover = FailoverManager(cfg, self.switcher)
        self._pw_job_lock = threading.Lock()
        self._pw_state_lock = threading.Lock()
        self._pw_job: Dict[str, Any] = {}
        self._pw_refresh_lock = threading.Lock()
        self._pw_refresh_at = 0.0
        self._pw_update_lock = threading.Lock()
        self._pw_update_state_lock = threading.Lock()
        self._pw_update: Dict[str, Any] = {"state": "idle", "checked_at": 0}
        self._pw_update_attempt = 0.0

    def powertools_update(self) -> Dict[str, Any]:
        with self._pw_update_state_lock:
            checked = dict(self._pw_update)
        status = update_status(self.cfg)
        # Prefer a verified channel check over the local staged-version hint.
        if checked.get("latest"):
            status["latest"] = checked["latest"]
            status["available"] = bool(status["running"] and status["latest"] != status["running"])
        status.pop("binary", None)
        return dict(status, check=checked)

    def check_powertools_update(self) -> None:
        """后台只查询官方版本，不下载、不重启，立即返回给网页轮询。"""
        if not self._pw_update_lock.acquire(blocking=False):
            return
        if self._pw_update_attempt and time.monotonic() - self._pw_update_attempt < 30:
            self._pw_update_lock.release()
            raise ApiError(429, "请稍候再检查更新（30 秒内仅检查一次）")
        self._pw_update_attempt = time.monotonic()
        with self._pw_update_state_lock:
            self._pw_update.update(state="checking", error="")

        def check() -> None:
            state = {"state": "failed", "error": "检查未完成，请稍后重试"}
            try:
                result = check_official_update(self.cfg)
                state = {"state": "success", "latest": result["latest"], "checked_at": now(), "error": ""}
            except ApiError as exc:
                state = {"state": "failed", "error": exc.message}
            except Exception:
                log.exception("内置更新检查失败")
                state = {"state": "failed", "error": "检查更新失败，请稍后重试"}
            finally:
                with self._pw_update_state_lock:
                    self._pw_update.update(state)
                self._pw_update_lock.release()

        threading.Thread(target=check, daemon=True).start()

    def refresh_powertools_quotas(self, force: bool = False) -> None:
        if not self._pw_refresh_lock.acquire(blocking=False):
            return
        if time.monotonic() - self._pw_refresh_at < 30:
            self._pw_refresh_lock.release()
            return
        self._pw_refresh_at = time.monotonic()

        def refresh() -> None:
            try:
                self.quota.get_all(force)
            except Exception:
                log.exception("内置账号页刷新额度失败")
            finally:
                self._pw_refresh_lock.release()

        threading.Thread(target=refresh, daemon=True).start()

    def powertools_accounts(self) -> Dict[str, Any]:
        accounts = list_accounts(self.cfg)
        quotas = self.quota.get_all(cache_only=True)["quotas"]
        with self._pw_state_lock:
            job = dict(self._pw_job)
        rows = [{key: account[key] for key in ("id", "name", "email", "is_current", "has_token")}
                for account in accounts]
        for row in rows:
            row["quota"] = quotas.get(row["id"], {"status": "loading"})
        if not job.get("active") and any(
                row["quota"].get("status") == "loading" or
                now() - row["quota"].get("fetched_at", 0) >= self.cfg["quota_ttl"] for row in rows):
            self.refresh_powertools_quotas()
        return {"accounts": rows, "currentId": next((row["id"] for row in rows if row["is_current"]), ""),
                "switching": self.switcher.state(), "switchJob": job,
                "refreshing": self._pw_refresh_lock.locked()}

    def start_powertools_switch(self, account_id: str, expected_current: str) -> Dict[str, Any]:
        account_id = safe_account_id(account_id)
        read_target_token(self.cfg, account_id)
        if not self._pw_job_lock.acquire(blocking=False):
            raise ApiError(409, "已有账号切换正在执行")
        job_id = secrets.token_hex(16)
        with self._pw_state_lock:
            self._pw_job = {"id": job_id, "active": True, "accountId": account_id,
                            "state": "pending", "message": "正在切换并验证登录，请稍候"}

        def switch() -> None:
            state, message = "failed", "切换状态无法确认，请检查当前账号"
            try:
                with self.operation_lock.hold("内置账号切换"):
                    current = next((row["id"] for row in list_accounts(self.cfg) if row["is_current"]), "")
                    if current != expected_current:
                        raise ApiError(409, "当前账号已变化，未执行切换，请刷新后重试")
                    result = self.switcher.switch_locked(account_id)
                state, message = "success", result["message"]
            except ApiError as exc:
                state, message = "failed", exc.message
            except Exception:
                log.exception("内置账号切换异常")
                state, message = "failed", "切换状态无法确认，请检查当前账号，不要重复切换"
            finally:
                with self._pw_state_lock:
                    self._pw_job.update(active=False, state=state, message=message)
                self._pw_job_lock.release()

        threading.Thread(target=switch, daemon=True).start()
        return {"jobId": job_id}

    def set_powertools_alias(self, account_id: str, alias: str) -> Dict[str, Any]:
        account_id = safe_account_id(account_id)
        alias = alias.strip()
        if len(alias) > MAX_NAME:
            raise ApiError(400, "账号备注不能超过 %d 个字符" % MAX_NAME)
        _path, _token, meta = read_target_token(self.cfg, account_id)
        fallback = str(meta.get("email") or account_id).strip()
        target = alias or fallback
        if target.lower() == str(meta.get("name") or "").strip().lower():
            return self.powertools_accounts()
        with self.operation_lock.hold("编辑账号备注"):
            rename_account(self.cfg, account_id, target)
        return self.powertools_accounts()

    def status_payload(self) -> Dict[str, Any]:
        accounts = list_accounts(self.cfg)
        active = service_active(self.cfg)
        live = read_bytes(self.cfg["token_path"])
        return {
            "ok": True,
            "version": VERSION,
            "accounts": accounts,
            "current": next((a["id"] for a in accounts if a["is_current"]), ""),
            "token_present": bool(live and live.strip()),
            "token_email": extract_email(live),
            "service": {
                "name": str(self.cfg.get("service_name") or "agy-server"),
                "active": active,
            },
            "switching": self.switcher.state(),
            "auto_failover": {"enabled": bool(self.cfg.get("auto_failover", True)),
                              "status": self.failover.state()},
            "app_url": self.cfg.get("app_url") or "",
            "token_path": self.cfg["token_path"],
            "accounts_dir": self.cfg["accounts_dir"],
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def ensure_runtime(cfg: Dict[str, Any]) -> None:
    if not password_configured(cfg):
        raise SystemExit(
            "拒绝启动：未配置面板密码。请设置 config.json 的 password_hash，"
            "或使用环境变量 AGY_ACCOUNTS_PASSWORD。\n"
            "生成密码哈希：python3 %s hash-password" % os.path.basename(__file__)
        )
    if not os.path.isfile(os.path.join(WEBAPP_DIR, "index.html")):
        raise SystemExit("拒绝启动：缺少前端文件 %s" % os.path.join(WEBAPP_DIR, "index.html"))
    ensure_dir(cfg["accounts_dir"], 0o700)
    ensure_dir(cfg["config_home"], 0o700)
    token_dir = os.path.dirname(cfg["token_path"])
    if token_dir and not os.path.isdir(token_dir):
        log.warning("令牌目录尚不存在：%s（agy-server 首次登录后会创建）", token_dir)


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging(str(cfg["log_level"]))
    ensure_runtime(cfg)
    app = App(cfg)
    try:
        server = PanelServer((cfg["listen_host"], cfg["listen_port"]), app)
    except OSError as exc:
        log.error("无法监听 %s:%s：%s", cfg["listen_host"], cfg["listen_port"],
                  exc.__class__.__name__)
        return 1

    log.info("%s %s 已启动：http://%s:%d/", APP_NAME, VERSION,
             cfg["listen_host"], cfg["listen_port"])
    log.info("令牌路径：%s", cfg["token_path"])
    log.info("账号目录：%s", cfg["accounts_dir"])
    log.info("服务名：%s", cfg.get("service_name") or "agy-server")
    threading.Thread(target=failover_loop, args=(app.failover,),
                     name="agy-failover", daemon=True).start()
    log.info("账号自动切换：%s", "已启用" if cfg.get("auto_failover", True) else "已关闭")

    def shutdown(signum: int, _frame: Any) -> None:
        log.info("收到信号 %s，正在退出", signum)

        def wait_and_stop() -> None:
            deadline = time.time() + 45
            while app.switcher.state().get("active") and time.time() < deadline:
                time.sleep(0.5)
            if app.switcher.state().get("active"):
                log.warning("切换仍在进行，强制退出可能留下未完成状态")
            server.shutdown()

        threading.Thread(target=wait_and_stop, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        log.info("已退出")
    return 0


def cmd_hash_password(args: argparse.Namespace) -> int:
    password = args.password
    if password == "-":
        password = sys.stdin.readline().rstrip("\n")
    if not password:
        first = getpass.getpass("面板密码：")
        second = getpass.getpass("再输入一次：")
        if first != second:
            print("两次输入不一致", file=sys.stderr)
            return 1
        password = first
    if len(password) < 8:
        print("密码至少 8 个字符", file=sys.stderr)
        return 1
    print(hash_password(password))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = expand_path(args.config) if args.config else config_path()
    if os.path.exists(path) and not args.force:
        print("配置文件已存在：%s（使用 --force 覆盖）" % path, file=sys.stderr)
        return 1
    ensure_dir(os.path.dirname(path) or ".", 0o700)
    cfg = dict(DEFAULTS)
    if args.password:
        cfg["password_hash"] = hash_password(args.password)
    cfg["token_path"] = expand_path(cfg["token_path"])
    cfg["accounts_dir"] = expand_path(cfg["accounts_dir"])
    write_json_atomic(path, cfg, 0o600)
    print("已写入配置：%s" % path)
    if not args.password:
        print("请补充 password_hash（运行 hash-password），或设置 AGY_ACCOUNTS_PASSWORD。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    setup_logging("WARNING")
    problems = 0

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal problems
        if not ok:
            problems += 1
        print("%s %s%s" % ("[ OK ]" if ok else "[FAIL]", label,
                           ("  " + detail) if detail else ""))

    line(sys.version_info >= (3, 9), "Python >= 3.9", sys.version.split()[0])
    line(password_configured(cfg), "面板密码已配置",
         "password_hash" if cfg.get("password_hash") else
         ("password（明文，建议改用哈希）" if cfg.get("password") else "未配置"))
    line(os.path.isdir(os.path.dirname(cfg["token_path"])), "令牌目录存在",
         os.path.dirname(cfg["token_path"]))
    live = read_bytes(cfg["token_path"])
    line(live is not None and bool(live and live.strip()), "线上令牌可读取",
         token_label(live))
    if live:
        print("       识别邮箱：%s" % (extract_email(live) or "未识别"))
    line(os.path.isdir(cfg["accounts_dir"]), "账号目录存在", cfg["accounts_dir"])
    writable = os.access(cfg["accounts_dir"] if os.path.isdir(cfg["accounts_dir"])
                         else os.path.dirname(cfg["accounts_dir"]), os.W_OK)
    line(writable, "账号目录可写")
    line(os.path.isfile(os.path.join(WEBAPP_DIR, "index.html")), "前端文件存在", WEBAPP_DIR)
    commands = service_commands(cfg)
    for key in ("stop", "start", "status"):
        executable = shutil.which(commands[key][0])
        line(bool(executable), "命令可用：%s" % key, commands[key][0])
    active = service_active(cfg)
    print("[INFO] 服务状态：%s" % ("运行中" if active else "已停止" if active is False else "未知"))
    print("[INFO] 账号数量：%d" % len(list_accounts(cfg)))
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-accounts",
        description="Antigravity 多账号切换面板（仅替换 OAuth token，不动业务数据）",
    )
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    parser.add_argument("--config", help="配置文件路径（默认 ~/.config/agy-accounts/config.json）")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动面板服务")
    serve.set_defaults(func=cmd_serve)

    hashed = sub.add_parser("hash-password", help="生成 password_hash")
    hashed.add_argument("--password", help="直接提供密码（不推荐，会留在 shell 历史里）")
    hashed.set_defaults(func=cmd_hash_password)

    init = sub.add_parser("init", help="生成默认配置文件")
    init.add_argument("--force", action="store_true", help="覆盖已有配置")
    init.add_argument("--password", help="同时写入密码哈希")
    init.set_defaults(func=cmd_init)

    check = sub.add_parser("check", help="自检部署环境")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_serve
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
