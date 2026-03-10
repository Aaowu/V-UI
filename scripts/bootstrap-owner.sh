#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

PANEL_RUNTIME=${PANEL_RUNTIME:-docker}
OWNER_USERNAME=${PANEL_BOOTSTRAP_OWNER_USERNAME:-admin}
OWNER_PASSWORD=${PANEL_BOOTSTRAP_OWNER_PASSWORD:-}
HOST_DATA_DIR=${PANEL_HOST_DATA_DIR:-${PANEL_DATA_DIR:-$ROOT_DIR/docker-data}}
DB_PATH="$HOST_DATA_DIR/panel.db"

mkdir -p "$HOST_DATA_DIR"

has_admin_users() {
  if [[ ! -f "$DB_PATH" ]]; then
    return 1
  fi
  python3 - "$DB_PATH" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
conn = sqlite3.connect(path)
try:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='admin_users'"
    ).fetchone()
    if not row:
        raise SystemExit(1)
    count = conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
    raise SystemExit(0 if count > 0 else 1)
finally:
    conn.close()
PY
}

if has_admin_users; then
  echo "[INFO] 已存在管理员用户，跳过初始化"
  exit 0
fi

if [[ -z "$OWNER_PASSWORD" ]]; then
  OWNER_PASSWORD=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18)[:20])
PY
)
fi

if [[ ${#OWNER_PASSWORD} -lt 12 ]]; then
  echo "[ERROR] PANEL_BOOTSTRAP_OWNER_PASSWORD 至少需要 12 位"
  exit 1
fi

bootstrap_python='from app import init_db, ensure_default_settings_from_xray, ensure_admin_user, bootstrap_links_from_xray; init_db(); ensure_default_settings_from_xray(); ensure_admin_user(); bootstrap_links_from_xray()'

if [[ "$PANEL_RUNTIME" == "docker" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "[ERROR] 未找到 docker 命令"
    exit 1
  fi
  (
    cd "$ROOT_DIR"
    PANEL_BOOTSTRAP_OWNER_USERNAME="$OWNER_USERNAME" \
    PANEL_BOOTSTRAP_OWNER_PASSWORD="$OWNER_PASSWORD" \
    docker compose -f docker-compose.yml build vui-plan >/dev/null
    PANEL_BOOTSTRAP_OWNER_USERNAME="$OWNER_USERNAME" \
    PANEL_BOOTSTRAP_OWNER_PASSWORD="$OWNER_PASSWORD" \
    docker compose -f docker-compose.yml run --rm vui-plan python -c "$bootstrap_python" >/dev/null
  )
else
  if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
    echo "[ERROR] 未找到本地虚拟环境，请先完成面板依赖安装"
    exit 1
  fi
  (
    cd "$ROOT_DIR"
    PANEL_BOOTSTRAP_OWNER_USERNAME="$OWNER_USERNAME" \
    PANEL_BOOTSTRAP_OWNER_PASSWORD="$OWNER_PASSWORD" \
    "$ROOT_DIR/.venv/bin/python" -c "$bootstrap_python" >/dev/null
  )
fi

echo "[OK] 初始管理员已创建"
echo "- 用户名: $OWNER_USERNAME"
echo "- 密码: $OWNER_PASSWORD"
