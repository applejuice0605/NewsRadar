#!/bin/zsh
set -eu
DATA_REPO="${1:?collector repository required}"
PREVIEW_REPO="${NEWSRADAR_PREVIEW_REPO:-/Users/bruce/Coding/NewsRadar}"
WRANGLER="${NEWSRADAR_WRANGLER_BIN:-$PREVIEW_REPO/.deploy/tools/wrangler-4.158.0/node_modules/.bin/wrangler}"
cd "$PREVIEW_REPO"
# Publish the collector's completion timestamp, never the upload time.
value=$(python3 - "$DATA_REPO/TrendRadar/output/meta/runtime_health.json" <<'PY'
import json, sys
from pathlib import Path
collection = json.loads(Path(sys.argv[1]).read_text())["collection"]
assert type(collection["success"]) is bool and collection["last_completed_at"]
print(json.dumps({key: collection[key] for key in ("last_completed_at", "success")}))
PY
)
"$WRANGLER" kv key put collection-health:v1 "$value" --binding HEALTH_MONITOR_STATE --env preview --remote
