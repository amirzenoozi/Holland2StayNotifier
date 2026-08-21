FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/listings.db \
    CONFIG_PATH=/app/config.json \
    RUN_INTERVAL=3600

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py h2s.py funda.py store.py telegram.py main.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME ["/data"]

CMD ["/entrypoint.sh"]
