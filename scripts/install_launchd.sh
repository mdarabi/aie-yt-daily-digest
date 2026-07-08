#!/usr/bin/env bash
# Install (or update) the daily LaunchAgent for the digest (default 6:00 local).
# Idempotent: re-running regenerates the plist and reloads it.
# Usage: scripts/install_launchd.sh [hour] [minute]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.mdarabi.aie-yt-daily-digest"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR="${1:-6}"
MINUTE="${2:-0}"

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

AGENT_PATH="$(dirname "$UV_BIN"):/usr/local/bin:/usr/bin:/bin"
if [[ -n "$CLAUDE_BIN" ]]; then
  AGENT_PATH="$(dirname "$CLAUDE_BIN"):$AGENT_PATH"
fi

mkdir -p "$REPO/logs" "$REPO/state" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_BIN</string>
        <string>run</string>
        <string>aie-digest</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$AGENT_PATH</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$HOUR</integer>
        <key>Minute</key>
        <integer>$MINUTE</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$REPO/logs/cron.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO/logs/cron.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# Mutual exclusion: remove any cron variant of the schedule (best effort — crontab
# writes need Full Disk Access on macOS).
MARKER="# aie-yt-daily-digest"
if crontab -l 2>/dev/null | grep -qF "$MARKER"; then
  if (crontab -l 2>/dev/null | grep -vF "$MARKER") | crontab - 2>/dev/null; then
    echo "Removed the crontab entry (schedule now handled by launchd only)."
  else
    echo "warning: could not remove the existing crontab entry — run 'crontab -e'" \
         "and delete the aie-yt-daily-digest line to avoid double runs" >&2
  fi
fi

printf 'Installed LaunchAgent: %s (daily at %d:%02d)\n' "$PLIST" "$HOUR" "$MINUTE"
echo "Check with: launchctl print gui/$(id -u)/$LABEL | grep state"
