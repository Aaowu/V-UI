import calendar
import hashlib
import json
import logging
import os
import platform
import shutil
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("PANEL_DATA_DIR", str(APP_DIR / "data")))
STATIC_DIR = APP_DIR / "static"
TEMPLATES_DIR = APP_DIR / "templates"
DB_PATH = DATA_DIR / "panel.db"
OWNER_CREDENTIALS_PATH = DATA_DIR / "admin_credentials.txt"
XRAY_CONFIG_PATH = Path(os.getenv("XRAY_CONFIG_PATH", "/usr/local/etc/xray/config.json"))
XRAY_ENV_PATH = Path(os.getenv("XRAY_ENV_PATH", "/root/xray-reality-main.env"))
XRAY_BIN = Path(os.getenv("XRAY_BIN", "/usr/local/bin/xray"))
XRAY_API_SERVER = os.getenv("XRAY_API_SERVER", "127.0.0.1:10085")
XRAY_LOG_DIR = Path(os.getenv("XRAY_LOG_DIR", "/var/log/xray"))
XRAY_ACCESS_LOG = XRAY_LOG_DIR / "access.log"
XRAY_ERROR_LOG = XRAY_LOG_DIR / "error.log"
POLL_INTERVAL_SECONDS = 60
SESSION_COOKIE_NAME = "vui_session"
LEGACY_SESSION_COOKIE_NAMES = ("icu_panel_session",)
LOGIN_CSRF_COOKIE_NAME = "vui_login_csrf"
SESSION_TTL_DAYS = 14
INBOUND_TAG = "vless-reality"
REALITY_FIREWALL_COMMENT = "vui-xray-reality"
LEGACY_REALITY_FIREWALL_COMMENTS = ("icu-xray-reality",)
DEFAULT_PANEL_TITLE = os.getenv("PANEL_DEFAULT_TITLE", "V UI")
DEFAULT_SERVER_DOMAIN = os.getenv("PANEL_DEFAULT_DOMAIN", "panel.example.com")
DEFAULT_FLOW = os.getenv("PANEL_DEFAULT_FLOW", "xtls-rprx-vision")
ACCESS_EVENTS_RETENTION_DAYS = 14
SAMPLE_RETENTION_DAYS = 30
DISPLAY_TZ = ZoneInfo(os.getenv("PANEL_TIMEZONE", "Asia/Shanghai"))
DISPLAY_TZ_LABEL = os.getenv("PANEL_TIMEZONE_LABEL", "北京时间 (UTC+8)")
PANEL_SERVICE_NAME = os.getenv("PANEL_SERVICE_NAME", "vui-plan")
BOOTSTRAP_OWNER_USERNAME = (os.getenv("PANEL_BOOTSTRAP_OWNER_USERNAME", "admin") or "admin").strip() or "admin"
BOOTSTRAP_OWNER_PASSWORD = os.getenv("PANEL_BOOTSTRAP_OWNER_PASSWORD", "")
TRUSTED_PROXY_IPS = {item.strip() for item in os.getenv("PANEL_TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",") if item.strip()}
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = max(1, env_int("PANEL_LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 5))
LOGIN_RATE_LIMIT_WINDOW_SECONDS = max(60, env_int("PANEL_LOGIN_RATE_LIMIT_WINDOW_SECONDS", 300))
LOGIN_RATE_LIMIT_LOCKOUT_SECONDS = max(60, env_int("PANEL_LOGIN_RATE_LIMIT_LOCKOUT_SECONDS", 900))
RUNTIME_MODE = (os.getenv("PANEL_RUNTIME_MODE", "systemd") or "systemd").strip().lower()
if RUNTIME_MODE not in {"systemd", "docker"}:
    RUNTIME_MODE = "systemd"
PANEL_SESSION_COOKIE_SECURE = env_flag("PANEL_SESSION_COOKIE_SECURE", True)
PANEL_MANAGE_FIREWALL = env_flag("PANEL_MANAGE_FIREWALL", RUNTIME_MODE == "systemd")
SYSTEMCTL_COMMAND = shlex.split(os.getenv("PANEL_SYSTEMCTL_COMMAND", "systemctl" if RUNTIME_MODE == "systemd" else ""))
HOST_SYSTEMD_ETC_DIR = Path(os.getenv("PANEL_HOST_SYSTEMD_ETC_DIR", "/host/etc/systemd/system"))
HOST_SYSTEMD_UNIT_DIRS = [
    Path(item.strip())
    for item in os.getenv(
        "PANEL_HOST_SYSTEMD_UNIT_DIRS",
        "/host/etc/systemd/system:/host/usr/lib/systemd/system:/host/lib/systemd/system",
    ).split(":")
    if item.strip()
]
HOST_SYSTEMD_MULTI_USER_WANTS_DIR = HOST_SYSTEMD_ETC_DIR / "multi-user.target.wants"

app = FastAPI(title=DEFAULT_PANEL_TITLE)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger("uvicorn.error")

stop_event = threading.Event()
poller_thread: threading.Thread | None = None
runtime_state: dict[str, Any] = {
    "last_sync_at": None,
    "last_sync_error": None,
    "last_access_parse_at": None,
    "last_access_parse_error": None,
}
login_rate_limit_lock = threading.Lock()
login_rate_limit_state: dict[str, dict[str, float | int]] = {}

ACCESS_LINE_TIME = re.compile(r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+(?P<body>.*)$")
ACCESS_ROUTE = re.compile(r"\[(?P<inbound>.+?)\s*(?:>>|->)\s*(?P<outbound>.+?)\]")
ACCESS_TARGET = re.compile(r"(?:accepted|rejected)\s+(?P<network>[a-z0-9]+):(?P<destination>\S+)", re.I)
ACCESS_EMAIL_PATTERNS = [
    re.compile(r"email:\s*(?P<email>[^\s]+)", re.I),
    re.compile(r"user:\s*(?P<email>[^\s]+)", re.I),
]
ACCESS_SOURCE = re.compile(r"from\s+(?:tcp:)?(?P<client_ip>\[[^\]]+\]|[^\s:]+(?:\.[^\s:]+){0,10}|[0-9a-fA-F:]+)(?::\d+)?\s+accepted", re.I)


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now_utc().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def to_display_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    dt = value if isinstance(value, datetime) else parse_iso(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(DISPLAY_TZ)


def format_display_dt(value: str | datetime | None, date_only: bool = False) -> str:
    dt = to_display_dt(value)
    if dt is None:
        return ''
    return dt.strftime('%Y-%m-%d' if date_only else '%Y-%m-%d %H:%M:%S')


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    XRAY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass


def set_path_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def harden_data_permissions() -> None:
    ensure_dirs()
    set_path_mode(DATA_DIR, 0o700)
    if DB_PATH.exists():
        set_path_mode(DB_PATH, 0o600)
    if OWNER_CREDENTIALS_PATH.exists():
        set_path_mode(OWNER_CREDENTIALS_PATH, 0o600)


def remove_owner_credentials_file() -> None:
    try:
        OWNER_CREDENTIALS_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    return connection


def ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_db() -> None:
    ensure_dirs()
    with closing(db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES admin_users(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                uuid TEXT NOT NULL,
                share_token TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                quota_bytes INTEGER NOT NULL DEFAULT 0,
                baseline_total_bytes INTEGER NOT NULL DEFAULT 0,
                accumulated_up_bytes INTEGER NOT NULL DEFAULT 0,
                accumulated_down_bytes INTEGER NOT NULL DEFAULT 0,
                last_api_up_bytes INTEGER NOT NULL DEFAULT 0,
                last_api_down_bytes INTEGER NOT NULL DEFAULT 0,
                last_online_count INTEGER NOT NULL DEFAULT 0,
                last_online_ips TEXT NOT NULL DEFAULT '[]',
                last_seen_at TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS traffic_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                delta_up_bytes INTEGER NOT NULL DEFAULT 0,
                delta_down_bytes INTEGER NOT NULL DEFAULT 0,
                total_used_bytes INTEGER NOT NULL DEFAULT 0,
                online_count INTEGER NOT NULL DEFAULT 0,
                online_ips TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(link_id) REFERENCES links(id)
            );

            CREATE TABLE IF NOT EXISTS access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                link_id INTEGER,
                link_email TEXT,
                client_ip TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT '',
                network TEXT NOT NULL DEFAULT '',
                inbound_tag TEXT NOT NULL DEFAULT '',
                outbound_tag TEXT NOT NULL DEFAULT '',
                raw_line TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL
            );
            """
        )
        ensure_columns(
            conn,
            "sessions",
            {
                "csrf_token": "TEXT",
            },
        )
        ensure_columns(
            conn,
            "admin_users",
            {
                "role": "TEXT NOT NULL DEFAULT 'owner'",
                "link_id": "INTEGER",
                "quota_bytes": "INTEGER NOT NULL DEFAULT 0",
                "reset_day": "INTEGER NOT NULL DEFAULT 1",
                "last_reset_at": "TEXT",
                "status_reason": "TEXT NOT NULL DEFAULT 'active'",
            },
        )
        ensure_columns(
            conn,
            "links",
            {
                "owner_user_id": "INTEGER",
                "expires_at": "TEXT",
                "last_reset_at": "TEXT",
                "reset_day": "INTEGER NOT NULL DEFAULT 1",
                "auto_disable_on_expire": "INTEGER NOT NULL DEFAULT 1",
                "auto_reenable_on_reset": "INTEGER NOT NULL DEFAULT 1",
                "status_reason": "TEXT NOT NULL DEFAULT 'active'",
                "baseline_up_bytes": "INTEGER NOT NULL DEFAULT 0",
                "baseline_down_bytes": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        conn.execute("UPDATE admin_users SET role = COALESCE(NULLIF(role, ''), 'owner')")
        owner = conn.execute("SELECT id FROM admin_users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
        if owner:
            conn.execute("UPDATE admin_users SET last_reset_at = COALESCE(last_reset_at, created_at) WHERE role = 'member'")
            conn.execute("UPDATE links SET owner_user_id = COALESCE(owner_user_id, ?) WHERE owner_user_id IS NULL", (owner['id'],))
            rows = conn.execute("SELECT id, link_id FROM admin_users WHERE role = 'member' AND link_id IS NOT NULL").fetchall()
            for row in rows:
                conn.execute("UPDATE links SET owner_user_id = ? WHERE id = ?", (row['id'], row['link_id']))
        conn.commit()
    harden_data_permissions()


def hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(salt_hex)
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1).hex()


def verify_password(password: str, salt_hex: str, password_hash: str) -> bool:
    return secrets.compare_digest(hash_password(password, salt_hex), password_hash)


def random_password(length: int = 20) -> str:
    return secrets.token_urlsafe(length)[:length]


def set_setting(key: str, value: str) -> None:
    with closing(db()) as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now_iso()),
        )
        conn.commit()


def get_settings() -> dict[str, str]:
    with closing(db()) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def get_setting(key: str, default: str = "") -> str:
    return get_settings().get(key, default)


def add_audit(action: str, detail: str) -> None:
    with closing(db()) as conn:
        conn.execute("INSERT INTO audit_logs(ts, action, detail) VALUES(?, ?, ?)", (now_iso(), action, detail))
        conn.commit()


def random_share_token() -> str:
    return secrets.token_urlsafe(18)


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def load_xray_config() -> dict[str, Any]:
    if not XRAY_CONFIG_PATH.exists():
        raise FileNotFoundError(f"未找到 Xray 配置文件：{XRAY_CONFIG_PATH}")
    return json.loads(XRAY_CONFIG_PATH.read_text())


def load_xray_config_if_exists() -> dict[str, Any] | None:
    try:
        return load_xray_config()
    except FileNotFoundError:
        return None


def save_xray_config(config: dict[str, Any]) -> None:
    backup = XRAY_CONFIG_PATH.with_suffix(f".json.bak-{int(time.time())}")
    if XRAY_CONFIG_PATH.exists():
        backup.write_text(XRAY_CONFIG_PATH.read_text())
    XRAY_CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def get_vless_inbound(config: dict[str, Any]) -> dict[str, Any]:
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") == "vless":
            return inbound
    raise RuntimeError("未找到 VLESS 入站配置")


def xray_x25519() -> tuple[str, str]:
    if not XRAY_BIN.exists():
        raise RuntimeError(f"未找到 Xray 可执行文件：{XRAY_BIN}")
    result = subprocess.run([str(XRAY_BIN), "x25519"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "生成 x25519 失败")
    private_key = ""
    public_key = ""
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("PrivateKey:"):
            private_key = line.split(":", 1)[1].strip()
        if line.startswith("PublicKey:") or line.startswith("Password:"):
            public_key = line.split(":", 1)[1].strip()
    if not private_key or not public_key:
        raise RuntimeError("未能解析 x25519 输出")
    return private_key, public_key


def ensure_default_settings_from_xray() -> None:
    config = load_xray_config_if_exists()
    inbound: dict[str, Any] = {}
    reality: dict[str, Any] = {}
    if config:
        try:
            inbound = get_vless_inbound(config)
            reality = inbound.get("streamSettings", {}).get("realitySettings", {})
        except Exception as exc:
            logger.warning("LOAD_XRAY_DEFAULTS_FAIL %s", exc)
    env = parse_env_file(XRAY_ENV_PATH)
    settings = get_settings()
    sni_default = env.get("XRAY_REALITY_SNI") or (reality.get("serverNames") or [DEFAULT_SERVER_DOMAIN])[0]
    port_default = env.get("XRAY_REALITY_PORT") or inbound.get("port") or 30828
    flow_default = settings.get("flow") or DEFAULT_FLOW
    clients = inbound.get("settings", {}).get("clients", [{}])
    if clients:
        flow_default = clients[0].get("flow", flow_default)
    private_key_default = reality.get("privateKey") or env.get("XRAY_REALITY_PRIVATE_KEY") or settings.get("private_key", "")
    public_key_default = env.get("XRAY_REALITY_PUBLIC_KEY") or settings.get("public_key", "")
    defaults = {
        "panel_title": DEFAULT_PANEL_TITLE,
        "announcement": "",
        "server_domain": env.get("XRAY_REALITY_SERVER", DEFAULT_SERVER_DOMAIN),
        "sni": sni_default,
        "port": str(port_default),
        "flow": flow_default,
        "short_id": (reality.get("shortIds") or [secrets.token_hex(8)])[0],
        "public_key": public_key_default,
        "private_key": private_key_default,
        "target": env.get("XRAY_REALITY_TARGET", reality.get("dest", "127.0.0.1:443")),
        "access_log_offset": settings.get("access_log_offset", "0"),
        "max_traffic_rows": settings.get("max_traffic_rows", "5000"),
        "max_access_rows": settings.get("max_access_rows", "10000"),
        "max_access_log_mb": settings.get("max_access_log_mb", "64"),
        "max_error_log_mb": settings.get("max_error_log_mb", "16"),
    }
    if not defaults["private_key"] or not defaults["public_key"]:
        try:
            private_key, public_key = xray_x25519()
            defaults["private_key"] = defaults["private_key"] or private_key
            defaults["public_key"] = defaults["public_key"] or public_key
        except Exception as exc:
            logger.warning("XRAY_KEY_BOOTSTRAP_SKIP %s", exc)
    for key, value in defaults.items():
        if key not in settings:
            set_setting(key, str(value))


def sync_owner_credentials_file(username: str, password: str) -> None:
    del username, password
    remove_owner_credentials_file()
    harden_data_permissions()


def get_session_token(request: Request) -> str | None:
    for cookie_name in (SESSION_COOKIE_NAME, *LEGACY_SESSION_COOKIE_NAMES):
        token = request.cookies.get(cookie_name)
        if token:
            return token
    return None


def get_login_csrf_token(request: Request) -> str:
    token = (request.cookies.get(LOGIN_CSRF_COOKIE_NAME) or "").strip()
    if token:
        return token
    return secrets.token_urlsafe(24)


def get_client_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client and request.client.host else "-"
    if peer_ip in TRUSTED_PROXY_IPS:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            client_ip = forwarded_for.split(",", 1)[0].strip()
            if client_ip:
                return client_ip
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    if request.client and request.client.host:
        return request.client.host
    return "-"


def set_session_cookie(response: RedirectResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=PANEL_SESSION_COOKIE_SECURE,
        path="/",
        max_age=int(timedelta(days=SESSION_TTL_DAYS).total_seconds()),
    )


def set_login_csrf_cookie(response: HTMLResponse | RedirectResponse, token: str) -> None:
    response.set_cookie(
        LOGIN_CSRF_COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=PANEL_SESSION_COOKIE_SECURE,
        path="/login",
        max_age=3600,
    )


def ensure_session_csrf_token(session_token: str, current_token: str | None = None) -> str:
    if current_token:
        return current_token
    csrf_token = secrets.token_urlsafe(24)
    with closing(db()) as conn:
        conn.execute("UPDATE sessions SET csrf_token = ? WHERE token = ?", (csrf_token, session_token))
        conn.commit()
    return csrf_token


def get_session_csrf_token(session_token: str) -> str | None:
    with closing(db()) as conn:
        row = conn.execute("SELECT csrf_token FROM sessions WHERE token = ?", (session_token,)).fetchone()
    if row is None:
        return None
    return ensure_session_csrf_token(session_token, row["csrf_token"])


def prune_login_rate_limit_state(now_ts: float) -> None:
    stale_after = now_ts - max(LOGIN_RATE_LIMIT_WINDOW_SECONDS, LOGIN_RATE_LIMIT_LOCKOUT_SECONDS) * 2
    expired = [key for key, value in login_rate_limit_state.items() if float(value.get("updated_at", 0.0)) < stale_after]
    for key in expired:
        login_rate_limit_state.pop(key, None)


def get_login_lockout_remaining(client_ip: str) -> int:
    now_ts = time.time()
    with login_rate_limit_lock:
        prune_login_rate_limit_state(now_ts)
        state = login_rate_limit_state.get(client_ip)
        if not state:
            return 0
        blocked_until = float(state.get("blocked_until", 0.0))
        if blocked_until <= now_ts:
            return 0
        return max(1, int(blocked_until - now_ts))


def register_login_failure(client_ip: str) -> None:
    now_ts = time.time()
    with login_rate_limit_lock:
        prune_login_rate_limit_state(now_ts)
        state = login_rate_limit_state.get(client_ip)
        if not state or now_ts - float(state.get("window_started_at", 0.0)) > LOGIN_RATE_LIMIT_WINDOW_SECONDS:
            state = {"window_started_at": now_ts, "failures": 0, "blocked_until": 0.0, "updated_at": now_ts}
        failures = int(state.get("failures", 0)) + 1
        state["failures"] = failures
        state["updated_at"] = now_ts
        if failures >= LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
            state["blocked_until"] = now_ts + LOGIN_RATE_LIMIT_LOCKOUT_SECONDS
            state["window_started_at"] = now_ts
            state["failures"] = 0
        login_rate_limit_state[client_ip] = state


def clear_login_failures(client_ip: str) -> None:
    with login_rate_limit_lock:
        login_rate_limit_state.pop(client_ip, None)


def ensure_admin_user() -> None:
    with closing(db()) as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM admin_users").fetchone()
        if row["count"]:
            remove_owner_credentials_file()
            harden_data_permissions()
            return
        username = BOOTSTRAP_OWNER_USERNAME
        password = BOOTSTRAP_OWNER_PASSWORD.strip()
        if len(username) < 3:
            raise RuntimeError("PANEL_BOOTSTRAP_OWNER_USERNAME 至少 3 位")
        if len(password) < 12:
            raise RuntimeError("首次启动必须通过 PANEL_BOOTSTRAP_OWNER_PASSWORD 提供至少 12 位管理员密码")
        salt = secrets.token_hex(16)
        password_hash = hash_password(password, salt)
        conn.execute(
            "INSERT INTO admin_users(username, password_hash, salt, role, created_at) VALUES(?, ?, ?, ?, ?)",
            (username, password_hash, salt, "owner", now_iso()),
        )
        conn.commit()
    sync_owner_credentials_file(username, password)



def bootstrap_links_from_xray() -> None:
    with closing(db()) as conn:
        existing_count = conn.execute("SELECT COUNT(*) AS count FROM links").fetchone()["count"]
        if existing_count:
            return
        owner = conn.execute("SELECT id FROM admin_users WHERE role='owner' ORDER BY id LIMIT 1").fetchone()
        owner_id = owner['id'] if owner else None
        config = load_xray_config_if_exists()
        if not config:
            return
        try:
            inbound = get_vless_inbound(config)
        except Exception as exc:
            logger.warning("BOOTSTRAP_LINKS_SKIP %s", exc)
            return
        clients = inbound.get("settings", {}).get("clients", [])
        now = now_iso()
        for index, client in enumerate(clients, start=1):
            email = client.get("email") or f"user-{index}@example.com"
            name = email.split("@", 1)[0] or f"Link {index}"
            conn.execute(
                """
                INSERT INTO links(
                    owner_user_id, name, email, uuid, share_token, enabled, quota_bytes, baseline_total_bytes, baseline_up_bytes, baseline_down_bytes,
                    accumulated_up_bytes, accumulated_down_bytes, last_api_up_bytes, last_api_down_bytes,
                    last_online_count, last_online_ips, last_seen_at, notes, expires_at,
                    last_reset_at, reset_day, auto_disable_on_expire, auto_reenable_on_reset,
                    status_reason, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, '[]', NULL, '', NULL, ?, 1, 1, 1, 'active', ?, ?)
                """,
                (owner_id, name, email, client.get("id", str(uuid.uuid4())), random_share_token(), now, now, now),
            )
        conn.commit()


def get_current_settings() -> dict[str, str]:
    settings = get_settings()
    return {
        "panel_title": settings.get("panel_title", DEFAULT_PANEL_TITLE),
        "announcement": settings.get("announcement", ""),
        "server_domain": settings.get("server_domain", DEFAULT_SERVER_DOMAIN),
        "sni": settings.get("sni", DEFAULT_SERVER_DOMAIN),
        "port": settings.get("port", "30828"),
        "flow": settings.get("flow", DEFAULT_FLOW),
        "short_id": settings.get("short_id", secrets.token_hex(8)),
        "public_key": settings.get("public_key", ""),
        "private_key": settings.get("private_key", ""),
        "target": settings.get("target", "127.0.0.1:443"),
        "access_log_offset": settings.get("access_log_offset", "0"),
        "max_traffic_rows": settings.get("max_traffic_rows", "5000"),
        "max_access_rows": settings.get("max_access_rows", "10000"),
        "max_access_log_mb": settings.get("max_access_log_mb", "64"),
        "max_error_log_mb": settings.get("max_error_log_mb", "16"),
    }


def validate_port(value: str) -> int:
    port = int(value)
    if port < 1 or port > 65535:
        raise ValueError("端口必须在 1-65535")
    return port


def validate_short_id(value: str) -> str:
    short_id = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{1,16}", short_id):
        raise ValueError("Short ID 必须是 1-16 位十六进制")
    return short_id


def parse_quota_gb(value: str) -> int:
    text = (value or "0").strip()
    if not text:
        return 0
    quota = float(text)
    if quota < 0:
        raise ValueError("流量额度不能小于 0")
    return int(quota * (1024**3))


def quota_gb_from_bytes(value: int | None) -> str:
    if not value or value <= 0:
        return ""
    return f"{value / (1024**3):.2f}".rstrip("0").rstrip(".")


def human_bytes(value: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    current = float(max(value, 0))
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.2f} {unit}"
        current /= 1024
    return f"{value} B"


def parse_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            amount = value.strip().split()[0]
            result[key] = int(amount) * 1024
    except Exception:
        return {}
    return result


def format_uptime(seconds: float) -> str:
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分钟")
    return ''.join(parts)


def get_system_info() -> dict[str, str]:
    disk = shutil.disk_usage('/')
    meminfo = parse_meminfo()
    uptime = ''
    try:
        uptime_seconds = float(Path('/proc/uptime').read_text().split()[0])
        uptime = format_uptime(uptime_seconds)
    except Exception:
        uptime = '未知'
    pretty_name = platform.platform()
    os_release = Path('/etc/os-release')
    if os_release.exists():
        for line in os_release.read_text().splitlines():
            if line.startswith('PRETTY_NAME='):
                pretty_name = line.split('=', 1)[1].strip().strip('"')
                break
    loadavg = ' / '.join(f"{value:.2f}" for value in os.getloadavg()) if hasattr(os, 'getloadavg') else '未知'
    memory_total = int(meminfo.get('MemTotal', 0))
    memory_available = int(meminfo.get('MemAvailable', 0))
    memory_used = max(memory_total - memory_available, 0)
    swap_total = int(meminfo.get('SwapTotal', 0))
    swap_free = int(meminfo.get('SwapFree', 0))
    swap_used = max(swap_total - swap_free, 0)
    return {
        'hostname': platform.node(),
        'os_name': pretty_name,
        'kernel': platform.release(),
        'cpu_count': str(os.cpu_count() or 0),
        'loadavg': loadavg,
        'uptime': uptime,
        'memory_used': human_bytes(memory_used),
        'memory_total': human_bytes(memory_total),
        'disk_used': human_bytes(disk.used),
        'disk_total': human_bytes(disk.total),
        'disk_free': human_bytes(disk.free),
        'swap_used': human_bytes(swap_used),
        'swap_total': human_bytes(swap_total),
    }


def build_subscription_headers(link: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    expires_at = parse_iso(link.get('expires_at') if isinstance(link, dict) else link['expires_at'])
    expire_ts = str(int(expires_at.timestamp())) if expires_at else '0'
    upload = int((link.get('accumulated_up_bytes') if isinstance(link, dict) else link['accumulated_up_bytes']) or 0) - int((link.get('baseline_up_bytes') if isinstance(link, dict) else link['baseline_up_bytes']) or 0)
    download = int((link.get('accumulated_down_bytes') if isinstance(link, dict) else link['accumulated_down_bytes']) or 0) - int((link.get('baseline_down_bytes') if isinstance(link, dict) else link['baseline_down_bytes']) or 0)
    upload = max(upload, 0)
    download = max(download, 0)
    total = int((link.get('quota_bytes') if isinstance(link, dict) else link['quota_bytes']) or 0)
    headers = {
        'Subscription-Userinfo': f'upload={upload}; download={download}; total={total}; expire={expire_ts}',
        'Profile-Update-Interval': '24',
        'Cache-Control': 'no-store',
    }
    return headers


def paginate(total: int, page: int, page_size: int) -> dict[str, int]:
    page = max(page, 1)
    page_size = max(page_size, 1)
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    return {'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages, 'offset': offset}


def run_command(command: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def can_manage_system_services() -> bool:
    return bool(SYSTEMCTL_COMMAND)


def run_systemctl(*args: str) -> tuple[int, str, str]:
    if not can_manage_system_services():
        return 127, "", "未配置服务管理命令"
    return run_command([*SYSTEMCTL_COMMAND, *args])


def restart_panel_container_later(delay_seconds: float = 1.0) -> None:
    def _exit_process() -> None:
        time.sleep(delay_seconds)
        os._exit(0)

    threading.Thread(target=_exit_process, daemon=True).start()


def apply_result_message(message: str, note: str | None) -> tuple[str, str]:
    if not note:
        return message, "success"
    return f"{message}；{note}", "info"


def xray_api_json(*args: str) -> dict[str, Any] | None:
    command = [str(XRAY_BIN), "api", *args, f"--server={XRAY_API_SERVER}"]
    code, stdout, stderr = run_command(command)
    if code != 0:
        lowered = f"{stdout}\n{stderr}".lower()
        if "not found" in lowered or "failed to get stats" in lowered:
            return None
        raise RuntimeError(stderr or stdout or "xray api 调用失败")
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"raw": stdout}


def parse_stats_payload(payload: dict[str, Any] | None) -> dict[str, int]:
    if not payload:
        return {"uplink": 0, "downlink": 0, "online": 0}
    rows = payload.get("stat") or payload.get("stats") or []
    result = {"uplink": 0, "downlink": 0, "online": 0}
    for row in rows:
        name = row.get("name", "")
        value = int(row.get("value", 0))
        if name.endswith("traffic>>>uplink"):
            result["uplink"] = value
        elif name.endswith("traffic>>>downlink"):
            result["downlink"] = value
        elif name.endswith(">>>online"):
            result["online"] = value
    return result


def get_online_ips_from_payload(payload: Any) -> list[str]:
    found: list[str] = []
    ip_pattern = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{2,}")
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            for ip in ip_pattern.findall(value):
                if ip not in found and ip not in {"127.0.0.1", "::1"}:
                    found.append(ip)
    walk(payload)
    return found


def get_link_stats(email: str) -> dict[str, int]:
    payload = xray_api_json("statsquery", f"-pattern=user>>>{email}>>>")
    return parse_stats_payload(payload)


def get_link_online_ips(email: str) -> list[str]:
    payload = xray_api_json("statsonlineiplist", f"-email={email}")
    if not payload:
        return []
    return get_online_ips_from_payload(payload)


def ensure_xray_log_permissions() -> None:
    XRAY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    XRAY_ACCESS_LOG.touch(exist_ok=True)
    XRAY_ERROR_LOG.touch(exist_ok=True)
    subprocess.run(["bash", "-lc", f"id -u xray >/dev/null 2>&1 && chown xray:xray {XRAY_LOG_DIR} {XRAY_ACCESS_LOG} {XRAY_ERROR_LOG} || true"], check=True)


def set_managed_firewall_port(port: int) -> None:
    script = f'''
set -e
for tool in iptables ip6tables; do
  command -v "$tool" >/dev/null 2>&1 || continue
  for comment in "{REALITY_FIREWALL_COMMENT}" "{LEGACY_REALITY_FIREWALL_COMMENTS[0]}"; do
    while "$tool" -S INPUT 2>/dev/null | grep -F -q "$comment"; do
      rule=$($tool -S INPUT | grep -F "$comment" | head -n1)
      $tool ${{rule/-A/-D}}
    done
  done
done
iptables -C INPUT -p tcp --dport {port} -m comment --comment '{REALITY_FIREWALL_COMMENT}' -j ACCEPT 2>/dev/null || \
iptables -I INPUT -p tcp --dport {port} -m comment --comment '{REALITY_FIREWALL_COMMENT}' -j ACCEPT
ip6tables -C INPUT -p tcp --dport {port} -m comment --comment '{REALITY_FIREWALL_COMMENT}' -j ACCEPT 2>/dev/null || \
ip6tables -I INPUT -p tcp --dport {port} -m comment --comment '{REALITY_FIREWALL_COMMENT}' -j ACCEPT
iptables-save > /etc/iptables/rules.v4
ip6tables-save > /etc/iptables/rules.v6
'''
    subprocess.run(["bash", "-lc", script], check=True)

def write_env_file(settings: dict[str, str]) -> None:
    XRAY_ENV_PATH.write_text(
        "\n".join(
            [
                f"XRAY_REALITY_PORT={settings['port']}",
                f"XRAY_REALITY_PRIVATE_KEY={settings['private_key']}",
                f"XRAY_REALITY_PUBLIC_KEY={settings['public_key']}",
                f"XRAY_REALITY_SHORT_ID={settings['short_id']}",
                f"XRAY_REALITY_SERVER={settings['server_domain']}",
                f"XRAY_REALITY_SNI={settings['sni']}",
                f"XRAY_REALITY_TARGET={settings['target']}",
            ]
        )
        + "\n"
    )



def get_links() -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            """
            SELECT l.*, u.username AS owner_username, u.role AS owner_role,
                   COALESCE(u.quota_bytes, 0) AS owner_quota_bytes,
                   COALESCE(u.reset_day, 1) AS owner_reset_day,
                   u.last_reset_at AS owner_last_reset_at,
                   u.status_reason AS owner_status_reason
            FROM links l
            LEFT JOIN admin_users u ON u.id = l.owner_user_id
            ORDER BY l.id ASC
            """
        ).fetchall()


def get_link(link_id: int) -> sqlite3.Row | None:
    with closing(db()) as conn:
        return conn.execute(
            """
            SELECT l.*, u.username AS owner_username, u.role AS owner_role,
                   COALESCE(u.quota_bytes, 0) AS owner_quota_bytes,
                   COALESCE(u.reset_day, 1) AS owner_reset_day,
                   u.last_reset_at AS owner_last_reset_at,
                   u.status_reason AS owner_status_reason
            FROM links l
            LEFT JOIN admin_users u ON u.id = l.owner_user_id
            WHERE l.id = ?
            """,
            (link_id,),
        ).fetchone()


def ensure_link_access(user: sqlite3.Row, link: sqlite3.Row | None) -> RedirectResponse | None:
    if link is None:
        return redirect_with_message('/dashboard', '链接不存在', 'error')
    if is_owner(user):
        return None
    if int(link['owner_user_id'] or 0) != int(user['id']):
        return redirect_with_message('/dashboard', '你没有权限操作这个链接', 'error')
    return None


def get_account_usage_map() -> dict[int, dict[str, int]]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT owner_user_id,
                   SUM(MAX(accumulated_up_bytes - baseline_up_bytes, 0)) AS used_up,
                   SUM(MAX(accumulated_down_bytes - baseline_down_bytes, 0)) AS used_down
            FROM links
            WHERE owner_user_id IS NOT NULL
            GROUP BY owner_user_id
            """
        ).fetchall()
    result = {}
    for row in rows:
        owner_id = int(row['owner_user_id'])
        used_up = int(row['used_up'] or 0)
        used_down = int(row['used_down'] or 0)
        result[owner_id] = {'used_up': used_up, 'used_down': used_down, 'used_total': used_up + used_down}
    return result


def restart_xray_after_apply() -> str | None:
    if can_manage_system_services():
        code, stdout, stderr = run_systemctl("restart", "xray")
        if code != 0:
            raise RuntimeError(stderr or stdout or "Xray 重启失败")
        return None
    if RUNTIME_MODE == "docker":
        return "Xray 配置已写入，请在宿主机手动重启 Xray"
    raise RuntimeError("未配置可用的 Xray 重启方式")


def apply_xray_config(reason: str) -> str | None:
    settings = get_current_settings()
    ensure_xray_log_permissions()
    config = load_xray_config()
    inbound = get_vless_inbound(config)
    inbound["tag"] = INBOUND_TAG
    inbound["port"] = validate_port(settings["port"])
    inbound.setdefault("settings", {})["decryption"] = "none"
    clients = []
    for link in get_links():
        if not bool(link["enabled"]):
            continue
        clients.append(
            {
                "id": link["uuid"],
                "flow": settings["flow"],
                "email": link["email"],
                "level": 0,
            }
        )
    inbound["settings"]["clients"] = clients
    inbound.setdefault("streamSettings", {})["network"] = "tcp"
    inbound["streamSettings"]["security"] = "reality"
    reality = inbound["streamSettings"].setdefault("realitySettings", {})
    reality["show"] = False
    reality["dest"] = settings["target"]
    reality["xver"] = 0
    reality["serverNames"] = [settings["sni"]]
    reality["privateKey"] = settings["private_key"]
    reality["shortIds"] = [settings["short_id"]]

    config["api"] = {
        "tag": "api",
        "listen": XRAY_API_SERVER,
        "services": ["HandlerService", "StatsService"],
    }
    config["stats"] = {}
    config["policy"] = {
        "levels": {
            "0": {
                "statsUserUplink": True,
                "statsUserDownlink": True,
                "statsUserOnline": True,
            }
        },
        "system": {
            "statsInboundUplink": True,
            "statsInboundDownlink": True,
        },
    }
    config["log"] = {
        "access": str(XRAY_ACCESS_LOG),
        "error": str(XRAY_ERROR_LOG),
        "loglevel": "warning",
    }
    save_xray_config(config)
    code, stdout, stderr = run_command([str(XRAY_BIN), "run", "-test", "-config", str(XRAY_CONFIG_PATH)])
    if code != 0:
        raise RuntimeError(stderr or stdout or "Xray 配置校验失败")
    if PANEL_MANAGE_FIREWALL:
        set_managed_firewall_port(inbound["port"])
    write_env_file(settings)
    note = restart_xray_after_apply()
    add_audit("xray_apply", reason)
    return note


def current_cycle_start(now: datetime, reset_day: int) -> datetime:
    reset_day = max(1, min(28, int(reset_day or 1)))
    current_last_day = calendar.monthrange(now.year, now.month)[1]
    current_day = min(reset_day, current_last_day)
    if now.day >= current_day:
        return datetime(now.year, now.month, current_day, tzinfo=UTC)
    if now.month == 1:
        prev_year, prev_month = now.year - 1, 12
    else:
        prev_year, prev_month = now.year, now.month - 1
    prev_last_day = calendar.monthrange(prev_year, prev_month)[1]
    prev_day = min(reset_day, prev_last_day)
    return datetime(prev_year, prev_month, prev_day, tzinfo=UTC)


def should_reset_monthly(last_reset_at: str | None, reset_day: int) -> bool:
    last_reset = parse_iso(last_reset_at)
    cycle_start = current_cycle_start(now_utc(), reset_day)
    if last_reset is None:
        return False
    return last_reset < cycle_start


def parse_access_line(raw_line: str) -> dict[str, Any] | None:
    raw_line = raw_line.strip()
    if not raw_line:
        return None
    match = ACCESS_LINE_TIME.match(raw_line)
    if not match:
        return None
    raw_ts = match.group("ts")
    fmt = "%Y/%m/%d %H:%M:%S.%f" if "." in raw_ts else "%Y/%m/%d %H:%M:%S"
    ts = datetime.strptime(raw_ts, fmt).replace(tzinfo=UTC).isoformat()
    body = match.group("body")
    target_match = ACCESS_TARGET.search(body)
    route_match = ACCESS_ROUTE.search(body)
    email = ""
    for pattern in ACCESS_EMAIL_PATTERNS:
        found = pattern.search(body)
        if found:
            email = found.group("email")
            break
    source_match = ACCESS_SOURCE.search(body)
    client_ip = source_match.group("client_ip") if source_match else ""
    if client_ip.startswith("[") and client_ip.endswith("]"):
        client_ip = client_ip[1:-1]
    destination = target_match.group("destination") if target_match else ""
    network = target_match.group("network") if target_match else ""
    inbound_tag = route_match.group("inbound") if route_match else ""
    outbound_tag = route_match.group("outbound") if route_match else ""
    return {
        "ts": ts,
        "client_ip": client_ip,
        "destination": destination,
        "network": network,
        "inbound_tag": inbound_tag,
        "outbound_tag": outbound_tag,
        "email": email,
        "raw_line": raw_line,
    }


def parse_access_log_incrementally() -> None:
    ensure_xray_log_permissions()
    settings = get_current_settings()
    offset = int(settings.get("access_log_offset", "0") or 0)
    if not XRAY_ACCESS_LOG.exists():
        return
    file_size = XRAY_ACCESS_LOG.stat().st_size
    if file_size < offset:
        offset = 0
    with XRAY_ACCESS_LOG.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(offset)
        lines = handle.readlines()
        new_offset = handle.tell()
    with closing(db()) as conn:
        access_count = conn.execute("SELECT COUNT(*) AS count FROM access_events").fetchone()["count"]
    if access_count == 0 and offset != 0:
        offset = 0
        with XRAY_ACCESS_LOG.open("r", encoding="utf-8", errors="ignore") as handle:
            handle.seek(offset)
            lines = handle.readlines()
            new_offset = handle.tell()
    if not lines:
        runtime_state["last_access_parse_at"] = now_iso()
        runtime_state["last_access_parse_error"] = None
        set_setting("access_log_offset", str(new_offset))
        return
    links = {row["email"]: row for row in get_links()}
    with closing(db()) as conn:
        for line in lines:
            item = parse_access_line(line)
            if not item:
                continue
            link = links.get(item["email"])
            if not link and item["client_ip"]:
                for candidate in links.values():
                    ips = json.loads(candidate["last_online_ips"] or "[]")
                    if item["client_ip"] in ips:
                        link = candidate
                        item["email"] = candidate["email"]
                        break
            conn.execute(
                """
                INSERT INTO access_events(ts, link_id, link_email, client_ip, destination, network, inbound_tag, outbound_tag, raw_line)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["ts"],
                    link["id"] if link else None,
                    item["email"] or (link["email"] if link else None),
                    item["client_ip"],
                    item["destination"],
                    item["network"],
                    item["inbound_tag"],
                    item["outbound_tag"],
                    item["raw_line"],
                ),
            )
        conn.execute("DELETE FROM access_events WHERE ts < ?", ((now_utc() - timedelta(days=ACCESS_EVENTS_RETENTION_DAYS)).isoformat(),))
        conn.commit()
    trim_table_rows('access_events', int(get_current_settings()['max_access_rows']))
    trim_log_file(XRAY_ACCESS_LOG, int(get_current_settings()['max_access_log_mb']))
    trim_log_file(XRAY_ERROR_LOG, int(get_current_settings()['max_error_log_mb']))
    set_setting("access_log_offset", str(new_offset))
    runtime_state["last_access_parse_at"] = now_iso()
    runtime_state["last_access_parse_error"] = None


def poll_once() -> str | None:
    disabled_names: list[str] = []
    reenabled_names: list[str] = []
    apply_note: str | None = None
    now = now_utc()
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM links ORDER BY id ASC").fetchall()
        for row in rows:
            stats = get_link_stats(row["email"])
            current_up = int(stats["uplink"])
            current_down = int(stats["downlink"])
            online_count = int(stats["online"])
            previous_up = int(row["last_api_up_bytes"])
            previous_down = int(row["last_api_down_bytes"])
            delta_up = current_up - previous_up if current_up >= previous_up else current_up
            delta_down = current_down - previous_down if current_down >= previous_down else current_down
            accumulated_up = int(row["accumulated_up_bytes"]) + max(delta_up, 0)
            accumulated_down = int(row["accumulated_down_bytes"]) + max(delta_down, 0)
            online_ips = get_link_online_ips(row["email"]) if online_count > 0 else []
            last_seen_at = row["last_seen_at"]
            if online_count > 0 or delta_up > 0 or delta_down > 0:
                last_seen_at = now_iso()
            baseline_total = int(row["baseline_total_bytes"] or 0)
            baseline_up = int(row["baseline_up_bytes"] or 0)
            baseline_down = int(row["baseline_down_bytes"] or 0)
            status_reason = row["status_reason"] or "active"
            enabled = int(row["enabled"])
            last_reset_at = row["last_reset_at"] or row["created_at"]
            expires_at = parse_iso(row["expires_at"])
            if enabled and expires_at and int(row["auto_disable_on_expire"] or 0) and now >= expires_at:
                enabled = 0
                status_reason = "expired"
                disabled_names.append(row["name"])
            total_used = max(0, (accumulated_up - baseline_up) + (accumulated_down - baseline_down))
            quota_bytes = int(row["quota_bytes"] or 0)
            if enabled and quota_bytes > 0 and total_used >= quota_bytes and not row["owner_user_id"]:
                enabled = 0
                status_reason = "quota"
                disabled_names.append(row["name"])
            conn.execute(
                """
                UPDATE links
                SET accumulated_up_bytes = ?,
                    accumulated_down_bytes = ?,
                    last_api_up_bytes = ?,
                    last_api_down_bytes = ?,
                    last_online_count = ?,
                    last_online_ips = ?,
                    last_seen_at = ?,
                    baseline_total_bytes = ?,
                    baseline_up_bytes = ?,
                    baseline_down_bytes = ?,
                    last_reset_at = ?,
                    enabled = ?,
                    status_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    accumulated_up, accumulated_down, current_up, current_down, online_count, json.dumps(online_ips),
                    last_seen_at, baseline_total, baseline_up, baseline_down, last_reset_at, enabled, status_reason, now_iso(), row["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO traffic_samples(link_id, ts, delta_up_bytes, delta_down_bytes, total_used_bytes, online_count, online_ips)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (row["id"], now_iso(), max(delta_up, 0), max(delta_down, 0), total_used, online_count, json.dumps(online_ips)),
            )

        members = conn.execute("SELECT * FROM admin_users WHERE role = 'member' ORDER BY id ASC").fetchall()
        for member in members:
            member_links = conn.execute("SELECT * FROM links WHERE owner_user_id = ? ORDER BY id ASC", (member["id"],)).fetchall()
            last_reset_at = member["last_reset_at"] or member["created_at"]
            if should_reset_monthly(last_reset_at, int(member["reset_day"] or 1)):
                for link in member_links:
                    conn.execute(
                        "UPDATE links SET baseline_total_bytes = ?, baseline_up_bytes = ?, baseline_down_bytes = ?, last_reset_at = ?, enabled = 1, status_reason = 'active', updated_at = ? WHERE id = ?",
                        (
                            int(link["accumulated_up_bytes"] or 0) + int(link["accumulated_down_bytes"] or 0),
                            int(link["accumulated_up_bytes"] or 0),
                            int(link["accumulated_down_bytes"] or 0),
                            now_iso(), now_iso(), link["id"],
                        ),
                    )
                conn.execute("UPDATE admin_users SET last_reset_at = ?, status_reason = 'active' WHERE id = ?", (now_iso(), member["id"]))
                reenabled_names.append(member["username"])
                member_links = conn.execute("SELECT * FROM links WHERE owner_user_id = ? ORDER BY id ASC", (member["id"],)).fetchall()
            account_used = 0
            for link in member_links:
                account_used += max(0, int(link["accumulated_up_bytes"] or 0) - int(link["baseline_up_bytes"] or 0))
                account_used += max(0, int(link["accumulated_down_bytes"] or 0) - int(link["baseline_down_bytes"] or 0))
            quota_bytes = int(member["quota_bytes"] or 0)
            if quota_bytes > 0 and account_used >= quota_bytes:
                conn.execute("UPDATE admin_users SET status_reason = 'quota' WHERE id = ?", (member["id"],))
                conn.execute("UPDATE links SET enabled = 0, status_reason = 'quota', updated_at = ? WHERE owner_user_id = ?", (now_iso(), member["id"]))
                disabled_names.append(member["username"])
            else:
                conn.execute("UPDATE admin_users SET status_reason = 'active' WHERE id = ?", (member["id"],))

        conn.execute("DELETE FROM traffic_samples WHERE ts < ?", ((now_utc() - timedelta(days=SAMPLE_RETENTION_DAYS)).isoformat(),))
        conn.commit()
    trim_table_rows('traffic_samples', int(get_current_settings()['max_traffic_rows']))
    if disabled_names or reenabled_names:
        apply_note = apply_xray_config("poll_status_change")
        if disabled_names:
            add_audit("auto_disable", f"自动停用: {', '.join(sorted(set(disabled_names)))}")
        if reenabled_names:
            add_audit("auto_reenable", f"按月重置后恢复: {', '.join(sorted(set(reenabled_names)))}")
    return apply_note


def poll_loop() -> None:
    while not stop_event.is_set():
        try:
            note = poll_once()
            runtime_state["last_sync_at"] = now_iso()
            runtime_state["last_sync_error"] = note
        except Exception as exc:
            runtime_state["last_sync_error"] = str(exc)
        try:
            parse_access_log_incrementally()
        except Exception as exc:
            runtime_state["last_access_parse_error"] = str(exc)
        stop_event.wait(POLL_INTERVAL_SECONDS)


def cleanup_expired_sessions() -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        conn.commit()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    expires_at = (now_utc() + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at, csrf_token) VALUES(?, ?, ?, ?, ?)",
            (token, user_id, now_iso(), expires_at, csrf_token),
        )
        conn.commit()
    return token


def get_current_user(request: Request) -> dict[str, Any]:
    cleanup_expired_sessions()
    token = get_session_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    with closing(db()) as conn:
        row = conn.execute(
            """
            SELECT
                sessions.token AS session_token,
                sessions.expires_at,
                sessions.csrf_token,
                admin_users.id AS id,
                admin_users.username,
                admin_users.role,
                admin_users.password_hash,
                admin_users.salt,
                admin_users.link_id,
                admin_users.quota_bytes,
                admin_users.reset_day,
                admin_users.last_reset_at,
                admin_users.status_reason
            FROM sessions
            JOIN admin_users ON admin_users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = dict(row)
    expires_at = parse_iso(user["expires_at"])
    if expires_at and expires_at < now_utc():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user["csrf_token"] = ensure_session_csrf_token(token, user.get("csrf_token"))
    return user

def optional_current_user(request: Request) -> dict[str, Any] | None:
    try:
        return get_current_user(request)
    except HTTPException:
        return None


def is_owner(user: dict[str, Any]) -> bool:
    return (user["role"] or "owner") == "owner"


def ensure_owner_or_redirect(user: dict[str, Any]) -> RedirectResponse | None:
    if is_owner(user):
        return None
    return redirect_with_message("/dashboard", "当前账号仅可查看自己的使用情况", "info")


def redirect_with_message(path: str, message: str, level: str = "success") -> RedirectResponse:
    query = urlencode({"msg": message, "level": level})
    separator = "&" if "?" in path else "?"
    return RedirectResponse(url=f"{path}{separator}{query}", status_code=status.HTTP_303_SEE_OTHER)


def csrf_failure_path(request: Request) -> str:
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        parsed = urlparse(referer)
        if not parsed.netloc or parsed.netloc == request.url.netloc:
            path = parsed.path or "/dashboard"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            return path
    if request.url.path.startswith("/settings"):
        return "/settings"
    if request.url.path.startswith("/links"):
        return "/links"
    if request.url.path.startswith("/activity"):
        return "/activity"
    if request.url.path == "/logout":
        return "/login"
    return "/dashboard"


def validate_login_csrf(request: Request, submitted_token: str) -> RedirectResponse | None:
    expected_token = (request.cookies.get(LOGIN_CSRF_COOKIE_NAME) or "").strip()
    submitted_token = submitted_token.strip()
    if submitted_token and expected_token and secrets.compare_digest(submitted_token, expected_token):
        return None
    logger.warning("CSRF_FAIL path=%s ip=%s mode=login", request.url.path, get_client_ip(request))
    return redirect_with_message("/login", "登录页面已失效，请刷新后重试", "error")


def validate_session_csrf(request: Request, submitted_token: str) -> RedirectResponse | None:
    session_token = get_session_token(request)
    expected_token = get_session_csrf_token(session_token) if session_token else None
    submitted_token = submitted_token.strip()
    if submitted_token and expected_token and secrets.compare_digest(submitted_token, expected_token):
        return None
    logger.warning("CSRF_FAIL path=%s ip=%s mode=session", request.url.path, get_client_ip(request))
    return redirect_with_message(csrf_failure_path(request), "页面已过期，请刷新后重试", "error")


def service_restart_supported(name: str) -> bool:
    if name == PANEL_SERVICE_NAME and RUNTIME_MODE == "docker":
        return True
    return can_manage_system_services()


def service_unit_name(name: str) -> str:
    return name if name.endswith(".service") else f"{name}.service"


def resolve_host_service_unit(name: str) -> Path | None:
    unit_name = service_unit_name(name)
    for unit_dir in HOST_SYSTEMD_UNIT_DIRS:
        candidate = unit_dir / unit_name
        if candidate.exists():
            return candidate
    return None


def can_manage_service_autostart(name: str) -> bool:
    if name == PANEL_SERVICE_NAME:
        return False
    if can_manage_system_services():
        return True
    if RUNTIME_MODE != "docker":
        return False
    return HOST_SYSTEMD_ETC_DIR.exists() and resolve_host_service_unit(name) is not None


def host_service_enabled(name: str) -> bool:
    link_path = HOST_SYSTEMD_MULTI_USER_WANTS_DIR / service_unit_name(name)
    return link_path.exists() or link_path.is_symlink()


def set_host_service_enabled(name: str, enabled: bool) -> None:
    unit_path = resolve_host_service_unit(name)
    if unit_path is None:
        raise RuntimeError(f"未找到 {name} 的 systemd unit 文件")

    link_path = HOST_SYSTEMD_MULTI_USER_WANTS_DIR / service_unit_name(name)
    if enabled:
        HOST_SYSTEMD_MULTI_USER_WANTS_DIR.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_dir() and not link_path.is_symlink():
                raise RuntimeError(f"{link_path} 不是可覆盖的链接")
            link_path.unlink()
        relative_target = os.path.relpath(unit_path, HOST_SYSTEMD_MULTI_USER_WANTS_DIR)
        link_path.symlink_to(relative_target)
        return

    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            raise RuntimeError(f"{link_path} 不是可移除的链接")
        link_path.unlink()


def service_toggle_supported(name: str) -> bool:
    return can_manage_service_autostart(name)


def service_status(name: str) -> str:
    if name == PANEL_SERVICE_NAME and RUNTIME_MODE == "docker":
        return "docker"
    if not can_manage_system_services():
        if can_manage_service_autostart(name):
            return "host"
        return "external"
    code, stdout, _ = run_systemctl("is-active", name)
    if code == 0:
        return stdout or "active"
    return stdout or "inactive"


def service_enabled(name: str) -> bool:
    if can_manage_system_services():
        code, stdout, _ = run_systemctl("is-enabled", name)
        return code == 0 and stdout.strip() == 'enabled'
    if can_manage_service_autostart(name):
        return host_service_enabled(name)
    return False


def get_admin_users() -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute("SELECT id, username, role, link_id, created_at, last_login_at FROM admin_users ORDER BY id ASC").fetchall()


def trim_table_rows(table: str, max_rows: int) -> None:
    if max_rows <= 0:
        return
    with closing(db()) as conn:
        count = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        if count <= max_rows:
            return
        delete_count = count - max_rows
        conn.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY id ASC LIMIT ?)", (delete_count,))
        conn.commit()


def trim_log_file(path: Path, max_mb: int) -> None:
    if max_mb <= 0 or not path.exists():
        return
    max_bytes = max_mb * 1024 * 1024
    size = path.stat().st_size
    if size <= max_bytes:
        return
    keep_bytes = max_bytes // 2
    with path.open('rb') as handle:
        if keep_bytes > 0:
            handle.seek(max(-keep_bytes, -size), 2)
            data = handle.read()
        else:
            data = b''
    if b'\n' in data:
        data = data.split(b'\n', 1)[-1]
    path.write_bytes(data)


def manage_service_action(name: str, action: str) -> None:
    if name not in {'xray', 'nginx', PANEL_SERVICE_NAME}:
        raise RuntimeError('不支持的服务')
    if action == 'restart':
        if name == PANEL_SERVICE_NAME:
            if RUNTIME_MODE == "docker":
                restart_panel_container_later()
                return
            if not can_manage_system_services():
                raise RuntimeError('当前运行模式未配置面板服务重启命令')
            def _restart_panel_service() -> None:
                time.sleep(1)
                run_systemctl("restart", PANEL_SERVICE_NAME)
            threading.Thread(target=_restart_panel_service, daemon=True).start()
            return
        if not can_manage_system_services():
            raise RuntimeError(f'当前运行模式未配置 {name} 服务控制，请在宿主机手动处理')
        code, stdout, stderr = run_systemctl("restart", name)
        if code != 0:
            raise RuntimeError(stderr or stdout or f'{name} 重启失败')
        return
    if action == 'enable':
        if name == PANEL_SERVICE_NAME:
            raise RuntimeError('面板服务默认建议保持开机自启')
        if can_manage_system_services():
            code, stdout, stderr = run_systemctl("enable", name)
            if code != 0:
                raise RuntimeError(stderr or stdout or f'{name} 开启自启失败')
            return
        if not can_manage_service_autostart(name):
            raise RuntimeError(f'当前运行模式未配置 {name} 服务自启控制')
        set_host_service_enabled(name, True)
        return
    if action == 'disable':
        if name == PANEL_SERVICE_NAME:
            raise RuntimeError('为避免自锁，面板服务不提供关闭开机自启')
        if can_manage_system_services():
            code, stdout, stderr = run_systemctl("disable", name)
            if code != 0:
                raise RuntimeError(stderr or stdout or f'{name} 关闭自启失败')
            return
        if not can_manage_service_autostart(name):
            raise RuntimeError(f'当前运行模式未配置 {name} 服务自启控制')
        set_host_service_enabled(name, False)
        return
    raise RuntimeError('不支持的操作')


def build_link_urls(link: sqlite3.Row, settings: dict[str, str]) -> dict[str, str]:
    server = settings["server_domain"]
    port = settings["port"]
    public_key = settings["public_key"]
    short_id = settings["short_id"]
    sni = settings["sni"]
    flow = settings["flow"]
    token = link["share_token"]
    loon_sub = f"https://{server}/subscribe/{token}"
    vless_sub = f"https://{server}/subscribe/{token}/vless"
    loon_import = "loon://import?nodelist=" + quote(loon_sub, safe="")
    vless_uri = (
        f"vless://{link['uuid']}@{server}:{port}?encryption=none&security=reality"
        f"&sni={quote(sni)}&fp=chrome&pbk={quote(public_key)}&sid={short_id}&type=tcp&flow={flow}"
        f"#{quote(link['name'])}"
    )
    return {
        "loon_sub": loon_sub,
        "vless_sub": vless_sub,
        "loon_import": loon_import,
        "vless_uri": vless_uri,
    }


def build_chart_data() -> dict[str, Any]:
    with closing(db()) as conn:
        sample_rows = conn.execute(
            "SELECT ts, delta_up_bytes, delta_down_bytes FROM traffic_samples WHERE ts >= ? ORDER BY ts ASC",
            ((now_utc() - timedelta(hours=24)).isoformat(),),
        ).fetchall()
        link_rows = conn.execute(
            "SELECT name, quota_bytes, baseline_total_bytes, accumulated_up_bytes, accumulated_down_bytes FROM links ORDER BY id ASC"
        ).fetchall()
    buckets: dict[str, dict[str, int]] = {}
    for row in sample_rows:
        ts = parse_iso(row["ts"])
        if not ts:
            continue
        hour_key = ts.astimezone(DISPLAY_TZ).strftime("%m-%d %H:00")
        bucket = buckets.setdefault(hour_key, {"up": 0, "down": 0})
        bucket["up"] += int(row["delta_up_bytes"])
        bucket["down"] += int(row["delta_down_bytes"])
    labels = list(buckets.keys())[-24:]
    usage_line = {
        "labels": labels,
        "uplink": [buckets[label]["up"] for label in labels],
        "downlink": [buckets[label]["down"] for label in labels],
    }
    link_usage = {
        "labels": [],
        "used": [],
        "quota": [],
    }
    for row in link_rows:
        used = max(0, (int(row["accumulated_up_bytes"]) + int(row["accumulated_down_bytes"])) - int(row["baseline_total_bytes"] or 0))
        link_usage["labels"].append(row["name"])
        link_usage["used"].append(used)
        link_usage["quota"].append(int(row["quota_bytes"] or 0))
    return {"usage_line": usage_line, "link_usage": link_usage}


def get_recent_activity(page: int = 1, page_size: int = 20, link_id: int | None = None, owner_user_id: int | None = None) -> dict[str, Any]:
    where_clause = "WHERE (s.delta_up_bytes > 0 OR s.delta_down_bytes > 0)"
    join_clause = "JOIN links l ON l.id = s.link_id"
    params: list[Any] = []
    if link_id is not None:
        where_clause += " AND s.link_id = ?"
        params.append(int(link_id))
    if owner_user_id is not None:
        where_clause += " AND l.owner_user_id = ?"
        params.append(int(owner_user_id))
    with closing(db()) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM traffic_samples s {join_clause} {where_clause}", tuple(params)
        ).fetchone()["count"]
        meta = paginate(int(total), page, page_size)
        rows = conn.execute(
            f"""
            SELECT s.*, l.name AS link_name, l.email AS link_email
            FROM traffic_samples s
            JOIN links l ON l.id = s.link_id
            {where_clause}
            ORDER BY s.id DESC LIMIT ? OFFSET ?
            """,
            (*params, meta['page_size'], meta['offset']),
        ).fetchall()
    items = []
    for row in rows:
        items.append(
            {
                "ts": row["ts"],
                "link_name": row["link_name"],
                "link_email": row["link_email"],
                "delta_up_human": human_bytes(int(row["delta_up_bytes"])),
                "delta_down_human": human_bytes(int(row["delta_down_bytes"])),
                "total_used_human": human_bytes(int(row["total_used_bytes"])),
                "online_count": int(row["online_count"]),
                "online_ips": json.loads(row["online_ips"] or "[]"),
            }
        )
    meta['items'] = items
    return meta


def get_recent_access_events(page: int = 1, page_size: int = 30, link_id: int | None = None, owner_user_id: int | None = None) -> dict[str, Any]:
    where_clause = ""
    params: list[Any] = []
    join_clause = ""
    if link_id is not None:
        where_clause = "WHERE access_events.link_id = ?"
        params.append(int(link_id))
    if owner_user_id is not None:
        join_clause = "JOIN links l ON l.id = access_events.link_id"
        where_clause = (where_clause + " AND" if where_clause else "WHERE") + " l.owner_user_id = ?"
        params.append(int(owner_user_id))
    with closing(db()) as conn:
        total = conn.execute(f"SELECT COUNT(*) AS count FROM access_events {join_clause} {where_clause}", tuple(params)).fetchone()["count"]
        meta = paginate(int(total), page, page_size)
        rows = conn.execute(f"SELECT access_events.* FROM access_events {join_clause} {where_clause} ORDER BY access_events.id DESC LIMIT ? OFFSET ?", (*params, meta['page_size'], meta['offset'])).fetchall()
    meta['items'] = [dict(row) for row in rows]
    return meta


def get_recent_client_ip_map() -> dict[str, str]:
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT link_email, client_ip, MAX(id) AS max_id
            FROM access_events
            WHERE link_email IS NOT NULL AND client_ip IS NOT NULL AND client_ip != ''
            GROUP BY link_email
            """
        ).fetchall()
    return {row['link_email']: row['client_ip'] for row in rows if row['link_email']}


def get_recent_audit(limit: int = 50) -> list[dict[str, Any]]:
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]



def build_links_view(settings: dict[str, str], only_link_id: int | None = None, only_owner_user_id: int | None = None) -> list[dict[str, Any]]:
    items = []
    recent_client_ip_map = get_recent_client_ip_map()
    account_usage_map = get_account_usage_map()
    source_links = get_links()
    if only_link_id is not None:
        source_links = [link for link in source_links if int(link["id"]) == int(only_link_id)]
    if only_owner_user_id is not None:
        source_links = [link for link in source_links if int(link["owner_user_id"] or 0) == int(only_owner_user_id)]
    for link in source_links:
        baseline_up = int(link["baseline_up_bytes"] or 0)
        baseline_down = int(link["baseline_down_bytes"] or 0)
        link_used_up = max(0, int(link["accumulated_up_bytes"] or 0) - baseline_up)
        link_used_down = max(0, int(link["accumulated_down_bytes"] or 0) - baseline_down)
        link_used_total = link_used_up + link_used_down
        link_quota_bytes = int(link["quota_bytes"] or 0)
        link_usage_percent = 0 if link_quota_bytes <= 0 else min(100, round(link_used_total * 100 / link_quota_bytes, 1))

        owner_user_id = int(link["owner_user_id"] or 0)
        owner_role = link["owner_role"] or 'owner'
        account_usage = account_usage_map.get(owner_user_id, {'used_up': link_used_up, 'used_down': link_used_down, 'used_total': link_used_total})

        if owner_role == 'member':
            account_quota_bytes = int(link["owner_quota_bytes"] or 0)
            account_used_total = int(account_usage['used_total'])
            account_used_up = int(account_usage['used_up'])
            account_used_down = int(account_usage['used_down'])
            account_reset_day = int(link["owner_reset_day"] or 1)
            account_usage_percent = 0 if account_quota_bytes <= 0 else min(100, round(account_used_total * 100 / account_quota_bytes, 1))
        else:
            account_quota_bytes = link_quota_bytes
            account_used_total = link_used_total
            account_used_up = link_used_up
            account_used_down = link_used_down
            account_reset_day = int(link["reset_day"] or 1)
            account_usage_percent = link_usage_percent

        urls = build_link_urls(link, settings)
        expires_at = parse_iso(link["expires_at"])
        items.append(
            {
                'id': link['id'],
                'owner_user_id': owner_user_id,
                'owner_username': link['owner_username'] or 'owner',
                'owner_role': owner_role,
                'name': link['name'],
                'email': link['email'],
                'uuid': link['uuid'],
                'enabled': bool(link['enabled']),
                'status_reason': link['status_reason'],
                'quota_bytes': account_quota_bytes,
                'quota_gb': quota_gb_from_bytes(account_quota_bytes),
                'used_bytes': account_used_total,
                'used_up_bytes': account_used_up,
                'used_down_bytes': account_used_down,
                'used_human': human_bytes(account_used_total),
                'used_up_human': human_bytes(account_used_up),
                'used_down_human': human_bytes(account_used_down),
                'quota_human': human_bytes(account_quota_bytes) if account_quota_bytes else '不限',
                'remaining_human': human_bytes(max(account_quota_bytes - account_used_total, 0)) if account_quota_bytes else '不限',
                'usage_percent': account_usage_percent,
                'link_quota_bytes': link_quota_bytes,
                'link_quota_gb': quota_gb_from_bytes(link_quota_bytes),
                'link_quota_human': human_bytes(link_quota_bytes) if link_quota_bytes else '不限',
                'link_remaining_human': human_bytes(max(link_quota_bytes - link_used_total, 0)) if link_quota_bytes else '不限',
                'link_used_bytes': link_used_total,
                'link_used_human': human_bytes(link_used_total),
                'link_used_up_human': human_bytes(link_used_up),
                'link_used_down_human': human_bytes(link_used_down),
                'link_usage_percent': link_usage_percent,
                'online_count': int(link['last_online_count']),
                'online_ips': json.loads(link['last_online_ips'] or '[]'),
                'recent_client_ip': recent_client_ip_map.get(link['email'], ''),
                'last_seen_at': link['last_seen_at'],
                'expires_at': expires_at.isoformat() if expires_at else '',
                'expires_at_display': expires_at.strftime('%Y-%m-%d') if expires_at else '不限',
                'last_reset_at': link['last_reset_at'] or link['created_at'],
                'reset_day': account_reset_day,
                'auto_disable_on_expire': bool(link['auto_disable_on_expire']),
                'auto_reenable_on_reset': bool(link['auto_reenable_on_reset']),
                'notes': link['notes'],
                'created_at': link['created_at'],
                'updated_at': link['updated_at'],
                **urls,
            }
        )
    return items


def build_link_groups(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for link in links:
        key = f"{link.get('owner_role') or 'owner'}:{link.get('owner_user_id') or 0}"
        if key not in grouped:
            grouped[key] = {
                'key': key,
                'owner_user_id': link.get('owner_user_id'),
                'owner_username': link.get('owner_username') or 'owner',
                'owner_role': link.get('owner_role') or 'owner',
                'quota_human': link['quota_human'],
                'used_human': link['used_human'],
                'remaining_human': link['remaining_human'],
                'usage_percent': link['usage_percent'],
                'links': [],
            }
        grouped[key]['links'].append(link)
    groups = list(grouped.values())
    groups.sort(key=lambda item: (item['owner_role'] != 'owner', item['owner_username'] or ''))
    return groups


def build_summary(links: list[dict[str, Any]]) -> dict[str, Any]:
    account_seen: set[tuple[str, int]] = set()
    total_used = 0
    total_quota = 0
    for link in links:
        if link.get('owner_role') == 'member':
            key = ('member', int(link.get('owner_user_id') or 0))
            if key in account_seen:
                continue
            account_seen.add(key)
        total_used += int(link['used_bytes'])
        total_quota += int(link['quota_bytes'])
    total_online = sum(int(link['online_count']) for link in links)
    active_accounts = len({(link.get('owner_role') or 'owner', int(link.get('owner_user_id') or 0)) for link in links})
    with closing(db()) as conn:
        traffic_24h = conn.execute(
            "SELECT COALESCE(SUM(delta_up_bytes + delta_down_bytes), 0) AS total FROM traffic_samples WHERE ts >= ?",
            ((now_utc() - timedelta(hours=24)).isoformat(),),
        ).fetchone()['total']
        target_24h = conn.execute(
            "SELECT COUNT(DISTINCT destination) AS total FROM access_events WHERE ts >= ? AND destination IS NOT NULL AND destination != ''",
            ((now_utc() - timedelta(hours=24)).isoformat(),),
        ).fetchone()['total']
    return {
        'total_links': len(links),
        'active_links': sum(1 for link in links if link['enabled']),
        'total_used_human': human_bytes(total_used),
        'total_quota_human': human_bytes(total_quota) if total_quota else '不限',
        'quota_percent': 0 if total_quota <= 0 else min(100, round(total_used * 100 / total_quota, 1)),
        'total_online': total_online,
        'expired_links': sum(1 for link in links if link['status_reason'] == 'expired'),
        'quota_limited_links': sum(1 for link in links if link['status_reason'] in {'quota', 'quota_link', 'quota_account'}),
        'active_accounts': active_accounts,
        'traffic_24h_human': human_bytes(int(traffic_24h or 0)),
        'targets_24h': int(target_24h or 0),
    }



def get_allocated_quota_bytes(owner_user_id: int, exclude_link_id: int | None = None) -> int:
    with closing(db()) as conn:
        if exclude_link_id is not None:
            row = conn.execute("SELECT COALESCE(SUM(quota_bytes), 0) AS total FROM links WHERE owner_user_id = ? AND id != ?", (owner_user_id, exclude_link_id)).fetchone()
        else:
            row = conn.execute("SELECT COALESCE(SUM(quota_bytes), 0) AS total FROM links WHERE owner_user_id = ?", (owner_user_id,)).fetchone()
    return int(row['total'] or 0)


def build_common_context(request: Request, user: dict[str, Any], active_nav: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = get_current_settings()
    owner = is_owner(user)
    links = build_links_view(settings, only_owner_user_id=(None if owner else user["id"]))
    nav = [
        {"name": "概览", "path": "/dashboard", "key": "dashboard"},
        {"name": "链接", "path": "/links", "key": "links"},
        {"name": "活动明细", "path": "/activity", "key": "activity"},
        {"name": "系统设置", "path": "/settings", "key": "settings"},
    ]
    allocated_quota_bytes = sum(int(link.get('link_quota_bytes') or 0) for link in links)
    member_total_quota_bytes = int(user['quota_bytes'] or 0) if not owner else 0
    context = {
        "request": request,
        "user": user,
        "is_owner": owner,
        "settings": {key: value for key, value in settings.items() if key != "private_key"},
        "summary": build_summary(links),
        "links": links,
        "link_groups": build_link_groups(links),
        "member_allocation": {
            'total_quota_human': human_bytes(member_total_quota_bytes) if member_total_quota_bytes else '不限',
            'allocated_quota_human': human_bytes(allocated_quota_bytes),
            'remaining_allocatable_human': human_bytes(max(member_total_quota_bytes - allocated_quota_bytes, 0)) if member_total_quota_bytes else '不限',
        },
        "nav": nav,
        "active_nav": active_nav,
        "message": request.query_params.get("msg", ""),
        "level": request.query_params.get("level", "info"),
        "csrf_token": user["csrf_token"],
        "runtime": runtime_state.copy(),
        "audit": get_recent_audit(),
        "system_info": get_system_info(),
        "admin_users": get_admin_users(),
        "panel_service_name": PANEL_SERVICE_NAME,
        "runtime_mode": RUNTIME_MODE,
        "service_manager_available": can_manage_system_services(),
        "host_service_restart_available": service_restart_supported("xray") or service_restart_supported("nginx"),
        "host_service_toggle_available": service_toggle_supported("xray") or service_toggle_supported("nginx"),
        "service_status": {
            "xray": service_status("xray"),
            "panel": service_status(PANEL_SERVICE_NAME),
            "nginx": service_status("nginx"),
        },
        "service_enabled": {
            "xray": service_enabled("xray"),
            "panel": service_enabled(PANEL_SERVICE_NAME),
            "nginx": service_enabled("nginx"),
        },
        "service_actions": {
            "xray": {
                "restart": service_restart_supported("xray"),
                "toggle": service_toggle_supported("xray"),
            },
            "panel": {
                "restart": service_restart_supported(PANEL_SERVICE_NAME),
                "toggle": service_toggle_supported(PANEL_SERVICE_NAME),
            },
            "nginx": {
                "restart": service_restart_supported("nginx"),
                "toggle": service_toggle_supported("nginx"),
            },
        },
    }
    if extra:
        context.update(extra)
    return context


@app.on_event("startup")
def startup_event() -> None:
    global poller_thread
    init_db()
    ensure_default_settings_from_xray()
    ensure_admin_user()
    bootstrap_links_from_xray()
    harden_data_permissions()
    if poller_thread is None or not poller_thread.is_alive():
        stop_event.clear()
        poller_thread = threading.Thread(target=poll_loop, daemon=True)
        poller_thread.start()


@app.on_event("shutdown")
def shutdown_event() -> None:
    stop_event.set()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def root(request: Request) -> RedirectResponse:
    if optional_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    if optional_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    settings = get_current_settings()
    csrf_token = get_login_csrf_token(request)
    response = templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "panel_title": get_setting("panel_title", DEFAULT_PANEL_TITLE),
            "announcement": get_setting("announcement", ""),
            "server_domain": settings["server_domain"],
            "csrf_token": csrf_token,
            "message": request.query_params.get("msg", ""),
            "level": request.query_params.get("level", "info"),
        },
    )
    set_login_csrf_cookie(response, csrf_token)
    return response


@app.post("/login")
def login_submit(
    request: Request,
    csrf_token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
) -> RedirectResponse:
    csrf_error = validate_login_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    normalized_username = username.strip()
    client_ip = get_client_ip(request)
    if get_login_lockout_remaining(client_ip) > 0:
        logger.warning("AUTH_RATE_LIMIT username=%s ip=%s", normalized_username or "-", client_ip)
        return redirect_with_message("/login", "登录尝试过于频繁，请稍后再试", "error")
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM admin_users WHERE username = ?", (normalized_username,)).fetchone()
        if not row or not verify_password(password, row["salt"], row["password_hash"]):
            register_login_failure(client_ip)
            logger.warning("AUTH_FAIL username=%s ip=%s", normalized_username or "-", client_ip)
            return redirect_with_message("/login", "用户名或密码不正确", "error")
        conn.execute("UPDATE admin_users SET last_login_at = ? WHERE id = ?", (now_iso(), row["id"]))
        conn.commit()
    clear_login_failures(client_ip)
    token = create_session(row["id"])
    response = RedirectResponse(url="/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, token)
    response.delete_cookie(LOGIN_CSRF_COOKIE_NAME, path="/login")
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    token = get_session_token(request)
    if token:
        with closing(db()) as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    for cookie_name in (SESSION_COOKIE_NAME, *LEGACY_SESSION_COOKIE_NAMES):
        response.delete_cookie(cookie_name, path="/")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user: sqlite3.Row = Depends(get_current_user)) -> HTMLResponse:
    owner = is_owner(user)
    owner_filter = None if owner else user['id']
    context = build_common_context(
        request,
        user,
        "dashboard",
        {
            "charts": build_chart_data() if owner else None,
            "recent_activity": get_recent_activity(1, 12, owner_user_id=owner_filter)["items"],
            "recent_access": get_recent_access_events(1, 12, owner_user_id=owner_filter)["items"],
        },
    )
    return templates.TemplateResponse("overview.html", context)


@app.get("/links", response_class=HTMLResponse)
def links_page(request: Request, user: sqlite3.Row = Depends(get_current_user)):
    return templates.TemplateResponse("links.html", build_common_context(request, user, "links"))



@app.get("/activity", response_class=HTMLResponse)
def activity_page(
    request: Request,
    traffic_page: int = 1,
    access_page: int = 1,
    account_id: int | None = None,
    user: sqlite3.Row = Depends(get_current_user),
):
    owner = is_owner(user)
    owner_filter = account_id if owner and account_id else (None if owner else user['id'])
    traffic = get_recent_activity(traffic_page, 20, owner_user_id=owner_filter)
    access = get_recent_access_events(access_page, 30, owner_user_id=owner_filter)
    return templates.TemplateResponse(
        "activity.html",
        build_common_context(
            request,
            user,
            "activity",
            {
                "traffic_page_data": traffic,
                "access_page_data": access,
                "recent_activity": traffic["items"],
                "recent_access": access["items"],
                "current_account_id": account_id or 0,
            },
        ),
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: sqlite3.Row = Depends(get_current_user)):
    return templates.TemplateResponse("settings.html", build_common_context(request, user, "settings"))


@app.get("/api/state")
def api_state(user: sqlite3.Row = Depends(get_current_user)) -> JSONResponse:
    settings = get_current_settings()
    owner_filter = None if is_owner(user) else user['id']
    links = build_links_view(settings, only_owner_user_id=owner_filter)
    return JSONResponse(
        {
            "settings": {key: value for key, value in settings.items() if key != "private_key"},
            "summary": build_summary(links),
            "links": links,
            "activity": get_recent_activity(1, 60, owner_user_id=owner_filter),
            "access": get_recent_access_events(1, 60, owner_user_id=owner_filter),
            "runtime": runtime_state,
        }
    )


@app.post("/settings/general")
def update_general_settings(
    request: Request,
    csrf_token: str = Form(...),
    panel_title: str = Form(...),
    announcement: str = Form(""),
    server_domain: str = Form(...),
    sni: str = Form(...),
    port: str = Form(...),
    short_id: str = Form(...),
    target: str = Form(...),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    try:
        port_int = validate_port(port)
        short_id_clean = validate_short_id(short_id)
    except Exception as exc:
        return redirect_with_message("/settings", str(exc), "error")
    set_setting("panel_title", panel_title.strip() or DEFAULT_PANEL_TITLE)
    set_setting("announcement", announcement.strip())
    set_setting("server_domain", server_domain.strip())
    set_setting("sni", sni.strip())
    set_setting("port", str(port_int))
    set_setting("short_id", short_id_clean)
    set_setting("target", target.strip())
    try:
        note = apply_xray_config(f"general_settings_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/settings", f"保存成功，但应用失败：{exc}", "error")
    message, level = apply_result_message("全局参数已更新", note)
    return redirect_with_message("/settings", message, level)


@app.post("/settings/rotate-shortid")
def rotate_shortid(request: Request, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    set_setting("short_id", secrets.token_hex(8))
    try:
        note = apply_xray_config(f"rotate_shortid_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/settings", f"Short ID 轮换失败：{exc}", "error")
    message, level = apply_result_message("Short ID 已轮换", note)
    return redirect_with_message("/settings", message, level)


@app.post("/settings/rotate-keys")
def rotate_reality_keys(request: Request, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    try:
        private_key, public_key = xray_x25519()
        set_setting("private_key", private_key)
        set_setting("public_key", public_key)
        note = apply_xray_config(f"rotate_keys_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/settings", f"Reality 密钥轮换失败：{exc}", "error")
    message, level = apply_result_message("Reality 密钥已轮换", note)
    return redirect_with_message("/settings", message, level)


@app.post("/settings/password")
def change_password(
    request: Request,
    csrf_token: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    if len(new_password) < 8:
        return redirect_with_message("/settings", "新密码至少 8 位", "error")
    if not verify_password(current_password, user["salt"], user["password_hash"]):
        return redirect_with_message("/settings", "当前密码不正确", "error")
    salt = secrets.token_hex(16)
    password_hash = hash_password(new_password, salt)
    with closing(db()) as conn:
        conn.execute("UPDATE admin_users SET password_hash = ?, salt = ? WHERE id = ?", (password_hash, salt, user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.commit()
    token = create_session(user["id"])
    if is_owner(user):
        sync_owner_credentials_file(user['username'], new_password)
    add_audit("change_password", f"user={user['username']}")
    response = redirect_with_message("/settings", "登录密码已更新，其他会话已全部退出")
    set_session_cookie(response, token)
    logger.info("AUTH_SESSIONS_RESET username=%s ip=%s", user["username"], get_client_ip(request))
    return response


@app.post("/settings/username")
def change_username(
    request: Request,
    csrf_token: str = Form(...),
    new_username: str = Form(...),
    current_password: str = Form(...),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    new_username = new_username.strip()
    if len(new_username) < 3:
        return redirect_with_message("/settings", "用户名至少 3 位", "error")
    if not verify_password(current_password, user["salt"], user["password_hash"]):
        return redirect_with_message("/settings", "当前密码不正确", "error")
    with closing(db()) as conn:
        try:
            conn.execute("UPDATE admin_users SET username = ? WHERE id = ?", (new_username, user["id"]))
            conn.commit()
        except sqlite3.IntegrityError:
            return redirect_with_message("/settings", "用户名已存在", "error")
    if is_owner(user):
        sync_owner_credentials_file(new_username, current_password)
    add_audit("change_username", f"user_id={user['id']} -> {new_username}")
    return redirect_with_message("/settings", "用户名已更新")


@app.post("/settings/users/create")
def create_member_user(
    request: Request,
    csrf_token: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    link_name: str = Form(""),
    email: str = Form(""),
    quota_gb: str = Form(...),
    reset_day: str = Form("1"),
    expires_at: str = Form(""),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    username = username.strip()
    if len(username) < 3 or len(password) < 8:
        return redirect_with_message("/settings", "用户名至少 3 位，密码至少 8 位", "error")
    try:
        quota_bytes = parse_quota_gb(quota_gb)
        if quota_bytes <= 0:
            return redirect_with_message("/settings", "新增用户必须设置流量额度", "error")
        reset_day_value = max(1, min(28, int(reset_day)))
    except Exception:
        return redirect_with_message("/settings", "用户流量参数格式不正确", "error")
    salt = secrets.token_hex(16)
    password_hash = hash_password(password, salt)
    with closing(db()) as conn:
        try:
            conn.execute(
                "INSERT INTO admin_users(username, password_hash, salt, role, quota_bytes, reset_day, last_reset_at, status_reason, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, password_hash, salt, 'member', quota_bytes, reset_day_value, now_iso(), 'active', now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return redirect_with_message("/settings", "用户名已存在", "error")
    add_audit("create_member_user", f"{username} by {user['username']}")
    return redirect_with_message("/settings", "朋友账号已创建")


@app.post("/settings/users/{user_id}/delete")
def delete_admin_user(request: Request, user_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    if int(user_id) == int(user['id']):
        return redirect_with_message("/settings", "不能删除当前登录用户", "error")
    with closing(db()) as conn:
        target = conn.execute("SELECT id, role, link_id FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS count FROM admin_users WHERE role = 'owner'").fetchone()["count"]
        if target and target['role'] == 'owner' and count <= 1:
            return redirect_with_message("/settings", "至少保留一个管理员", "error")
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM admin_users WHERE id = ?", (user_id,))
        if target and target['link_id']:
            conn.execute("DELETE FROM traffic_samples WHERE link_id = ?", (target['link_id'],))
            conn.execute("DELETE FROM access_events WHERE link_id = ?", (target['link_id'],))
            conn.execute("DELETE FROM links WHERE id = ?", (target['link_id'],))
        conn.commit()
    try:
        note = apply_xray_config(f"delete_admin_user_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/settings", f"用户已删除，但应用失败：{exc}", "error")
    add_audit("delete_admin_user", f"user_id={user_id} by {user['username']}")
    message, level = apply_result_message("用户已删除", note)
    return redirect_with_message("/settings", message, level)


@app.post("/settings/storage")
def update_storage_settings(
    request: Request,
    csrf_token: str = Form(...),
    max_traffic_rows: str = Form(...),
    max_access_rows: str = Form(...),
    max_access_log_mb: str = Form(...),
    max_error_log_mb: str = Form(...),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    try:
        traffic_rows = max(100, int(max_traffic_rows))
        access_rows = max(100, int(max_access_rows))
        access_log_mb = max(1, int(max_access_log_mb))
        error_log_mb = max(1, int(max_error_log_mb))
    except Exception:
        return redirect_with_message("/settings", "存储参数格式不正确", "error")
    set_setting('max_traffic_rows', str(traffic_rows))
    set_setting('max_access_rows', str(access_rows))
    set_setting('max_access_log_mb', str(access_log_mb))
    set_setting('max_error_log_mb', str(error_log_mb))
    trim_table_rows('traffic_samples', traffic_rows)
    trim_table_rows('access_events', access_rows)
    trim_log_file(XRAY_ACCESS_LOG, access_log_mb)
    trim_log_file(XRAY_ERROR_LOG, error_log_mb)
    add_audit('update_storage', f"by {user['username']}")
    return redirect_with_message("/settings", "存储容量设置已更新")


@app.post("/settings/service/{service_name}/{action}")
def service_action(
    request: Request,
    service_name: str,
    action: str,
    csrf_token: str = Form(...),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    denied = ensure_owner_or_redirect(user)
    if denied:
        return denied
    try:
        manage_service_action(service_name, action)
    except Exception as exc:
        return redirect_with_message("/settings", f"服务操作失败：{exc}", "error")
    add_audit('service_action', f"{service_name}:{action} by {user['username']}")
    return redirect_with_message("/settings", f"服务操作已执行：{service_name} {action}")



@app.post("/links/create")
def create_link(
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    quota_gb: str = Form(""),
    reset_day: str = Form("1"),
    expires_at: str = Form(""),
    auto_disable_on_expire: str | None = Form(None),
    auto_reenable_on_reset: str | None = Form(None),
    notes: str = Form(""),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    try:
        quota_bytes = parse_quota_gb(quota_gb) if quota_gb.strip() else 0
        reset_day_value = max(1, min(28, int(reset_day or 1)))
    except Exception:
        return redirect_with_message("/links", "链接参数格式不正确", "error")
    owner_id = int(user['id'])
    expires_value = expires_at.strip() or None
    auto_disable_value = 1 if auto_disable_on_expire is not None else 0
    auto_reenable_value = 1 if auto_reenable_on_reset is not None else 0
    if not is_owner(user):
        if quota_bytes <= 0:
            return redirect_with_message("/links", "朋友账号新增链接必须设置单链接流量额度", "error")
        with closing(db()) as conn:
            member = conn.execute("SELECT quota_bytes FROM admin_users WHERE id = ?", (owner_id,)).fetchone()
        total_quota = int(member['quota_bytes'] or 0) if member else 0
        allocated = get_allocated_quota_bytes(owner_id)
        if allocated + quota_bytes > total_quota:
            remaining = max(total_quota - allocated, 0)
            return redirect_with_message("/links", f"可分配额度不足，当前最多还能分配 {human_bytes(remaining)}", "error")
        reset_day_value = 1
        expires_value = None
        auto_disable_value = 0
        auto_reenable_value = 0
    with closing(db()) as conn:
        try:
            conn.execute(
                """
                INSERT INTO links(
                    owner_user_id, name, email, uuid, share_token, enabled, quota_bytes, baseline_total_bytes, baseline_up_bytes, baseline_down_bytes,
                    accumulated_up_bytes, accumulated_down_bytes, last_api_up_bytes, last_api_down_bytes,
                    last_online_count, last_online_ips, last_seen_at, notes, expires_at,
                    last_reset_at, reset_day, auto_disable_on_expire, auto_reenable_on_reset, status_reason,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, ?, 0, 0, 0, 0, 0, 0, 0, 0, '[]', NULL, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    owner_id, name.strip(), email.strip(), str(uuid.uuid4()), random_share_token(), quota_bytes, notes.strip(),
                    expires_value, now_iso(), reset_day_value, auto_disable_value, auto_reenable_value, now_iso(), now_iso(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return redirect_with_message("/links", "邮箱已存在，请换一个", "error")
    try:
        note = apply_xray_config(f"create_link_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"链接已创建，但应用失败：{exc}", "error")
    message, level = apply_result_message("链接已创建", note)
    return redirect_with_message("/links", message, level)


@app.post("/links/{link_id}/update")
def update_link(
    request: Request,
    link_id: int,
    csrf_token: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    quota_gb: str = Form(""),
    reset_day: str = Form("1"),
    expires_at: str = Form(""),
    auto_disable_on_expire: str | None = Form(None),
    auto_reenable_on_reset: str | None = Form(None),
    notes: str = Form(""),
    user: sqlite3.Row = Depends(get_current_user),
) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    try:
        quota_bytes = parse_quota_gb(quota_gb) if quota_gb.strip() else 0
        reset_day_value = max(1, min(28, int(reset_day or 1)))
    except Exception:
        return redirect_with_message("/links", "链接参数格式不正确", "error")
    expires_value = expires_at.strip() or None
    auto_disable_value = 1 if auto_disable_on_expire is not None else 0
    auto_reenable_value = 1 if auto_reenable_on_reset is not None else 0
    if not is_owner(user):
        with closing(db()) as conn:
            member = conn.execute("SELECT quota_bytes FROM admin_users WHERE id = ?", (int(user['id']),)).fetchone()
        total_quota = int(member['quota_bytes'] or 0) if member else 0
        allocated = get_allocated_quota_bytes(int(user['id']), exclude_link_id=link_id)
        if quota_bytes <= 0:
            return redirect_with_message("/links", "单链接流量额度必须大于 0", "error")
        if allocated + quota_bytes > total_quota:
            remaining = max(total_quota - allocated, 0)
            return redirect_with_message("/links", f"可分配额度不足，当前最多还能分配 {human_bytes(remaining)}", "error")
        reset_day_value = 1
        expires_value = None
        auto_disable_value = 0
        auto_reenable_value = 0
    with closing(db()) as conn:
        try:
            conn.execute(
                """
                UPDATE links
                SET name = ?, email = ?, quota_bytes = ?, notes = ?, expires_at = ?,
                    reset_day = ?, auto_disable_on_expire = ?, auto_reenable_on_reset = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name.strip(), email.strip(), quota_bytes, notes.strip(), expires_value or None,
                    reset_day_value, auto_disable_value, auto_reenable_value, now_iso(), link_id,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return redirect_with_message("/links", "邮箱重复，更新失败", "error")
    try:
        note = apply_xray_config(f"update_link_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"链接已更新，但应用失败：{exc}", "error")
    message, level = apply_result_message("链接已更新", note)
    return redirect_with_message("/links", message, level)


@app.post("/links/{link_id}/toggle")
def toggle_link(request: Request, link_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    if not link:
        return redirect_with_message("/links", "链接不存在", "error")
    new_enabled = 0 if link["enabled"] else 1
    new_reason = "manual_disabled" if link["enabled"] else "active"
    with closing(db()) as conn:
        conn.execute("UPDATE links SET enabled = ?, status_reason = ?, updated_at = ? WHERE id = ?", (new_enabled, new_reason, now_iso(), link_id))
        conn.commit()
    try:
        note = apply_xray_config(f"toggle_link_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"状态已改，但应用失败：{exc}", "error")
    message, level = apply_result_message("链接状态已切换", note)
    return redirect_with_message("/links", message, level)


@app.post("/links/{link_id}/rotate")
def rotate_link_uuid(request: Request, link_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    with closing(db()) as conn:
        conn.execute("UPDATE links SET uuid = ?, updated_at = ? WHERE id = ?", (str(uuid.uuid4()), now_iso(), link_id))
        conn.commit()
    try:
        note = apply_xray_config(f"rotate_link_uuid_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"UUID 已轮换，但应用失败：{exc}", "error")
    message, level = apply_result_message("UUID 已轮换", note)
    return redirect_with_message("/links", message, level)


@app.post("/links/{link_id}/reset-usage")
def reset_link_usage(request: Request, link_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    if not link:
        return redirect_with_message("/links", "链接不存在", "error")
    current_total = int(link["accumulated_up_bytes"] or 0) + int(link["accumulated_down_bytes"] or 0)
    with closing(db()) as conn:
        conn.execute(
            "UPDATE links SET baseline_total_bytes = ?, baseline_up_bytes = ?, baseline_down_bytes = ?, last_reset_at = ?, updated_at = ?, enabled = 1, status_reason = 'active' WHERE id = ?",
            (current_total, int(link["accumulated_up_bytes"] or 0), int(link["accumulated_down_bytes"] or 0), now_iso(), now_iso(), link_id),
        )
        conn.commit()
    try:
        note = apply_xray_config(f"reset_usage_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"流量已重置，但应用失败：{exc}", "error")
    message, level = apply_result_message("流量基线已重置", note)
    return redirect_with_message("/links", message, level)


@app.post("/links/{link_id}/share-token")
def rotate_share_token(request: Request, link_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    with closing(db()) as conn:
        conn.execute("UPDATE links SET share_token = ?, updated_at = ? WHERE id = ?", (random_share_token(), now_iso(), link_id))
        conn.commit()
    add_audit("rotate_share_token", f"link_id={link_id} by {user['username']}")
    return redirect_with_message("/links", "分享 Token 已重置")


@app.post("/links/{link_id}/delete")
def delete_link(request: Request, link_id: int, csrf_token: str = Form(...), user: sqlite3.Row = Depends(get_current_user)) -> RedirectResponse:
    csrf_error = validate_session_csrf(request, csrf_token)
    if csrf_error:
        return csrf_error
    link = get_link(link_id)
    denied = ensure_link_access(user, link)
    if denied:
        return denied
    with closing(db()) as conn:
        conn.execute("DELETE FROM traffic_samples WHERE link_id = ?", (link_id,))
        conn.execute("DELETE FROM access_events WHERE link_id = ?", (link_id,))
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
        conn.commit()
    try:
        note = apply_xray_config(f"delete_link_by:{user['username']}")
    except Exception as exc:
        return redirect_with_message("/links", f"链接已删除，但应用失败：{exc}", "error")
    message, level = apply_result_message("链接已删除", note)
    return redirect_with_message("/links", message, level)


@app.get("/subscribe/{token}")
@app.get("/subscribe/{token}/loon.txt")
def subscribe_loon(token: str) -> PlainTextResponse:
    settings = get_current_settings()
    with closing(db()) as conn:
        link = conn.execute("SELECT * FROM links WHERE share_token = ? AND enabled = 1", (token,)).fetchone()
    if not link:
        raise HTTPException(status_code=404)
    line = (
        f"{link['name']} = VLESS,{settings['server_domain']},{settings['port']},\"{link['uuid']}\","
        f"transport=tcp,over-tls=true,tls-name={settings['sni']},flow={settings['flow']},"
        f"reality=true,reality-public-key=\"{settings['public_key']}\","
        f"reality-short-id={settings['short_id']},udp=true,tfo=true"
    )
    headers = build_subscription_headers(link)
    return PlainTextResponse(line + "\n", headers=headers)


@app.get("/subscribe/{token}/vless")
@app.get("/subscribe/{token}/vless.txt")
def subscribe_vless(token: str) -> PlainTextResponse:
    settings = get_current_settings()
    with closing(db()) as conn:
        link = conn.execute("SELECT * FROM links WHERE share_token = ? AND enabled = 1", (token,)).fetchone()
    if not link:
        raise HTTPException(status_code=404)
    headers = build_subscription_headers(link)
    return PlainTextResponse(build_link_urls(link, settings)["vless_uri"] + "\n", headers=headers)


templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["bjt"] = format_display_dt
templates.env.filters["bjt_date"] = lambda value: format_display_dt(value, True)
templates.env.globals["display_tz_label"] = DISPLAY_TZ_LABEL
