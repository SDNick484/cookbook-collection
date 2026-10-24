#!/bin/sh
# Cookbook Collection installer for Alpine Linux (OpenRC). Build with installer/build.sh.
# Re-runnable: upgrades the app in place; never touches existing data, and only *adds*
# missing settings to existing config files.
#
#   sh setup-cookbooks.sh               install / upgrade
#   sh setup-cookbooks.sh --uninstall   remove app + service (keeps /var/lib/cookbooks and backups)
set -eu

APP_DIR=/opt/cookbooks
DATA_DIR=/var/lib/cookbooks
CONF=/etc/conf.d/cookbooks
OPS_CONF=/etc/conf.d/ct210-ops
PORT=8767
SVC_USER=cookbooks
HA_WEBHOOK_ID=${HA_WEBHOOK_ID:-__HA_WEBHOOK_ID__}  # filled in by build.sh, or set when running

if [ "$(id -u)" != 0 ]; then echo "Run as root"; exit 1; fi

if [ "${1:-}" = "--uninstall" ]; then
  rc-service cookbooks stop 2>/dev/null || true
  rc-update del cookbooks default 2>/dev/null || true
  rm -f /etc/init.d/cookbooks /etc/periodic/daily/ct210-backup /etc/periodic/15min/app-health
  rm -rf "$APP_DIR"
  echo "Removed app, service and scheduled jobs. Data kept in $DATA_DIR, backups in /var/backups/ct210."
  exit 0
fi

echo "==> Installing packages"
apk add --no-cache python3 py3-flask py3-gunicorn py3-pillow py3-jwt py3-cryptography sqlite >/dev/null

if ! python3 -c "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('create virtual table t using fts5(x)')" 2>/dev/null; then
  echo "    note: this sqlite has no FTS5; search will fall back to simple matching"
fi

# an upgrade can clean up data when it first starts, so keep a copy of the database as it was
if [ -f "$DATA_DIR/cookbooks.db" ]; then
  PRE=/var/backups/ct210/cookbooks-before-upgrade-$(date +%Y%m%d-%H%M%S).db
  mkdir -p /var/backups/ct210
  if sqlite3 "$DATA_DIR/cookbooks.db" ".backup '$PRE'" && gzip "$PRE"; then
    echo "==> Saved the database as it was before this upgrade: $PRE.gz"
  else
    echo "    couldn't save a pre-upgrade copy of the database; stopping so nothing changes"; exit 1
  fi
fi

# stop with the *old* init script before replacing it (v1 used start-stop-daemon, v2 supervise-daemon)
[ -x /etc/init.d/cookbooks ] && rc-service cookbooks stop >/dev/null 2>&1 || true

