#!/bin/bash
# Starts the local monitor dashboard and exposes it publicly via a
# Cloudflare Quick Tunnel. Prints the share-able URL for friends.
#
# Usage:  ./share_dashboard.sh
# Stop:   Ctrl+C (kills both monitor and tunnel)

set -e

cd "$(dirname "$0")"

# Kill any existing monitor/tunnel on exit
cleanup() {
  echo ""
  echo "Shutting down..."
  kill $MONITOR_PID 2>/dev/null || true
  kill $TUNNEL_PID 2>/dev/null || true
  rm -f /tmp/golf-tunnel.log
  exit 0
}
trap cleanup INT TERM EXIT

# 1. Start monitor if not already running on port 8111
if lsof -ti :8111 > /dev/null 2>&1; then
  echo "Monitor already running on port 8111 — reusing"
  MONITOR_PID=""
else
  echo "Starting monitor..."
  /usr/bin/python3 monitor.py > /tmp/golf-monitor.log 2>&1 &
  MONITOR_PID=$!
  sleep 2
  if ! lsof -ti :8111 > /dev/null 2>&1; then
    echo "Monitor failed to start. Log:"
    cat /tmp/golf-monitor.log
    exit 1
  fi
fi

# 2. Start cloudflared quick tunnel
echo "Starting Cloudflare tunnel (may take 10-20s to stabilize)..."
cloudflared tunnel --url http://localhost:8111 > /tmp/golf-tunnel.log 2>&1 &
TUNNEL_PID=$!

# Wait for URL to appear AND for the tunnel to register at least one connection.
# cloudflared emits some transient "control stream failure" errors while it
# negotiates the initial handshake — normal. We wait for the "Registered
# tunnel connection" marker before declaring the URL ready, so curls issued
# immediately after the script prints don't bounce.
URL=""
for i in {1..60}; do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/golf-tunnel.log 2>/dev/null | head -1 || true)
  if [ -n "$URL" ] && grep -q "Registered tunnel connection" /tmp/golf-tunnel.log 2>/dev/null; then
    break
  fi
  sleep 0.5
done

if [ -z "$URL" ]; then
  echo "Tunnel didn't produce a URL within 30s. Full log:"
  cat /tmp/golf-tunnel.log
  exit 1
fi

if ! grep -q "Registered tunnel connection" /tmp/golf-tunnel.log 2>/dev/null; then
  echo "WARNING: tunnel URL printed but connection hasn't registered yet."
  echo "The URL may not work for another 10-20s. If you see errors, retry shortly."
fi

echo ""
echo "================================================================"
echo "  Austin Tee Time Dashboard is live."
echo ""
echo "  Share this URL with friends:"
echo "    $URL"
echo ""
echo "  (Valid until you stop this script. New URL each run.)"
echo "================================================================"
echo ""
echo "Press Ctrl+C to stop."

# Wait on tunnel process
wait $TUNNEL_PID
