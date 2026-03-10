#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ ${EUID} -ne 0 ]]; then
  echo "[ERROR] 请使用 root 执行安装脚本"
  exit 1
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  source "$ROOT_DIR/.env"
  set +a
fi

DOMAIN=${DOMAIN:-}
TLS_CERT=${TLS_CERT:-}
TLS_KEY=${TLS_KEY:-}
INSTALL_DIR=${INSTALL_DIR:-/opt/vui-plan}
SERVICE_NAME=${SERVICE_NAME:-vui-plan}
PANEL_CONTAINER_NAME=${PANEL_CONTAINER_NAME:-$SERVICE_NAME}
PANEL_RUNTIME=${PANEL_RUNTIME:-docker}
PANEL_PORT=${PANEL_PORT:-9200}
PANEL_BIND_HOST=${PANEL_BIND_HOST:-127.0.0.1}
PANEL_USER=${PANEL_USER:-root}
PANEL_DATA_DIR=${PANEL_DATA_DIR:-/var/lib/vui-plan}
PANEL_TIMEZONE=${PANEL_TIMEZONE:-Asia/Shanghai}
PANEL_TIMEZONE_LABEL=${PANEL_TIMEZONE_LABEL:-北京时间\ \(UTC+8\)}
PANEL_DEFAULT_TITLE=${PANEL_DEFAULT_TITLE:-V UI}
PANEL_SESSION_COOKIE_SECURE=${PANEL_SESSION_COOKIE_SECURE:-1}
PANEL_TRUSTED_PROXY_IPS=${PANEL_TRUSTED_PROXY_IPS:-127.0.0.1,::1}
PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS=${PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS:-5}
PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS=${PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS:-300}
PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS=${PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS:-900}
PANEL_BOOTSTRAP_OWNER_USERNAME=${PANEL_BOOTSTRAP_OWNER_USERNAME:-admin}
PANEL_BOOTSTRAP_OWNER_PASSWORD=${PANEL_BOOTSTRAP_OWNER_PASSWORD:-}
XRAY_CONFIG_PATH=${XRAY_CONFIG_PATH:-/usr/local/etc/xray/config.json}
XRAY_ENV_PATH=${XRAY_ENV_PATH:-/root/xray-reality-main.env}
XRAY_BIN=${XRAY_BIN:-/usr/local/bin/xray}
XRAY_API_SERVER=${XRAY_API_SERVER:-127.0.0.1:10085}
XRAY_LOG_DIR=${XRAY_LOG_DIR:-/var/log/xray}
REALITY_PORT=${REALITY_PORT:-30828}
REALITY_SERVER_DOMAIN=${REALITY_SERVER_DOMAIN:-$DOMAIN}
REALITY_SNI=${REALITY_SNI:-$DOMAIN}
REALITY_TARGET=${REALITY_TARGET:-127.0.0.1:443}
REALITY_UUID=${REALITY_UUID:-}
REALITY_EMAIL=${REALITY_EMAIL:-admin@example.com}
REALITY_SHORT_ID=${REALITY_SHORT_ID:-}
REALITY_PRIVATE_KEY=${REALITY_PRIVATE_KEY:-}
REALITY_PUBLIC_KEY=${REALITY_PUBLIC_KEY:-}
INSTALL_XRAY_IF_MISSING=${INSTALL_XRAY_IF_MISSING:-1}
BOOTSTRAP_OWNER_CREATED=0

if [[ "$PANEL_RUNTIME" != "docker" && "$PANEL_RUNTIME" != "systemd" ]]; then
  echo "[ERROR] PANEL_RUNTIME 仅支持 docker 或 systemd"
  exit 1
fi

if [[ -z "$DOMAIN" || -z "$TLS_CERT" || -z "$TLS_KEY" ]]; then
  cat <<MSG
[ERROR] 缺少必要变量。
至少需要提供：
  DOMAIN=panel.example.com
  TLS_CERT=/etc/letsencrypt/live/panel.example.com/fullchain.pem
  TLS_KEY=/etc/letsencrypt/live/panel.example.com/privkey.pem

示例：
  DOMAIN=panel.example.com \
  TLS_CERT=/etc/letsencrypt/live/panel.example.com/fullchain.pem \
  TLS_KEY=/etc/letsencrypt/live/panel.example.com/privkey.pem \
  bash scripts/install.sh