echo "==> Creating user and folders"
id "$SVC_USER" >/dev/null 2>&1 || adduser -S -D -H -h "$DATA_DIR" -s /sbin/nologin "$SVC_USER"
mkdir -p "$APP_DIR" "$DATA_DIR/covers" "$DATA_DIR/photos" /var/lib/ct210-ops /var/backups/ct210
# read-only access to Dinner Log's database (it lives in a group-readable folder)
if grep -q '^dinnerlog:' /etc/group; then
  addgroup "$SVC_USER" dinnerlog 2>/dev/null || true
  chmod g+r /var/lib/dinner-log/*.db 2>/dev/null || true
fi

echo "==> Unpacking app"
TMP=$(mktemp -d)
sed -n '/^__PAYLOAD_BELOW__$/,$p' "$0" | tail -n +2 | base64 -d | tar xz -C "$TMP"
rm -rf "$APP_DIR.new" && mv "$TMP/cookbooks" "$APP_DIR.new"
rm -rf "$APP_DIR.old"; [ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
mv "$APP_DIR.new" "$APP_DIR"; rm -rf "$APP_DIR.old" "$TMP"
chown -R root:root "$APP_DIR"
chown -R "$SVC_USER" "$DATA_DIR"

# ---------------------------------------------------------------- config
add_setting() {  # add_setting FILE KEY VALUE COMMENT — only if KEY isn't there yet
  grep -q "^$2=" "$1" 2>/dev/null && return 0
  printf '\n# %s\n%s=%s\n' "$4" "$2" "$3" >> "$1"
  ADDED="${ADDED:-} $2"
}
if [ ! -f "$CONF" ]; then
  echo "==> Writing $CONF"
  cat > "$CONF" <<EOF
# Cookbook Collection configuration — after editing: rc-service cookbooks restart
PORT=$PORT
DATA_DIR=$DATA_DIR
EOF
fi
add_setting "$CONF" CF_TEAM_DOMAIN "" "Cloudflare Access team domain, e.g. yourteam.cloudflareaccess.com"
add_setting "$CONF" CF_AUD "" "AUD tag of the Cookbooks Access application (path: admin)"
add_setting "$CONF" ADMIN_EMAILS "" "Only these emails may edit (comma separated); empty = anyone the Access policy lets in"
add_setting "$CONF" LAN_EDIT 1 "1 = direct LAN requests (not through Cloudflare) may edit"
add_setting "$CONF" DINNERLOG_DB /var/lib/dinner-log/meals.db "Dinner Log database (read-only) for cooking history"
add_setting "$CONF" ANTHROPIC_API_KEY "" "Claude API key for reading photos of index pages (console.anthropic.com)"
add_setting "$CONF" ANTHROPIC_MODEL claude-sonnet-5-5 "Model used for photo import"
add_setting "$CONF" MCP_TOKEN "$(head -c 24 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=')" \
  "Secret for private MCP tools: https://<your public address>/mcp/<MCP_TOKEN>"
add_setting "$CONF" MCP_PUBLIC 1 "1 = /mcp without a token serves public search (books, recipes, pages)"
add_setting "$CONF" WISHLIST_URL http://127.0.0.1:8766 "Family wish list app (local) for 'Add to a wish list'"
add_setting "$CONF" WISHLIST_BOOK_URL "https://www.amazon.com/s?k={q}" "Shopping link put on wish list items; {q} = ISBN (or title + author)"
add_setting "$CONF" DINNERLOG_URL "" "Dinner Log's public address, e.g. https://dinners.example.com — turns on 'Log this dinner' links"
chmod 640 "$CONF"; chown root:"$SVC_USER" "$CONF"

[ -f "$OPS_CONF" ] || printf '# Backups + health alerts for the apps on this server (scripts in /usr/local/bin)\n' > "$OPS_CONF"
add_setting "$OPS_CONF" HA_URL "" "Home Assistant LAN address, e.g. http://192.168.1.50:8123 (alerts are off until set)"
add_setting "$OPS_CONF" HA_WEBHOOK_ID "$HA_WEBHOOK_ID" "Webhook ID of the Home Assistant automation that receives app health alerts"
add_setting "$OPS_CONF" BACKUP_TARGET "" "Optional folder on other storage (e.g. an OMV NFS mount) that also gets photos/covers"
add_setting "$OPS_CONF" KEEP_DAYS 14 "Days of nightly database snapshots to keep"
chmod 600 "$OPS_CONF"

# ---------------------------------------------------------------- service
cat > /etc/init.d/cookbooks <<'EOF'
#!/sbin/openrc-run
name="cookbooks"
description="Cookbook Collection"
: ${PORT:=8767}
: ${DATA_DIR:=/var/lib/cookbooks}
export DATA_DIR CF_TEAM_DOMAIN CF_AUD ADMIN_EMAILS LAN_EDIT DINNERLOG_DB ANTHROPIC_API_KEY ANTHROPIC_MODEL ANTHROPIC_EFFORT MCP_TOKEN MCP_PUBLIC WISHLIST_URL WISHLIST_BOOK_URL DINNERLOG_URL
supervisor="supervise-daemon"
respawn_delay=5
respawn_max=10
respawn_period=600
command="/usr/bin/gunicorn"
command_args="--preload --workers 2 --threads 4 --timeout 180 --bind 0.0.0.0:${PORT} --chdir /opt/cookbooks --access-logfile /var/log/cookbooks.log --error-logfile /var/log/cookbooks.log app:app"
command_user="cookbooks"
depend() { need net; after firewall; }
start_pre() { checkpath -f -o cookbooks /var/log/cookbooks.log; }
EOF
chmod +x /etc/init.d/cookbooks

# ---------------------------------------------------------------- ops jobs
echo "==> Installing nightly backups and health checks"
install -m 755 "$APP_DIR/ops/ct210-backup" "$APP_DIR/ops/ct210-notify" /usr/local/bin/
mkdir -p /etc/periodic/daily /etc/periodic/15min
ln -sf /usr/local/bin/ct210-backup /etc/periodic/daily/ct210-backup
install -m 755 "$APP_DIR/ops/app-health" /etc/periodic/15min/app-health
rc-update add crond default >/dev/null 2>&1 || true
rc-service crond start >/dev/null 2>&1 || true

echo "==> Starting service"
rc-update add cookbooks default >/dev/null 2>&1 || true
rc-service cookbooks start

i=0
until wget -qO- "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do
  i=$((i+1)); [ $i -gt 25 ] && { echo "Service didn't come up; see /var/log/cookbooks.log"; tail -n 20 /var/log/cookbooks.log; exit 1; }
  sleep 1
done

echo "==> Taking a first backup"
/usr/local/bin/ct210-backup || echo "    (backup reported a problem; see /var/log/ct210-ops.log)"

IP=$(ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1 | head -n1)
echo
echo "Cookbook Collection is running:  http://$IP:$PORT/        (editing on the LAN: http://$IP:$PORT/admin)"
echo "Health: $(wget -qO- http://127.0.0.1:$PORT/healthz)"
DL=$(wget -qO- "http://127.0.0.1:$PORT/admin/api/dinnerlog" 2>/dev/null | sed -n 's/.*"available": *\(true\|false\).*/\1/p' | head -n1)
echo "Dinner Log link: ${DL:-unknown}"
[ -n "${ADDED:-}" ] && echo "New settings added:${ADDED}"
grep -q '^ANTHROPIC_API_KEY=$' "$CONF" && echo "  - Photo import is off until you set ANTHROPIC_API_KEY in $CONF"
grep -q '^HA_URL=$' "$OPS_CONF" && echo "  - Health alerts are off until you set HA_URL in $OPS_CONF"
grep -q "^HA_WEBHOOK_ID=$" "$OPS_CONF" && echo "  - Health alerts also need HA_WEBHOOK_ID in $OPS_CONF (the HA automation's webhook)"
grep -q '^DINNERLOG_URL=$' "$CONF" && echo "  - 'Log this dinner' links are off until you set DINNERLOG_URL in $CONF"
echo "  - Private MCP URL: https://<your public address>/mcp/$(sed -n 's/^MCP_TOKEN=//p' "$CONF")"
echo "Config: $CONF, $OPS_CONF   Backups: /var/backups/ct210   Logs: /var/log/cookbooks.log, /var/log/ct210-ops.log"
exit 0
