-- === Роли ===
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='metabase_app') THEN
    CREATE ROLE metabase_app LOGIN PASSWORD 'metabase_app_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='metabase_ro') THEN
    CREATE ROLE metabase_ro  LOGIN PASSWORD 'metabase_ro_pass'  NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='metrics_ingest') THEN
    CREATE ROLE metrics_ingest LOGIN PASSWORD 'ingest_pass' NOSUPERUSER NOCREATEDB NOCREATEROLE;
  END IF;
END$$;

-- === Базы данных ===
-- служебная БД для Metabase, сразу назначаем владельца
CREATE DATABASE metabase OWNER metabase_app;

-- включаем расширение TimescaleDB в обеих БД
\connect metrics
CREATE EXTENSION IF NOT EXISTS timescaledb;

\connect metabase
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- === Структура и права в БД metrics ===
\connect metrics

-- схема под данные метрик (владелец — app, как POSTGRES_USER)
CREATE SCHEMA IF NOT EXISTS metrics AUTHORIZATION app;

-- пример таблицы метрик
CREATE TABLE IF NOT EXISTS metrics.readings (
    ts     TIMESTAMPTZ      NOT NULL,
    source TEXT             NOT NULL,
    key    TEXT             NOT NULL,
    value  DOUBLE PRECISION NOT NULL
);

-- превращаем таблицу в hypertable (без ошибок при повторном запуске)
SELECT create_hypertable('metrics.readings','ts', if_not_exists => TRUE);

-- индексы
CREATE INDEX IF NOT EXISTS idx_readings_ts      ON metrics.readings (ts DESC);
CREATE INDEX IF NOT EXISTS idx_readings_src_key ON metrics.readings (source, key);

-- права: Metabase читает, сервис-сборщик пишет
GRANT CONNECT ON DATABASE metrics TO metabase_ro;
GRANT USAGE   ON SCHEMA  metrics TO metabase_ro, metrics_ingest;

GRANT SELECT                                   ON ALL TABLES IN SCHEMA metrics TO metabase_ro;
GRANT SELECT, INSERT, UPDATE, DELETE           ON ALL TABLES IN SCHEMA metrics TO metrics_ingest;

-- дефолтные привилегии на будущие таблицы (которые будет создавать текущий владелец — app)
ALTER DEFAULT PRIVILEGES IN SCHEMA metrics
  GRANT SELECT ON TABLES TO metabase_ro;

ALTER DEFAULT PRIVILEGES IN SCHEMA metrics
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO metrics_ingest;

-- (необязательно) если в будущем таблицы будет создавать другая роль — продублируйте через:
-- ALTER DEFAULT PRIVILEGES FOR ROLE app IN SCHEMA metrics GRANT ... ;

-- === Наведение порядка в БД metabase ===
\connect metabase
-- ограничим PUBLIC на всякий случай и выдадим явные права владельцу
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT  ALL ON SCHEMA public TO metabase_app;


CREATE SCHEMA IF NOT EXISTS metrics AUTHORIZATION app;

CREATE TABLE IF NOT EXISTS metrics.proxy_bandwidth (
  collected_at           timestamptz NOT NULL DEFAULT now(),
  server_id              text        NOT NULL,
  server_name            text        NOT NULL,
  imei                   text        NOT NULL,
  port_id                text        NOT NULL,
  login                  text        NOT NULL,
  lifetime_in_bytes      bigint      NOT NULL,
  lifetime_out_bytes     bigint      NOT NULL
);

-- Hypertable в TimescaleDB
SELECT create_hypertable('metrics.proxy_bandwidth','collected_at', if_not_exists => TRUE);

-- Индексы под частые фильтры/джойны
CREATE INDEX IF NOT EXISTS idx_pb_time      ON metrics.proxy_bandwidth (collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_pb_server    ON metrics.proxy_bandwidth (server_id, server_name);
CREATE INDEX IF NOT EXISTS idx_pb_imei      ON metrics.proxy_bandwidth (imei);
CREATE INDEX IF NOT EXISTS idx_pb_port      ON metrics.proxy_bandwidth (port_id);

-- Права (чтение Metabase, запись коллектора)
GRANT USAGE ON SCHEMA metrics TO metabase_ro, metrics_ingest;
GRANT SELECT ON metrics.proxy_bandwidth TO metabase_ro;
GRANT SELECT, INSERT ON metrics.proxy_bandwidth TO metrics_ingest;

