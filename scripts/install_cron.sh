#!/usr/bin/env bash
# Install (or update) the daily 6:00 crontab entry for the digest.
# Idempotent: re-running replaces the existing entry (identified by the marker).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# aie-yt-daily-digest"
SCHEDULE="${1:-0 6 * * *}"

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
  UV_BIN="$HOME/.local/bin/uv"
fi
if [[ -z "$UV_BIN" ]]; then
  echo "error: uv not found (install from https://docs.astral.sh/uv/)" >&2
  exit 1
fi

CLAUDE_BIN="$(command -v claude || true)"
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "warning: claude CLI not found on PATH — set CLAUDE_BIN in $REPO/.env" >&2
fi

# cron runs with a bare PATH; bake in the directories that hold uv and claude.
CRON_PATH="$(dirname "$UV_BIN"):/usr/local/bin:/usr/bin:/bin"
if [[ -n "$CLAUDE_BIN" ]]; then
  CRON_PATH="$(dirname "$CLAUDE_BIN"):$CRON_PATH"
fi

mkdir -p "$REPO/logs" "$REPO/state"

LINE="$SCHEDULE cd $REPO && PATH=$CRON_PATH $UV_BIN run aie-digest >> $REPO/logs/cron.log 2>&1 $MARKER"

( crontab -l 2>/dev/null | grep -vF "$MARKER" || true; echo "$LINE" ) | crontab -

# Mutual exclusion: remove the LaunchAgent variant so the digest never runs twice.
PLIST="$HOME/Library/LaunchAgents/com.mdarabi.aie-yt-daily-digest.plist"
if [[ -f "$PLIST" ]]; then
  launchctl bootout "gui/$(id -u)/com.mdarabi.aie-yt-daily-digest" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed the LaunchAgent (schedule now handled by cron only)."
fi

echo "Installed crontab entry:"
echo "  $LINE"
echo
echo "Check with: crontab -l"
