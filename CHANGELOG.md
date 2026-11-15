# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。

## [1.1.0] - 2026-11-16

### 新增

- 账号额度显示：套餐等级、5 小时 / 每周剩余比例、重置时间
- `/api/quotas` 接口，支持 `?refresh=1` 强制刷新
- 额度缓存（默认 5 分钟）与失败回退到上次结果
- 快照令牌过期时自动用 `refresh_token` 续期，并与切换事务互斥回写
- 无令牌时提供「去登录 Google 账号」入口

### 安全

- 额度缓存与 OAuth 客户端缓存权限均为 `0600`
- 安装型 OAuth 客户端公开凭据拆段存放，避免密钥扫描误报

## [1.0.1] - 2026-11-16

### 修复

- systemd 单元改为 `--config <path> serve`，修复全局参数位置错误导致面板无法启动
- 安装脚本新增 `--listen` 与 `--probe-url`，支持局域网访问
- 升级安装时从现有配置读取实际监听地址与端口
- 登录页提示不再写死“只监听回环地址”
- 账号识别改用 `refresh_token`，正确处理 `access_token` 轮换

### 文档与交互

- 补充局域网部署命令与明文 HTTP 风险说明
- 未检测到令牌时，面板直接提供「去登录 Google 账号」入口

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
