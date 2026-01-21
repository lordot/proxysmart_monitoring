#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""speed — daily Proxysmart speedtests per modem.

What it does:
  - reads /config/servers.yaml (same structure as other services)
  - for each server runs speedtest for each modem once a day at 12:00 local time
  - stores results into the existing Postgres DB (PGDATABASE), without creating a separate DB/user

Notes:
  - lists modems via /apix/show_status_json (IMEI + nick + online flag)
  - runs /apix/speedtest?arg=<IMEI>
  - 3 attempts per modem (retry only on failure)

DB tables (schema configurable via SPEED_DB_SCHEMA, default: metrics):
  - <schema>.speedtest_runs
  - <schema>.speedtest_results
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import psycopg
from psycopg.types.json import Jsonb
import requests
import yaml
from requests.auth import HTTPBasicAuth

from mobileproxy import (
    mobileproxy_enabled,
    mobileproxy_required_tables,
    start_mobileproxy_collector,
)

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception as e:  # pragma: no cover
    raise SystemExit("Python zoneinfo is required (Python 3.9+)") from e


SHOW_STATUS_PATH = "/apix/show_status_json"
SPEEDTEST_PATH = "/apix/speedtest"

# Config
CONFIG_PATH = os.getenv("MG_CONFIG", "/config/servers.yaml")

# DB
DB_SCHEMA = os.getenv("SPEED_DB_SCHEMA", "metrics").strip() or "metrics"

# Scheduler
POLL_SECONDS = int(os.getenv("SPEED_POLL_SECONDS", "20"))
RUN_HOUR = int(os.getenv("SPEED_RUN_HOUR", "12"))
RUN_MINUTE = int(os.getenv("SPEED_RUN_MINUTE", "0"))
RUN_WINDOW_MINUTES = int(os.getenv("SPEED_RUN_WINDOW_MINUTES", "15"))
ALLOW_LATE_RUN = os.getenv("SPEED_ALLOW_LATE_RUN", "false").lower() == "true"

# Attempts
ATTEMPTS = int(os.getenv("SPEED_ATTEMPTS", "3"))
SLEEP_BETWEEN_ATTEMPTS_SECONDS = int(os.getenv("SPEED_SLEEP_BETWEEN_ATTEMPTS_SECONDS", "5"))

# HTTP
HTTP_TIMEOUT_SECONDS = int(os.getenv("SPEED_HTTP_TIMEOUT_SECONDS", "240"))
RETRY_FAILED_RUN = os.getenv("SPEED_RETRY_FAILED_RUN", "false").lower() == "true"


log = logging.getLogger("speed")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str, what: str) -> str:
    if not _IDENT_RE.match(name):
        raise SystemExit(f"Invalid {what} identifier: {name!r}")
    return name


DB_SCHEMA = _validate_ident(DB_SCHEMA, "schema")


def tbl(name: str) -> str:
    """Qualified table name in configured schema."""
    _validate_ident(name, "table")
    return f"{DB_SCHEMA}.{name}"


@dataclass(frozen=True)
class Server:
    server_id: str
    name: str
    base_root: str
    status_url: str
    auth: Optional[HTTPBasicAuth]
    verify_ssl: bool
    timezone: ZoneInfo


def _normalize_path(p: Optional[str]) -> str:
    if not p:
        return "/"
    return p if p.startswith("/") else f"/{p}"


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get("servers", [])
    if not isinstance(servers, list) or not servers:
        raise ValueError("В конфиге нет списка 'servers'")
    data.setdefault("defaults", {})
    return data


