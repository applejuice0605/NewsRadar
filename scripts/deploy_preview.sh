#!/bin/zsh
set -eu

REPO_DIR="${REPO_DIR:-/Users/bruce/Coding/NewsRadar}"
PREVIEW_DIR="$REPO_DIR/.deploy/preview"
PRODUCTION_DIR="${NEWSRADAR_PRODUCTION_DIR:-/Users/bruce/Coding/NewsRadar-production}"

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
  rsync -a --delete "$PRODUCTION_DIR/TrendRadar/output/audio/" "$PREVIEW_DIR/audio/"
fi
python3 "$REPO_DIR/scripts/generate_site_insights.py" \
  --repo-dir "$DATA_REPO" \
  --site-dir "$PREVIEW_DIR"
python3 "$REPO_DIR/scripts/generate_product_experience.py" \
  --repo-dir "$DATA_REPO" \
  --site-dir "$PREVIEW_DIR"
python3 "$REPO_DIR/scripts/apply_site_features.py" \
  --site-dir "$PREVIEW_DIR" \
  --environment preview \
  --speech "${NEWSRADAR_FEATURE_SPEECH:-}" \
  --trends "${NEWSRADAR_FEATURE_TRENDS:-}" \
  --status "${NEWSRADAR_FEATURE_STATUS:-}" \
  --sources "${NEWSRADAR_FEATURE_SOURCES:-}" \
  --experience "${NEWSRADAR_FEATURE_EXPERIENCE:-}"

echo "[preview] deploying newsradar-preview"
npx wrangler deploy --env preview
echo "[preview] deployed: https://preview.newsradar.ccwu.cc"
