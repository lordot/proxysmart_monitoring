#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import logging
import re
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Tuple

import yaml
import requests
from requests.auth import HTTPBasicAuth
import psycopg

CONFIG_PATH = os.getenv("CONFIG_PATH", "/config/servers.yaml")
LIST_PORTS_PATH = "/apix/list_ports_json"
BANDWIDTH_PATH = "/apix/bandwidth_report_json"  # ?arg=<portID>

# ——— логгер ———
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("collector")

# ——— разбор "19.6 GB" -> bytes ———
_MULT = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}
_RX = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*([KMGTP]?B)\s*$", re.IGNORECASE)


def parse_bytes(s: str) -> int:
    if not s:
        return 0
    m = _RX.match(s.strip())
    if not m:
        try:
            return int(s)
        except Exception:
            log.warning("Не удалось распарсить bytes: %r, пишу 0", s)
            return 0
    num = Decimal(m.group(1).replace(",", "."))
    unit = m.group(2).upper()
    return int(num * _MULT.get(unit, 1))


# ——— конфиг ———
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("defaults", {})
    cfg.setdefault("servers", [])
    return cfg


def merged(server: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    out.update(server or {})
    out["timeout_seconds"] = int(out.get("timeout_seconds", 5))
    out["verify_ssl"] = bool(out.get("verify_ssl", True))
    out["scheme"] = out.get("scheme", "http")
    out["path"] = out.get("path", "/apix/show_status_json")
    return out


def base_url(item: Dict[str, Any]) -> str:
    return f"{item['scheme']}://{item['host']}:{item['port']}"


# ——— HTTP ———
def fetch_ports(item: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    url = base_url(item) + LIST_PORTS_PATH
    auth = HTTPBasicAuth(item["auth_user"], item["auth_pass"])
    r = requests.get(url, auth=auth, timeout=item["timeout_seconds"], verify=item["verify_ssl"])
    r.raise_for_status()
    return r.json()


def fetch_bandwidth(item: Dict[str, Any], port_id: str) -> Dict[str, Any]:
    url = base_url(item) + BANDWIDTH_PATH
    auth = HTTPBasicAuth(item["auth_user"], item["auth_pass"])
    r = requests.get(url, params={"arg": port_id}, auth=auth,
                     timeout=item["timeout_seconds"], verify=item["verify_ssl"])
    r.raise_for_status()
    return r.json()


# ——— БД ———
def db_connect_from_env():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg.connect(dsn, autocommit=True)
    return psycopg.connect(
        host=os.getenv("PGHOST", "db"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "metrics"),
        user=os.getenv("PGUSER", "metrics_ingest"),
        password=os.getenv("PGPASSWORD", ""),
        autocommit=True,
    )


def insert_rows(conn, rows: Iterable[Tuple]):
    # collected_at опущен — БД проставит DEFAULT now() (UTC-инстант для timestamptz)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO metrics.proxy_bandwidth
              (server_id, server_name, imei, port_id, login, lifetime_in_bytes, lifetime_out_bytes)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            rows
        )


# ——— основной проход ———
def collect_once(cfg: Dict[str, Any]) -> int:
    defaults = cfg.get("defaults", {})
    servers = cfg.get("servers", [])

    inserted = 0
    conn = db_connect_from_env()

    for srv in servers:
        item = merged(srv, defaults)
        sid, sname = item.get("id", ""), item.get("name", "")
        try:
            ports_by_imei = fetch_ports(item)
        except Exception as e:
            log.error("Сервер %s (%s): ошибка list_ports_json: %s", sname, sid, e)
            continue

        for imei, entries in (ports_by_imei or {}).items():
            if not isinstance(entries, list):
                continue
            for p in entries:
                port_id = p.get("portID") or p.get("portId") or p.get("port_id")
                login = p.get("LOGIN") or p.get("login") or ""
                if not port_id:
                    continue
                try:
                    bw = fetch_bandwidth(item, port_id)
                    in_b = parse_bytes(bw.get("bandwidth_bytes_day_in", ""))
                    out_b = parse_bytes(bw.get("bandwidth_bytes_day_out", ""))
                    row = (sid, sname, str(imei), str(port_id), str(login), in_b, out_b)
                    insert_rows(conn, [row])
                    inserted += 1
                    log.info("OK %s/%s imei=%s port=%s in=%d out=%d",
                             sname, sid, imei, port_id, in_b, out_b)
                except Exception as e:
                    log.error("Сервер %s (%s) imei=%s port=%s: ошибка bandwidth: %s",
                              sname, sid, imei, port_id, e)
                    continue
    return inserted


def main():
    cfg = load_config(CONFIG_PATH)
    every = int(os.getenv("RUN_EVERY_SECONDS", "0"))

    if every <= 0:
        n = collect_once(cfg)
        log.info("Завершено, вставлено строк: %d", n)
        return

    log.info("Запущен в режиме опроса каждые %d сек", every)
    while True:
        try:
            n = collect_once(cfg)
            log.info("Цикл завершён, вставлено строк: %d", n)
        except Exception as e:
            log.exception("Необработанная ошибка цикла: %s", e)
        time.sleep(every)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
