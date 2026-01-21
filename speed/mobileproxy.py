#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""MobileProxy collector.

This module is intentionally isolated from proxysmart logic.

It fetches:
  - modem list & meta:  ?command=load_modems
  - speed history:      ?command=get_eid_speed_history&eid=<eid>

And stores *daily* data into Postgres tables in a chosen schema.

Scheduling:
  - We don't need to run exactly at 12:00 (MobileProxy runs their own tests).
  - Default is hourly. Collector deduplicates by (eid, calendar_day).

Tables (schema is configurable):
  - <schema>.mobileproxy_modems_daily
      one row per eid per calendar day (snapshot of modem meta)
  - <schema>.mobileproxy_speed_daily
      one row per eid per speed day (latest record from history)

Env:
  MOBILEPROXY_ENABLED=true/false (auto-enabled if token is set)
  MOBILEPROXY_URL=https://mobileproxy.rent/api.html
  MOBILEPROXY_TOKEN=...
  MOBILEPROXY_POLL_SECONDS=3600
  MOBILEPROXY_HTTP_TIMEOUT_SECONDS=60
  MOBILEPROXY_MAX_EIDS_PER_TICK=0  (0 = unlimited)

  # NEW: delay between API requests (e.g., to avoid 429)
  MOBILEPROXY_REQUEST_DELAY_SECONDS=0.0

"""

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb
import requests

log = logging.getLogger("speed.mobileproxy")


@dataclass(frozen=True)
class MobileProxyConfig:
    enabled: bool
    base_url: str
    token: str
    poll_seconds: int
    http_timeout_seconds: int
    max_eids_per_tick: int
    request_delay_seconds: float


def load_mobileproxy_config() -> MobileProxyConfig:
    token = (os.getenv("MOBILEPROXY_TOKEN", "") or "").strip()
    enabled_env = os.getenv("MOBILEPROXY_ENABLED", "").strip().lower()

    # default behaviour: if token is present => enabled
    if enabled_env in ("true", "1", "yes", "y"):
        enabled = True
    elif enabled_env in ("false", "0", "no", "n"):
        enabled = False
    else:
        enabled = bool(token)

    base_url = (os.getenv("MOBILEPROXY_URL", "https://mobileproxy.rent/api.html") or "").strip()
    poll_seconds = int(os.getenv("MOBILEPROXY_POLL_SECONDS", "3600"))
    http_timeout_seconds = int(os.getenv("MOBILEPROXY_HTTP_TIMEOUT_SECONDS", "60"))
    max_eids_per_tick = int(os.getenv("MOBILEPROXY_MAX_EIDS_PER_TICK", "0"))
    request_delay_seconds = float(os.getenv("MOBILEPROXY_REQUEST_DELAY_SECONDS", "0") or "0")

    return MobileProxyConfig(
        enabled=enabled,
        base_url=base_url,
        token=token,
        poll_seconds=poll_seconds,
        http_timeout_seconds=http_timeout_seconds,
        max_eids_per_tick=max_eids_per_tick,
        request_delay_seconds=request_delay_seconds,
    )


def _headers(cfg: MobileProxyConfig) -> Dict[str, str]:
    return {"Authorization": f"Bearer {cfg.token}"}


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def mp_load_modems(session: requests.Session, cfg: MobileProxyConfig) -> List[Dict[str, Any]]:
    resp = session.get(
        cfg.base_url,
        params={"command": "load_modems"},
        headers=_headers(cfg),
        timeout=cfg.http_timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("MobileProxy: load_modems returned non-list JSON")
    return data


def mp_get_speed_history(session: requests.Session, cfg: MobileProxyConfig, eid: str) -> Dict[str, Any]:
    resp = session.get(
        cfg.base_url,
        params={"command": "get_eid_speed_history", "eid": eid},
        headers=_headers(cfg),
        timeout=cfg.http_timeout_seconds,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("status") == "err":
        raise RuntimeError(f"MobileProxy: error for eid={eid}: {data.get('message')}")
    if not isinstance(data, dict) or "speed" not in data:
        raise RuntimeError(f"MobileProxy: unexpected speed_history response for eid={eid}")
    return data


def extract_latest_speed_record(speed_history: Dict[str, Any]) -> Optional[Tuple[date, datetime, Dict[str, Any]]]:
    """Returns (speed_day, source_dt, record) where record is the chosen item from array."""
    items = speed_history.get("speed")
    if not isinstance(items, list) or not items:
        return None

    best_dt: Optional[datetime] = None
    best_item: Optional[Dict[str, Any]] = None
    for it in items:
        if not isinstance(it, dict):
            continue
        dt = _parse_dt(str(it.get("date", "")))
        if not dt:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_item = it

    if best_dt is None or best_item is None:
        return None

    return (best_dt.date(), best_dt, best_item)


def ensure_mobileproxy_tables_exist(conn: psycopg.Connection, schema: str) -> None:
    need = ["mobileproxy_modems_daily", "mobileproxy_speed_daily"]
    with conn.cursor() as cur:
        for t in need:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema=%s AND table_name=%s
                """,
                (schema, t),
            )
            if cur.fetchone() is None:
                raise RuntimeError(
                    f"Missing table {schema}.{t}. Create MobileProxy tables (migration SQL) in schema '{schema}'."
                )


