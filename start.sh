#!/bin/sh
set -e

supercronic /code/crontab &

exec uv run uvicorn bigdata_briefs.api.app:app \
    --host 0.0.0.0 --port 8000 --workers 1
