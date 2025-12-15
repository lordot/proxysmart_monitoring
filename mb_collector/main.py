import requests
import yaml
import os
import csv
import io
import re
import time
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Iterable

# Установим логгер
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("db_collector")

# ──────────── Настройки ────────────
CONFIG_PATH = os.getenv("CONFIG_PATH", "servers.yaml")
DB_CONNECTION_RETRY = 5


# ──────────── Утилиты для конфига и даты ────────────
def load_config_for_servers(path: str) -> List[str]:
    """Загружает список серверов из YAML-файла."""
    log.info(f"Попытка загрузки конфига из: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        server_list = data.get("mobile_proxies", [])

        if not isinstance(server_list, list) or not server_list:
            raise ValueError("В конфиге нет списка 'mobile_proxies'")

        return server_list

    except FileNotFoundError:
        log.error(f"Ошибка: Файл конфига не найден по пути: {path}")
        return []
    except yaml.YAMLError as e:
        log.error(f"Ошибка при чтении YAML-файла: {e}")
        return []
    except ValueError as e:
        log.error(f"Ошибка в структуре конфига: {e}")
        return []


def get_previous_day_date_range() -> str:
    """
    Генерирует строку диапазона дат для ПРЕДЫДУЩЕГО дня
    в формате YYYY-MM-DD%20-%20YYYY-MM-DD.
    """
    # Получаем вчерашнюю дату
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_str = yesterday.strftime("%Y-%m-%d")

    # Создаем диапазон: "вчера - вчера" и заменяем пробел на %20
    date_range_str = f"{yesterday_str} - {yesterday_str}"
    return date_range_str.replace(" ", "%20")


# ──────────── Работа с БД (PostgreSQL) ────────────
def db_connect_from_env():
    """Подключается к PostgreSQL, используя переменные окружения, как в вашем примере."""
    import psycopg

    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg.connect(dsn, autocommit=True)

    # Параметры по умолчанию
    return psycopg.connect(
        host=os.getenv("PGHOST", "db"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "mobile_history"),
        user=os.getenv("PGUSER", "history_ingest"),
        password=os.getenv("PGPASSWORD", ""),
        autocommit=True,
    )


def ensure_table_exists(conn, table_name: str) -> None:
    """Создает таблицу для данного сервера, если она не существует. ОБНОВЛЕНО: amount теперь NUMERIC."""
    safe_table_name = re.sub(r'[^a-zA-Z0-9_]', '_', table_name.lower())

    with conn.cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS public.{safe_table_name} (
                id SERIAL PRIMARY KEY,
                op_datetime TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                amount NUMERIC(18, 2) NOT NULL,  -- ИЗМЕНЕНИЕ: тип NUMERIC для чисел
                description TEXT,
                proxy_end_date TEXT,
                extra_field TEXT,
                collected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
    log.info(f"Таблица public.{safe_table_name} проверена/создана (amount: NUMERIC).")
    return safe_table_name


def insert_history_rows(conn, safe_table_name: str, rows: Iterable[Tuple]) -> int:
    """Вставляет строки истории в указанную таблицу."""
    inserted_count = 0
    with conn.cursor() as cur:
        # Теперь мы вставляем числа в поле amount
        cur.executemany(
            f"""
            INSERT INTO public.{safe_table_name}
              (op_datetime, amount, description, proxy_end_date, extra_field)
            VALUES (%s, %s, %s, %s, %s)
            """,
            rows
        )
        inserted_count = cur.rowcount
    return inserted_count


# ──────────── Утилита для очистки суммы ────────────
def clean_and_convert_amount(amount_str: str) -> float:
    """Очищает строку суммы от пробелов и преобразует ее в число с плавающей точкой."""
    # Удаляем все пробелы (для обработки разделителя тысяч: 9 337.39 -> 9337.39)
    cleaned_str = amount_str.replace(' ', '')

    # Ваш пример использует точку как десятичный разделитель, поэтому запятые не трогаем.

    try:
        # Преобразуем очищенную строку в число с плавающей точкой (float)
        return float(cleaned_str)
    except ValueError:
        log.error(f"Не удалось преобразовать сумму '{amount_str}' в число. Использую 0.0")
        return 0.0


# ──────────── Основная логика ────────────
def parse_csv_response(csv_data: str) -> List[Tuple]:
    """Парсит CSV-ответ и возвращает список кортежей для вставки в БД. ОБНОВЛЕНО: amount преобразуется в число."""
    rows_to_insert = []

    csvfile = io.StringIO(csv_data)
    reader = csv.reader(csvfile, delimiter=',', quotechar='"', skipinitialspace=True)

    for row in reader:
        if len(row) >= 5:
            op_datetime_str = row[0].strip()
            amount_str = row[1].strip()  # Исходная строка суммы

            # ПРЕОБРАЗОВАНИЕ СТРОКИ В ЧИСЛО
            amount_numeric = clean_and_convert_amount(amount_str)

            description = row[2].strip()
            proxy_end_date = row[3].strip() if len(row) > 3 else ''
            extra_field = row[4].strip() if len(row) > 4 else ''

            # Преобразование строки даты/времени в объект datetime
            try:
                op_datetime = datetime.strptime(op_datetime_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                log.warning(f"Не удалось распарсить дату/время: {op_datetime_str}. Пропускаю строку.")
                continue

            # Вставляем объект datetime и ЧИСЛО (amount_numeric)
            rows_to_insert.append((op_datetime, amount_numeric, description, proxy_end_date, extra_field))

    return rows_to_insert


def collect_history() -> int:
    """Основной проход сбора данных."""
    server_list = load_config_for_servers(CONFIG_PATH)

    if not server_list:
        log.warning("Список серверов пуст. Выполнение запросов отменено.")
        return 0

    # Получаем динамический диапазон дат для вчерашнего дня
    date_range = get_previous_day_date_range()
    log.info(f"Используемый диапазон дат (вчера): {date_range.replace('%20', ' ')}")

    url_template = f'https://mobileproxy.rent/en/user.html?history&load_history_data=1&export_csv=1&date_range={date_range}&show_payout=false&show_partners_deductions=false&phone_list_server_ip='

    headers = {
        'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
    }
    cookies = {
        'a': '2p7si20rggiur4m3v8cc37s85u',
        'lang': 'en',
    }

    total_inserted = 0
    conn = None

    # Попытка подключения к БД
    for attempt in range(DB_CONNECTION_RETRY):
        try:
            conn = db_connect_from_env()
            break
        except Exception as e:
            log.error(f"Ошибка подключения к БД (попытка {attempt + 1}/{DB_CONNECTION_RETRY}): {e}")
            if attempt < DB_CONNECTION_RETRY - 1:
                time.sleep(2 ** attempt)  # Экспоненциальная задержка
            else:
                log.fatal("Не удалось подключиться к БД после нескольких попыток. Скрипт остановлен.")
                return 0

    log.info(f"Загружено {len(server_list)} серверов. Начинаем запросы и запись в БД.")

    # 2. Перебор серверов и выполнение запроса
    for servername in server_list:
        # Имя таблицы генерируется из имени сервера
        safe_table_name = ensure_table_exists(conn, f"history_{servername.replace('.', '_')}")
        full_url = f"{url_template}{servername}"

        log.info(f"--- Запрос для сервера: {servername} (Таблица: {safe_table_name}) ---")

        try:
            response = requests.get(full_url, headers=headers, cookies=cookies, timeout=15)
            response.raise_for_status()

            csv_data = response.text

            # Парсинг данных
            rows_to_insert = parse_csv_response(csv_data)

            if not rows_to_insert:
                log.info(f"Сервер {servername}: Нет данных для вставки или данные не распознаны.")
                continue

            # Вставка в БД
            inserted_count = insert_history_rows(conn, safe_table_name, rows_to_insert)
            total_inserted += inserted_count
            log.info(f"Сервер {servername}: Успешно вставлено {inserted_count} строк в таблицу {safe_table_name}.")

        except requests.exceptions.RequestException as e:
            log.error(f"Произошла ошибка HTTP-запроса для {servername}: {e}")
        except Exception as e:
            log.exception(f"Произошла непредвиденная ошибка для {servername}: {e}")

    try:
        if conn:
            conn.close()
    except Exception as e:
        log.error(f"Ошибка закрытия соединения с БД: {e}")

    return total_inserted


if __name__ == "__main__":
    try:
        inserted = collect_history()
        log.info("=" * 40)
        log.info(f"Сбор завершен. Всего вставлено строк в БД: {inserted}")
    except Exception as e:
        log.fatal(f"Критическая ошибка в main: {e}")
    except KeyboardInterrupt:
        log.info("Сбор остановлен пользователем.")