MSG
  exit 1
fi

backup_if_exists() {
  local target="$1"
  if [[ -e "$target" ]]; then
    cp -a "$target" "${target}.bak-$(date +%F-%H%M%S)"
  fi
}

ensure_apt_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl unzip ca-certificates nginx python3 python3-venv python3-pip jq tar fail2ban iptables-persistent netfilter-persistent || \
    apt-get install -y curl unzip ca-certificates nginx python3 python3-venv python3-pip jq tar fail2ban
  if [[ "$PANEL_RUNTIME" == "docker" ]]; then
    apt-get install -y docker.io docker-compose-plugin || apt-get install -y docker.io
  fi
}

ensure_docker_if_needed() {
  if [[ "$PANEL_RUNTIME" != "docker" ]]; then
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "[ERROR] 未找到 docker，请先安装 Docker"
    exit 1
  fi
  systemctl enable --now docker
  if ! docker compose version >/dev/null 2>&1; then
    echo "[ERROR] 未找到 docker compose 插件，请安装 docker-compose-plugin"
    exit 1
  fi
}

install_xray_if_needed() {
  if [[ -x "$XRAY_BIN" ]]; then
    return 0
  fi
  if [[ "$INSTALL_XRAY_IF_MISSING" != "1" ]]; then
    echo "[ERROR] 未找到 Xray，可执行文件：$XRAY_BIN"
    exit 1
  fi
  echo "[INFO] 未检测到 Xray，开始安装官方 Xray"
  curl -fsSL https://raw.githubusercontent.com/XTLS/Xray-install/main/install-release.sh -o /tmp/install-release.sh
  bash /tmp/install-release.sh install
}

ensure_xray_user() {
  id -u xray >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin xray
  mkdir -p /etc/systemd/system/xray.service.d
  cat > /etc/systemd/system/xray.service.d/20-run-as-xray.conf <<UNIT
[Service]
User=xray
Group=xray
UNIT
}

generate_uuid_if_needed() {
  if [[ -z "$REALITY_UUID" ]]; then
    REALITY_UUID=$($XRAY_BIN uuid)
  fi
}

generate_short_id_if_needed() {
  if [[ -z "$REALITY_SHORT_ID" ]]; then
    REALITY_SHORT_ID=$(openssl rand -hex 8)
  fi
}

generate_keys_if_needed() {
  if [[ -n "$REALITY_PRIVATE_KEY" && -n "$REALITY_PUBLIC_KEY" ]]; then
    return 0
  fi
  local output
  output=$($XRAY_BIN x25519)
  REALITY_PRIVATE_KEY=$(printf '%s\n' "$output" | awk -F ': ' '/^PrivateKey:/{print $2}')
  REALITY_PUBLIC_KEY=$(printf '%s\n' "$output" | awk -F ': ' '/^PublicKey:/{print $2}')
  if [[ -z "$REALITY_PUBLIC_KEY" ]]; then
    REALITY_PUBLIC_KEY=$(printf '%s\n' "$output" | awk -F ': ' '/^Password:/{print $2}')
  fi
  if [[ -z "$REALITY_PRIVATE_KEY" || -z "$REALITY_PUBLIC_KEY" ]]; then
    echo "[ERROR] 无法解析 x25519 密钥输出"
    exit 1
  fi
}

write_xray_config() {
  mkdir -p "$(dirname "$XRAY_CONFIG_PATH")" "$XRAY_LOG_DIR"
  touch "$XRAY_LOG_DIR/access.log" "$XRAY_LOG_DIR/error.log"
  chown xray:xray "$XRAY_LOG_DIR" "$XRAY_LOG_DIR/access.log" "$XRAY_LOG_DIR/error.log" || true
  backup_if_exists "$XRAY_CONFIG_PATH"
  cat > "$XRAY_CONFIG_PATH" <<JSON
{
  "log": {
    "access": "$XRAY_LOG_DIR/access.log",
    "error": "$XRAY_LOG_DIR/error.log",
    "loglevel": "warning"
  },
  "api": {
    "tag": "api",
    "listen": "$XRAY_API_SERVER",
    "services": ["HandlerService", "StatsService"]
  },
  "stats": {},
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true,
        "statsUserOnline": true
      }
    },
    "system": {
      "statsInboundUplink": true,
      "statsInboundDownlink": true
    }
  },
  "inbounds": [
    {
      "tag": "vless-reality",
      "listen": "0.0.0.0",
      "port": $REALITY_PORT,
      "protocol": "vless",
      "settings": {
        "clients": [
          {
            "id": "$REALITY_UUID",
            "flow": "xtls-rprx-vision",
            "email": "$REALITY_EMAIL",
            "level": 0
          }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "$REALITY_TARGET",
          "xver": 0,
          "serverNames": ["$REALITY_SNI"],
          "privateKey": "$REALITY_PRIVATE_KEY",
          "shortIds": ["$REALITY_SHORT_ID"]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls", "quic"]
      }
    }
  ],
  "outbounds": [
    {"protocol": "freedom", "tag": "direct"},
    {"protocol": "blackhole", "tag": "block"}
  ]
}
JSON

  cat > "$XRAY_ENV_PATH" <<ENV
