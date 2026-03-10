# Publish Workflow

这份文档说明如何把线上实际运行目录 `vui-plan` 的改动，同步到发布仓库 `V-UI`，再脱敏并推送到 GitHub。

## 目录约定

- 实际运行目录：`/root/vui-plan`
- 发布仓库目录：`/root/V-UI`
- GitHub 仓库：`git@github.com:Aaowu/V-UI.git`

## 总体原则

- 所有功能开发、线上验证，优先在 `vui-plan` 完成
- 所有文档、截图、发布说明，优先在 `V-UI` 维护
- `release-sanitize.sh` 只在 `V-UI` 执行，不要在运行中的 `vui-plan` 执行
- 推送前一定检查是否误带了数据库、账号文件、证书、`.env` 等敏感内容

## 标准发布流程

### 1. 先在 `vui-plan` 完成功能与验证

先确认当前线上运行正常：

```bash
bash scripts/panel-docker.sh status
curl -s http://127.0.0.1:9200/healthz
```

如果改了页面，建议至少手动确认：
- 登录页
- 概览页
- 链接页
- 活动明细页
- 设置页

### 2. 同步代码到 `V-UI`

只同步应用代码与前端资源，避免误覆盖发布专用文档：

```bash
cp /root/vui-plan/app.py /root/V-UI/app.py
cp -a /root/vui-plan/templates/. /root/V-UI/templates/
cp -a /root/vui-plan/static/. /root/V-UI/static/
```

如果本次也改了这些发布侧文件，再按需同步或直接在 `V-UI` 修改：
- `README.md`
- `.env.example`
- `docker-compose.yml`
- `scripts/install.sh`
- `scripts/panel-docker.sh`
- `scripts/migrate-to-docker.sh`
- `scripts/release-sanitize.sh`
- `docs/`
- `1.png`
- `2.png`

### 3. 查看同步结果

```bash
cd /root/V-UI
git status --short
git diff --stat
```

如果想快速确认 `vui-plan` 和 `V-UI` 的差异，可以用：

```bash
diff -qr \
  --exclude .git \
  --exclude .venv \
  --exclude __pycache__ \
  --exclude data \
  --exclude docker-data \
  /root/vui-plan /root/V-UI
```

### 4. 在 `V-UI` 执行脱敏

```bash
cd /root/V-UI
bash scripts/release-sanitize.sh
```

这个脚本会清理：
- `.venv`
- `data/panel.db`
- `data/admin_credentials.txt`
- `docker-data/panel.db`
- `docker-data/admin_credentials.txt`
- `__pycache__`

### 5. 做发布前校验

```bash
cd /root/V-UI
bash -n scripts/install.sh
bash -n scripts/panel-docker.sh
bash -n scripts/migrate-to-docker.sh
docker compose -f docker-compose.yml config >/dev/null
bash -n scripts/release-sanitize.sh
python3 -m py_compile app.py
```

如有需要，再做一次敏感信息扫描：

```bash
cd /root/V-UI
rg -n 'example\\.com|example\\.net|BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY|XRAY_REALITY_PRIVATE_KEY=.+[^$]' . \
  --glob '!data/**' \
  --glob '!docker-data/**' \
  --glob '!.venv/**' \
  --glob '!static/vendor/**'
```

## 6. 提交并推送 GitHub

先拉一下远端，避免直接推冲突：

```bash
cd /root/V-UI
git pull --rebase origin main
```

然后提交并推送：

```bash
cd /root/V-UI
git add .
git commit -m "Sync runtime updates"
git push origin main
```

## 一次完整示例

```bash
cp /root/vui-plan/app.py /root/V-UI/app.py
cp -a /root/vui-plan/templates/. /root/V-UI/templates/
cp -a /root/vui-plan/static/. /root/V-UI/static/

cd /root/V-UI
bash scripts/release-sanitize.sh
bash -n scripts/install.sh
bash -n scripts/panel-docker.sh
bash -n scripts/migrate-to-docker.sh
docker compose -f docker-compose.yml config >/dev/null
bash -n scripts/release-sanitize.sh
python3 -m py_compile app.py

git status --short
git add .
git commit -m "Sync runtime updates"
git push origin main
```

## 文档 / 截图单独发布

如果你只是更新了文档、截图或仓库说明，不需要先动 `vui-plan`，可以直接在 `V-UI` 里改并发布。

例如：

```bash
cd /root/V-UI
git add README.md docs/ 1.png 2.png
git commit -m "Update docs and screenshots"
git push origin main
```

## 发布前检查清单

发布前确认这些项目：

- 没有提交 `.env`
- 没有提交 `data/panel.db`
- 没有提交 `data/admin_credentials.txt`
- 没有提交 `docker-data/panel.db`
- 没有提交 `docker-data/admin_credentials.txt`
- 没有提交证书、私钥、密钥材料
- `scripts/install.sh` 语法检查通过
- `scripts/panel-docker.sh` 语法检查通过
- `scripts/migrate-to-docker.sh` 语法检查通过
- `docker-compose.yml` 配置检查通过
- `app.py` 语法检查通过
- README 截图与实际界面一致
- GitHub 提交信息能说明这次更新内容

## 注意事项

- 不建议直接在 `V-UI` 上做大量线上验证改动
- 不建议在运行中的 `vui-plan` 执行 `release-sanitize.sh`
- 如果改了安装逻辑，至少跑一次 `bash -n scripts/install.sh`
- 如果改了 Docker 部署逻辑，至少跑一次 `docker compose -f docker-compose.yml config`
- 如果改了静态资源并发现浏览器没更新，记得同步调整版本号参数
