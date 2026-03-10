# Security Policy

## Supported Versions

当前建议只使用最新版本。

## Reporting a Vulnerability

如果你发现安全问题，请不要直接公开提交敏感细节。

建议处理方式：
- 先私下联系维护者
- 描述影响范围、复现步骤、修复建议
- 等待修复后再公开披露

## Security Notes

本项目不会尝试解密 `HTTPS / REALITY` 流量内容。
活动明细仅展示连接层可被动观测到的目标域名 / IP、时间、来源 IP 和路由信息。

当前默认安全措施包括：
- 登录 Cookie 使用 `HttpOnly` + `SameSite=Lax`
- 后台敏感 `POST` 操作启用 CSRF 校验
- 登录失败同时受应用内限速和 `fail2ban` 封禁保护
