#!/bin/sh
# Replaces the cron job the upstream project relied on: run one scrape cycle,
# sleep, repeat. Keeping it in one container means the SQLite state and the
# schedule live together.

echo "Holland2Stay notifier started, checking every ${RUN_INTERVAL}s"

while true; do
    python main.py || echo "cycle failed, retrying after ${RUN_INTERVAL}s"
    sleep "${RUN_INTERVAL}"
done
