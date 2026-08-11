#!/bin/bash
# setup.sh — set up (or re-point) the email-to-WordPress-drafts schedule.
#
# This script figures out its OWN location, so you can run it from wherever
# the project folder lives and the launchd schedule will point there correctly.
# Safe to re-run any time you move the folder.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh

set -e

# The project folder is wherever THIS script lives.
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_NAME="com.johnnie.emailtowp.plist"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST_DST="$LAUNCH_DIR/$PLIST_NAME"

echo "==> Project folder: $APP_DIR"

# Ensure logs/ exists before launchd writes to it.
mkdir -p "$APP_DIR/logs"

# --- venv ---
if [ ! -d "$APP_DIR/venv" ]; then
    echo "==> Creating virtual environment"
    python3 -m venv "$APP_DIR/venv"
fi
echo "==> Installing dependencies"
source "$APP_DIR/venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$APP_DIR/requirements.txt"

# --- runtime files sanity check ---
missing=""
for f in .env credentials.json; do
    [ -f "$APP_DIR/$f" ] || missing="$missing $f"
done
if [ -n "$missing" ]; then
    echo "!! WARNING: missing runtime file(s):$missing"
    echo "   The schedule won't work until these are in $APP_DIR"
    echo "   (.env from .env.example; credentials.json from Google Cloud.)"
fi

# --- write the launchd plist, pointing at THIS folder ---
echo "==> Writing schedule to $PLIST_DST (7am & 7pm daily)"
mkdir -p "$LAUNCH_DIR"
cat > "$PLIST_DST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.johnnie.emailtowp</string>
    <key>ProgramArguments</key>
    <array>
        <string>$APP_DIR/venv/bin/python3</string>
        <string>$APP_DIR/email_to_wp_drafts.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$APP_DIR</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Hour</key><integer>19</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$APP_DIR/logs/run.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/logs/error.log</string>
</dict>
</plist>
PLIST

# --- (re)load ---
echo "==> Reloading schedule"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo ""
echo "Done. Verify with:"
echo "  launchctl list | grep emailtowp        # 0 or - in middle column = ok"
echo "  cd \"$APP_DIR\" && source venv/bin/activate && python3 email_to_wp_drafts.py --dry-run"
echo ""
echo "Logs: $APP_DIR/logs/run.log  and  logs/error.log"
echo "Stop the schedule: launchctl unload $PLIST_DST"
