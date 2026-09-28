#!/bin/sh
set -eu

URL="${WEBSTATS_HEALTH_URL:-http://127.0.0.1:5010/api/health}"
exec curl --fail --silent --show-error --max-time 10 "$URL"

