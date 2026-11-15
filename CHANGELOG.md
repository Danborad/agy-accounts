# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [1.0.0] - 2026-11-16

### 新增

- 面向 AFSlayer/antigravity-server 的账号切换面板
- 保存 / 切换 / 重命名 / 删除账号快照
- 当前账号判定：字节一致或 `refresh_token` 相同，无需额外状态文件
- 切换流程：停止 → 备份 → 原子写入 → 启动 → 探测，失败自动回滚
- 切换互斥，并发请求返回 409
- 令牌轮换回写：access_token / 过期时间刷新后自动同步快照
- PBKDF2 密码哈希、常量时间比较、HttpOnly / SameSite=Strict 会话
- 登录失败按 IP 限速
- CSRF 请求头与 Origin 校验
- 暗色响应式单页界面，切换成功自动跳转
- `/healthz` 与 `/api/status`
- systemd 单元、安装脚本、卸载脚本、sudoers 示例
- 不依赖真实 agy-server 的自动化冒烟测试（13 项）
- 部署指南与验收清单
- 环境变量覆盖与 `check` 自检命令

### 已知限制

- 不包含网页汉化注入，计划在后续版本以独立反向代理实现
- 不实现 Google 授权流程，新账号仍在 Antigravity 页面登录
- 切换过程会短暂中断 `agy-server`
