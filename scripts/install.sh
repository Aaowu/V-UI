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
CERTBOT_EMAIL=${CERTBOT_EMAIL:-}
INSTALL_DIR=${INSTALL_DIR:-/opt/v-ui}
SERVICE_NAME=${SERVICE_NAME:-v-ui}
PANEL_PORT=${PANEL_PORT:-9200}
PANEL_USER=${PANEL_USER:-root}
PANEL_DATA_DIR=${PANEL_DATA_DIR:-/var/lib/v-ui}
PANEL_TIMEZONE=${PANEL_TIMEZONE:-Asia/Shanghai}
PANEL_TIMEZONE_LABEL=${PANEL_TIMEZONE_LABEL:-北京时间\ \(UTC+8\)}
XRAY_CONFIG_PATH=${XRAY_CONFIG_PATH:-/usr/local/etc/xray/config.json}
XRAY_ENV_PATH=${XRAY_ENV_PATH:-/root/xray-reality-main.env}
XRAY_BIN=${XRAY_BIN:-/usr/local/bin/xray}
XRAY_API_SERVER=${XRAY_API_SERVER:-127.0.0.1:10085}
XRAY_LOG_DIR=${XRAY_LOG_DIR:-/var/log/xray}
REALITY_PORT=${REALITY_PORT:-30828}
REALITY_SERVER_DOMAIN=${REALITY_SERVER_DOMAIN:-}
REALITY_SNI=${REALITY_SNI:-}
REALITY_TARGET=${REALITY_TARGET:-127.0.0.1:443}
REALITY_UUID=${REALITY_UUID:-}
REALITY_EMAIL=${REALITY_EMAIL:-}
REALITY_SHORT_ID=${REALITY_SHORT_ID:-}
REALITY_PRIVATE_KEY=${REALITY_PRIVATE_KEY:-}
REALITY_PUBLIC_KEY=${REALITY_PUBLIC_KEY:-}
INSTALL_XRAY_IF_MISSING=${INSTALL_XRAY_IF_MISSING:-1}

