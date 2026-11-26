#!/bin/zsh
set -eu

REPO_DIR="${REPO_DIR:-/Users/bruce/Coding/NewsRadar}"
PREVIEW_DIR="$REPO_DIR/.deploy/preview"
PRODUCTION_DIR="${NEWSRADAR_PRODUCTION_DIR:-/Users/bruce/Coding/NewsRadar-production}"

export PATH="$REPO_DIR/TrendRadar/.venv/bin:$PATH"
# The hourly monitor and podcast worker share one preview build directory.
# Hold the same descriptor lock across preparation and remote deployment.
if [[ "${NEWSRADAR_PREVIEW_DEPLOY_LOCK_HELD:-0}" != "1" ]]; then
  exec python3 - "$0" "$REPO_DIR" <<'PYLOCK'
import fcntl, os, sys
from pathlib import Path
repo = Path(sys.argv[2]).resolve()
path = repo / '.deploy/.preview-deploy.lock'
path.parent.mkdir(parents=True, exist_ok=True)
lock = path.open('a')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('[preview] another deployment is active; retry on the next worker run')
    sys.exit(75)
os.set_inheritable(lock.fileno(), True)
script = str(Path(sys.argv[1]).resolve())
os.execve(script, [script], {**os.environ, 'NEWSRADAR_PREVIEW_DEPLOY_LOCK_HELD': '1'})
PYLOCK
fi
cd "$REPO_DIR"
BRANCH="$(git branch --show-current)"
if [ "$BRANCH" != "staging" ] && [ "${NEWSRADAR_ALLOW_NON_STAGING_PREVIEW:-0}" != "1" ]; then
  echo "[preview] expected branch 'staging', found '$BRANCH'"
  echo "[preview] set NEWSRADAR_ALLOW_NON_STAGING_PREVIEW=1 only for an intentional one-off preview"
  exit 1
fi

SOURCE_DIR="$REPO_DIR/docs"
DATA_REPO="$REPO_DIR"
if [ -f "$PRODUCTION_DIR/docs/index.html" ]; then
  SOURCE_DIR="$PRODUCTION_DIR/docs"
  DATA_REPO="$PRODUCTION_DIR"
  echo "[preview] using current production reports from $PRODUCTION_DIR"
else
  NEWSRADAR_SITE_APPLY_FEATURES=0 NEWSRADAR_SITE_GIT_PUSH=0 "$REPO_DIR/scripts/publish_reports_site.sh"
  echo "[preview] production reports unavailable; using staging reports"
fi
python3 "$REPO_DIR/scripts/prepare_preview_site.py" \
  --repo "$REPO_DIR" \
  --data-repo "$DATA_REPO" \
  --source "$SOURCE_DIR" \
  --destination "$PREVIEW_DIR"
python3 "$REPO_DIR/scripts/filter_report_history.py" --site-dir "$PREVIEW_DIR"

# Preview presentation assets come from staging, while reports and audio come
# from the live collector worktree so feature testing never freezes old news.
mkdir -p "$PREVIEW_DIR/assets/brand"
rsync -a --delete "$REPO_DIR/docs/assets/brand/" "$PREVIEW_DIR/assets/brand/"
cp "$REPO_DIR/docs/assets/report-speech.css" "$PREVIEW_DIR/assets/report-speech.css"
cp "$REPO_DIR/docs/assets/report-speech.js" "$PREVIEW_DIR/assets/report-speech.js"
cp "$REPO_DIR/docs/assets/report-experience.css" "$PREVIEW_DIR/assets/report-experience.css"
cp "$REPO_DIR/docs/assets/report-experience.js" "$PREVIEW_DIR/assets/report-experience.js"
if [ -d "$PRODUCTION_DIR/TrendRadar/output/audio" ]; then
  mkdir -p "$PREVIEW_DIR/audio"
  rsync -a --delete --exclude='.*' --exclude='drafts/' "$PRODUCTION_DIR/TrendRadar/output/audio/" "$PREVIEW_DIR/audio/"
fi
python3 "$REPO_DIR/scripts/generate_site_insights.py" \
  --repo-dir "$DATA_REPO" \
  --site-dir "$PREVIEW_DIR"
python3 "$REPO_DIR/scripts/generate_product_experience.py" \
  --repo-dir "$DATA_REPO" \
  --site-dir "$PREVIEW_DIR"
# Canonical editorial materials are local/read-only builds; no automatic AI calls.
python3 "$REPO_DIR/scripts/build_fact_pack.py"   --repo-dir "$DATA_REPO" --site-dir "$PREVIEW_DIR" --enrich --max-primary 5
if [ -f "$REPO_DIR/.deploy/p1/fact-pack.json" ]; then
  python3 "$REPO_DIR/scripts/merge_preview_materials.py"     --site-dir "$PREVIEW_DIR" --material "$REPO_DIR/.deploy/p1/fact-pack.json"
fi
# Isolated preview audio is never copied back into the production collector.
if [ -d "$REPO_DIR/.deploy/p1/audio" ]; then
  python3 "$REPO_DIR/scripts/sync_preview_audio.py" --source "$REPO_DIR/.deploy/p1/audio" --destination "$PREVIEW_DIR/audio"
fi
python3 "$REPO_DIR/scripts/generate_reports_archive.py" --site-dir "$PREVIEW_DIR" --preview
python3 "$REPO_DIR/scripts/apply_site_features.py" \
  --site-dir "$PREVIEW_DIR" \
  --environment preview \
  --speech "${NEWSRADAR_FEATURE_SPEECH:-}" \
  --trends "${NEWSRADAR_FEATURE_TRENDS:-}" \
  --status "${NEWSRADAR_FEATURE_STATUS:-}" \
  --sources "${NEWSRADAR_FEATURE_SOURCES:-}" \
  --experience "${NEWSRADAR_FEATURE_EXPERIENCE:-}"

python3 "$REPO_DIR/scripts/publish_daily_editions.py" --repo "$DATA_REPO" --site "$PREVIEW_DIR"

# Explicit smoke-test artifact only; never overwrite the live daily report.
if [ -f "$REPO_DIR/.deploy/first-batch/analysis-preview.html" ]; then
  cp "$REPO_DIR/.deploy/first-batch/analysis-preview.html" "$PREVIEW_DIR/analysis-preview.html"
fi

if [ -f "$REPO_DIR/.deploy/p1/analysis-preview.html" ]; then
  cp "$REPO_DIR/.deploy/p1/analysis-preview.html" "$PREVIEW_DIR/analysis-preview.html"
fi

echo "[preview] deploying newsradar-preview"
WRANGLER_BIN="${NEWSRADAR_WRANGLER_BIN:-$REPO_DIR/.deploy/tools/wrangler-4.158.0/node_modules/.bin/wrangler}"
if [[ ! -x "$WRANGLER_BIN" ]]; then
  echo "[preview] Wrangler executable missing: $WRANGLER_BIN" >&2
  exit 1
fi
"$WRANGLER_BIN" deploy --env preview
echo "[preview] deployed: https://preview.newsradar.ccwu.cc"
