import json
import os
import tempfile
import unittest

import agy_accounts as app
from core_update_worker import run


class CoreUpdateWorkerTest(unittest.TestCase):
    def test_worker_passes_fixed_version_and_requires_verified_result(self):
        for success in (True, False):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as root:
                app.write_json_atomic(os.path.join(root, "plan.json"), {
                    "id": "a" * 32, "version": "2.35.1", "at": app.now()})

                def apply(cfg, lock, expected_version, progress):
                    self.assertEqual(expected_version, "2.35.1")
                    progress("verifying", "验证中")
                    return {"running": "2.35.1" if success else "2.35.0", "message": "已验证更新"}

                self.assertEqual(run({}, root, apply), 0 if success else 1)
                with open(os.path.join(root, "state.json")) as handle:
                    state = json.load(handle)
                self.assertEqual(state["state"], "success" if success else "failed")

    def test_invalid_or_expired_plan_never_runs_updater(self):
        with tempfile.TemporaryDirectory() as root:
            app.write_json_atomic(os.path.join(root, "plan.json"), {
                "id": "a" * 32, "version": "../../shell", "at": app.now()})
            with self.assertRaises(app.ApiError):
                run({}, root, lambda *args, **kwargs: self.fail("不应执行"))
