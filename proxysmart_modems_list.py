#!/usr/bin/env python3
import os
import time
import logging
from logging.handlers import RotatingFileHandler
import requests

# ──────────── НАСТРОЙКИ ────────────
AUTH = ('proxy', 'proxy')
TELEGRAM_BOT_TOKEN = 'TOKEN'
TELEGRAM_CHAT_ID = 'CHAT_ID'
API_URL = 'http://localhost:8080/apix/show_status_json'
CHECK_INTERVAL = 60  # сек
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

# ──────────── ЛОГИКА ────────────
last_count: int | None = None


def fetch_modem_list():
    try:
        resp = requests.get(API_URL, auth=AUTH, timeout=5)
        if resp.status_code == 200:
            return resp.json()
        logger.error("[API error] Status code: %s", resp.status_code)
    except Exception as exc:
        logger.exception("[API request failed] %s", exc)
    return None


def send_telegram_message(msg: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': msg,
        'parse_mode': 'Markdown'
    }
    try:
        resp = requests.post(url, data=payload, timeout=5)
        if not resp.ok:
            logger.error("Failed to send Telegram message: %s", resp.text)
    except Exception as exc:
        logger.exception("Telegram send error: %s", exc)


def check_and_notify() -> None:
    global last_count

    def grab_count() -> int | None:
        modems = fetch_modem_list()
        return None if modems is None else len(modems)

    current = grab_count()
    if current is None:
        return

    # Первая инициализация
    if last_count is None:
        last_count = current
        logger.info("[Init] Модемов: %s", current)
        return

    # Ничего не изменилось
    if current == last_count:
        logger.debug("[No change] Модемов: %s", current)
        return

    # Обнаружено расхождение — ждём 5 минут
    logger.warning(
        "[Drift] Было %s, стало %s. Ждём подтверждения 5 мин…",
        last_count, current
    )
    time.sleep(5 * 60)

    after_wait = grab_count()
    if after_wait is None:
        return

    if after_wait != last_count:
        hostname = os.environ.get("HOSTNAME", "localhost")
        delta = after_wait - last_count
        action = "добавлены" if delta > 0 else "удалены"

        message = (
            f"{hostname}:\n"
            "Изменение количества модемов (подтверждено):\n"
            f"Было: {last_count} → Стало: {after_wait}\n"
            f"Изменение: {abs(delta)} модем(ов) {action}"
        )
        send_telegram_message(message)
        logger.info(
            "Отправлено уведомление: было %s, стало %s (%+d)",
            last_count, after_wait, delta
        )
        last_count = after_wait
    else:
        logger.info(
            "[Transient] Число вернулось к %s, уведомление не требуется.",
            last_count
        )


if __name__ == '__main__':
    logger.info("=== proxysmart_modems_list started ===")
    while True:
        check_and_notify()
        time.sleep(CHECK_INTERVAL)
