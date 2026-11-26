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
import sqlite3
import stat
import sys
import tempfile
import threading
import time
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
        core_service = mock.patch.object(self.app, "core_update_enabled", return_value=False)
        core_service.start()
        self.addCleanup(core_service.stop)
        verify = mock.patch.object(agy_accounts, "wait_language_server_auth", return_value={
            "signed_in": True, "ineligible": False, "message": ""})
        self.verify_auth = verify.start()
        self.addCleanup(verify.stop)
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

    def test_cannot_confirm_stopped_leaves_live_token_unchanged(self):
        self.login()
        self.write_token('{"access_token":"candidate"}')
        self.assertEqual(self.save("candidate")[0], 200)
        candidate = self.account_id("candidate")
        original = b'{"access_token":"unsaved"}'
        self.write_token(original)

        with mock.patch.object(agy_accounts, "service_active", return_value=None), \
                mock.patch.object(agy_accounts.time, "monotonic", side_effect=[0, 0, 10]), \
                mock.patch.object(agy_accounts.time, "sleep"):
            with self.assertRaises(agy_accounts.ApiError) as caught:
                self.app.switcher.switch(candidate)
        self.assertEqual(caught.exception.status, 504)
        self.assertTrue(caught.exception.extra["unchanged"])
        self.assertEqual(agy_accounts.read_bytes(self.token_path), original)

    def test_manual_verification_timeout_rolls_back_unsaved_original(self):
        self.login()
        self.write_token('{"access_token":"candidate"}')
        self.assertEqual(self.save("candidate")[0], 200)
        original = b'{"access_token":"unsaved"}'
        self.write_token(original)
        self.verify_auth.return_value = None
        status, data, _ = self.switch(self.account_id("candidate"))
        self.assertEqual(status, 504, data)
        self.assertEqual(agy_accounts.read_bytes(self.token_path), original)
        self.assertTrue(data["rolled_back"])
        self.assertFalse(self.app.switcher.state()["active"])
        self.assertEqual(os.listdir(os.path.join(self.config_home, "switch-recovery")), [])

    def test_metadata_write_failure_does_not_invalidate_verified_switch(self):
        self.login()
        self.write_token('{"access_token":"candidate"}')
        self.assertEqual(self.save("candidate")[0], 200)
        self.write_token('{"access_token":"unsaved"}')
        with mock.patch.object(agy_accounts, "touch_account", side_effect=PermissionError):
            status, data, _ = self.switch(self.account_id("candidate"))
        self.assertEqual(status, 200, data)
        self.assertEqual(agy_accounts.read_bytes(self.token_path), b'{"access_token":"candidate"}')

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
        self.write_token('{"access_token":"backup"}')
        agy_accounts.save_account(self.loaded, "备用账号")
        self.write_token('{"access_token":"current"}')
        agy_accounts.save_account(self.loaded, "主账号")
        with mock.patch.object(agy_accounts, "language_server_auth", return_value={
            "signed_in": False, "ineligible": True, "message": "需要验证",
        }):
            state = self.app.failover.check(force=True)
        self.assertEqual(state["state"], "switched")
        self.assertEqual(state["to"], "备用账号")
        self.assertEqual(agy_accounts.read_bytes(self.token_path), b'{"access_token":"backup"}')
        self.assertEqual(agy_accounts.load_config(self.config_path)["auto_failover_last_good"], "备用账号")

    def test_failover_timeout_rolls_back_and_blocks_conflicting_requests(self):
        self.login()
        self.write_token('{"access_token":"first"}')
        first = agy_accounts.save_account(self.loaded, "first")["id"]
        self.write_token('{"access_token":"second"}')
        agy_accounts.save_account(self.loaded, "second")
        original = b'{"access_token":"unsaved"}'
        self.write_token(original)

        def verify(_cfg):
            self.assertEqual(self.switch(first)[0], 409)
            self.assertEqual(self.request("POST", "/api/update", {"action": "apply"})[0], 409)
            return None

        self.verify_auth.side_effect = verify
        starts = self.start_count()
        with mock.patch.object(agy_accounts, "language_server_auth", return_value={
            "signed_in": False, "ineligible": True, "message": "需要验证",
        }):
            state = self.app.failover.check(force=True)
        self.assertEqual(state["state"], "unknown")
        self.assertIn("已回滚", state["message"])
        self.verify_auth.assert_called_once()
        self.assertEqual(agy_accounts.read_bytes(self.token_path), original)
        self.assertEqual(self.start_count() - starts, 2)
        with self.app.operation_lock.hold("再次操作"):
            pass

    def test_failed_rollback_does_not_claim_original_was_restored(self):
        self.write_token('{"access_token":"candidate"}')
        agy_accounts.save_account(self.loaded, "candidate")
        self.write_token('{"access_token":"original"}')
        self.verify_auth.return_value = None
        with mock.patch.object(agy_accounts, "language_server_auth", return_value={
                    "signed_in": False, "ineligible": True, "message": "需要验证",
                }), mock.patch.object(self.app.switcher, "_rollback", return_value=False):
            state = self.app.failover.check(force=True)
        self.assertEqual(state["state"], "failed")
        self.assertIn("回滚未完成", state["message"])
        directory = os.path.join(self.config_home, "switch-recovery")
        backup, = os.listdir(directory)
        path = os.path.join(directory, backup)
        self.assertEqual(agy_accounts.read_bytes(path), b'{"access_token":"original"}')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


    def test_failover_recovers_after_unknown_auth_streak(self):
        manager = self.app.failover
        original = b'{"access_token":"unsaved"}'
        self.write_token(original)
        starts = self.start_count()
        with mock.patch.object(agy_accounts, "language_server_auth", return_value=None):
            self.assertEqual(manager.check(force=True)["state"], "unknown")
            self.assertEqual(manager.check(force=True)["state"], "unknown")
            with self.app.operation_lock.hold("系统更新"):
                self.assertEqual(manager.check(force=True)["state"], "busy")
            self.assertEqual(manager.check(force=True)["state"], "recovered")
            self.assertEqual(manager.unknown_streak, 0)
            for _ in range(3):
                self.assertEqual(manager.check(force=True)["state"], "unknown")
        self.verify_auth.assert_not_called()
        self.assertEqual(self.start_count(), starts + 1)
        self.assertEqual(agy_accounts.read_bytes(self.token_path), original)

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

    def test_core_check_background_deduplicates_and_never_applies_update(self):
        started = threading.Event()
        finish = threading.Event()
        def check(_cfg):
            started.set()
            self.assertTrue(finish.wait(timeout=5))
            return {"latest": "2.35.0"}

        with mock.patch.object(agy_accounts, "check_official_update", side_effect=check) as official, \
                mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.34.0"), \
                mock.patch.object(agy_accounts, "apply_language_server_update") as apply:
            self.app.check_powertools_update()
            self.assertTrue(started.wait(timeout=5))
            self.app.check_powertools_update()
            self.assertEqual(self.app.powertools_update()["check"]["state"], "checking")
            finish.set()
            self.assertTrue(self.app._pw_update_lock.acquire(timeout=5))
            self.app._pw_update_lock.release()
            result = self.app.powertools_update()
            self.assertEqual(result["check"]["state"], "success")
            self.assertEqual(result["latest"], "2.35.0")
            self.assertTrue(result["available"])
            self.assertGreater(result["check"]["checked_at"], 0)
            with self.assertRaises(agy_accounts.ApiError) as caught:
                self.app.check_powertools_update()
            self.assertEqual(caught.exception.status, 429)
            official.assert_called_once()
            apply.assert_not_called()

    def test_core_check_failure_not_reported_as_up_to_date(self):
        with mock.patch.object(agy_accounts, "check_official_update", side_effect=agy_accounts.ApiError(502, "网络超时")), \
                mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.34.0"):
            self.app.check_powertools_update()
            self.assertTrue(self.app._pw_update_lock.acquire(timeout=5))
            self.app._pw_update_lock.release()
            self.assertEqual(self.app.powertools_update()["check"], {"state": "failed", "checked_at": 0, "error": "网络超时"})

    def test_core_update_runs_separate_service_holds_lock_and_reports_worker_status(self):
        started, finish = threading.Event(), threading.Event()
        root = os.path.join(self.tmp, "core-updates")

        def worker(args, **kwargs):
            self.assertEqual(args, ["systemctl", "start", "agy-core-update"])
            started.set()
            self.assertTrue(finish.wait(timeout=5))
            plan = agy_accounts.read_json(os.path.join(root, "plan.json"))
            agy_accounts.write_json_atomic(os.path.join(root, "state.json"), {
                "id": plan["id"], "version": plan["version"], "state": "success", "message": "已验证 2.35.1"})
            return mock.Mock(returncode=0)

        with mock.patch.object(self.app, "core_update_root", return_value=root), \
                mock.patch.object(self.app, "core_update_enabled", return_value=True), \
                mock.patch.object(agy_accounts, "update_status", return_value={"running": "2.35.0", "latest": "2.35.1", "available": True}), \
                mock.patch.object(agy_accounts.subprocess, "run", side_effect=worker):
            with self.assertRaises(agy_accounts.ApiError):
                self.app.start_core_update("2.34.0")
            result = self.app.start_core_update("2.35.1")
            self.assertTrue(started.wait(timeout=5))
            self.assertEqual(self.app.powertools_update()["update"]["state"], "pending")
            with self.assertRaises(agy_accounts.ApiError):
                self.app.operation_lock.hold("账号切换").__enter__()
            with self.assertRaises(agy_accounts.ApiError):
                self.app.start_core_update("2.35.1")
            finish.set()
            self.assertTrue(self.app._core_update_lock.acquire(timeout=5))
            self.app._core_update_lock.release()
            self.assertEqual(self.app.core_update_job()["id"], result["jobId"])
            self.assertEqual(self.app.core_update_job()["state"], "success")
            with self.app.operation_lock.hold("后续操作"):
                pass

    def test_plugin_update_reserves_shared_lock_and_releases_after_worker(self):
        started, finish = threading.Event(), threading.Event()
        root = os.path.join(self.tmp, 'updates')
        def worker(args, **kwargs):
            self.assertEqual(args, ['systemctl', 'start', 'agy-powertools-update'])
            started.set()
            self.assertTrue(finish.wait(timeout=5))
            agy_accounts.write_json_atomic(os.path.join(root, 'state.json'), {"status": "completed", "version": "2.14.3"})
            return mock.Mock(returncode=0)
        with mock.patch.object(self.app, 'plugin_update_root', return_value=root), \
                mock.patch.object(agy_accounts.os.path, 'isfile', return_value=True), \
                mock.patch.object(agy_accounts.subprocess, 'run', side_effect=worker):
            self.app.start_plugin_update('2.14.3')
            self.assertTrue(started.wait(timeout=5))
            with self.assertRaises(agy_accounts.ApiError):
                self.app.operation_lock.hold('切号').__enter__()
            with self.assertRaises(agy_accounts.ApiError):
                self.app.start_plugin_update('2.14.3')
            finish.set()
            self.assertTrue(self.app._plugin_update_lock.acquire(timeout=5))
            self.app._plugin_update_lock.release()
            with self.app.operation_lock.hold('后续操作'):
                pass
            self.assertEqual(self.app.plugin_update_status()['status'], 'completed')

    def test_update_rejects_read_only_sandbox_before_downloading(self):
        self.login()
        with mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.30.0"), \
                mock.patch.object(agy_accounts.os, "access", return_value=False), \
                mock.patch.object(agy_accounts.subprocess, "run") as command:
            status, data, _ = self.request("POST", "/api/update", {"action": "apply"})
        self.assertEqual(status, 503, data)
        self.assertIn("无法写入", data["error"])
        command.assert_not_called()
        with self.app.operation_lock.hold("再次操作"):
            pass

    def test_legacy_update_button_uses_worker_not_readonly_panel_download(self):
        self.login()
        with mock.patch.object(self.app, "core_update_enabled", return_value=True), \
                mock.patch.object(self.app, "powertools_update", return_value={"latest": "2.35.1"}), \
                mock.patch.object(self.app, "start_core_update", return_value={"jobId": "test-job"}) as worker, \
                mock.patch.object(agy_accounts, "apply_language_server_update") as direct:
            status, data, _ = self.request("POST", "/api/update", {"action": "apply"})
            self.assertEqual(status, 200)
            self.assertEqual(data["jobId"], "test-job")
            worker.assert_called_once_with("2.35.1")
            direct.assert_not_called()

    def test_changed_official_version_does_not_restart_unconfirmed_target(self):
        with mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.35.0"), \
                mock.patch.object(agy_accounts.os, "access", return_value=True), \
                mock.patch.object(agy_accounts.subprocess, "run", return_value=mock.Mock(
                    returncode=0, stdout="Latest version : 2.36.0", stderr="")), \
                mock.patch.object(agy_accounts, "stop_service") as stop:
            with self.assertRaises(agy_accounts.ApiError) as caught:
                agy_accounts.apply_language_server_update(self.loaded, self.app.operation_lock, expected_version="2.35.1")
            self.assertEqual(caught.exception.status, 409)
            stop.assert_not_called()

    def test_update_must_reach_expected_version(self):
        self.login()
        for actual, expected_status in [("2.30.0", 502), ("", 502), ("2.31.0", 200)]:
            with self.subTest(actual=actual), \
                    mock.patch.object(agy_accounts, "running_language_server_version",
                                      side_effect=["2.30.0", actual]), \
                    mock.patch.object(agy_accounts.os, "access", return_value=True), \
                    mock.patch.object(agy_accounts.subprocess, "run") as command, \
                    mock.patch.object(agy_accounts, "stop_service") as stop, \
                    mock.patch.object(agy_accounts, "start_service") as start, \
                    mock.patch.object(agy_accounts.time, "monotonic", side_effect=[0, 10]):
                command.return_value = mock.Mock(returncode=0, stderr="", stdout=(
                    "Latest version : \x1b[32m2.31.0\x1b[0m for linux-x64\n" + ("progress\n" * 250)))
                status, data, _ = self.request("POST", "/api/update", {"action": "apply"})
                self.assertEqual(status, expected_status, data)
                self.assertEqual(data["running"], actual)
                stop.assert_called_once()
                start.assert_called_once()

    def test_failed_config_write_leaves_runtime_password_and_failover_unchanged(self):
        self.login()
        old_hash = self.loaded["password_hash"]
        with mock.patch.object(agy_accounts, "write_json_atomic", side_effect=PermissionError):
            self.assertEqual(self.request("POST", "/api/failover", {"enabled": False})[0], 500)
            with self.assertRaises(agy_accounts.ApiError):
                agy_accounts.change_password(self.loaded, "test-password", "new-password")
        self.assertTrue(self.loaded["auto_failover"])
        self.assertEqual(self.loaded["password_hash"], old_hash)
        self.assertEqual(agy_accounts.read_json(self.config_path)["password_hash"], old_hash)

    def test_internal_accounts_require_key_and_do_not_authorize_other_routes(self):
        path = os.path.join(self.tmp, "accounts.key")
        agy_accounts.write_atomic(path, b"a" * 64)
        with mock.patch.dict(os.environ, {"AGY_ACCOUNTS_BRIDGE_KEY": path}):
            self.assertEqual(self.request("POST", "/internal/powertools/accounts", {"action": "list"})[0], 403)
            headers = {"Content-Type": "application/json", "X-Agy-Accounts-Bridge": "a" * 64}
            def internal(body, route="/internal/powertools/accounts"):
                conn = http.client.HTTPConnection("127.0.0.1", self.port)
                conn.request("POST", route, body=json.dumps(body), headers=headers)
                response = conn.getresponse()
                result = (response.status, json.loads(response.read()))
                conn.close()
                return result

            self.write_token('{"access_token":"candidate"}')
            account = agy_accounts.save_account(self.loaded, "candidate")
            with mock.patch.object(self.app.quota, "get_all", return_value={"quotas": {}}), \
                    mock.patch.object(self.app, "refresh_powertools_quotas"):
                status, data = internal({"action": "list"})
                self.assertEqual(status, 200)
                self.assertEqual(data["currentId"], account["id"])
                self.assertNotIn("access_token", json.dumps(data))
                self.assertNotIn("refresh_token", json.dumps(data))
                self.assertNotIn("accounts_dir", data)
                status, data = internal({"action": "alias", "accountId": account["id"], "alias": "新备注"})
                self.assertEqual(status, 200)
                self.assertEqual(data["accounts"][0]["name"], "新备注")
                status, data = internal({"action": "alias", "accountId": account["id"], "alias": ""})
                self.assertEqual(status, 200)
                self.assertEqual(data["accounts"][0]["name"], account["id"])
                self.assertEqual(internal({"action": "alias", "accountId": account["id"], "alias": "x" * 61})[0], 400)
                self.assertEqual(internal({"action": "delete", "accountId": account["id"]})[0], 400)
                self.assertEqual(internal({}, "/api/password")[0], 400)
                self.assertEqual(internal({"action": "switch", "accountId": account["id"]})[0], 400)
                with mock.patch.object(self.app, "powertools_update", return_value={"running": "2.34.0"}), \
                        mock.patch.object(self.app, "check_powertools_update") as check:
                    self.assertEqual(internal({"action": "core_update_status"})[0], 200)
                    check.assert_not_called()
                    self.assertEqual(internal({"action": "core_update_check"})[0], 200)
                    check.assert_called_once()
                    self.assertEqual(internal({"action": "core_update_apply"})[0], 400)
                    with mock.patch.object(self.app, "start_core_update") as apply:
                        self.assertEqual(internal({"action": "core_update_apply", "version": "2.35.1", "confirmed": True})[0], 200)
                        apply.assert_called_once_with("2.35.1")

    def test_background_switch_uses_shared_lock_and_reports_verification_result(self):
        self.write_token('{"access_token":"candidate"}')
        account = agy_accounts.save_account(self.loaded, "candidate")
        self.write_token('{"access_token":"original"}')

        def complete_job():
            self.assertTrue(self.app._pw_job_lock.acquire(timeout=5))
            self.app._pw_job_lock.release()
            return dict(self.app._pw_job)

        # A stale browser must never switch a newly selected account.
        self.app.start_powertools_switch(account["id"], "stale-current")
        self.assertEqual(complete_job()["state"], "failed")
        self.assertEqual(agy_accounts.read_bytes(self.token_path), b'{"access_token":"original"}')
        self.verify_auth.assert_not_called()

        with self.app.operation_lock.hold("系统更新"):
            self.app.start_powertools_switch(account["id"], "")
            self.assertEqual(complete_job()["state"], "failed")
        self.verify_auth.assert_not_called()

        self.verify_auth.return_value = None
        result = self.app.start_powertools_switch(account["id"], "")
        job = complete_job()
        self.assertEqual(job["id"], result["jobId"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("已回滚", job["message"])
        self.assertEqual(agy_accounts.read_bytes(self.token_path), b'{"access_token":"original"}')

        self.verify_auth.return_value = {"signed_in": True, "ineligible": False, "message": ""}
        self.app.start_powertools_switch(account["id"], "")
        self.assertEqual(complete_job()["state"], "success")
        self.assertEqual(agy_accounts.read_bytes(self.token_path), b'{"access_token":"candidate"}')

    def test_cached_quota_read_does_not_wait_for_network_refresh(self):
        self.write_token('{"access_token":"candidate"}')
        agy_accounts.save_account(self.loaded, "candidate")
        with self.app.quota._fetch_lock:
            result = self.app.quota.get_all(cache_only=True)
        self.assertEqual(len(result["quotas"]), 1)


    def test_version_compare_only_reports_strictly_newer_versions(self):
        for candidate, current, expected in (
                ("2.35.1", "2.35.0", True),
                ("2.36.0", "2.35.9", True),
                ("2.35.1", "2.35.1", False),
                ("2.35.0", "2.35.1", False),
                ("2.35.1.0", "2.35.1", False)):
            with self.subTest(candidate=candidate, current=current):
                self.assertEqual(agy_accounts.version_is_newer(candidate, current), expected)

    def test_stale_update_results_never_prompt_downgrade_or_keep_badge(self):
        with mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.35.1"), \
                mock.patch.object(agy_accounts, "staged_language_server_version", return_value="2.35.0"):
            self.assertFalse(agy_accounts.update_status(self.loaded)["available"])
        with mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.35.1"), \
                mock.patch.object(agy_accounts, "staged_language_server_version", return_value="2.35.2"):
            self.assertTrue(agy_accounts.update_status(self.loaded)["available"])

        with mock.patch.object(agy_accounts, "running_language_server_version", return_value="2.35.1"), \
                mock.patch.object(agy_accounts, "staged_language_server_version", return_value=""):
            with self.app._pw_update_state_lock:
                self.app._pw_update = {"state": "success", "latest": "2.35.0", "checked_at": 1}
            info = self.app.powertools_update()
            self.assertFalse(info["available"])
            with self.app._pw_update_state_lock:
                self.app._pw_update = {"state": "success", "latest": "2.35.2", "checked_at": 1}
            self.assertTrue(self.app.powertools_update()["available"])

    # -- 网页版新增账号 --------------------------------------------------

    @staticmethod
    def refreshed(raw, email):
        obj = json.loads(raw.decode("utf-8"))
        obj["token"].update({"access_token": "ya29.test", "email": email})
        return json.dumps(obj).encode("utf-8")

    def test_web_import_token_saves_verified_snapshot_and_duplicate(self):
        self.login()
        refresh = "1//refresh-token-for-web-import"
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=lambda raw: self.refreshed(raw, "web@example.com")):
            status, data, _ = self.request("POST", "/api/accounts/import-token", {
                "refreshToken": refresh, "name": "网页导入",
            })
            self.assertEqual(status, 200, data)
            self.assertTrue(data["created"])
            self.assertEqual(data["account"]["name"], "网页导入")
            self.assertEqual(data["account"]["email"], "web@example.com")

            status, data, _ = self.request("POST", "/api/accounts/import-token", {
                "refreshToken": refresh, "name": "另一个名字",
            })
        self.assertEqual(status, 200, data)
        self.assertFalse(data["created"])
        self.assertTrue(data["duplicate"])
        self.assertEqual(len(self.app.status_payload()["accounts"]), 1)
        with open(os.path.join(self.accounts_dir, data["account"]["id"], "token"), "rb") as fh:
            self.assertEqual(agy_accounts.refresh_token_of(fh.read()), refresh)

        # 未提供名称时应回填验证后拿到的邮箱，而不是落成“账号”。
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=lambda raw: self.refreshed(raw, "bare@example.com")):
            status, bare, _ = self.request("POST", "/api/accounts/import-token", {
                "refreshToken": "1//bare-refresh-token-value",
            })
        self.assertEqual(status, 200, bare)
        self.assertEqual(bare["account"]["email"], "bare@example.com")
        self.assertEqual(bare["account"]["name"], "bare@example.com")

        status, data, _ = self.request("POST", "/api/accounts/import-token", {
            "refreshToken": "not-a-refresh-token",
        })
        self.assertEqual(status, 400)
        self.assertIn("1//", data["error"])

    def test_web_import_token_rolls_back_when_google_rejects(self):
        self.login()
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=agy_accounts.QuotaError("该账号的登录已失效")):
            status, data, _ = self.request("POST", "/api/accounts/import-token", {
                "refreshToken": "1//expired-refresh-token",
            })
        self.assertEqual(status, 400)
        self.assertIn("无法验证", data["error"])
        self.assertEqual(self.app.status_payload()["accounts"], [])

    def test_web_import_file_accepts_json_export_and_vscdb(self):
        self.login()
        export = {"schema_version": 2, "accounts": [{
            "email": "export@example.com", "name": "导出账号",
            "refresh_token": "1//json-export-refresh-token",
        }]}
        encoded = base64.b64encode(json.dumps(export).encode("utf-8")).decode("ascii")
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=lambda raw: self.refreshed(raw, "export@example.com")):
            status, data, _ = self.request("POST", "/api/accounts/import-file", {
                "filename": "agy-export.json", "data": encoded,
            })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["imported"], 1)
        self.assertEqual(data["sources"][0]["source"], "agy-export.json")

        database = os.path.join(self.tmp, "state.vscdb")
        connection = sqlite3.connect(database)
        try:
            connection.execute("CREATE TABLE ItemTable (key TEXT, value BLOB)")
            connection.execute("INSERT INTO ItemTable VALUES (?, ?)", (
                "antigravity.auth",
                json.dumps({"token": {"refresh_token": "1//vscdb-refresh-token",
                                      "email": "db@example.com"}}).encode("utf-8")))
            connection.commit()
        finally:
            connection.close()
        with open(database, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=lambda raw: self.refreshed(raw, "db@example.com")):
            status, data, _ = self.request("POST", "/api/accounts/import-file", {
                "filename": "state.vscdb", "data": encoded,
            })
        self.assertEqual(status, 200, data)
        self.assertEqual(data["imported"], 1)
        ids = {account["email"] for account in self.app.status_payload()["accounts"]}
        self.assertEqual(ids, {"export@example.com", "db@example.com"})

    def test_web_import_file_rejects_unknown_and_corrupted_vscdb(self):
        self.login()
        encoded = base64.b64encode(b"nothing useful here").decode("ascii")
        status, data, _ = self.request("POST", "/api/accounts/import-file", {
            "filename": "notes.txt", "data": encoded,
        })
        self.assertEqual(status, 400)
        self.assertIn("没有找到", data["error"])

        corrupt = base64.b64encode(b"SQLite format 3\x00truncated").decode("ascii")
        status, data, _ = self.request("POST", "/api/accounts/import-file", {
            "filename": "broken.vscdb", "data": corrupt,
        })
        self.assertEqual(status, 400)
        self.assertIn("损坏", data["error"])

    def test_web_scan_imports_current_antigravity_login_once(self):
        self.login()
        self.write_token(json.dumps({
            "token": {"refresh_token": "1//current-live-refresh-token",
                      "email": "live@example.com"},
        }))
        with mock.patch.object(self.app.quota, "verify_refresh_token",
                               side_effect=lambda raw: self.refreshed(raw, "live@example.com")):
            status, data, _ = self.request("POST", "/api/accounts/scan", {})
            self.assertEqual(status, 200, data)
            self.assertEqual(data["imported"], 1)
            self.assertEqual(data["sources"][0]["source"], "当前 Antigravity 登录")
            status, data, _ = self.request("POST", "/api/accounts/scan", {})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["imported"], 0)
        self.assertEqual(data["duplicate"], 1)
        self.assertFalse(data["failed"])

    def test_import_validation_does_not_hold_operation_lock_during_network_call(self):
        self.login()
        checked = []

        def verify(raw):
            with self.app.operation_lock.hold("并发检查"):
                checked.append(True)
            return self.refreshed(raw, "nolock@example.com")

        with mock.patch.object(self.app.quota, "verify_refresh_token", side_effect=verify):
            status, data, _ = self.request("POST", "/api/accounts/import-token", {
                "refreshToken": "1//nolock-refresh-token",
            })
        self.assertEqual(status, 200, data)
        self.assertEqual(checked, [True])

    def test_rename_empty_clears_alias_and_falls_back_to_email(self):
        self.login()
        self.write_token(json.dumps({"access_token": "aaa", "email": "clear@example.com"}))
        self.assertEqual(self.save("自定义备注")[0], 200)
        account_id = self.account_id("自定义备注")
        status, data, _ = self.request("POST", "/api/accounts/%s/rename" %
                                       urllib.parse.quote(account_id, safe=""),
                                       {"name": ""})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["account"]["name"], "clear@example.com")

    def test_stale_core_update_state_expires_after_crash(self):
        root = os.path.join(self.tmp, "core-updates")
        agy_accounts.ensure_dir(root)
        agy_accounts.write_json_atomic(os.path.join(root, "state.json"), {
            "id": "a" * 32, "version": "2.35.1", "state": "restarting",
            "message": "正在重启", "at": agy_accounts.now() - 900,
        })
        with mock.patch.object(self.app, "core_update_root", return_value=root):
            job = self.app.core_update_job()
        self.assertEqual(job["state"], "failed")
        self.assertIn("超时", job["message"])