def build_server(srv: dict, defaults: dict) -> Server:
    scheme = (srv.get("scheme") or defaults.get("scheme") or "http").lower()
    verify_ssl = bool(srv.get("verify_ssl", defaults.get("verify_ssl", True)))
    status_path = _normalize_path(srv.get("path") or defaults.get("path") or SHOW_STATUS_PATH)

    if "api_url" in srv:
        u = urlparse(srv["api_url"])
        if not u.scheme or not u.netloc:
            raise ValueError(f"{srv.get('id', '?')}: некорректный api_url")
        base_root = f"{u.scheme}://{u.netloc}"
        status_url = srv["api_url"]
    else:
        host = srv["host"]
        port = int(srv["port"])
        host_fmt = f"[{host}]" if (":" in host and not host.startswith("[")) else host
        default_port = 80 if scheme == "http" else 443
        netloc = host_fmt if port == default_port else f"{host_fmt}:{port}"
        base_root = f"{scheme}://{netloc}"
        status_url = f"{base_root}{status_path}"

    auth_user = srv.get("auth_user") or ""
    auth_pass = srv.get("auth_pass") or ""
    auth = HTTPBasicAuth(auth_user, auth_pass) if auth_user else None

    tz_name = srv.get("timezone") or defaults.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(str(tz_name))
    except Exception as e:
        raise ValueError(f"{srv.get('id','?')}: неизвестная timezone '{tz_name}'") from e

    return Server(
        server_id=str(srv.get("id") or srv.get("name") or "unknown"),
        name=str(srv.get("name") or srv.get("id") or "unknown"),
        base_root=base_root,
        status_url=status_url,
        auth=auth,
        verify_ssl=verify_ssl,
        timezone=tz,
    )


def get_db_conn() -> psycopg.Connection:
    """Uses standard libpq env vars: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD."""
    return psycopg.connect("", autocommit=True)


def ensure_tables_exist(conn: psycopg.Connection) -> None:
    """Fail fast if tables are missing (we do not auto-migrate to avoid requiring CREATE privileges)."""
    need = ["speedtest_runs", "speedtest_results"]
    if mobileproxy_enabled():
        need += mobileproxy_required_tables()
    with conn.cursor() as cur:
        for t in need:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema=%s AND table_name=%s
                """,
                (DB_SCHEMA, t),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Missing table {tbl(t)}. Apply init.sql (fresh DB) or run the migration SQL for speed tables in schema '{DB_SCHEMA}'."
                )


def parse_rate_to_mbps(v: Optional[str]) -> Optional[float]:
    """Parses strings like '52.79mbps', '8.85 mbps', '120 kbps', '1.2 gbps'."""
    if v is None:
        return None
    s = str(v).strip().lower().replace(" ", "")
    if not s:
        return None
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(k|m|g)?bps$", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or "m"
    if unit == "k":
        return num / 1000.0
    if unit == "g":
        return num * 1000.0
    return num


def parse_ms(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().lower().replace(" ", "")
    if not s:
        return None
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)ms$", s)
    if not m:
        return None
    return float(m.group(1))


def fetch_modems(server: Server) -> List[dict]:
    resp = requests.get(
        server.status_url,
        auth=server.auth,
        timeout=HTTP_TIMEOUT_SECONDS,
        verify=server.verify_ssl,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "result"):
            if k in data and isinstance(data[k], list):
                return data[k]
    raise RuntimeError("Неожиданный формат ответа show_status_json")


def extract_modems(modems_raw: List[dict]) -> List[Tuple[str, str, str]]:
    """Returns (imei, nick, is_online) list."""
    out: List[Tuple[str, str, str]] = []
    for m in modems_raw:
        md = m.get("modem_details", {}) if isinstance(m, dict) else {}
        nd = m.get("net_details", {}) if isinstance(m, dict) else {}
        imei = str(md.get("IMEI") or m.get("IMEI") or "").strip()
        if not imei:
            continue
        nick = str(md.get("NICK") or md.get("name") or m.get("name") or "").strip()
        is_online = str(nd.get("IS_ONLINE") or m.get("IS_ONLINE") or "").strip()
        out.append((imei, nick, is_online))

    uniq: Dict[str, Tuple[str, str, str]] = {}
    for imei, nick, online in out:
        uniq.setdefault(imei, (imei, nick, online))
    return list(uniq.values())


def run_speedtest(server: Server, imei: str) -> Dict[str, Any]:
    url = f"{server.base_root}{SPEEDTEST_PATH}?arg={imei}"
    resp = requests.get(
        url,
        auth=server.auth,
        timeout=HTTP_TIMEOUT_SECONDS,
        verify=server.verify_ssl,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("Неожиданный формат ответа speedtest")
    return data


def db_has_run(conn: psycopg.Connection, server_id: str, run_date_local: date) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status FROM {tbl('speedtest_runs')} WHERE server_id=%s AND run_date_local=%s",
            (server_id, run_date_local),
        )
        row = cur.fetchone()
        return row[0] if row else None


def db_start_run(conn: psycopg.Connection, server_id: str, run_date_local: date, tz: str) -> bool:
    """Returns True if we acquired today's run for this server."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {tbl('speedtest_runs')}(server_id, run_date_local, tz, status)
            VALUES (%s, %s, %s, 'running')
            ON CONFLICT (server_id, run_date_local) DO NOTHING
            """,
            (server_id, run_date_local, tz),
        )
        if cur.rowcount == 1:
            return True

        cur.execute(
            f"SELECT status FROM {tbl('speedtest_runs')} WHERE server_id=%s AND run_date_local=%s",
            (server_id, run_date_local),
        )
        row = cur.fetchone()
        if not row:
            return False
        status = row[0]
        if status in ("running", "success"):
            return False
        if status == "failed" and RETRY_FAILED_RUN:
            cur.execute(
                f"""
                UPDATE {tbl('speedtest_runs')}
                SET status='running', started_at=now(), finished_at=NULL, note=NULL,
                    total_modems=NULL, ok_modems=NULL, fail_modems=NULL
                WHERE server_id=%s AND run_date_local=%s
                """,
                (server_id, run_date_local),
            )
            return True
        return False


def db_finish_run(
    conn: psycopg.Connection,
    server_id: str,
    run_date_local: date,
    status: str,
    total: int,
    ok: int,
    fail: int,
    note: Optional[str] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {tbl('speedtest_runs')}
            SET finished_at=now(), status=%s, total_modems=%s, ok_modems=%s, fail_modems=%s, note=%s
            WHERE server_id=%s AND run_date_local=%s
            """,
            (status, total, ok, fail, note, server_id, run_date_local),
        )