XRAY_REALITY_PORT=$REALITY_PORT
XRAY_REALITY_UUID=$REALITY_UUID
XRAY_REALITY_PRIVATE_KEY=$REALITY_PRIVATE_KEY
XRAY_REALITY_PUBLIC_KEY=$REALITY_PUBLIC_KEY
XRAY_REALITY_SHORT_ID=$REALITY_SHORT_ID
XRAY_REALITY_SERVER=$REALITY_SERVER_DOMAIN
XRAY_REALITY_SNI=$REALITY_SNI
XRAY_REALITY_TARGET=$REALITY_TARGET
ENV

  $XRAY_BIN run -test -config "$XRAY_CONFIG_PATH"
}

open_reality_port() {
  for tool in iptables ip6tables; do
    command -v "$tool" >/dev/null 2>&1 || continue
  done
  iptables -C INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT
  ip6tables -C INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT 2>/dev/null || \
    ip6tables -I INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT
  mkdir -p /etc/iptables
  iptables-save > /etc/iptables/rules.v4
  ip6tables-save > /etc/iptables/rules.v6
  systemctl enable netfilter-persistent >/dev/null 2>&1 || true
}

panel_has_admin_users() {
  local db_path="$PANEL_DATA_DIR/panel.db"
  if [[ ! -f "$db_path" ]]; then
    return 1
  fi
  python3 - "$db_path" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
conn = sqlite3.connect(path)
try:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_users'").fetchone()
    if not row:
        raise SystemExit(1)
    count = conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
    raise SystemExit(0 if count > 0 else 1)
finally:
    conn.close()
PY
}


generate_bootstrap_owner_password_if_needed() {
  if [[ -n "$PANEL_BOOTSTRAP_OWNER_PASSWORD" ]]; then
    return 0
  fi
  PANEL_BOOTSTRAP_OWNER_PASSWORD=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(18)[:20])
PY
)
}

