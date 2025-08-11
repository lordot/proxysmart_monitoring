#!/usr/bin/env python3
import os
import json
import time
import platform
import socket
import subprocess
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

# ──────────── НАСТРОЙКИ ────────────
AUTH_USER = os.getenv("MG_USER")
AUTH_PASS = os.getenv("MG_PASSWORD", "")
TELEGRAM_BOT_TOKEN = os.getenv("MG_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("MG_TG_CHAT", "-4850356170")
API_URL = os.getenv("MG_API_URL", "http://localhost:8080/apix/show_status_json")

STATE_DIR = Path(os.getenv("MG_STATE_DIR", "/var/lib/proxysmart"))
STATE_FILE = STATE_DIR / "modems_state.json"
DRIFT_CONFIRM_SECONDS = 300  # 5 минут

LOG_FILE = '/var/log/proxysmart_modems_list.log'
LOG_SIZE = 50 * 1024 * 1024  # 50 МБ
LOG_BACKUPS = 7

# ──────────── ЛОГИ ────────────
logger = logging.getLogger('proxysmart')
logger.setLevel(logging.INFO)

handler = RotatingFileHandler(
    LOG_FILE, maxBytes=LOG_SIZE, backupCount=LOG_BACKUPS, encoding='utf-8'
)
handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(handler)


# ──────────── УТИЛИТЫ ────────────
def build_auth() -> Optional[HTTPBasicAuth]:
    """Вернёт HTTPBasicAuth, если задан MG_USER, иначе None."""
    if AUTH_USER:
        return HTTPBasicAuth(AUTH_USER, AUTH_PASS)
    return None


def get_hostname() -> str:
    try:
        hn = socket.gethostname()
        if hn:
            return hn
    except Exception:
        pass
    try:
        hn = platform.node()
        if hn:
            return hn
    except Exception:
        pass
    hn = os.environ.get("HOSTNAME")
    if hn:
        return hn
    try:
        hn = Path("/etc/hostname").read_text().strip()
        if hn:
            return hn
    except FileNotFoundError:
        pass
    try:
        hn = subprocess.check_output(["hostname"], text=True).strip()
        if hn:
            return hn
    except Exception:
        pass
    return "localhost"


def fetch_modem_count() -> Optional[int]:
    try:
        resp = requests.get(API_URL, auth=build_auth(), timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return len(data)
        logger.error("[API error] Status code: %s", resp.status_code)
    except Exception as exc:
        logger.exception("[API request failed] %s", exc)
    return None


def send_telegram_message(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured: missing token/chat id")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'}
    try:
        resp = requests.post(url, data=payload, timeout=5)
        if not resp.ok:
            logger.error("Failed to send Telegram message: %s", resp.text)
    except Exception as exc:
        logger.exception("Telegram send error: %s", exc)


# ──────────── СОСТОЯНИЕ ────────────
# Структура файла состояния:
# {
#   "last_count": 12,               # подтверждённое число модемов
#   "pending": {                    # необязательный блок "ожидания подтверждения"
#       "value": 10,                # наблюдаемое новое значение
#       "first_seen": 1710000000    # когда впервые заметили (epoch)
#   }
# }
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("Failed to read state: %s", e)
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        logger.exception("Failed to write state: %s", e)


# ──────────── ЛОГИКА ОДНОГО ЗАПУСКА ────────────
def run_once() -> None:
    now = int(time.time())
    current = fetch_modem_count()
    if current is None:
        logger.error("Не удалось получить список модемов — выхожу.")
        return

    state = load_state()
    last_count = state.get("last_count")
    pending = state.get("pending")

    # Инициализация
    if last_count is None:
        state["last_count"] = current
        state.pop("pending", None)
        save_state(state)
        logger.info("[Init] Модемов: %s (сохранено в состояние)", current)
        return

    # Если уже ждём подтверждение дрейфа
    if pending:
        pending_value = int(pending.get("value", current))
        first_seen = int(pending.get("first_seen", now))

        if current == pending_value:
            # Изменение держится; проверим время ожидания
            if now - first_seen >= DRIFT_CONFIRM_SECONDS:
                delta = pending_value - last_count
                action = "добавлены" if delta > 0 else "удалены"
                hostname = get_hostname()
                message = (
                    f"{hostname}:\n"
                    "Изменение количества модемов (подтверждено):\n"
                    f"Было: {last_count} → Стало: {pending_value}\n"
                    f"Изменение: {abs(delta)} модем(ов) {action}"
                )
                send_telegram_message(message)
                logger.info(
                    "Подтверждён дрейф: было %s, стало %s (%+d) — отправлено уведомление",
                    last_count, pending_value, delta,
                )
                # зафиксировать новое подтверждённое значение
                state["last_count"] = pending_value
                state.pop("pending", None)
                save_state(state)
            else:
                # ждём дальше
                wait_left = DRIFT_CONFIRM_SECONDS - (now - first_seen)
                logger.info(
                    "[Drift waiting] Было %s, наблюдаем %s, ждём подтверждения ещё ~%ss",
                    last_count, pending_value, max(0, wait_left),
                )
                # состояние без изменений
        else:
            # значение снова поменялось — начинаем ожидание заново
            logger.warning(
                "[Drift changed] Было %s, раньше видели %s, сейчас %s — перезапускаю ожидание",
                last_count, pending_value, current,
            )
            state["pending"] = {"value": current, "first_seen": now}
            save_state(state)
        return

    # pending нет. Проверим, изменилось ли относительно подтверждённого last_count
    if current != int(last_count):
        logger.warning(
            "[Drift detected] Было %s, стало %s — начинаю ожидание подтверждения 5 мин",
            last_count, current,
        )
        state["pending"] = {"value": current, "first_seen": now}
        save_state(state)
    else:
        logger.debug("[No change] Модемов: %s", current)


# ──────────── Точка входа ────────────
if __name__ == "__main__":
    run_once()