def db_insert_result(
    conn: psycopg.Connection,
    *,
    server_id: str,
    run_date_local: date,
    tz: str,
    imei: str,
    nick: str,
    is_online: str,
    attempt: int,
    success: bool,
    download_mbps: Optional[float],
    upload_mbps: Optional[float],
    ping_ms: Optional[float],
    raw: Dict[str, Any],
    error: Optional[str],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {tbl('speedtest_results')}(
              server_id, run_date_local, tz, imei, nick, is_online, attempt, success,
              download_mbps, upload_mbps, ping_ms, raw, error
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                server_id,
                run_date_local,
                tz,
                imei,
                nick or None,
                is_online or None,
                int(attempt),
                bool(success),
                download_mbps,
                upload_mbps,
                ping_ms,
                Jsonb(raw),
                error,
            ),
        )


def is_due(now_local: datetime, run_date_local: date) -> bool:
    target = datetime.combine(run_date_local, dtime(RUN_HOUR, RUN_MINUTE), tzinfo=now_local.tzinfo)
    if ALLOW_LATE_RUN:
        return now_local >= target
    return target <= now_local < (target + timedelta(minutes=RUN_WINDOW_MINUTES))


def run_for_server(server: Server, run_date_local: date) -> None:
    thread_name = threading.current_thread().name
    tz_name = str(server.timezone.key) if hasattr(server.timezone, "key") else ""
    log.info("[%s] run start: server=%s date=%s tz=%s", thread_name, server.server_id, run_date_local, tz_name)

    conn = get_db_conn()
    try:
        ensure_tables_exist(conn)
        if not db_start_run(conn, server.server_id, run_date_local, tz_name):
            log.info("[%s] already running/done: server=%s date=%s", thread_name, server.server_id, run_date_local)
            return

        ok = 0
        fail = 0

        try:
            raw = fetch_modems(server)
            modems = extract_modems(raw)
        except Exception as e:
            db_finish_run(conn, server.server_id, run_date_local, "failed", 0, 0, 0, note=f"fetch_modems: {e}")
            log.exception("[%s] fetch_modems failed: server=%s", thread_name, server.server_id)
            return

        total = len(modems)
        log.info("[%s] server=%s modems=%d", thread_name, server.server_id, total)

        for imei, nick, is_online in modems:
            success = False
            last_err: Optional[str] = None

            for attempt in range(1, ATTEMPTS + 1):
                try:
                    data = run_speedtest(server, imei)
                    d_mbps = parse_rate_to_mbps(data.get("download"))
                    u_mbps = parse_rate_to_mbps(data.get("upload"))
                    p_ms = parse_ms(data.get("ping"))
                    if d_mbps is None or u_mbps is None or p_ms is None:
                        raise RuntimeError(
                            f"parse_failed: download={data.get('download')} upload={data.get('upload')} ping={data.get('ping')}"
                        )

                    db_insert_result(
                        conn,
                        server_id=server.server_id,
                        run_date_local=run_date_local,
                        tz=tz_name,
                        imei=imei,
                        nick=nick,
                        is_online=is_online,
                        attempt=attempt,
                        success=True,
                        download_mbps=d_mbps,
                        upload_mbps=u_mbps,
                        ping_ms=p_ms,
                        raw=data,
                        error=None,
                    )
                    success = True
                    break
                except Exception as e:
                    last_err = str(e)
                    try:
                        db_insert_result(
                            conn,
                            server_id=server.server_id,
                            run_date_local=run_date_local,
                            tz=tz_name,
                            imei=imei,
                            nick=nick,
                            is_online=is_online,
                            attempt=attempt,
                            success=False,
                            download_mbps=None,
                            upload_mbps=None,
                            ping_ms=None,
                            raw={"error": last_err},
                            error=last_err,
                        )
                    except Exception:
                        log.exception("[%s] db_insert_result failed (attempt=%d imei=%s)", thread_name, attempt, imei)

                    if attempt < ATTEMPTS:
                        time.sleep(SLEEP_BETWEEN_ATTEMPTS_SECONDS)

            if success:
                ok += 1
            else:
                fail += 1
                log.warning(
                    "[%s] speedtest failed: server=%s imei=%s nick=%s err=%s",
                    thread_name,
                    server.server_id,
                    imei,
                    nick,
                    last_err,
                )

        status = "success" if fail == 0 else "failed"
        db_finish_run(conn, server.server_id, run_date_local, status, total, ok, fail)
        log.info(
            "[%s] run finished: server=%s date=%s status=%s ok=%d fail=%d",
            thread_name,
            server.server_id,
            run_date_local,
            status,
            ok,
            fail,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> None:
    cfg = load_config(CONFIG_PATH)
    defaults = cfg.get("defaults", {}) or {}
    servers_cfg = cfg.get("servers", [])

    servers: List[Server] = []
    for s in servers_cfg:
        try:
            servers.append(build_server(s, defaults))
        except Exception as e:
            log.error("skip server config: %s", e)

    if not servers:
        raise SystemExit("Нет валидных серверов в конфиге")

    # Fail fast if DB auth or tables are missing
    conn = get_db_conn()
    try:
        ensure_tables_exist(conn)
    finally:
        conn.close()

    # Start MobileProxy collector in a separate thread (if configured).
    # It uses the same DB connection settings and schema, but its logic lives in mobileproxy.py.
    if mobileproxy_enabled():
        start_mobileproxy_collector(get_db_conn, DB_SCHEMA)

    running: set[str] = set()
    lock = threading.Lock()

    def _spawn(server: Server, run_date_local: date) -> None:
        def _wrapped() -> None:
            try:
                run_for_server(server, run_date_local)
            finally:
                with lock:
                    running.discard(server.server_id)

        with lock:
            if server.server_id in running:
                return
            running.add(server.server_id)

        t = threading.Thread(target=_wrapped, name=f"speed-{server.server_id}", daemon=True)
        t.start()

    log.info(
        "speed service started: servers=%d run_at=%02d:%02d window=%dmin allow_late=%s db_schema=%s",
        len(servers),
        RUN_HOUR,
        RUN_MINUTE,
        RUN_WINDOW_MINUTES,
        ALLOW_LATE_RUN,
        DB_SCHEMA,
    )

    while True:
        conn = get_db_conn()
        try:
            for server in servers:
                now_local = datetime.now(server.timezone)
                run_date_local = now_local.date()
                if not is_due(now_local, run_date_local):
                    continue

                status = db_has_run(conn, server.server_id, run_date_local)
                if status in ("running", "success"):
                    continue

                _spawn(server, run_date_local)
        except Exception:
            log.exception("scheduler tick failed")
        finally:
            try:
                conn.close()
            except Exception:
                pass

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
