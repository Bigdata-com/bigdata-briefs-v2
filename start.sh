#!/bin/sh
set -e

if [ "${ENABLE_CRON:-0}" = "1" ]; then
    supercronic /code/crontab &
fi

exec /code/.venv/bin/uvicorn bigdata_briefs.api.app:app \
    --host 0.0.0.0 --port 8000 --workers 1 --lifespan on