prompt_required() {
  local var_name="$1"
  local prompt_text="$2"
  local current_value="${!var_name:-}"
  if [[ -n "$current_value" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    echo "[ERROR] 缺少必要变量：$var_name"
    echo "当前运行不是交互式终端，无法继续提示输入。"
    echo "请改用环境变量方式，例如：DOMAIN=panel.example.com bash scripts/install.sh"
    exit 1
  fi
  while [[ -z "$current_value" ]]; do
    read -r -p "$prompt_text" current_value
  done
  printf -v "$var_name" '%s' "$current_value"
}

prompt_default() {
  local var_name="$1"
  local prompt_text="$2"
  local default_value="$3"
  local current_value="${!var_name:-}"
  if [[ -n "$current_value" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    printf -v "$var_name" '%s' "$default_value"
    return 0
  fi
  read -r -p "$prompt_text [$default_value]: " current_value
  current_value=${current_value:-$default_value}
  printf -v "$var_name" '%s' "$current_value"
}

prompt_install_inputs() {
  if [[ -t 0 ]]; then
    echo "[INFO] 进入交互式安装模式"
  fi
  prompt_required DOMAIN "请输入面板域名 (例如 panel.example.com): "
  prompt_default PANEL_PORT "请输入面板监听端口" "9200"
  prompt_default REALITY_PORT "请输入 Reality 端口" "30828"
  REALITY_SERVER_DOMAIN=${REALITY_SERVER_DOMAIN:-$DOMAIN}
  REALITY_SNI=${REALITY_SNI:-$DOMAIN}
  REALITY_EMAIL=${REALITY_EMAIL:-admin@${DOMAIN}}
  TLS_CERT=${TLS_CERT:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}
  TLS_KEY=${TLS_KEY:-/etc/letsencrypt/live/${DOMAIN}/privkey.pem}
}

check_domain_and_ports() {
  echo "[INFO] 检查域名解析与本地端口条件"
  local public_ip=""
  public_ip=$(curl -4 -fsS https://api.ipify.org 2>/dev/null || true)
  local resolved_ips=""
  resolved_ips=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | xargs || true)
  if [[ -n "$public_ip" && -n "$resolved_ips" ]]; then
    echo "[INFO] 域名解析: $resolved_ips"
    echo "[INFO] 当前公网 IP: $public_ip"
    if ! printf '%s
' "$resolved_ips" | tr ' ' '
' | grep -qx "$public_ip"; then
      echo "[ERROR] 域名 $DOMAIN 当前未解析到本机公网 IP ($public_ip)"
      echo "[ERROR] 请先确认 DNS 解析正确后再继续"
      exit 1
    fi
  else
    echo "[WARN] 无法完成域名解析或公网 IP 检查，请手工确认 $DOMAIN 已解析到本机"
  fi
  if ss -lnt 2>/dev/null | awk '{print $4}' | grep -Eq '(^|:)(80|443)$'; then
    echo "[INFO] 检测到 80/443 端口已有监听，安装时会尝试停掉 nginx 后申请证书"
  else
    echo "[INFO] 当前 80/443 端口未被监听"
  fi
  echo "[INFO] 请确保云防火墙/安全组已放行 80 和 443"
}

backup_if_exists() {
  local target="$1"
  if [[ -e "$target" ]]; then
    cp -a "$target" "${target}.bak-$(date +%F-%H%M%S)"
  fi
}

ensure_apt_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl unzip ca-certificates nginx python3 python3-venv python3-pip jq tar certbot iptables-persistent netfilter-persistent || \
    apt-get install -y curl unzip ca-certificates nginx python3 python3-venv python3-pip jq tar certbot
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

issue_certificate_if_needed() {
  if [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]]; then
    echo "[INFO] 检测到现有证书，直接使用：$TLS_CERT"
    return 0
  fi
  echo "[INFO] 未检测到证书，开始尝试为 $DOMAIN 自动申请 Let's Encrypt 证书"
  systemctl stop nginx >/dev/null 2>&1 || true
  local certbot_args=(certonly --standalone --non-interactive --agree-tos -d "$DOMAIN")
  if [[ -n "$CERTBOT_EMAIL" ]]; then
    certbot_args+=(-m "$CERTBOT_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi
  certbot "${certbot_args[@]}"
  TLS_CERT=${TLS_CERT:-/etc/letsencrypt/live/${DOMAIN}/fullchain.pem}
  TLS_KEY=${TLS_KEY:-/etc/letsencrypt/live/${DOMAIN}/privkey.pem}
  if [[ ! -f "$TLS_CERT" || ! -f "$TLS_KEY" ]]; then
    echo "[ERROR] 证书申请完成后仍未找到证书文件"
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

  "$XRAY_BIN" run -test -config "$XRAY_CONFIG_PATH"
}

open_reality_port() {
  iptables -C INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT
  ip6tables -C INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT 2>/dev/null || \
    ip6tables -I INPUT -p tcp --dport "$REALITY_PORT" -m comment --comment 'vui-xray-reality' -j ACCEPT
  mkdir -p /etc/iptables
  iptables-save > /etc/iptables/rules.v4
  ip6tables-save > /etc/iptables/rules.v6
  systemctl enable netfilter-persistent >/dev/null 2>&1 || true
}

install_panel_files() {
  mkdir -p "$INSTALL_DIR" "$PANEL_DATA_DIR"
  local tmpdir
  tmpdir=$(mktemp -d)
  trap 'rm -rf "$tmpdir"' RETURN
  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='data' \
    --exclude='node_modules' \
    --exclude='__pycache__' \
    -cf - -C "$ROOT_DIR" . | tar -xf - -C "$tmpdir"
  cp -a "$tmpdir"/. "$INSTALL_DIR"/
  cat > "$INSTALL_DIR/.env" <<ENV
PANEL_DATA_DIR=$PANEL_DATA_DIR
PANEL_TIMEZONE=$PANEL_TIMEZONE
PANEL_TIMEZONE_LABEL=$PANEL_TIMEZONE_LABEL
PANEL_DEFAULT_TITLE=V UI
PANEL_DEFAULT_DOMAIN=$DOMAIN
PANEL_DEFAULT_FLOW=xtls-rprx-vision
XRAY_CONFIG_PATH=$XRAY_CONFIG_PATH
XRAY_ENV_PATH=$XRAY_ENV_PATH
XRAY_BIN=$XRAY_BIN
XRAY_API_SERVER=$XRAY_API_SERVER
XRAY_LOG_DIR=$XRAY_LOG_DIR
ENV
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
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
insert = '    map $http_upgrade $connection_upgrade {\n        default upgrade;\n        "" close;\n    }\n\n'
if insert not in text:
    text = text.replace('http {', 'http {\n' + insert, 1)
path.write_text(text)
PY
}

start_services() {
  systemctl daemon-reload
  systemctl enable --now xray
  systemctl enable --now "$SERVICE_NAME"
  nginx -t
  systemctl reload nginx
}

print_summary() {
  local uri
  uri="vless://${REALITY_UUID}@${REALITY_SERVER_DOMAIN}:${REALITY_PORT}?encryption=none&security=reality&sni=${REALITY_SNI}&fp=chrome&pbk=${REALITY_PUBLIC_KEY}&sid=${REALITY_SHORT_ID}&type=tcp&flow=xtls-rprx-vision#${REALITY_EMAIL}"
  local creds_file="$PANEL_DATA_DIR/admin_credentials.txt"
  echo
  echo "[OK] 安装完成"
  echo "- 面板地址: https://${DOMAIN}/login"
  echo "- 面板服务: ${SERVICE_NAME}"
  echo "- 面板目录: ${INSTALL_DIR}"
  echo "- 面板数据目录: ${PANEL_DATA_DIR}"
  echo "- Reality 端口: ${REALITY_PORT}"
  echo "- 主链接邮箱: ${REALITY_EMAIL}"
  echo "- 默认 Reality 链接: ${uri}"
  if [[ -f "$creds_file" ]]; then
    echo "- 初始账号文件: ${creds_file}"
    cat "$creds_file"
  else
    echo "- 初始账号文件尚未生成，请稍后查看: ${creds_file}"
  fi
  echo
  echo "[常用管理命令]"
  echo "systemctl status ${SERVICE_NAME}"
  echo "systemctl restart ${SERVICE_NAME}"
  echo "journalctl -u ${SERVICE_NAME} -f"
  echo "systemctl status xray"
  echo "systemctl restart xray"
  echo "journalctl -u xray -f"
  echo "nginx -t && systemctl reload nginx"
}

prompt_install_inputs
ensure_apt_packages
check_domain_and_ports
install_xray_if_needed
ensure_xray_user
generate_uuid_if_needed
generate_short_id_if_needed
generate_keys_if_needed
issue_certificate_if_needed
write_xray_config
open_reality_port
install_panel_files
write_panel_service
write_nginx_config
start_services
print_summary
