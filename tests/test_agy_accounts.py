#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agy-accounts 冒烟测试：不依赖真实 agy-server。

用一个假的 systemctl 脚本模拟 stop/start/is-active，验证保存、切换、
令牌轮换回写、失败回滚、鉴权与路径校验。
"""

import base64
import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agy_accounts  # noqa: E402

FAKE_CONTROL = r"""#!/bin/sh
STATE="${FAKE_STATE:?}"
ACTION="$1"
case "$ACTION" in
  stop)
    [ -f "$FAKE_FAIL_STOP" ] && exit 1
    echo stopped > "$STATE"
    exit 0 ;;
  start)
    if [ -f "$FAKE_FAIL_START_ONCE" ]; then
      rm -f "$FAKE_FAIL_START_ONCE"
      exit 1
    fi
    [ -f "$FAKE_FAIL_START" ] && exit 1
    echo running > "$STATE"
    echo start >> "${STATE}.log"
    exit 0 ;;
  is-active)
    grep -q running "$STATE" 2>/dev/null && exit 0
    exit 3 ;;
esac
exit 0
"""


def jwt(claims):
    def enc(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return enc({"alg": "none", "typ": "JWT"}) + "." + enc(claims) + ".sig"


class PanelTest(unittest.TestCase):
    def setUp(self):
        import logging
        logging.disable(logging.CRITICAL)
        self.tmp = tempfile.mkdtemp(prefix="agy-accounts-test-")
        self.home = os.path.join(self.tmp, "home")
        self.config_home = os.path.join(self.home, ".config", "agy-accounts")
        self.config_path = os.path.join(self.config_home, "config.json")
        self.token_path = os.path.join(self.home, ".gemini",
                                       "jetski-standalone-oauth-token")
        self.accounts_dir = os.path.join(self.home, "accounts")
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        os.makedirs(self.config_home, exist_ok=True)

        self.control = os.path.join(self.tmp, "fakectl")
        with open(self.control, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CONTROL)
        os.chmod(self.control, 0o755)
        self.state = os.path.join(self.tmp, "service.state")
        self.fake_env = {
            "FAKE_STATE": self.state,
            "FAKE_FAIL_START": os.path.join(self.tmp, "fail-start"),
            "FAKE_FAIL_START_ONCE": os.path.join(self.tmp, "fail-start-once"),
            "FAKE_FAIL_STOP": os.path.join(self.tmp, "fail-stop"),
        }
        self._old_env = {}
        for key, value in self.fake_env.items():
            self._old_env[key] = os.environ.get(key)
            os.environ[key] = value
        self._old_home = os.environ.get("AGY_ACCOUNTS_HOME")
        os.environ["AGY_ACCOUNTS_HOME"] = self.config_home
        os.environ.pop("AGY_ACCOUNTS_PASSWORD", None)

        self.cfg = {
            "token_path": self.token_path,
            "accounts_dir": self.accounts_dir,
            "service_name": "agy-server",
            "stop_command": [self.control, "stop", "agy-server"],
            "start_command": [self.control, "start", "agy-server"],
            "status_command": [self.control, "is-active", "agy-server"],
            "probe_url": "",
            "app_url": "",
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "password_hash": agy_accounts.hash_password("test-password", 1000),
            "password": "",
            "session_ttl": 3600,
            "cookie_secure": False,
            "probe_timeout": 3,
            "command_timeout": 10,
            "stop_timeout": 5,
            "login_max_failures": 5,
            "login_window": 300,
            "log_level": "ERROR",
        }
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh)
        os.chmod(self.config_path, 0o600)

        self.loaded = agy_accounts.load_config(self.config_path)
        self.app = agy_accounts.App(self.loaded)
        self.server = agy_accounts.PanelServer(("127.0.0.1", 0), self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""

        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write("running")

    def tearDown(self):
        import logging
        logging.disable(logging.NOTSET)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if self._old_home is None:
            os.environ.pop("AGY_ACCOUNTS_HOME", None)
        else:
            os.environ["AGY_ACCOUNTS_HOME"] = self._old_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def request(self, method, path, body=None, auth=True, csrf=True, password=None):
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-Agy-Accounts"] = "1"
        if auth and self.cookie:
            headers["Cookie"] = self.cookie.split(";", 1)[0]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = None if body is None else json.dumps(body).encode()
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        data = json.loads(raw.decode() or "{}") if raw else {}
        set_cookie = response.getheader("Set-Cookie") or ""
        conn.close()
        return response.status, data, set_cookie

    def login(self, password="test-password"):
        status, data, cookie = self.request("POST", "/api/login",
                                            {"password": password}, auth=False)
        if status == 200:
            self.cookie = cookie
        return status, data

    def write_token(self, value):
        data = value.encode() if isinstance(value, str) else value
        agy_accounts.write_atomic(self.token_path, data, 0o600)

    def save(self, name):
        return self.request("POST", "/api/accounts", {"name": name})

    def switch(self, account_id):
        return self.request("POST", "/api/accounts/%s/switch" %
                            urllib.parse.quote(account_id, safe=""), {})

    def status(self):
        return self.request("GET", "/api/status")

    def account_id(self, name):
        for account in self.app.status_payload()["accounts"]:
            if account["name"] == name:
                return account["id"]
        self.fail("账号不存在：%s" % name)

    def start_count(self):
        path = self.state + ".log"
        if not os.path.exists(path):
            return 0
        with open(path, encoding="utf-8") as fh:
            return len([line for line in fh if line.strip()])

    # -- tests -----------------------------------------------------------

    def test_health_needs_no_auth(self):
        status, data, _ = self.request("GET", "/healthz", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_api_requires_login(self):
        status, data, _ = self.request("GET", "/api/status", auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn("accounts", data)

    def test_wrong_password_does_not_leak(self):
        status, data = self.login("wrong-password")
        self.assertEqual(status, 401)
        self.assertEqual(data.get("error"), "密码不正确")
        self.assertNotIn("accounts", data)

    def test_login_then_status(self):
        status, _ = self.login()
        self.assertEqual(status, 200)
        status, data, _ = self.status()
        self.assertEqual(status, 200)
        self.assertEqual(data["accounts"], [])
        self.assertFalse(data["token_present"])
        self.assertIn("token_path", data)

    def test_save_and_switch(self):
        self.assertTrue(self.login()[0] == 200)
        self.write_token('{"access_token":"aaa","email":"a@example.com"}')
        status, data, _ = self.save("A 号")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["account"]["email"], "a@example.com")

        # 重复保存同一登录应被拒绝
        status, data, _ = self.save("另一个名字")
        self.assertEqual(status, 409)

        self.write_token('{"access_token":"bbb","email":"b@example.com"}')
        status, data, _ = self.save("B 号")
        self.assertEqual(status, 200, data)

        status, data, _ = self.status()
        self.assertEqual(status, 200)
        self.assertEqual(len(data["accounts"]), 2)
        self.assertEqual(data["current"], self.account_id("B 号"))

        with open(self.token_path, "rb") as fh:
            live_before = fh.read()

        starts = self.start_count()
        status, data, _ = self.switch(self.account_id("A 号"))
        self.assertEqual(status, 200, data)
        self.assertFalse(data["already_current"])
        with open(self.token_path, "rb") as fh:
            self.assertIn(b'"aaa"', fh.read())
        self.assertEqual(self.start_count(), starts + 1)
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "running")

        # 再切一次同一个账号：不重启
        starts = self.start_count()
        status, data, _ = self.switch(self.account_id("A 号"))
        self.assertEqual(status, 200, data)
        self.assertTrue(data["already_current"])
        self.assertEqual(self.start_count(), starts)

        status, data, _ = self.status()
        self.assertTrue(any(a["name"] == "A 号" and a["is_current"]
                            for a in data["accounts"]))
        with open(self.token_path, "rb") as fh:
            self.assertNotEqual(live_before, fh.read())

    def test_rotation_is_written_back(self):
        self.login()
        first = (b'{"token":{"refresh_token":"stable-rt","access_token":"a1"},'
                 b'"id_token":"' + jwt({"email": "a@example.com"}).encode() + b'"}')
        self.write_token(first)
        self.assertEqual(self.save("A 号")[0], 200)
        account_id = self.account_id("A 号")
        path = os.path.join(self.accounts_dir, account_id)

        # access_token 轮换，refresh_token 不变：仍应识别为同一账号
        rotated = (b'{"token":{"refresh_token":"stable-rt","access_token":"a2"},'
                   b'"id_token":"' + jwt({"email": "a@example.com"}).encode() + b'"}')
        self.write_token(rotated)
        status, data, _ = self.status()
        self.assertTrue(any(a["id"] == account_id and a["is_current"]
                            for a in data["accounts"]))
        self.assertEqual(self.save("重复保存")[0], 409)

        # 切换前把线上最新令牌写回快照
        self.assertEqual(agy_accounts.sync_rotated_token(self.loaded, rotated), account_id)
        with open(os.path.join(path, "token"), "rb") as fh:
            self.assertEqual(fh.read(), rotated)

        # 切到同一账号：不重启，但快照会同步为最新令牌
        self.write_token(first)
        starts = self.start_count()
        status, data, _ = self.switch(account_id)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["already_current"])
        self.assertEqual(self.start_count(), starts)
        with open(os.path.join(path, "token"), "rb") as fh:
            self.assertEqual(fh.read(), first)

    def test_start_failure_rolls_back(self):
        self.login()
        self.write_token('{"access_token":"aaa"}')
        self.assertEqual(self.save("A 号")[0], 200)
        self.write_token('{"access_token":"bbb"}')
        self.assertEqual(self.save("B 号")[0], 200)

        self.write_token('{"access_token":"bbb"}')
        with open(self.fake_env["FAKE_FAIL_START_ONCE"], "w") as fh:
            fh.write("1")

        status, data, _ = self.switch(self.account_id("A 号"))
        self.assertEqual(status, 500, data)
        self.assertIn("回滚", data["error"])
        self.assertTrue(data.get("rolled_back"), data)
        with open(self.token_path, encoding="utf-8") as fh:
            self.assertIn('"bbb"', fh.read())
        with open(self.state, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "running")

    def test_stop_failure_does_not_touch_token(self):
        self.login()
        self.write_token('{"access_token":"aaa"}')
        self.assertEqual(self.save("A 号")[0], 200)
        self.write_token('{"access_token":"bbb"}')
        self.assertEqual(self.save("B 号")[0], 200)
        with open(self.fake_env["FAKE_FAIL_STOP"], "w") as fh:
            fh.write("1")
        status, data, _ = self.switch(self.account_id("A 号"))
        self.assertEqual(status, 500)
        self.assertIn("未改动", data["error"])
        with open(self.token_path, encoding="utf-8") as fh:
            self.assertIn('"bbb"', fh.read())

    def test_rename_and_delete(self):
        self.login()
        self.write_token('{"access_token":"aaa"}')
        self.assertEqual(self.save("旧名字")[0], 200)
        account_id = self.account_id("旧名字")
        status, data, _ = self.request("POST", "/api/accounts/%s/rename" %
                                       urllib.parse.quote(account_id, safe=""),
                                       {"name": "新名字"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["account"]["name"], "新名字")
        # 目录 id 不变
        self.assertTrue(os.path.isdir(os.path.join(self.accounts_dir, account_id)))

        status, data, _ = self.request("POST", "/api/accounts/%s/delete" %
                                       urllib.parse.quote(account_id, safe=""), {})
        self.assertEqual(status, 200, data)
        self.assertFalse(os.path.exists(os.path.join(self.accounts_dir, account_id)))
        # 删除快照不应该删线上令牌
        self.assertTrue(os.path.exists(self.token_path))

    def test_path_traversal_rejected(self):
        self.login()
        status, data, _ = self.switch("..%2F..%2Fetc")
        self.assertEqual(status, 400, data)

    def test_login_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.login("bad")[0], 401)
        status, data = self.login("test-password")
        self.assertEqual(status, 429)
        self.assertIn("retry_after", data)

    def test_email_extraction(self):
        payload = jwt({"email": "jwt@example.com"})
        self.assertEqual(
            agy_accounts.extract_email(('{"id_token":"%s"}' % payload).encode()),
            "jwt@example.com")
        self.assertEqual(
            agy_accounts.extract_email(b'{"account":"plain@example.com"}'),
            "plain@example.com")
        self.assertEqual(agy_accounts.extract_email(b"not a token"), "")

    def test_slugify_keeps_chinese(self):
        self.assertEqual(agy_accounts.slugify("主账号 A"), "主账号-a")
        self.assertEqual(agy_accounts.slugify("///"), "account")


if __name__ == "__main__":
    unittest.main(verbosity=2)
