#!/usr/bin/env python3
import os
import json
import time
import socket
import subprocess
import platform
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional, Dict, Any, List

import requests
from requests.auth import HTTPBasicAuth
import yaml

# ──────────── Глобальные настройки ────────────
CONFIG_PATH = os.getenv("MG_CONFIG", "/config/servers.yaml")
STATE_BASE = Path(os.getenv("MG_STATE_DIR", "/state"))
LOG_DIR = os.getenv("MG_LOG_DIR", "/logs")
DRIFT_CONFIRM_SECONDS = int(os.getenv("MG_DRIFT_CONFIRM_SECONDS", "300"))

# ENV-переменные оставляем как fallback, но основным источником стал конфиг
ENV_FALLBACK_TG_TOKEN = os.getenv("MG_TG_TOKEN", "")
ENV_FALLBACK_TG_CHAT = os.getenv("MG_TG_CHAT", "")

# ──────────── Логгер ────────────
root_logger = logging.getLogger("proxysmart")
root_logger.setLevel(logging.INFO)
stdout_handler = logging.StreamHandler()
stdout_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
root_logger.addHandler(stdout_handler)


def ensure_file_logger(server_id: str) -> Optional[logging.Handler]:
    try:
        if not LOG_DIR:
            return None
        log_dir = Path(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"proxysmart_{server_id}.log"
        handler = RotatingFileHandler(
            str(log_path), maxBytes=50 * 1024 * 1024, backupCount=7, encoding='utf-8'
        )
        handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
        ))
        return handler
    except Exception:
        return None


# ──────────── Утилиты ────────────
def get_hostname() -> str:
    for f in (
            lambda: socket.gethostname(),
            lambda: platform.node(),
            lambda: os.environ.get("HOSTNAME"),
            lambda: Path("/etc/hostname").read_text().strip(),
            lambda: subprocess.check_output(["hostname"], text=True).strip(),
    ):
        try:
            v = f()
            if v:
                return v
        except Exception:
            pass
    return "localhost"


def send_telegram_message(text: str, bot_token: Optional[str], chat_id: Optional[str]) -> None:
    logger = root_logger
    token = (bot_token or "").strip() or ENV_FALLBACK_TG_TOKEN
    if not token:
        logger.warning("Telegram not configured: missing bot token (config/env)")
        return
    cid = (chat_id or "").strip() or ENV_FALLBACK_TG_CHAT
    if not cid:
        logger.warning("Telegram chat id is not set (config/env)")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {'chat_id': cid, 'text': text, 'parse_mode': 'Markdown'}
    try:
        resp = requests.post(url, data=payload, timeout=8)
        if not resp.ok:
            logger.error("Telegram send failed: %s", resp.text)
    except Exception as exc:
        logger.exception("Telegram send error: %s", exc)


def http_auth(user: Optional[str], password: Optional[str]) -> Optional[HTTPBasicAuth]:
    if user:
        return HTTPBasicAuth(user, password or "")
    return None


def fetch_modem_count(api_url: str, auth: Optional[HTTPBasicAuth], timeout: int, verify_ssl: bool,
                      logger: logging.Logger) -> Optional[int]:
    try:
        resp = requests.get(api_url, auth=auth, timeout=timeout, verify=verify_ssl)
        if resp.status_code == 200:
            data = resp.json()
            return len(data)
        logger.error("[API error] Status code: %s body=%s", resp.status_code, resp.text[:200])
    except Exception as exc:
        logger.exception("[API request failed] %s", exc)
    return None


# ──────────── State helpers ────────────
def state_paths_for(server_id: str) -> Path:
    return STATE_BASE / server_id


def load_state(server_id: str, logger: logging.Logger) -> Dict[str, Any]:
    try:
        base = state_paths_for(server_id)
        f = base / "modems_state.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("Failed to read state: %s", e)
    return {}


def save_state(server_id: str, state: Dict[str, Any], logger: logging.Logger) -> None:
    try:
        base = state_paths_for(server_id)
        base.mkdir(parents=True, exist_ok=True)
        f = base / "modems_state.json"
        tmp = base / "modems_state.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except Exception as e:
        logger.exception("Failed to write state: %s", e)


# ──────────── URL builder (host+port → api_url) ────────────
def _normalize_path(p: str) -> str:
    if not p:
        return "/"
    return p if p.startswith("/") else f"/{p}"


def build_api_url(srv: dict, defaults: dict) -> str:
    scheme = (srv.get("scheme") or defaults.get("scheme") or "http").lower()
    path = _normalize_path(srv.get("path") or defaults.get("path") or "/apix/show_status_json")
    if "api_url" in srv:
        return srv["api_url"]  # legacy support
    host = srv["host"]
    port = int(srv["port"])
    host_fmt = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default_port = 80 if scheme == "http" else 443
    if port == default_port:
        return f"{scheme}://{host_fmt}{path}"
    return f"{scheme}://{host_fmt}:{port}{path}"


