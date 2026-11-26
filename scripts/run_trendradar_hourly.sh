#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR:h}}"
PROJECT_DIR="$REPO_DIR/TrendRadar"
UV_BIN="/opt/homebrew/bin/uv"
LOG_DIR="$PROJECT_DIR/output/logs"
LOCK_DIR="/tmp/newsradar-trendradar.lock"

mkdir -p "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] previous TrendRadar run still active, skipping"
  exit 0
fi

RUN_LOG="$(mktemp /tmp/newsradar-run.XXXXXX)"

cleanup() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
  rm -f "$RUN_LOG"
}
trap cleanup EXIT INT TERM

python3 "$REPO_DIR/scripts/update_runtime_health.py" start

cd "$PROJECT_DIR"
export PYTHONUNBUFFERED=1
export TRENDRADAR_NO_BROWSER=1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting TrendRadar hourly run"
set +e
"$UV_BIN" run python -m trendradar 2>&1 | tee "$RUN_LOG"
RUN_STATUS=${pipestatus[1]}
set -e

python3 "$REPO_DIR/scripts/update_runtime_health.py" finish \
  --exit-code "$RUN_STATUS" \
  --run-log "$RUN_LOG"

if ! "$REPO_DIR/scripts/publish_collection_health.sh" "$REPO_DIR"; then
  echo "[health] collection checkpoint upload failed; preview asset remains fallback"
fi

if [ "$RUN_STATUS" -ne 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] TrendRadar run failed with status $RUN_STATUS"
  exit "$RUN_STATUS"
fi

# Podcast runs in its own daily worker; report publication never waits for GPU.

set +e
REPO_DIR="$REPO_DIR" "$REPO_DIR/scripts/publish_reports_site.sh"
PUBLISH_STATUS=$?
set -e
python3 "$REPO_DIR/scripts/update_runtime_health.py" publish \
  "$([ "$PUBLISH_STATUS" -eq 0 ] && printf '%s' '--success' || printf '%s' '--no-success')"

if [ "$PUBLISH_STATUS" -ne 0 ]; then
  exit "$PUBLISH_STATUS"
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] finished TrendRadar hourly run"
