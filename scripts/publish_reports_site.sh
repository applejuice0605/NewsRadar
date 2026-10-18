#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
REPO_DIR="${REPO_DIR:-${SCRIPT_DIR:h}}"
PROJECT_DIR="$REPO_DIR/TrendRadar"
SITE_DIR="$REPO_DIR/docs"
DOMAIN="${NEWSRADAR_SITE_DOMAIN:-newsradar.ccwu.cc}"
GIT_PUSH="${NEWSRADAR_SITE_GIT_PUSH:-1}"
SITE_ENV="${NEWSRADAR_SITE_ENV:-production}"
APPLY_FEATURES="${NEWSRADAR_SITE_APPLY_FEATURES:-1}"

INDEX_FILE="$PROJECT_DIR/output/index.html"
HTML_DIR="$PROJECT_DIR/output/html"
AUDIO_DIR="$PROJECT_DIR/output/audio"

if [ ! -f "$INDEX_FILE" ]; then
  echo "[site] missing report index: $INDEX_FILE"
  exit 0
fi

mkdir -p "$SITE_DIR/reports"

cp "$INDEX_FILE" "$SITE_DIR/index.html"
rsync -a --exclude='*/notification_test_*.html' "$HTML_DIR/" "$SITE_DIR/reports/"
if [ -d "$AUDIO_DIR" ]; then
  mkdir -p "$SITE_DIR/audio"
  rsync -a "$AUDIO_DIR/" "$SITE_DIR/audio/"
fi
printf "%s\n" "$DOMAIN" > "$SITE_DIR/CNAME"
touch "$SITE_DIR/.nojekyll"
python3 "$REPO_DIR/scripts/generate_reports_archive.py"
if [ -f "$REPO_DIR/scripts/generate_site_insights.py" ]; then
  python3 "$REPO_DIR/scripts/generate_site_insights.py"
fi
if [ -f "$REPO_DIR/scripts/generate_product_experience.py" ]; then
  python3 "$REPO_DIR/scripts/generate_product_experience.py"
fi
if [ "$APPLY_FEATURES" = "1" ]; then
  python3 "$REPO_DIR/scripts/apply_site_features.py" \
    --site-dir "$SITE_DIR" \
    --environment "$SITE_ENV" \
    --speech "${NEWSRADAR_FEATURE_SPEECH:-}" \
    --trends "${NEWSRADAR_FEATURE_TRENDS:-}" \
    --status "${NEWSRADAR_FEATURE_STATUS:-}" \
    --sources "${NEWSRADAR_FEATURE_SOURCES:-}" \
    --experience "${NEWSRADAR_FEATURE_EXPERIENCE:-}"
fi

echo "[site] reports synced to $SITE_DIR"
echo "[site] custom domain: $DOMAIN"

if [ "$GIT_PUSH" != "1" ]; then
  exit 0
fi

cd "$REPO_DIR"

CURRENT_BRANCH="$(git branch --show-current)"
if [ -z "$CURRENT_BRANCH" ]; then
  echo "[site] refusing to publish from a detached HEAD"
  exit 1
fi

git add -A docs TrendRadar/index.html

if git diff --cached --quiet; then
  echo "[site] no report changes to publish"
  exit 0
fi

git commit -m "docs: publish latest TrendRadar report"
git push origin "$CURRENT_BRANCH"
