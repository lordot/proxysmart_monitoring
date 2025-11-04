FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py /app/main.py

# по умолчанию разовый запуск; задайте RUN_EVERY_SECONDS в compose для опроса по расписанию
CMD ["python", "/app/main.py"]
