#!/bin/sh
# Daily rolling retention prune of heavy pipeline history.
#
# Opt-in: does nothing unless ENABLE_RETENTION_PRUNE is set. The retention window
# is not passed here on purpose, so RETENTION_KEEP_DAYS stays the single source of
# truth and the API applies its own novelty floor to it.
set -e

case "${ENABLE_RETENTION_PRUNE:-0}" in
    1|true|True|TRUE|yes|on) ;;
    *)
        echo "prune-retention: disabled (ENABLE_RETENTION_PRUNE unset), skipping"
        exit 0
        ;;
esac

echo "prune-retention: starting at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"

curl -s -X POST "http://localhost:8000/api/v1/utilities/prune-retention?dry_run=false" \
    -H "X-Api-Key: ${PIPELINE_API_KEY}"

echo ""
echo "prune-retention: finished at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
