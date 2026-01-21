FROM python:3.11-slim

RUN apt-get update -y \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MG_CONFIG=/config/servers.yaml

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py /app/app.py
COPY mobileproxy.py /app/mobileproxy.py

RUN useradd -r -u 10001 appuser \
 && mkdir -p /config \
 && chown -R appuser:appuser /app /config

USER appuser

ENTRYPOINT ["/usr/local/bin/python"]
CMD ["/app/app.py"]
