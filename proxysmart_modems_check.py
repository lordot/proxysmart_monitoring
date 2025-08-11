#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import json
import os
import platform
import socket
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import BasicAuth, ClientTimeout

SHOW_STATUS_PATH = "/apix/show_status_json"
RESET_PATH = "/apix/reset_modem_by_imei"
REBOOT_PATH = "/apix/reboot_modem_by_imei"
USB_RESET_PATH = "/apix/usb_reset_modem_json"


# -------- Helpers --------

def is_ping_dead(ping_stats: Optional[str]) -> bool:
    """
    Критерий "модем мёртв": в ping_stats есть "100% loss".
    Пример из ТЗ: "?ms, 100% loss"
    """
    if not ping_stats:
        return True
    return "100% loss" in ping_stats


def get_hostname() -> str:
    """
    Возвращает максимально надёжное имя хоста.
    Порядок шагов подобран от самых быстрых к самым «тяжёлым».
    Если всё неудачно — 'localhost'.
    """

    # 1. socket.gethostname() — прямой системный вызов
    try:
        hn = socket.gethostname()
        if hn:
            return hn
    except Exception:
        pass

    # 2. platform.node() — обёртка вокруг того же системного вызова,
    #    но существует давно и иногда работает там, где первый поймал OSError
    try:
        hn = platform.node()
        if hn:
            return hn
    except Exception:
        pass

    # 3. Переменная окружения HOSTNAME (как раньше)
    hn = os.environ.get("HOSTNAME")
    if hn:
        return hn

    # 4. Linux/Unix: читаем /etc/hostname
    try:
        hn = Path("/etc/hostname").read_text().strip()
        if hn:
            return hn
    except FileNotFoundError:
        pass

    # 5. Последний шанс — вызываем утилиту hostname
    try:
        hn = subprocess.check_output(["hostname"], text=True).strip()
        if hn:
            return hn
    except Exception:
        pass

    # 6. Совсем ничего не нашли
    return "localhost"


def build_base_url(server: str, scheme: str = "http") -> str:
    server = server.strip()
    if server.startswith("http://") or server.startswith("https://"):
        return server.rstrip("/")
    return f"{scheme}://{server}".rstrip("/")


def modem_key(modem: dict) -> Tuple[str, str]:
    """Возвращает (IMEI, DEV) для удобства логирования."""
    imei = modem.get("modem_details", {}).get("IMEI", "") or ""
    dev = modem.get("net_details", {}).get("DEV", "") or ""
    return imei, dev


def pick_ping(modem: dict) -> str:
    return modem.get("net_details", {}).get("ping_stats", "") or ""


def get_battery_percent(modem: dict) -> Optional[int]:
    """
    Возвращает процент заряда батареи как int (0..100), либо None,
    если поля нет/пустое/некорректное.
    Допускает строку с '%' или пробелами.
    """
    val = modem.get("android", {}).get("battery")
    if val is None:
        return None
    # Число?
    if isinstance(val, (int, float)):
        try:
            i = int(val)
            return i if 0 <= i <= 100 else None
        except Exception:
            return None
    # Строка?
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return None
        # срезаем знак %
        if s.endswith("%"):
            s = s[:-1].strip()
        # оставляем только цифры (на случай "80 " или " ~80")
        digits = "".join(ch for ch in s if ch.isdigit())
        if not digits:
            return None
        try:
            i = int(digits)
            return i if 0 <= i <= 100 else None
        except Exception:
            return None
    return None


async def check_battery_levels(
        session: aiohttp.ClientSession,
        modems: List[dict],
        server_name: str,
        tg_token: Optional[str],
        tg_chat: Optional[str],
        threshold: int = 40,
) -> None:
    """
    Перебирает модемы, ищет android.battery, и если заряд <= threshold,
    отправляет уведомление в Telegram. Пустые/некорректные значения пропускает.
    """
    for m in modems:
        imei, dev = modem_key(m)
        if not imei:
            continue
        batt = get_battery_percent(m)
        if batt is None:
            continue  # "Если пустое значение, то ничего не делать"
        if batt <= threshold:
            prefix = f"[{server_name}] {dev} (IMEI {imei})"
            await tg_send(
                session, tg_token, tg_chat,
                f"🔋 {prefix}: заряд батареи {batt}% (≤{threshold}%)."
            )


# -------- Telegram --------

