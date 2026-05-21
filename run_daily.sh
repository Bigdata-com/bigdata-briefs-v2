#!/bin/sh
set -e

if [ "$(date -u +%u)" = "1" ]; then
    START=$(date -u -d "3 days ago 12:00:00" +"%Y-%m-%dT%H:%M:%SZ")
else
    START=$(date -u -d "yesterday 12:00:00" +"%Y-%m-%dT%H:%M:%SZ")
fi
END=$(date -u -d "today 12:00:00" +"%Y-%m-%dT%H:%M:%SZ")

curl -s -X POST http://localhost:8000/api/v1/batch/run-parallel \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: ${PIPELINE_API_KEY}" \
    -d "{\"universe_name\":\"dow_30\",\"force_window_start\":\"${START}\",\"force_window_end\":\"${END}\",\"categories\":[\"news\"],\"ranking_metric\":\"media_attention_momentum\"}"
