# 安全策略

## 报告问题

请通过 GitHub Security Advisory 私下报告，不要开公开 Issue 贴出令牌、密码或可复现的绕过细节。

## 威胁模型

本项目假设：

- 面板只运行在你控制的机器上，默认监听 `127.0.0.1`
- 能读取 `~/.gemini` 与 `~/.config/agy-accounts` 的本机用户已被信任
- 访问面板的人已经知道共享密码

不防御：

- 已经拿到 root / 当前用户权限的攻击者
- 恶意 systemd 单元或已被替换的 `systemctl`
- 浏览器端恶意扩展读取已解锁页面
- 公网直接暴露且没有额外鉴权的部署方式

## 凭据边界

以下文件等同密码库：

```
~/.gemini/jetski-standalone-oauth-token
~/.local/share/agy-accounts/accounts/*/token
~/.config/agy-accounts/secret.key
```

要求：

- 权限分别为 `0600`、`0600`、`0600`，目录 `0700`
- 不提交版本库、不打包分享、不放公开网盘
- 迁移时使用 `scp` / `rsync` 并保留权限

## 已实现的控制

- 未配置密码拒绝启动
- PBKDF2-SHA256（200,000 次）+ 常量时间比较
- 会话 HMAC-SHA256 签名，`HttpOnly` + `SameSite=Strict`
- 登录失败按 IP 限速
- 写操作要求 `X-Agy-Accounts: 1` 并校验 `Origin`
- 无 CORS 响应头
- 令牌原子写入，失败回滚
- 日志不输出令牌、密码或 Cookie 正文
- 路径参数白名单，拒绝目录穿越
- systemd 单元启用 `ProtectSystem=strict`、`ProtectHome=read-only`、`UMask=0077`

## 部署红线

- 不要把面板监听在 `0.0.0.0` 后直接映射公网
- 不要把面板密码发给多人
- 不要用本项目绕过服务条款或做账号共享/售卖
