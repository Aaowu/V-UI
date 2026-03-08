#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT_DIR"

echo "[INFO] 清理本地运行产物..."
python3 - <<'PY'
from pathlib import Path
import shutil
root = Path('.').resolve()
for target in [
    root / '.venv',
    root / '__pycache__',
    root / 'data' / 'panel.db',
    root / 'data' / 'admin_credentials.txt',
]:
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
PY

echo "[INFO] 扫描常见敏感信息..."
rg -n 'BEGIN (RSA|EC|OPENSSH|PRIVATE) KEY|XRAY_REALITY_PRIVATE_KEY=|set-cookie:|subscription-userinfo: upload=|api_token|secret|password=' . \
  --glob '!data/**' --glob '!.venv/**' --glob '!static/vendor/**' || true

echo
cat <<MSG
[OK] 本地运行产物已清理。
发布前仍建议人工再检查：
- README 里的示例域名是否为通用值
- 截图 / GIF / issue 模板中是否出现真实域名或账号
- .env 是否没有被提交
- data/ 目录中是否没有敏感内容
MSG
