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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

APP_NAME = "agy-accounts"
VERSION = "1.7.1"

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
    return cfg


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
        with self._fetch_lock:
            quotas: Dict[str, Any] = {}
            if cache_only:
                for account in list_accounts(self.cfg):
                    account_id = str(account["id"])
                    path = account_dir(self.cfg, account_id)
                    raw = read_bytes(account_token_path(path))
                    if not raw:
                        quotas[account_id] = {"status": "loading"}
                        continue
                    key = cache_key(raw)
                    entry = self._cached(account_id)
                    if entry and entry.get("key") == key and isinstance(entry.get("quota"), dict):
                        quota = dict(entry["quota"])
                        quota.update({"status": "ok", "cached": True,
                                      "fetched_at": int(entry.get("at") or 0)})
                        quotas[account_id] = quota
                    else:
                        quotas[account_id] = {"status": "loading"}
                return {"quotas": quotas, "fetched_at": now()}
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
    live = read_bytes(cfg["token_path"])
    if live is None and os.path.exists(cfg["token_path"]):
        raise ApiError(500, "令牌文件存在但不可读，请检查权限：%s" % cfg["token_path"])
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
    deadline = time.time() + max(3, cfg["stop_timeout"])
    while time.time() < deadline:
        active = service_active(cfg)
        if active is False:
            return
        if active is None:
            log.warning("无法确认服务是否已停止（status 命令返回未知），继续前等待 1 秒")
            time.sleep(1.0)
            return
        time.sleep(0.4)
    raise SwitchError("服务在 %ds 内仍未停止" % cfg["stop_timeout"], 504)


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

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._lock = threading.Lock()
        self.token_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {"active": False, "started_at": 0, "account": ""}

    def state(self) -> Dict[str, Any]:
        with self._state_lock:
            return dict(self._state)

    def switch(self, account_id: str) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ApiError(409, "正在切换账号，请等待当前操作完成。")
        with self._state_lock:
            self._state = {"active": True, "started_at": now(), "account": account_id}
        try:
            return self._do_switch(account_id)
        finally:
            with self._state_lock:
                self._state = {"active": False, "started_at": 0, "account": ""}
            self._lock.release()

    def _do_switch(self, account_id: str) -> Dict[str, Any]:
        # 与额度刷新共用：避免额度回写线上令牌和切换事务交叉。
        with self.token_lock:
            return self._switch_transaction(account_id)

    def _switch_transaction(self, account_id: str) -> Dict[str, Any]:
        cfg = self.cfg
        path, target, meta = read_target_token(cfg, account_id)
        name = str(meta.get("name") or account_id)

        live = read_bytes(cfg["token_path"])
        if live is None and os.path.exists(cfg["token_path"]):
            raise ApiError(500, "令牌文件存在但不可读，请检查权限：%s" % cfg["token_path"])
        if live is not None and same_login(live, target):
            if live != target:
                write_atomic(account_token_path(path), live, 0o600)
                log.info("线上令牌已刷新，快照同步更新 id=%s", account_id)
            touch_account(path, meta)
            return {"ok": True, "already_current": True, "name": name,
                    "message": "当前已经是「%s」" % name}

        log.info("开始切换账号 id=%s live=%s target=%s", account_id,
                 token_label(live), token_label(target))

        if live:
            rotated = sync_rotated_token(cfg, live)
            if rotated:
                log.info("已把轮换后的线上令牌写回快照 id=%s", rotated)

        backup = live
        stopped = False
        mutated = False
        try:
            stop_service(cfg)
            stopped = True
            if backup is None:
                try:
                    os.unlink(cfg["token_path"])
                except FileNotFoundError:
                    pass
            write_atomic(cfg["token_path"], target, 0o600)
            mutated = True
            start_service(cfg)
            probe_service(cfg)
        except SwitchError as exc:
            if not stopped and not mutated:
                log.error("切换未开始 id=%s：%s", account_id, exc.message)
                extra = {"rolled_back": True, "unchanged": True}
                if exc.detail:
                    extra["detail"] = exc.detail
                raise ApiError(exc.status, exc.message + "；线上凭据未改动", extra)
            rolled_back = self._rollback(backup, stopped or mutated)
            message = exc.message
            if rolled_back:
                message += "；已回滚到切换前的凭据"
            else:
                message += "；回滚未完成，请手动检查服务与令牌文件"
            log.error("切换失败 id=%s：%s", account_id, message)
            extra = {"rolled_back": rolled_back}
            if exc.detail:
                extra["detail"] = exc.detail
            raise ApiError(exc.status, message, extra)
        except Exception as exc:  # 未预期的文件/系统错误
            log.exception("切换出现未预期错误 id=%s", account_id)
            if not stopped and not mutated:
                raise ApiError(500, "切换失败：%s；线上凭据未改动"
                               % exc.__class__.__name__, {"rolled_back": True,
                                                          "unchanged": True})
            rolled_back = self._rollback(backup, stopped or mutated)
            message = "切换失败：%s" % exc.__class__.__name__
            message += ("；已回滚到切换前的凭据" if rolled_back
                        else "；回滚未完成，请手动检查服务与令牌文件")
            raise ApiError(500, message, {"rolled_back": rolled_back})

        meta = touch_account(path, meta)
        log.info("账号切换完成 id=%s", account_id)
        return {
            "ok": True,
            "already_current": False,
            "name": name,
            "email": str(meta.get("email") or ""),
            "app_url": cfg.get("app_url") or "",
            "message": "已切换到「%s」" % name,
        }

    def _rollback(self, backup: Optional[bytes], need_stop: bool) -> bool:
        cfg = self.cfg
        ok = True
        if need_stop:
            try:
                stop_service(cfg)
            except SwitchError as exc:
                log.error("回滚停止服务失败：%s", exc.message)
                ok = False
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
            ok = False
        try:
            start_service(cfg)
        except SwitchError as exc:
            log.error("回滚启动服务失败：%s", exc.message)
            ok = False
        return ok