write_panel_runtime_env() {
  local xray_config_dir xray_config_base
  xray_config_dir=$(dirname "$XRAY_CONFIG_PATH")
  xray_config_base=$(basename "$XRAY_CONFIG_PATH")
  if [[ "$PANEL_RUNTIME" == "docker" ]]; then
    cat > "$INSTALL_DIR/.env" <<ENV
PANEL_BUILD_CONTEXT=.
PANEL_CONTAINER_NAME=$PANEL_CONTAINER_NAME
PANEL_RESTART_POLICY=unless-stopped
PANEL_PORT=$PANEL_PORT
PANEL_BIND_HOST=$PANEL_BIND_HOST
PANEL_HOST_DATA_DIR=$PANEL_DATA_DIR
PANEL_SESSION_COOKIE_SECURE=$PANEL_SESSION_COOKIE_SECURE
PANEL_MANAGE_FIREWALL=0
PANEL_TRUSTED_PROXY_IPS="$PANEL_TRUSTED_PROXY_IPS"
PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS=$PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS
PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS=$PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS
PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS=$PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS
PANEL_TIMEZONE=$PANEL_TIMEZONE
PANEL_TIMEZONE_LABEL="$PANEL_TIMEZONE_LABEL"
PANEL_DEFAULT_TITLE="$PANEL_DEFAULT_TITLE"
PANEL_DEFAULT_DOMAIN=$DOMAIN
PANEL_DEFAULT_FLOW=xtls-rprx-vision
PANEL_SERVICE_NAME=$SERVICE_NAME
PANEL_SYSTEMCTL_COMMAND=
PANEL_HOST_XRAY_CONFIG_DIR=$xray_config_dir
PANEL_HOST_XRAY_ENV_PATH=$XRAY_ENV_PATH
PANEL_HOST_XRAY_BIN=$XRAY_BIN
PANEL_HOST_XRAY_LOG_DIR=$XRAY_LOG_DIR
XRAY_CONTAINER_CONFIG_DIR=$xray_config_dir
XRAY_CONTAINER_CONFIG_PATH=$xray_config_dir/$xray_config_base
XRAY_CONTAINER_ENV_PATH=$XRAY_ENV_PATH
XRAY_CONTAINER_BIN_PATH=$XRAY_BIN
XRAY_CONTAINER_LOG_DIR=$XRAY_LOG_DIR
XRAY_API_SERVER=$XRAY_API_SERVER
ENV
    return
  fi

  cat > "$INSTALL_DIR/.env" <<ENV
PANEL_DATA_DIR=$PANEL_DATA_DIR
PANEL_TIMEZONE=$PANEL_TIMEZONE
PANEL_TIMEZONE_LABEL="$PANEL_TIMEZONE_LABEL"
PANEL_DEFAULT_TITLE="$PANEL_DEFAULT_TITLE"
PANEL_DEFAULT_DOMAIN=$DOMAIN
PANEL_DEFAULT_FLOW=xtls-rprx-vision
PANEL_SERVICE_NAME=$SERVICE_NAME
PANEL_RUNTIME_MODE=systemd
PANEL_SESSION_COOKIE_SECURE=$PANEL_SESSION_COOKIE_SECURE
PANEL_MANAGE_FIREWALL=1
PANEL_SYSTEMCTL_COMMAND=systemctl
PANEL_TRUSTED_PROXY_IPS="$PANEL_TRUSTED_PROXY_IPS"
PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS=$PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS
PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS=$PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS
PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS=$PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS
XRAY_CONFIG_PATH=$XRAY_CONFIG_PATH
XRAY_ENV_PATH=$XRAY_ENV_PATH
XRAY_BIN=$XRAY_BIN
XRAY_API_SERVER=$XRAY_API_SERVER
XRAY_LOG_DIR=$XRAY_LOG_DIR
ENV
}