def _to_float_or_none(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(str(x).strip())
    except Exception:
        return None


def _to_int_or_none(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(str(x).strip())
    except Exception:
        return None


def extract_host(props: Optional[str]) -> Optional[str]:
    """Derive host from MobileProxy 'props'.

    Rules:
      - Only compute when props contains '@' or ':'
      - If '@' exists: drop everything up to and including '@'
      - If ':' exists: drop ':' and everything after it

    Examples:
      19216826126:kYp1KA@ajs.mobileproxy.space:1057 -> ajs.mobileproxy.space
      ajs.mobileproxy.space:1035 -> ajs.mobileproxy.space
    """
    if not props:
        return None
    s = str(props).strip()
    if "@" not in s and ":" not in s:
        return None
    if "@" in s:
        s = s.split("@", 1)[1]
    if ":" in s:
        s = s.split(":", 1)[0]
    s = s.strip()
    return s or None


def db_upsert_modem_daily(conn: psycopg.Connection, schema: str, day: date, m: Dict[str, Any]) -> None:
    eid = str(m.get("eid") or "").strip()
    if not eid:
        return

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.mobileproxy_modems_daily (
              snapshot_day, eid, name, admin_ip, local_ip, local_server_ip,
              operator, status, signal, number, props, modem, comment, raw
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (snapshot_day, eid) DO UPDATE SET
              name=EXCLUDED.name,
              admin_ip=EXCLUDED.admin_ip,
              local_ip=EXCLUDED.local_ip,
              local_server_ip=EXCLUDED.local_server_ip,
              operator=EXCLUDED.operator,
              status=EXCLUDED.status,
              signal=EXCLUDED.signal,
              number=EXCLUDED.number,
              props=EXCLUDED.props,
              modem=EXCLUDED.modem,
              comment=EXCLUDED.comment,
              raw=EXCLUDED.raw
            """,
            (
                day,
                eid,
                m.get("name"),
                m.get("admin_ip"),
                m.get("local_ip"),
                m.get("local_server_ip"),
                m.get("operator"),
                m.get("status"),
                _to_int_or_none(m.get("signal")),
                m.get("number"),
                m.get("props"),
                m.get("modem"),
                m.get("comment"),
                Jsonb(m),
            ),
        )


def db_upsert_speed_daily(
    conn: psycopg.Connection,
    schema: str,
    eid: str,
    host: Optional[str],
    speed_day: date,
    source_dt: datetime,
    record: Dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {schema}.mobileproxy_speed_daily (
              speed_day, eid, host, source_dt, ping_ms, download_mbps, upload_mbps, raw
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (speed_day, eid) DO UPDATE SET
              host=EXCLUDED.host,
              source_dt=EXCLUDED.source_dt,
              ping_ms=EXCLUDED.ping_ms,
              download_mbps=EXCLUDED.download_mbps,
              upload_mbps=EXCLUDED.upload_mbps,
              raw=EXCLUDED.raw
            """,
            (
                speed_day,
                eid,
                host,
                source_dt,
                _to_float_or_none(record.get("ping")),
                _to_float_or_none(record.get("download")),
                _to_float_or_none(record.get("upload")),
                Jsonb(record),
            ),
        )


def run_mobileproxy_tick(conn: psycopg.Connection, schema: str, cfg: MobileProxyConfig) -> None:
    if not cfg.enabled:
        return
    if not cfg.token:
        raise RuntimeError("MOBILEPROXY_TOKEN is required when MOBILEPROXY_ENABLED is true")

    ensure_mobileproxy_tables_exist(conn, schema)

    today = datetime.utcnow().date()

    session = requests.Session()
    try:
        modems = mp_load_modems(session, cfg)

        if cfg.max_eids_per_tick > 0:
            modems = modems[: cfg.max_eids_per_tick]

        ok = 0
        fail = 0

        for m in modems:
            try:
                db_upsert_modem_daily(conn, schema, today, m)

                eid = str(m.get("eid") or "").strip()
                if not eid:
                    continue

                h = mp_get_speed_history(session, cfg, eid)

                # NEW: delay between API requests (helps with 429)
                if cfg.request_delay_seconds > 0:
                    time.sleep(cfg.request_delay_seconds)

                latest = extract_latest_speed_record(h)
                if latest is None:
                    ok += 1
                    continue

                speed_day, source_dt, rec = latest
                host = extract_host(m.get("props"))
                db_upsert_speed_daily(conn, schema, eid, host, speed_day, source_dt, rec)
                ok += 1
            except Exception:
                fail += 1
                log.exception("mobileproxy tick: failed for modem")

        log.info("mobileproxy tick finished: modems=%d ok=%d fail=%d", len(modems), ok, fail)

    finally:
        try:
            session.close()
        except Exception:
            pass


# === Integration helpers expected by app.py ===

def mobileproxy_enabled() -> bool:
    """Return True if MobileProxy ingest is configured via env vars."""
    cfg = load_mobileproxy_config()
    return bool(cfg.enabled and cfg.token and cfg.base_url)


def mobileproxy_required_tables() -> List[str]:
    """Tables required in the target schema."""
    return ["mobileproxy_modems_daily", "mobileproxy_speed_daily"]


def _collector_loop(get_db_conn, schema: str, poll_seconds: int) -> None:
    log.info("mobileproxy collector loop started (poll=%ss, schema=%s)", poll_seconds, schema)
    while True:
        try:
            cfg = load_mobileproxy_config()
            if not cfg.enabled:
                time.sleep(max(60, poll_seconds))
                continue

            conn = get_db_conn()
            try:
                run_mobileproxy_tick(conn, schema, cfg)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            log.exception("mobileproxy collector tick failed")
        time.sleep(max(30, poll_seconds))


def start_mobileproxy_collector(get_db_conn, schema: str) -> None:
    """Start MobileProxy collector in a daemon thread."""
    cfg = load_mobileproxy_config()
    poll_seconds = int(os.getenv("MOBILEPROXY_POLL_SECONDS", str(cfg.poll_seconds or 3600)) or "3600")
    t = threading.Thread(
        target=_collector_loop,
        name="mobileproxy-collector",
        args=(get_db_conn, schema, poll_seconds),
        daemon=True,
    )
    t.start()
    log.info("mobileproxy collector thread started")