class UsagePerformanceTest(unittest.TestCase):
    @staticmethod
    def varint(value):
        encoded = bytearray()
        while value >= 128:
            encoded.append((value & 127) | 128)
            value >>= 7
        encoded.append(value)
        return bytes(encoded)

    def field(self, number, value):
        if isinstance(value, int):
            return self.varint(number << 3) + self.varint(value)
        return self.varint((number << 3) | 2) + self.varint(len(value)) + value

    def generation(self, tokens, latency=4, duration=2, nanos=250_000_000):
        f = self.field
        usage = f(2, 300) + f(3, tokens + 600) + f(9, 600) + f(10, tokens)
        return f(1, f(4, usage) + f(11, f(1, latency) + f(2, nanos))
                 + f(12, f(1, duration)) + f(19, b"test-model"))

    def fixture(self, directory, rows, extra_generation=False, legacy=False):
        path = os.path.join(directory, "conversation.db")
        f = self.field
        with sqlite3.connect(path) as conn:
            columns = "" if legacy else ", status INTEGER, step_payload BLOB"
            conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB"
                         + columns + ")")
            conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
            for i, (stamp, status, stop_reason, tool_call, blob) in enumerate(rows):
                metadata = f(1, f(1, stamp)) if stamp else None
                response = f(1, b"synthetic reply") + f(12, stop_reason)
                if tool_call:
                    response += f(7, b"")
                if legacy:
                    conn.execute("INSERT INTO steps VALUES (?, 15, ?)", (50 + i, metadata))
                else:
                    conn.execute("INSERT INTO steps VALUES (?, 15, ?, ?, ?)",
                                 (50 + i, metadata, status, f(20, response)))
                conn.execute("INSERT INTO gen_metadata VALUES (?, ?)", (i, blob))
            if extra_generation:
                conn.execute("INSERT INTO gen_metadata VALUES (1000, ?)", (self.generation(100),))
        conn.close()
        return path

    def test_recent_performance_uses_completed_text_and_median(self):
        now = int(time.time())
        rows = [
            (now, 3, 2, False, self.generation(200)),
            (now - 1, 3, 2, False, self.generation(400, latency=6)),
            (now - 2, 3, 2, True, self.generation(900)),
            (now - 3, 1, 2, False, self.generation(900)),
            (now - 4, 3, 1, False, self.generation(900)),
            (now - 5, 3, 2, False, self.generation(99)),
            (now - 6, 3, 2, False, self.generation(300, duration=0)),
            (now - 7, 3, 2, False, self.generation(300, nanos=1_000_000_000)),
            (now - 8, 3, 2, False, self.generation(300, latency=2**64 - 1)),
            (now - 9, 3, 2, False, b"\x0a\x01\x20"),
            (now - 700_000, 3, 2, False, self.generation(900)),
            (now + 60, 3, 2, False, self.generation(900)),
            (None, 3, 2, False, self.generation(900)),
        ]
        stats = agy_accounts.UsageStats({})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.fixture(tmp, rows)
            with mock.patch.object(stats, "_databases", return_value=[path]):
                data = stats.get()
        performance = data["recent_performance"]
        self.assertEqual(data["unreadable_databases"], 0)
        self.assertEqual(performance["sample_count"], 2)
        self.assertEqual(performance["first_text_seconds"], 5.25)
        self.assertEqual(performance["body_tokens_per_second"], 150)

    def test_recent_performance_limits_samples(self):
        now = int(time.time())
        rows = [(now - i, 3, 2, False, self.generation(200 + i * 10)) for i in range(12)]
        stats = agy_accounts.UsageStats({})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.fixture(tmp, rows)
            with mock.patch.object(stats, "_databases", return_value=[path]):
                performance = stats.get()["recent_performance"]
        self.assertEqual(performance["sample_count"], 10)
        self.assertEqual(performance["body_tokens_per_second"], 122.5)

    def test_missing_steps_do_not_invent_performance_or_discard_token_totals(self):
        for options in ({"extra_generation": True}, {"legacy": True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                stats = agy_accounts.UsageStats({})
                path = self.fixture(tmp, [(int(time.time()), 3, 2, False, self.generation(200))], **options)
                with mock.patch.object(stats, "_databases", return_value=[path]):
                    data = stats.get()
                self.assertIsNone(data["recent_performance"])
                self.assertEqual(data["last_7_days"]["output_tokens"], 800)


if __name__ == "__main__":
    unittest.main(verbosity=2)
