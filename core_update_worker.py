#!/usr/bin/env python3
"""独立限权服务执行核心更新；浏览器不能指定命令、配置路径或下载地址。"""
import argparse
import os
import re
import tempfile

import agy_accounts as app


def preflight(cfg, root):
    """验证独立服务写权限，不下载、不替换核心、不重启服务。"""
    for directory in (os.path.dirname(cfg["language_server_path"]), app.updater_home(), root):
        with tempfile.TemporaryFile(dir=directory) as handle:
            handle.write(b"core-update-permission-check")
            handle.flush()
    return 0


def run(cfg, root, apply=app.apply_language_server_update):
    plan = app.read_json(os.path.join(root, "plan.json"))
    version, job_id = plan.get("version"), plan.get("id")
    if (not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id)
            or not isinstance(plan.get("at"), int) or not 0 <= app.now() - plan["at"] <= 60):
        raise app.ApiError(400, "核心更新计划无效或已过期")

    def progress(state, message):
        app.write_json_atomic(os.path.join(root, "state.json"), {
            "id": job_id, "version": version, "state": state, "message": message, "at": app.now()})

    try:
        result = apply(cfg, app.ServiceOperationLock(), expected_version=version, progress=progress)
        if result.get("running") != version:
            raise app.ApiError(502, "目标核心版本未验证，不能报告成功")
        progress("success", result["message"] + "：" + version)
        return 0
    except app.ApiError as exc:
        progress("failed", exc.message)
        return 1
    except Exception:
        app.log.exception("核心更新工作进程异常")
        progress("failed", "核心更新异常，请核对服务与运行版本")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    app.setup_logging("INFO")
    config = app.load_config(args.config)
    directory = app.expand_path("~/.local/share/agy-accounts/core-updates")
    raise SystemExit(preflight(config, directory) if args.preflight else run(config, directory))
