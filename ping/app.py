#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import BasicAuth, ClientTimeout
import yaml

# ---------- Константы API путей ----------
SHOW_STATUS_PATH = "/apix/show_status_json"
RESET_PATH = "/apix/reset_modem_by_imei"
REBOOT_PATH = "/apix/reboot_modem_by_imei"
USB_RESET_PATH = "/apix/usb_reset_modem_json"

# ---------- Глобальные настройки ----------
CONFIG_PATH = os.getenv("MG_CONFIG", "/config/servers.yaml")
LOG_DIR = os.getenv("MG_LOG_DIR", "/logs")
DOUBLECHECK_SECONDS = int(os.getenv("MG_DOUBLECHECK_SECONDS", "120"))

# ENV-фоллбэки для Telegram
ENV_FALLBACK_TG_TOKEN = os.getenv("MG_TG_TOKEN", "")
ENV_FALLBACK_TG_CHAT = os.getenv("MG_TG_CHAT", "")

# ---------- Логирование ----------
root_logger = logging.getLogger("proxysmart")
root_logger.setLevel(logging.INFO)
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
root_logger.addHandler(_sh)


def ensure_file_logger(server_id: str) -> Optional[logging.Handler]:
    try:
        if not LOG_DIR:
            return None
        log_dir = Path(LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"proxysmart_{server_id}.log"
        handler = RotatingFileHandler(str(log_path), maxBytes=50 * 1024 * 1024, backupCount=7, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        return handler
    except Exception:
        return None


# ---------- Утилиты ----------
def _normalize_path(p: Optional[str]) -> str:
    if not p:
        return "/"
    return p if p.startswith("/") else f"/{p}"


def modem_key(modem: dict) -> Tuple[str, str]:
    imei = modem.get("modem_details", {}).get("IMEI", "") or ""
    dev = modem.get("net_details", {}).get("DEV", "") or ""
    return imei, dev


# ---- Обновленная логика проверки статуса ----
def is_offline(modem: dict) -> bool:
    """
    Считаем, что модем требует восстановления (офлайн), если:
    1. IS_ONLINE != 'yes'
    2. При этом он НЕ занят (LOCKED, REBOOTING, ROTATED все false).
    """
    net_details = modem.get("net_details", {})

    # Проверяем основной статус сети
    is_online_val = net_details.get("IS_ONLINE", "no")

    # Считаем онлайн, если явно 'yes', 'true', '1' и т.д.
    online_status = False
    if isinstance(is_online_val, bool):
        online_status = is_online_val
    elif isinstance(is_online_val, (int, float)):
        online_status = bool(is_online_val)
    else:
        s = str(is_online_val).strip().lower()
        online_status = s in {"yes", "true", "1", "ok", "online"}

    # Если модем онлайн — всё отлично
    if online_status:
        return False

    # Если модем офлайн, проверяем системные флаги (приходят как строки "true"/"false")
    # Если модем заблокирован, перезагружается или ротируется — мы его не трогаем (не считаем "мертвым")
    is_locked = str(modem.get("IS_LOCKED", "false")).lower() == "true"
    is_rebooting = str(modem.get("IS_REBOOTING", "false")).lower() == "true"
    is_rotated = str(modem.get("IS_ROTATED", "false")).lower() == "true"

    if is_locked or is_rebooting or is_rotated:
        return False

    # Если он офлайн и при этом ничего из вышеперечисленного не делает — значит он завис
    return True


def get_battery_percent(modem: dict) -> Optional[int]:
    val = modem.get("android", {}).get("battery")
    if val is None:
        return None
    if isinstance(val, (int, float)):
        i = int(val)
        return i if 0 <= i <= 100 else None
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        if s.endswith("%"):
            s = s[:-1].strip()
        digits = "".join(ch for ch in s if ch.isdigit())
        if not digits:
            return None
        i = int(digits)
        return i if 0 <= i <= 100 else None
    return None


# ---------- Загрузка конфига ----------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get("servers", [])
    if not isinstance(servers, list) or not servers:
        raise ValueError("В конфиге нет списка 'servers'")
    for i, s in enumerate(servers):
        if not (("host" in s and "port" in s) or ("api_url" in s)):
            raise ValueError(f"Сервер #{i}: укажи 'host' и 'port' (или 'api_url')")
    data.setdefault("defaults", {})
    return data


def build_endpoints(srv: dict, defaults: dict) -> Dict[str, Any]:
    scheme = (srv.get("scheme") or defaults.get("scheme") or "http").lower()
    verify_ssl = bool(srv.get("verify_ssl", defaults.get("verify_ssl", True)))
    timeout_seconds = int(srv.get("timeout_seconds", defaults.get("timeout_seconds", 5)))
    status_path = _normalize_path(srv.get("path") or defaults.get("path") or SHOW_STATUS_PATH)

    if "api_url" in srv:
        u = urlparse(srv["api_url"])
        if not u.scheme or not u.netloc:
            raise ValueError(f"{srv.get('id', '?')}: некорректный api_url")
        base_root = f"{u.scheme}://{u.netloc}"
        status_url = srv["api_url"]
        eff_scheme = u.scheme.lower()
    else:
        host = srv["host"]
        port = int(srv["port"])
        host_fmt = f"[{host}]" if (":" in host and not host.startswith("[")) else host
        default_port = 80 if scheme == "http" else 443
        netloc = host_fmt if port == default_port else f"{host_fmt}:{port}"
        base_root = f"{scheme}://{netloc}"
        status_url = f"{base_root}{status_path}"
        eff_scheme = scheme

    auth_user = srv.get("auth_user")
    auth_pass = srv.get("auth_pass") or ""
    auth = BasicAuth(auth_user, auth_pass) if auth_user else None

    tg_bot = srv.get("telegram_bot_token") or defaults.get("telegram_bot_token") or ENV_FALLBACK_TG_TOKEN
    tg_chat = srv.get("telegram_chat_id") or defaults.get("telegram_chat_id") or ENV_FALLBACK_TG_CHAT

    return {
        "server_id": srv["id"],
        "server_name": srv.get("name", srv["id"]),
        "base_root": base_root,
        "status_url": status_url,
        "scheme": eff_scheme,
        "verify_ssl": verify_ssl,
        "timeout": timeout_seconds,
        "auth": auth,
        "tg_bot": tg_bot,
        "tg_chat": tg_chat,
    }


# ---------- Telegram ----------
async def tg_send(session: aiohttp.ClientSession, bot_token: Optional[str], chat_id: Optional[str], text: str,
                  logger: Optional[logging.Logger] = None) -> None:
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200 and logger:
                body = await resp.text()
                logger.warning("Telegram send failed: HTTP %s %s", resp.status, body[:300])
    except Exception as e:
        if logger:
            logger.warning("Telegram exception: %s", e)


# ---------- HTTP API ----------
async def fetch_status(session: aiohttp.ClientSession, status_url: str) -> List[dict]:
    async with session.get(status_url) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("data", "items", "result"):
                if k in data and isinstance(data[k], list):
                    return data[k]
        raise RuntimeError("Неожиданный формат ответа show_status_json")


async def action_reset(session: aiohttp.ClientSession, base_root: str, imei: str):
    url = f"{base_root}{RESET_PATH}?IMEI={imei}"
    async with session.get(url) as resp:
        resp.raise_for_status()


async def action_reboot(session: aiohttp.ClientSession, base_root: str, imei: str):
    url = f"{base_root}{REBOOT_PATH}?IMEI={imei}"
    async with session.get(url) as resp:
        resp.raise_for_status()


async def action_usb_reset(session: aiohttp.ClientSession, base_root: str, imei: str):
    url = f"{base_root}{USB_RESET_PATH}?arg={imei}"
    async with session.get(url) as resp:
        resp.raise_for_status()


def index_by_imei(modems: List[dict]) -> Dict[str, dict]:
    out = {}
    for m in modems:
        imei, _ = modem_key(m)
        if imei:
            out[imei] = m
    return out


# ---------- Логика per-modem ----------
async def check_modem_alive(session: aiohttp.ClientSession, status_url: str, imei: str) -> bool:
    try:
        modems = await fetch_status(session, status_url)
        m = index_by_imei(modems).get(imei)
        return bool(m) and not is_offline(m)  # Используем обновленную функцию
    except Exception:
        return False


async def recover_one_modem(
        session: aiohttp.ClientSession,
        base_root: str,
        status_url: str,
        imei: str,
        dev: str,
        server_name: str,
        wait_seconds: int,
        tg_token: Optional[str],
        tg_chat: Optional[str],
        logger: logging.Logger,
):
    prefix = f"[{server_name}] {dev} (IMEI {imei})"
    try:
        await tg_send(session, tg_token, tg_chat, f"🛠 {prefix}: запускаю reset", logger)
        logger.info("%s reset", prefix)
        await action_reset(session, base_root, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, status_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после reset", logger)
            logger.info("%s восстановился после reset", prefix)
            return

        await tg_send(session, tg_token, tg_chat, f"🔁 {prefix}: reset не помог, выполняю reboot", logger)
        logger.info("%s reboot", prefix)
        await action_reboot(session, base_root, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, status_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после reboot", logger)
            logger.info("%s восстановился после reboot", prefix)
            return

        await tg_send(session, tg_token, tg_chat, f"🧰 {prefix}: reboot не помог, выполняю usb_reset", logger)
        logger.info("%s usb_reset", prefix)
        await action_usb_reset(session, base_root, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, status_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после usb_reset", logger)
            logger.info("%s восстановился после usb_reset", prefix)
            return

        await tg_send(session, tg_token, tg_chat, f"❌ {prefix}: не удалось восстановить работу модема", logger)
        logger.warning("%s не восстановился", prefix)
    except Exception as e:
        await tg_send(session, tg_token, tg_chat, f"❗ {prefix}: ошибка сценария восстановления: {e}", logger)
        logger.exception("%s ошибка сценария восстановления: %s", prefix, e)


# ---------- Перебор серверов ----------
async def check_battery_levels(session: aiohttp.ClientSession, modems: List[dict], server_name: str,
                               tg_token: Optional[str], tg_chat: Optional[str], threshold: int,
                               logger: logging.Logger):
    for m in modems:
        imei, dev = modem_key(m)
        if not imei:
            continue
        batt = get_battery_percent(m)
        if batt is None:
            continue
        if batt <= threshold:
            msg = f"🔋 [{server_name}] {dev} (IMEI {imei}): заряд батареи {batt}% (≤{threshold}%)."
            await tg_send(session, tg_token, tg_chat, msg, logger)
            logger.warning(msg)


async def process_server(
        srv: dict,
        defaults: dict,
        battery_threshold: int,
        wait_seconds: int,
        doublecheck_seconds: int,
):
    ep = build_endpoints(srv, defaults)
    server_id = ep["server_id"]
    server_name = ep["server_name"]

    logger = logging.getLogger(f"proxysmart.{server_id}")
    fh = ensure_file_logger(server_id)
    if fh:
        for h in list(logger.handlers):
            if isinstance(h, RotatingFileHandler):
                logger.removeHandler(h)
        logger.addHandler(fh)
    logger.setLevel(logging.INFO)

    timeout = ClientTimeout(total=ep["timeout"])
    ssl_flag = None
    if ep["scheme"] == "https" and not ep["verify_ssl"]:
        ssl_flag = False
    connector = aiohttp.TCPConnector(ssl=ssl_flag)

    async with aiohttp.ClientSession(timeout=timeout, auth=ep["auth"], connector=connector) as session:
        logger.info("[%s] старт проверки", server_id)
        try:
            modems = await fetch_status(session, ep["status_url"])
        except Exception as e:
            logger.error("[%s] не удалось получить список модемов: %s", server_id, e)
            return

        await check_battery_levels(session, modems, server_name, ep["tg_bot"], ep["tg_chat"], battery_threshold, logger)

        # ---- ДВОЙНАЯ ПРОВЕРКА СОСТОЯНИЯ (ONLINE + SYSTEM FLAGS) ----
        dead1: List[Tuple[str, str]] = []
        for m in modems:
            imei, dev = modem_key(m)
            if not imei:
                continue
            # Здесь вызываем обновленную функцию, передавая весь словарь модема
            if is_offline(m):
                dead1.append((imei, dev))

        if not dead1:
            logger.info("[%s] ✅ Все модемы в порядке или заняты системными процессами", server_id)
            return

        logger.info("[%s] найдено проблемных модемов: %d — подождём %d c для повторной проверки",
                    server_id, len(dead1), doublecheck_seconds)
        await asyncio.sleep(doublecheck_seconds)

        # Повторная проверка
        try:
            modems2 = await fetch_status(session, ep["status_url"])
        except Exception as e:
            logger.error("[%s] ошибка при повторной проверке статуса: %s", server_id, e)
            return

        idx2 = index_by_imei(modems2)
        dead2: List[Tuple[str, str]] = []
        for imei, dev in dead1:
            m2 = idx2.get(imei)
            # Если модем исчез из списка или всё еще офлайн по нашей логике
            if m2 is None or is_offline(m2):
                dead2.append((imei, dev))

        if not dead2:
            logger.info("[%s] ⚠️ после повторной проверки модемы восстановились — ничего не делаем", server_id)
            return

        start_msg_lines = [f"[{server_name}] Найдено офлайн-модемов (подтверждено): {len(dead2)}"] + \
                          [f"— {dev} (IMEI {imei})" for imei, dev in dead2]
        start_msg = "\n".join(start_msg_lines)
        await tg_send(session, ep["tg_bot"], ep["tg_chat"], start_msg, logger)
        logger.warning(start_msg)

        tasks = [
            asyncio.create_task(
                recover_one_modem(
                    session=session,
                    base_root=ep["base_root"],
                    status_url=ep["status_url"],
                    imei=imei,
                    dev=dev,
                    server_name=server_name,
                    wait_seconds=wait_seconds,
                    tg_token=ep["tg_bot"],
                    tg_chat=ep["tg_chat"],
                    logger=logger,
                )
            )
            for imei, dev in dead2
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("[%s] завершено", server_id)


# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser(
        description="Мониторинг модемов с учетом IS_ONLINE, IS_LOCKED, IS_REBOOTING, IS_ROTATED."
    )
    p.add_argument("--battery-threshold", type=int, default=int(os.getenv("MG_BATTERY_THRESHOLD", "40")),
                   help="Порог уведомления по батарее, % (по умолчанию 40)")
    p.add_argument("--wait-seconds", type=int, default=int(os.getenv("MG_WAIT_SECONDS", "180")),
                   help="Пауза после каждого действия, сек (по умолчанию 180)")
    p.add_argument("--doublecheck-seconds", type=int,
                   default=DOUBLECHECK_SECONDS,
                   help="Задержка перед повторной проверкой офлайна, сек (по умолчанию 120)")
    return p.parse_args()


# ---------- main ----------
async def main_async():
    args = parse_args()
    cfg = load_config(CONFIG_PATH)
    defaults = cfg.get("defaults", {})
    servers: List[Dict[str, Any]] = cfg["servers"]

    tasks = [
        asyncio.create_task(
            process_server(
                srv=srv,
                defaults=defaults,
                battery_threshold=args.battery_threshold,
                wait_seconds=args.wait_seconds,
                doublecheck_seconds=args.doublecheck_seconds,
            )
        )
        for srv in servers
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        root_logger.info("Остановлено пользователем.")


if __name__ == "__main__":
    main()