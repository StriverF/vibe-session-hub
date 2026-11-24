#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$PROJECT_DIR")"
APP_BUNDLE="$PARENT_DIR/Vibe Session Hub.app"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.vibesessionhub.app"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"
ICLOUD_SYNC_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub"

HUB_PORT="${VIBE_HUB_PORT:-17380}"
if ! [[ "$HUB_PORT" =~ ^[0-9]+$ ]] || [ "$HUB_PORT" -lt 1 ] || [ "$HUB_PORT" -gt 65535 ]; then
  echo "Invalid VIBE_HUB_PORT='$HUB_PORT', falling back to 17380" >&2
  HUB_PORT="17380"
fi

echo "==> [1/4] Building native macOS app: $APP_BUNDLE (port $HUB_PORT)"
bash "$PROJECT_DIR/scripts/package_macos.sh" \
  --app-only \
  --arch native \
  --port "$HUB_PORT" \
  --app-output "$APP_BUNDLE" \
  --dev-project-root "$PROJECT_DIR" \
  --update-web-logo

mkdir -p "$PROJECT_DIR/macos/bin"
cp "$APP_BUNDLE/Contents/MacOS/send_url_to_pid" "$PROJECT_DIR/macos/bin/send_url_to_pid"
chmod +x "$PROJECT_DIR/macos/bin/send_url_to_pid"

echo "==> [2/4] Running initial incremental index & iCloud export"
(cd "$PROJECT_DIR" && /usr/bin/python3 -B -m hub.server --sync-once)

echo "==> [3/4] Installing LaunchAgent background service (127.0.0.1:$HUB_PORT)"
mkdir -p "$LAUNCH_AGENTS_DIR" "$HOME/.vibe-session-hub"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$PLIST_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-B</string>
    <string>-m</string>
    <string>hub.server</string>
    <string>--port</string>
    <string>$HUB_PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>VIBE_HUB_PORT</key>
    <string>$HUB_PORT</string>
    <key>PYTHONDONTWRITEBYTECODE</key>
    <string>1</string>
    <key>VIBE_HUB_SEND_URL_BIN</key>
    <string>$APP_BUNDLE/Contents/MacOS/send_url_to_pid</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>SoftResourceLimits</key>
  <dict>
    <key>NumberOfFiles</key>
    <integer>4096</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>$HOME/.vibe-session-hub/server.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.vibe-session-hub/server.err.log</string>
</dict>
</plist>
EOF

/bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

if [ -d "$HOME/Library/Mobile Documents/com~apple~CloudDocs" ]; then
  echo "==> [4/4] Syncing portable installer to iCloud Drive for your other Mac"
  mkdir -p "$ICLOUD_SYNC_DIR/app-source"
  /usr/bin/rsync -a --delete \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.claude' \
    --exclude 'macos/bin' \
    --exclude 'dist' \
    --exclude '*.dmg' \
    "$PROJECT_DIR/" "$ICLOUD_SYNC_DIR/app-source/"
  cat > "$ICLOUD_SYNC_DIR/install-on-second-mac.sh" <<'BOOTEOF'
#!/usr/bin/env bash
set -euo pipefail
TARGET_BASE="${1:-$HOME/Applications}"
ICLOUD_SRC="$HOME/Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub/app-source"
mkdir -p "$TARGET_BASE/vibe-session-hub"
/usr/bin/rsync -a --delete "$ICLOUD_SRC/" "$TARGET_BASE/vibe-session-hub/"
bash "$TARGET_BASE/vibe-session-hub/install.sh"
open "$TARGET_BASE/Vibe Session Hub.app"
BOOTEOF
  chmod +x "$ICLOUD_SYNC_DIR/install-on-second-mac.sh"
fi

echo ""
echo "Done!"
echo "  Desktop App : $APP_BUNDLE"
echo "  Web URL     : http://127.0.0.1:$HUB_PORT"
