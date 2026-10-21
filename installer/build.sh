#!/bin/sh
# Build the one-file installer: dist/setup-cookbooks.sh (shell header + base64 tarball of the app).
# The Home Assistant webhook ID for health alerts isn't kept in the repo; pass it in if you want it baked in:
#   HA_WEBHOOK_ID=ct210-app-health-… sh installer/build.sh
set -eu
cd "$(dirname "$0")/.."
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p dist "$STAGE/cookbooks"
cp -r app.py extras.py eyb.py ingredients.py seed.json README.md ops static tests "$STAGE/cookbooks/"
find "$STAGE" \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
OUT=dist/setup-cookbooks.sh
{
  sed "s/__HA_WEBHOOK_ID__/${HA_WEBHOOK_ID:-}/" installer/setup-head.sh
  echo "__PAYLOAD_BELOW__"
  tar --owner=0 --group=0 -C "$STAGE" -czf - cookbooks | base64
} > "$OUT"
chmod +x "$OUT"
sh -n "$OUT"
echo "Built $OUT"
