#!/bin/sh
# Install OpenRC stubs (rc-service, rc-update, rc-status) for containerized CI
set -eu

mkdir -p /usr/local/bin

cat <<'EOF' > /usr/local/bin/rc-update
#!/bin/sh
# Stub rc-update for container environments
exit 0
EOF
chmod +x /usr/local/bin/rc-update

cat <<'EOF' > /usr/local/bin/rc-status
#!/bin/sh
# Stub rc-status for container environments
exit 0
EOF
chmod +x /usr/local/bin/rc-status

cat <<'EOF' > /usr/local/bin/rc-service
#!/bin/sh
# Stub rc-service for container environments
set -eu

SVC="${1:-}"
ACTION="${2:-}"

if [ "$SVC" = "cookbooks" ]; then
  case "$ACTION" in
    start)
      if [ -f /etc/conf.d/cookbooks ]; then
        . /etc/conf.d/cookbooks
      fi
      PORT="${PORT:-8767}"
      BIND="${BIND:-0.0.0.0}"
      DATA_DIR="${DATA_DIR:-/var/lib/cookbooks}"
      GUNICORN="$(command -v gunicorn || echo /usr/bin/gunicorn)"

      touch /var/log/cookbooks.log
      chown cookbooks /var/log/cookbooks.log 2>/dev/null || true

      pkill -u cookbooks gunicorn 2>/dev/null || true
      sleep 1

      su -s /bin/sh cookbooks -c "
        export PORT='$PORT'
        export BIND='$BIND'
        export DATA_DIR='$DATA_DIR'
        export CF_TEAM_DOMAIN='${CF_TEAM_DOMAIN:-}'
        export CF_AUD='${CF_AUD:-}'
        export ADMIN_EMAILS='${ADMIN_EMAILS:-}'
        export LAN_EDIT='${LAN_EDIT:-0}'
        export LAN_CIDRS='${LAN_CIDRS:-}'
        export LAN_HOSTS='${LAN_HOSTS:-}'
        export ALLOWED_ORIGINS='${ALLOWED_ORIGINS:-}'
        export DINNERLOG_DB='${DINNERLOG_DB:-/var/lib/dinner-log/meals.db}'
        export ANTHROPIC_API_KEY='${ANTHROPIC_API_KEY:-}'
        export ANTHROPIC_MODEL='${ANTHROPIC_MODEL:-claude-sonnet-5-5}'
        export ANTHROPIC_EFFORT='${ANTHROPIC_EFFORT:-low}'
        export MCP_TOKEN='${MCP_TOKEN:-}'
        export MCP_PUBLIC='${MCP_PUBLIC:-1}'
        export WISHLIST_URL='${WISHLIST_URL:-http://127.0.0.1:8766}'
        export WISHLIST_BOOK_URL='${WISHLIST_BOOK_URL:-https://www.amazon.com/s?k={q}}'
        export DINNERLOG_URL='${DINNERLOG_URL:-}'
        export READ_TOKEN='${READ_TOKEN:-}'
        nohup $GUNICORN --preload --workers 2 --threads 4 --timeout 180 --bind ${BIND}:${PORT} --chdir /opt/cookbooks --access-logfile /var/log/cookbooks.log --error-logfile /var/log/cookbooks.log app:app >/dev/null 2>&1 &
      "
      ;;
    stop)
      pkill -u cookbooks gunicorn 2>/dev/null || true
      sleep 1
      ;;
    status)
      pgrep -u cookbooks gunicorn >/dev/null 2>&1
      ;;
    restart)
      "$0" cookbooks stop
      sleep 1
      "$0" cookbooks start
      ;;
    *)
      exit 0
      ;;
  esac
  exit 0
fi

# Fallback for other services (crond, etc.)
exit 0
EOF
chmod +x /usr/local/bin/rc-service

echo "OpenRC stubs installed in /usr/local/bin"
