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
from unittest import mock

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

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

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

    def test_web_ui_routes(self):
        status, body = self.raw_get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"tauri-shim.js", body)
        status, body = self.raw_get("/accounts")
        self.assertEqual(status, 200)
        self.assertIn(b"tauri-shim.js", body)
        status, _ = self.raw_get("/assets/does-not-exist.js")
        self.assertEqual(status, 404)
        status, _ = self.raw_get("/assets/%2e%2e/agy_accounts.py")
        self.assertEqual(status, 404)

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

    def test_quota_normalization(self):
        load = {"paidTier": {"id": "g1-pro-tier", "name": "Pro"}}
        summary = {"groups": [{
            "displayName": "Claude and GPT models",
            "buckets": [
                {"window": "5h", "remainingFraction": 0.42,
                 "resetTime": "2026-11-16T12:00:00Z"},
                {"window": "weekly", "remainingFraction": 1.4,
                 "resetTime": "2026-11-22T12:00:00Z",
                 "description": "weekly limit does not currently apply"},
                {"window": "monthly", "remainingFraction": 0.1},
            ],
        }]}
        quota = agy_accounts.normalize_quota(load, summary)
        self.assertEqual(quota["tier"], "Pro")
        self.assertEqual(quota["groups"][0]["key"], "claude")
        self.assertEqual(quota["groups"][0]["windows"]["5h"]["remaining"], 0.42)
        self.assertEqual(quota["groups"][0]["windows"]["weekly"]["remaining"], 1.0)
        self.assertTrue(quota["groups"][0]["windows"]["weekly"]["disabled"])
        self.assertNotIn("monthly", quota["groups"][0]["windows"])

    def test_quota_model_parsing(self):
        models = agy_accounts.normalize_models({
            "claude-opus-4-6-thinking": {
                "displayName": "Claude Opus 4.6 (Thinking)",
                "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-15T22:27:22Z"},
                "supportsThinking": True,
            },
            "gemini-3.8-flash-tiered": {
                "quotaInfo": {"remainingFraction": 0.985, "resetTime": "2026-11-15T22:27:22Z"},
            },
            "chat_23310": {"quotaInfo": {"remainingFraction": 1}},
            "broken": {"quotaInfo": {}},
        })
        names = {m["name"]: m for m in models}
        self.assertIn("claude-opus-4-6-thinking", names)
        self.assertEqual(names["claude-opus-4-6-thinking"]["percentage"], 100.0)
        self.assertTrue(names["claude-opus-4-6-thinking"]["supports_thinking"])
        self.assertAlmostEqual(names["gemini-3.8-flash-tiered"]["percentage"], 98.5)
        self.assertNotIn("chat_23310", names)
        self.assertNotIn("broken", names)

    def test_quota_cache_key_ignores_access_token(self):
        first = b'{"token":{"refresh_token":"rt","access_token":"a1"}}'
        second = b'{"token":{"refresh_token":"rt","access_token":"a2"}}'
        self.assertEqual(agy_accounts.cache_key(first), agy_accounts.cache_key(second))
        self.assertNotEqual(agy_accounts.cache_key(first),
                            agy_accounts.cache_key(b'{"token":{"refresh_token":"other"}}'))

    def test_parse_expiry_nanoseconds(self):
        stamp = agy_accounts.parse_expiry("2026-11-15T17:54:10.268688727Z")
        self.assertGreater(stamp, 1_700_000_000)
        self.assertEqual(agy_accounts.parse_expiry(""), 0.0)

    def test_quota_endpoint_requires_auth(self):
        status, data, _ = self.request("GET", "/api/quotas", auth=False)
        self.assertEqual(status, 401)

    def test_slugify_keeps_chinese(self):
        self.assertEqual(agy_accounts.slugify("主账号 A"), "主账号-a")
        self.assertEqual(agy_accounts.slugify("///"), "account")


    def test_preferences_manual_save_requires_flag_and_revision(self):
        self.login()
        status, data, _ = self.request("GET", "/api/preferences")
        self.assertEqual(status, 200)
        rev = data["preferences"]["rev"]
        cards = ["total_tokens", "input_tokens"]
        order = ["total_tokens", "input_tokens"]
        body = {"rev": rev, "dashboard": {"cards": cards, "order": order}}

        status, data, _ = self.request("POST", "/api/preferences", body)
        self.assertEqual(status, 400)
        self.assertIn("手动保存", data["error"])

        body["manual"] = True
        status, data, _ = self.request("POST", "/api/preferences", body)
        self.assertEqual(status, 200)
        self.assertEqual(data["preferences"]["rev"], rev + 1)
        self.assertEqual(data["preferences"]["dashboard"]["cards"], cards)

        status, data, _ = self.request("POST", "/api/preferences", body)
        self.assertEqual(status, 409)

    def test_preferences_reject_empty_cards(self):
        self.login()
        status, data, _ = self.request("GET", "/api/preferences")
        rev = data["preferences"]["rev"]
        status, data, _ = self.request("POST", "/api/preferences", {
            "rev": rev, "manual": True,
            "dashboard": {"cards": [], "order": []},
        })
        self.assertEqual(status, 400)
        self.assertIn("至少保留", data["error"])
        status, data, _ = self.request("GET", "/api/preferences")
        self.assertEqual(data["preferences"]["rev"], rev)


    def test_failover_toggle_and_switch(self):
        self.login()
        status, data, _ = self.request("GET", "/api/failover")
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])

        status, data, _ = self.request("POST", "/api/failover", {"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(data["enabled"])
        with open(self.config_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertFalse(stored["auto_failover"])

        status, data, _ = self.request("POST", "/api/failover", {"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["enabled"])

    def test_failover_switches_when_account_ineligible(self):
        manager = agy_accounts.FailoverManager(self.loaded, self.app.switcher)
        accounts = [
            {"id": "current", "name": "主账号", "is_current": True,
             "last_used_at": 300, "created_at": 1},
            {"id": "backup", "name": "备用账号", "is_current": False,
             "last_used_at": 200, "created_at": 2},
        ]
        answers = [
            {"signed_in": False, "ineligible": True, "message": "需要验证", "checked_at": 0},
            {"signed_in": True, "ineligible": False, "message": "", "checked_at": 0},
        ]
        with mock.patch.object(agy_accounts, "language_server_auth", side_effect=answers), \
                mock.patch.object(agy_accounts, "list_accounts", return_value=accounts), \
                mock.patch.object(self.app.switcher, "switch", return_value={}) as switch:
            state = manager.check(force=True)
        switch.assert_called_once_with("backup")
        self.assertEqual(state["state"], "switched")
        self.assertEqual(state["to"], "备用账号")


    def test_failover_recovers_after_unknown_auth_streak(self):
        manager = agy_accounts.FailoverManager(self.loaded, self.app.switcher)
        accounts = [
            {"id": "current", "name": "主账号", "is_current": True,
             "last_used_at": 300, "created_at": 1},
            {"id": "backup", "name": "备用账号", "is_current": False,
             "last_used_at": 200, "created_at": 2},
        ]
        answers = [
            None, None, None,
            {"signed_in": True, "ineligible": False, "message": "", "checked_at": 0},
        ]
        with mock.patch.object(agy_accounts, "language_server_auth", side_effect=answers), \
                mock.patch.object(agy_accounts, "list_accounts", return_value=accounts), \
                mock.patch.object(self.app.switcher, "switch", return_value={}) as switch:
            manager.check(force=True)
            manager.check(force=True)
            state = manager.check(force=True)
        switch.assert_called_once_with("backup")
        self.assertEqual(state["state"], "switched")
        self.assertEqual(manager.unknown_streak, 0)

    def test_failover_disabled_does_not_check(self):
        self.loaded["auto_failover"] = False
        manager = agy_accounts.FailoverManager(self.loaded, self.app.switcher)
        with mock.patch.object(agy_accounts, "language_server_auth") as auth:
            state = manager.check(force=True)
        auth.assert_not_called()
        self.assertEqual(state["state"], "disabled")


    def test_update_endpoint_shape(self):
        self.login()
        status, data, _ = self.request("GET", "/api/update")
        self.assertEqual(status, 200)
        self.assertIn("running", data)
        self.assertIn("latest", data)
        self.assertIn("available", data)

        status, data, _ = self.request("POST", "/api/update", {"action": "unknown"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
