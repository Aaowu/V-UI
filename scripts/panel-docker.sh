#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi
SERVICE_NAME=${PANEL_CONTAINER_NAME:-vui-plan}
ACTION=${1:-up}

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] 未找到 docker 命令"
  exit 1
fi

run_compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

case "$ACTION" in
  up)
    run_compose up -d --build
    ;;
  start)
    run_compose up -d
    ;;
  stop)
    run_compose stop
    ;;
  restart)
    run_compose restart
    ;;
  down)
    run_compose down
    ;;
  ps|status)
    run_compose ps
    ;;
  logs)
    run_compose logs -f "$SERVICE_NAME"
    ;;
  pull)
    run_compose build --pull "$SERVICE_NAME"
    ;;
  *)
    cat <<'MSG'
用法:
  bash scripts/panel-docker.sh up
  bash scripts/panel-docker.sh start
  bash scripts/panel-docker.sh stop
  bash scripts/panel-docker.sh restart
  bash scripts/panel-docker.sh down
  bash scripts/panel-docker.sh status
  bash scripts/panel-docker.sh logs
  bash scripts/panel-docker.sh pull
MSG
    exit 1
    ;;
esac