install_panel_files() {
  mkdir -p "$INSTALL_DIR" "$PANEL_DATA_DIR"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap 'rm -rf "$tmpdir"' RETURN
  tar \
    --exclude='.git' \
    --exclude='.env' \
    --exclude='.venv' \
    --exclude='data' \
    --exclude='docker-data' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    -cf - -C "$ROOT_DIR" . | tar -xf - -C "$tmpdir"
  cp -a "$tmpdir"/. "$INSTALL_DIR"/
  chmod +x "$INSTALL_DIR"/scripts/*.sh
  write_panel_runtime_env
}

install_panel_systemd_runtime() {
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
  write_panel_service
}


bootstrap_owner_if_needed() {
  if panel_has_admin_users; then
    return 0
  fi
  generate_bootstrap_owner_password_if_needed
  (
    cd "$INSTALL_DIR"
    PANEL_BOOTSTRAP_OWNER_USERNAME="$PANEL_BOOTSTRAP_OWNER_USERNAME" \
    PANEL_BOOTSTRAP_OWNER_PASSWORD="$PANEL_BOOTSTRAP_OWNER_PASSWORD" \
    bash scripts/bootstrap-owner.sh
  )
  BOOTSTRAP_OWNER_CREATED=1
}

write_panel_service() {
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=V UI
After=network.target xray.service
Wants=xray.service

[Service]
Type=simple
User=$PANEL_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn app:app --host 127.0.0.1 --port $PANEL_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
}

write_nginx_config() {
  local nginx_conf="/etc/nginx/conf.d/${DOMAIN}.conf"
  backup_if_exists "$nginx_conf"
  cat > "$nginx_conf" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name $DOMAIN;

    ssl_certificate $TLS_CERT;
    ssl_certificate_key $TLS_KEY;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    add_header X-Frame-Options SAMEORIGIN always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer-when-downgrade always;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location = /login {
        limit_req zone=vui_login burst=5 nodelay;
        limit_req_status 429;

        proxy_pass http://127.0.0.1:$PANEL_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
    }

    location / {
        proxy_pass http://127.0.0.1:$PANEL_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;
    }
}
NGINX

  python3 - <<'PY'
from pathlib import Path
path = Path('/etc/nginx/nginx.conf')
text = path.read_text()
snippets = [
    '    map $http_upgrade $connection_upgrade {\n        default upgrade;\n        "" close;\n    }\n\n',
    '    map $request_method $vui_login_limit_key {\n        default "";\n        POST $binary_remote_addr;\n    }\n\n',
    '    limit_req_zone $vui_login_limit_key zone=vui_login:10m rate=5r/m;\n\n',
]
for snippet in reversed(snippets):
    if snippet not in text:
        text = text.replace('http {\n', 'http {\n' + snippet, 1)
path.write_text(text)
PY
}

write_fail2ban_jail() {
  mkdir -p /etc/fail2ban/jail.d
  cat > /etc/fail2ban/jail.d/vui-plan-login.local <<'JAIL'
[vui-plan-login]
enabled = true
filter = nginx-limit-req
logpath = /var/log/nginx/error.log
maxretry = 2
ngx_limit_req_zones = vui_login
port = http,https
findtime = 10m
bantime = 1h
JAIL
}

wait_for_panel_health() {
  for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:${PANEL_PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "[ERROR] 面板在预期时间内未返回健康检查"
  exit 1
}

start_panel_docker_runtime() {
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  (cd "$INSTALL_DIR" && bash scripts/panel-docker.sh up)
}

start_services() {
  systemctl daemon-reload
  systemctl enable --now xray
  systemctl enable --now fail2ban
  if [[ "$PANEL_RUNTIME" == "docker" ]]; then
    ensure_docker_if_needed
    start_panel_docker_runtime
  else
    systemctl enable --now "$SERVICE_NAME"
  fi
  nginx -t
  systemctl reload nginx
  fail2ban-client reload >/dev/null 2>&1 || systemctl restart fail2ban
  wait_for_panel_health
}

print_summary() {
  local uri
  uri="vless://${REALITY_UUID}@${REALITY_SERVER_DOMAIN}:${REALITY_PORT}?encryption=none&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${REALITY_PUBLIC_KEY}&sid=${REALITY_SHORT_ID}&type=tcp&flow=xtls-rprx-vision#${REALITY_EMAIL}"
  sleep 2
  echo
  echo "[OK] 安装完成"
  echo "- 面板地址: https://${DOMAIN}/login"
  echo "- 面板运行方式: ${PANEL_RUNTIME}"
  echo "- 面板目录: ${INSTALL_DIR}"
  echo "- 面板数据目录: ${PANEL_DATA_DIR}"
  echo "- Reality 端口: ${REALITY_PORT}"
  echo "- 主链接邮箱: ${REALITY_EMAIL}"
  echo "- Reality URI: ${uri}"
  if [[ "$PANEL_RUNTIME" == "docker" ]]; then
    echo "- 启动/更新: cd ${INSTALL_DIR} && bash scripts/panel-docker.sh up"
    echo "- 查看状态: cd ${INSTALL_DIR} && bash scripts/panel-docker.sh status"
    echo "- 查看日志: cd ${INSTALL_DIR} && bash scripts/panel-docker.sh logs"
  else
    echo "- 面板服务: ${SERVICE_NAME}"
    echo "- 查看状态: systemctl status ${SERVICE_NAME}"
    echo "- 查看日志: journalctl -u ${SERVICE_NAME} -f"
  fi
  if [[ "$BOOTSTRAP_OWNER_CREATED" == "1" ]]; then
    echo "- 初始管理员用户名: ${PANEL_BOOTSTRAP_OWNER_USERNAME}"
    echo "- 初始管理员密码: ${PANEL_BOOTSTRAP_OWNER_PASSWORD}"
    echo "- 请首次登录后立即修改密码"
  fi
}

ensure_apt_packages
ensure_docker_if_needed
install_xray_if_needed
ensure_xray_user
generate_uuid_if_needed
generate_short_id_if_needed
generate_keys_if_needed
write_xray_config
open_reality_port
install_panel_files
if [[ "$PANEL_RUNTIME" == "systemd" ]]; then
  install_panel_systemd_runtime
fi
bootstrap_owner_if_needed
write_nginx_config
write_fail2ban_jail
start_services
print_summary
