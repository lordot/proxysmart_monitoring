FROM python:3.11-slim

# Опционально: корневые сертификаты для https-запросов
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MG_CONFIG=/config/servers.yaml \
    MG_LOG_DIR=/logs

WORKDIR /app

# Сначала зависимости (лучше кешируется)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Скрипты
COPY app.py /app/app.py

# Непривилегированный пользователь и каталоги
RUN useradd -r -u 10001 appuser \
 && mkdir -p /state /logs /config \
 && chown -R appuser:appuser /app /state /logs /config

USER appuser

ENTRYPOINT ["/usr/local/bin/python"]
CMD ["/app/app.py"]
