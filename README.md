# V UI

一个面向 `Xray + VLESS + REALITY (xtls-rprx-vision)` 的轻量控制面板。

适合：
- 自用
- 偶尔分享给朋友
- 需要直接管理链接、流量上限、已用流量、订阅导入、访问活动明细

## 功能

- 登录后台
- 主账号 / 朋友账号权限分层
  - 主账号：可看全部、可配置系统、可新增朋友账号
  - 朋友账号：只看自己的概览 / 链接 / 活动 / 账户设置
- 链接管理
  - 新建 / 编辑 / 启停 / 删除
  - 独立 UUID
  - 独立订阅 Token
  - `Loon` 一键导入 / 订阅地址 / 通用 `vless://` / 二维码
- 流量管理
  - 朋友账号拥有一份“总额度”
  - 朋友账号可创建多个链接
  - 每个链接可设置“单链接额度”
  - 所有单链接额度总和不能超过该朋友账号的总额度
  - 链接用量与账号总额度联动显示
  - 每月重置日
  - 用尽后等待下一个周期恢复
- 概览页
  - 近 24 小时流量趋势
  - 各链接使用占比
  - 链接用量概览
  - 最近访问目标
- 活动明细页
  - 主账号：可看全部并按账户筛选
  - 朋友账号：只看自己的活动明细
- 系统设置页
  - 主账号：域名 / SNI / 端口 / Short ID / Reality Target / 服务控制 / 存储上限 / 新增朋友账号
  - 朋友账号：仅修改自己的用户名 / 密码
- 客户端流量信息
  - 订阅响应返回 `Subscription-Userinfo`
  - 客户端更新订阅后，可显示当前已用与总流量

## 安全边界说明

对于 `HTTPS / REALITY` 流量：
- 可以被动观察到目标域名 / IP、访问时间、来源 IP、路由方向
- 不能安全地读取完整网页 URL Path

如果强行实现完整 URL 级别明细，需要中间人解密，会破坏协议安全和隐私。本项目不这么做。

## 页面时间

页面默认显示：
- `Asia/Shanghai`
- `北京时间 (UTC+8)`

## 一键安装

这个仓库自带的一键安装脚本会自动完成：
- 安装官方 `Xray`（如果系统里还没有）
- 生成 `VLESS + REALITY + xtls-rprx-vision`
- 写入 `Xray API` / `access log` / `error log`
- 安装本面板
- 默认以 Docker 方式运行面板
- 写入 `nginx` 反代配置
- 打开 Reality 端口并尝试持久化防火墙规则

默认部署形态是：

- `Xray / nginx` 继续跑在宿主机
- `vui-plan` 面板跑在 Docker
- 宿主机 `nginx` 继续反代到 `127.0.0.1:9200`

### 1. 准备证书和域名

你需要先有：
- 一个可解析到 VPS 的域名
- 一张可用的 HTTPS 证书

### 2. 准备配置

```bash
cp .env.example .env
```

至少修改：

```bash
DOMAIN=panel.example.com
TLS_CERT=/etc/letsencrypt/live/panel.example.com/fullchain.pem
TLS_KEY=/etc/letsencrypt/live/panel.example.com/privkey.pem
PANEL_RUNTIME=docker
REALITY_SERVER_DOMAIN=panel.example.com
REALITY_SNI=panel.example.com
```

### 3. 执行安装

```bash
bash scripts/install.sh
```

也可以直接临时传参：

```bash
DOMAIN=panel.example.com \
TLS_CERT=/etc/letsencrypt/live/panel.example.com/fullchain.pem \
TLS_KEY=/etc/letsencrypt/live/panel.example.com/privkey.pem \
REALITY_SERVER_DOMAIN=panel.example.com \
REALITY_SNI=panel.example.com \
bash scripts/install.sh
```

安装完成后会输出：
- 面板登录地址
- 初始主账号文件位置
- 默认 `VLESS Reality Vision` 链接
- 面板 Docker 启停命令

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 9200 --reload
```

## Docker 运行

当前仓库默认推荐这条路径：

- `Xray / nginx` 继续跑在宿主机
- 只把 `vui-plan` 面板放进 Docker
- 容器通过挂载宿主机的 `Xray` 配置、日志、二进制和数据目录继续工作

仓库已经提供：

- `Dockerfile`
- `docker-compose.yml`

默认 `docker-compose.yml` 采用 `network_mode: host`，这样它可以继续访问宿主机上的 `127.0.0.1:10085` Xray API，也能继续配合现有的宿主机 `nginx -> 127.0.0.1:9200` 反代。

### 迁移步骤

先停止原来的 `systemd` 面板服务，避免和容器同时占用 `9200`：

```bash
systemctl stop vui-plan
systemctl disable vui-plan
```

然后在项目目录执行：

```bash
bash scripts/migrate-to-docker.sh
```

查看状态：

```bash
bash scripts/panel-docker.sh status
bash scripts/panel-docker.sh logs
curl http://127.0.0.1:9200/healthz
```

### Docker 模式的限制

- 默认不会在容器内直接控制宿主机 `systemd`
- 所以涉及 `Xray` 配置变更的操作，会先写入配置，再提示你去宿主机手动执行 `systemctl restart xray`
- 设置页里的服务控制按钮会根据当前运行模式自动降级
- 如果你确实要让容器代管宿主机服务，可以额外设置 `PANEL_SYSTEMCTL_COMMAND`，但这取决于你的 Docker 权限、挂载方式和宿主机环境，默认 compose 没有启用这条路径

如果你是直接用 `http://IP:9200` 访问容器，而不是走现有 HTTPS 反代，请把：

```bash
PANEL_SESSION_COOKIE_SECURE=0
```

否则浏览器不会在纯 HTTP 下带回登录 Cookie。

## 发布到 GitHub 前

如果你平时是在 `/root/vui-plan` 里开发、在 `/root/V-UI` 里发布，建议先阅读完整流程文档：

- `docs/PUBLISH_WORKFLOW.md`

它包含：同步代码、脱敏、校验、提交、推送 GitHub 的完整步骤。

执行：

```bash
bash scripts/release-sanitize.sh
```

这个脚本会：
- 清理本地 `.venv`
- 清理 `data/panel.db`
- 清理 `data/admin_credentials.txt`
- 清理 `docker-data/panel.db`
- 清理 `docker-data/admin_credentials.txt`
- 清理 `__pycache__`
- 粗扫常见敏感信息模式

## 常用文件

- 主程序：`app.py`
- 模板：`templates/`
- 静态资源：`static/`
- 一键安装：`scripts/install.sh`
- Docker 启停：`scripts/panel-docker.sh`
- 旧实例迁移到 Docker：`scripts/migrate-to-docker.sh`
- 发布前脱敏：`scripts/release-sanitize.sh`
- 示例配置：`.env.example`

## 常用命令

```bash
bash scripts/panel-docker.sh up
bash scripts/panel-docker.sh status
bash scripts/panel-docker.sh logs

systemctl status xray
systemctl restart xray
journalctl -u xray -f

systemctl status nginx
systemctl reload nginx
```