async def tg_send(session: aiohttp.ClientSession, bot_token: Optional[str], chat_id: Optional[str], text: str) -> None:
    """
    Отправка сообщения в Telegram. Если токен/чат не заданы — просто пропускаем.
    """
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        async with session.post(url, json=payload) as resp:
            # Игнорируем тело, но логично убедиться в 200
            if resp.status != 200:
                # Не бросаем исключение, чтобы не прервать основной поток восстановления
                body = await resp.text()
                print(f"[WARN] Telegram send failed: HTTP {resp.status} {body}")
    except Exception as e:
        print(f"[WARN] Telegram exception: {e}")


# -------- HTTP API --------

async def fetch_status(session: aiohttp.ClientSession, base_url: str) -> List[dict]:
    url = f"{base_url}{SHOW_STATUS_PATH}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
        if isinstance(data, list):
            return data
        # На всякий случай, если сервер вернул строку/текст
        if isinstance(data, dict):
            # некоторые реализации могут возвращать {"data":[...]}
            for k in ("data", "items", "result"):
                if k in data and isinstance(data[k], list):
                    return data[k]
        raise RuntimeError("Неожиданный формат ответа show_status_json")


async def action_reset(session: aiohttp.ClientSession, base_url: str, imei: str) -> None:
    url = f"{base_url}{RESET_PATH}?IMEI={imei}"
    async with session.get(url) as resp:
        print("reset ответ получен")
        resp.raise_for_status()


async def action_reboot(session: aiohttp.ClientSession, base_url: str, imei: str) -> None:
    url = f"{base_url}{REBOOT_PATH}?IMEI={imei}"
    async with session.get(url) as resp:
        print("reboot ответ получен")
        resp.raise_for_status()


async def action_usb_reset(session: aiohttp.ClientSession, base_url: str, imei: str) -> None:
    # обратите внимание: параметр называется arg, а не IMEI
    url = f"{base_url}{USB_RESET_PATH}?arg={imei}"
    async with session.get(url) as resp:
        print("usb reset ответ получен")
        resp.raise_for_status()


def index_by_imei(modems: List[dict]) -> Dict[str, dict]:
    result = {}
    for m in modems:
        imei, _ = modem_key(m)
        if imei:
            result[imei] = m
    return result


# -------- Recovery flow per modem (runs concurrently) --------

async def check_modem_alive(session: aiohttp.ClientSession, base_url: str, imei: str) -> bool:
    try:
        modems = await fetch_status(session, base_url)
        by_imei = index_by_imei(modems)
        m = by_imei.get(imei)
        if not m:
            # Если модема нет в списке — считаем, что не восстановился
            return False
        return not is_ping_dead(pick_ping(m))
    except Exception as e:
        print(f"[WARN] check_modem_alive error for IMEI {imei}: {e}")
        return False


