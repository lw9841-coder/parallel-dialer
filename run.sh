#!/usr/bin/env bash
# One command to dial: opens the tunnel, writes PUBLIC_URL into .env, starts
# the server. Ctrl-C tears both down.
#
#   ./run.sh
#
# Free Cloudflare quick tunnels hand out a new hostname every run, which is the
# only real friction in laptop-hosted mode. This removes it.

set -euo pipefail
cd "$(dirname "$0")"

VENV="./venv/bin/python"
LOG="$(mktemp -t dialer-tunnel)"

if [[ ! -x "$VENV" ]]; then
  echo "No venv. Run:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not installed. Run:  brew install cloudflared"
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "No .env. Run:  cp .env.example .env   then fill it in."
  exit 1
fi

PORT="$(grep -E '^PORT=' .env | cut -d= -f2 | tr -d ' \r' || true)"
PORT="${PORT:-5111}"

cleanup() {
  [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  rm -f "$LOG"
}
trap cleanup EXIT INT TERM

echo "Opening tunnel..."
cloudflared tunnel --url "http://localhost:${PORT}" >"$LOG" 2>&1 &
TUNNEL_PID=$!

URL=""
for _ in $(seq 1 30); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)"
  [[ -n "$URL" ]] && break
  if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "cloudflared died:"; cat "$LOG"; exit 1
  fi
  sleep 1
done

if [[ -z "$URL" ]]; then
  echo "Tunnel never came up. Last output:"; tail -20 "$LOG"; exit 1
fi

# Rewrite PUBLIC_URL in place, keeping everything else in .env untouched.
if grep -qE '^PUBLIC_URL=' .env; then
  /usr/bin/sed -i '' -E "s|^PUBLIC_URL=.*|PUBLIC_URL=${URL}|" .env
else
  printf '\nPUBLIC_URL=%s\n' "$URL" >> .env
fi

echo "Tunnel:  $URL"
echo "Dialer:  http://localhost:${PORT}"
echo
# caffeinate: a laptop that sleeps mid-session drops you and every live line.
exec caffeinate -i "$VENV" app.py
