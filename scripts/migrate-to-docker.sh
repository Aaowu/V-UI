#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi
SERVICE_NAME=${SERVICE_NAME:-vui-plan}
LEGACY_DATA_DIR=${LEGACY_DATA_DIR:-$ROOT_DIR/data}
TARGET_DATA_DIR=${PANEL_HOST_DATA_DIR:-$ROOT_DIR/docker-data}

if [[ ${EUID} -ne 0 ]]; then
  echo "[ERROR] 请使用 root 执行迁移脚本"
  exit 1
fi

mkdir -p "$TARGET_DATA_DIR"

if [[ -d "$LEGACY_DATA_DIR" ]]; then
  cp -a "$LEGACY_DATA_DIR"/. "$TARGET_DATA_DIR"/
fi

systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true

bash "$ROOT_DIR/scripts/panel-docker.sh" up

for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:9200/healthz >/dev/null 2>&1; then
    echo "[OK] 面板已切换到 Docker"
    echo "- 数据目录: $TARGET_DATA_DIR"
    echo "- 查看状态: bash scripts/panel-docker.sh status"
    echo "- 查看日志: bash scripts/panel-docker.sh logs"
    exit 0
  fi
  sleep 1
done

echo "[ERROR] Docker 面板未在预期时间内完成启动"
echo "- 回滚可执行: systemctl enable --now $SERVICE_NAME"
exit 1
