FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/listings.db \
    CONFIG_PATH=/app/config.json \
    RUN_INTERVAL=600

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py h2s.py funda.py huurwoningen.py ikwilhuren.py \
     geo.py registry.py control.py store.py telegram.py main.py ./
RUN mkdir -p /data

VOLUME ["/data"]

# main.py owns both loops now - the scrape timer and the Telegram listener -
# so there is no shell wrapper to sleep between runs.
CMD ["python", "main.py"]
