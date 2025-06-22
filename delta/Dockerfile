# Dockerfile
FROM python:3.11-slim

# Необязательные: ca-certificates для https, tzdata (если важно)
RUN apt-get update -y && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py

# Непривилегированный пользователь
RUN useradd -r -u 10001 appuser && mkdir -p /state /logs /config && chown -R appuser:appuser /state /logs /config
USER appuser

ENV PYTHONUNBUFFERED=1 \
    MG_CONFIG=/config/servers.yaml \
    MG_STATE_DIR=/state \
    MG_LOG_DIR=/logs \
    MG_DRIFT_CONFIRM_SECONDS=300

ENTRYPOINT ["python", "/app/app.py"]