async def recover_one_modem(
        session: aiohttp.ClientSession,
        base_url: str,
        imei: str,
        dev: str,
        server_name: str,
        wait_seconds: int,
        tg_token: Optional[str],
        tg_chat: Optional[str],
):
    prefix = f"[{server_name}] {dev} (IMEI {imei})"
    try:
        # Step 1: reset
        await tg_send(session, tg_token, tg_chat, f"🛠 {prefix}: запускаю reset")
        await action_reset(session, base_url, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, base_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после reset")
            return

        # Step 2: reboot
        await tg_send(session, tg_token, tg_chat, f"🔁 {prefix}: reset не помог, выполняю reboot")
        await action_reboot(session, base_url, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, base_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после reboot")
            return

        # Step 3: usb reset
        await tg_send(session, tg_token, tg_chat, f"🧰 {prefix}: reboot не помог, выполняю usb_reset")
        await action_usb_reset(session, base_url, imei)
        await asyncio.sleep(wait_seconds)
        if await check_modem_alive(session, base_url, imei):
            await tg_send(session, tg_token, tg_chat, f"✅ {prefix}: восстановился после usb_reset")
            return

        # Step 4: give up
        await tg_send(session, tg_token, tg_chat, f"❌ {prefix}: не удалось восстановить работу модема")
    except Exception as e:
        await tg_send(session, tg_token, tg_chat, f"❗ {prefix}: ошибка сценария восстановления: {e}")


# -------- Main orchestration --------

async def main_async(args):
    base_url = build_base_url(args.server, scheme=args.scheme)
    server_name = args.server_name or get_hostname()

    timeout = ClientTimeout(total=args.http_timeout)
    auth = None
    if args.user or args.password:
        auth = BasicAuth(login=args.user or "", password=args.password or "")

    connector = aiohttp.TCPConnector(
        ssl=False)  # в случае самоподписанных сертификатов на https можно оставить ssl=False
    async with aiohttp.ClientSession(timeout=timeout, auth=auth, connector=connector) as session:
        # 1) Получаем список модемов
        modems = await fetch_status(session, base_url)

        # 2) Проверяем уровни батарей (если есть поле android.battery)
        await check_battery_levels(
            session=session,
            modems=modems,
            server_name=server_name,
            tg_token=args.tg_token,
            tg_chat=args.tg_chat,
            threshold=args.battery_threshold,
        )

        # 3) Находим "мёртвые" по ping_stats
        dead = []
        for m in modems:
            imei, dev = modem_key(m)
            if not imei:
                continue
            if is_ping_dead(pick_ping(m)):
                dead.append((imei, dev))

        if not dead:
            print(f"[{server_name}] ✅ Все модемы в порядке (нет 100% loss).")
            return

        # 4) Запускаем параллельное восстановление для каждого модема
        start_msg_lines = [f"[{server_name}] Найдено модемов с 100% loss: {len(dead)}"]
        start_msg_lines += [f"— {dev} (IMEI {imei})" for imei, dev in dead]
        start_msg = "\n".join(start_msg_lines)
        print(start_msg)
        await tg_send(session, args.tg_token, args.tg_chat, start_msg)

        tasks = [
            asyncio.create_task(
                recover_one_modem(
                    session=session,
                    base_url=base_url,
                    imei=imei,
                    dev=dev,
                    server_name=server_name,
                    wait_seconds=args.wait_seconds,
                    tg_token=args.tg_token,
                    tg_chat=args.tg_chat,
                )
            )
            for imei, dev in dead
        ]

        # 4) Дожидаемся завершения всех задач
        await asyncio.gather(*tasks, return_exceptions=True)


def parse_args():
    # Значения из окружения (ENV) с дефолтами
    env_server = os.getenv("MG_SERVER", "localhost:8080")
    env_scheme = os.getenv("MG_SCHEME", "http")
    env_user = os.getenv("MG_USER", "")
    env_password = os.getenv("MG_PASSWORD", "")
    env_server_name = os.getenv("MG_SERVER_NAME", "")
    env_tg_token = os.getenv("MG_TG_TOKEN", "")
    env_tg_chat = os.getenv("MG_TG_CHAT", "-4850356170")

    try:
        env_battery_threshold = int(os.getenv("MG_BATTERY_THRESHOLD", "40"))
    except ValueError:
        env_battery_threshold = 40
    try:
        env_wait_seconds = int(os.getenv("MG_WAIT_SECONDS", "180"))
    except ValueError:
        env_wait_seconds = 180
    try:
        env_http_timeout = int(os.getenv("MG_HTTP_TIMEOUT", "240"))
    except ValueError:
        env_http_timeout = 30

    p = argparse.ArgumentParser(
        description="Асинхронный мониторинг/восстановление модемов по ping_stats и уведомления в Telegram."
    )
    # ВАЖНО: server больше не required=True, чтобы можно было работать через ENV/дефолт
    p.add_argument("--battery-threshold", type=int, default=env_battery_threshold,
                   help="Порог уведомления по батарее, % (по умолчанию 40)")
    p.add_argument("--server", default=env_server, help="Хост:порт сервера, например 66.179.81.210:7001")
    p.add_argument("--scheme", default=env_scheme, choices=["http", "https"], help="Схема протокола")
    p.add_argument("--user", default=env_user, help="HTTP Basic Auth логин (например: proxy)")
    p.add_argument("--password", default=env_password, help="HTTP Basic Auth пароль (например: ebd8b3675ee8)")
    p.add_argument("--server-name", default=env_server_name, help="Имя сервера для сообщений (по умолчанию hostname)")
    p.add_argument("--tg-token", default=env_tg_token, help="Telegram Bot Token")
    p.add_argument("--tg-chat", default=env_tg_chat, help="Telegram chat_id (например, @username или числовой id)")
    p.add_argument("--wait-seconds", type=int, default=env_wait_seconds,
                   help="Пауза после каждого действия, сек")
    p.add_argument("--http-timeout", type=int, default=env_http_timeout,
                   help="HTTP таймаут, сек")
    return p.parse_args()


def main():
    args = parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("Остановлено пользователем.")


if __name__ == "__main__":
    main()