# ──────────── Основная логика одного сервера ────────────
def run_once_for_server(srv: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    server_id = srv["id"]
    logger = logging.getLogger(f"proxysmart.{server_id}")
    file_h = ensure_file_logger(server_id)
    if file_h:
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
        logger.addHandler(file_h)

    name = srv.get("name", server_id)
    api_url = build_api_url(srv, defaults)
    auth = http_auth(srv.get("auth_user"), srv.get("auth_pass"))

    timeout = int(srv.get("timeout_seconds", defaults.get("timeout_seconds", 5)))
    verify_ssl = bool(srv.get("verify_ssl", defaults.get("verify_ssl", True)))

    # Telegram из конфига (с возможным override на сервере) + ENV как fallback
    tg_bot_token = srv.get("telegram_bot_token") or defaults.get("telegram_bot_token") or ENV_FALLBACK_TG_TOKEN
    tg_chat = srv.get("telegram_chat_id") or defaults.get("telegram_chat_id") or ENV_FALLBACK_TG_CHAT

    now = int(time.time())
    current = fetch_modem_count(api_url, auth, timeout, verify_ssl, logger)
    if current is None:
        logger.error("[%s] Не удалось получить список модемов — пропускаю.", server_id)
        return

    state = load_state(server_id, logger)
    last_count = state.get("last_count")
    pending = state.get("pending")

    # Инициализация
    if last_count is None:
        state["last_count"] = current
        state.pop("pending", None)
        save_state(server_id, state, logger)
        logger.info("[%s] [Init] Модемов: %s (сохранено в состояние)", server_id, current)
        return

    # Если уже ждём подтверждение дрейфа
    if pending:
        pending_value = int(pending.get("value", current))
        first_seen = int(pending.get("first_seen", now))

        if current == pending_value:
            if now - first_seen >= DRIFT_CONFIRM_SECONDS:
                delta = pending_value - last_count
                action = "добавлены" if delta > 0 else "удалены"
                host = get_hostname()
                message = (
                    f"*{name}* ({server_id}) на {host}:\n"
                    "Изменение количества модемов (подтверждено):\n"
                    f"Было: {last_count} → Стало: {pending_value}\n"
                    f"Изменение: {abs(delta)} модем(ов) {action}"
                )
                send_telegram_message(message, tg_bot_token, tg_chat)
                logger.info("[%s] Drift confirmed: %s -> %s (%+d), уведомление отправлено",
                            server_id, last_count, pending_value, delta)
                state["last_count"] = pending_value
                state.pop("pending", None)
                save_state(server_id, state, logger)
            else:
                wait_left = DRIFT_CONFIRM_SECONDS - (now - first_seen)
                logger.info("[%s] [Drift waiting] Было %s, наблюдаем %s, ждём ещё ~%ss",
                            server_id, last_count, pending_value, max(0, wait_left))
        else:
            logger.warning("[%s] [Drift changed] Было %s, раньше видели %s, сейчас %s — перезапускаю ожидание",
                           server_id, last_count, pending_value, current)
            state["pending"] = {"value": current, "first_seen": now}
            save_state(server_id, state, logger)
        return

    # pending нет — старт ожидания, если значение изменилось
    if current != int(last_count):
        logger.warning("[%s] [Drift detected] Было %s, стало %s — жду подтверждения %s сек",
                       server_id, last_count, current, DRIFT_CONFIRM_SECONDS)
        state["pending"] = {"value": current, "first_seen": now}
        save_state(server_id, state, logger)
    else:
        logger.debug("[%s] [No change] Модемов: %s", server_id, current)


# ──────────── Загрузка конфига ────────────
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get("servers", [])
    if not isinstance(servers, list) or not servers:
        raise ValueError("В конфиге нет списка 'servers'")
    for i, s in enumerate(servers):
        if not (("host" in s and "port" in s) or ("api_url" in s)):
            raise ValueError(f"Сервер #{i}: укажи 'host' и 'port' (или старый 'api_url')")
    data.setdefault("defaults", {})
    return data


def main():
    cfg = load_config(CONFIG_PATH)
    defaults = cfg.get("defaults", {})
    servers: List[Dict[str, Any]] = cfg["servers"]
    root_logger.info("Старт проверки. Серверов: %d", len(servers))
    for srv in servers:
        try:
            run_once_for_server(srv, defaults)
        except Exception as e:
            root_logger.exception("[%s] Ошибка верхнего уровня: %s", srv.get("id", "?"), e)


if __name__ == "__main__":
    main()