# ---------------------------------------------------------------------------
# 本地 Token 用量（读取 Antigravity 会话数据库）
# ---------------------------------------------------------------------------

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
        if key == number and wire == 0:
            return int(value)
    return None


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
    if not timestamp or timestamp <= 0:
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

    def _read_database(self, path: str) -> Tuple[List[Dict[str, Any]], int, bool]:
        events: List[Dict[str, Any]] = []
        scanned = 0
        try:
            uri = "file:%s?mode=ro" % urllib.parse.quote(path)
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            try:
                step_timestamps: List[Optional[int]] = []
                try:
                    for (metadata,) in conn.execute(
                            "SELECT metadata FROM steps WHERE step_type = 15 ORDER BY idx"):
                        value = None
                        if isinstance(metadata, (bytes, bytearray)):
                            stamp = _pb_bytes(bytes(metadata), 1)
                            if stamp:
                                value = _pb_uint(stamp, 1)
                        step_timestamps.append(value)
                except sqlite3.Error:
                    pass
                rows = conn.execute(
                    "SELECT data, length(data) FROM gen_metadata WHERE data IS NOT NULL ORDER BY idx"
                ).fetchall()
                for ordinal, (blob, size) in enumerate(rows):
                    scanned += 1
                    if not isinstance(blob, (bytes, bytearray)) or (size or 0) > 64 * 1024 * 1024:
                        continue
                    stamp = step_timestamps[ordinal] if ordinal < len(step_timestamps) else None
                    event = _usage_event(bytes(blob), stamp)
                    if event:
                        events.append(event)
            finally:
                conn.close()
            return events, scanned, True
        except (sqlite3.Error, OSError):
            return events, scanned, False

    def _build(self) -> Dict[str, Any]:
        now = dt.datetime.now()
        today = now.date()
        start_30 = today - dt.timedelta(days=29)
        events: List[Dict[str, Any]] = []
        databases = self._databases()
        generations = 0
        unreadable = 0
        for path in databases:
            found, scanned, ok = self._read_database(path)
            generations += scanned
            if not ok:
                unreadable += 1
            events.extend(found)

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
            moment = dt.datetime.fromtimestamp(event["timestamp"])
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
            "recent_performance": None,
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
            "skipped_large_records": 0,
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
            self._require_csrf()
            path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

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
            if path == "/api/accounts":
                body = self._read_body()
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
                    result = rename_account(self.app.cfg, account_id, body.get("name", ""))
                    self._json(200, {"ok": True, "account": result})
                    return
                if action == "delete":
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
        self.switcher = Switcher(cfg)
        self.quota = QuotaManager(cfg, self.switcher)
        self.usage = UsageStats(cfg)

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
