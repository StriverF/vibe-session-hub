#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARENT_DIR="$(dirname "$PROJECT_DIR")"
APP_BUNDLE="$PARENT_DIR/Vibe Session Hub.app"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.vibesessionhub.app"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$PLIST_LABEL.plist"
ICLOUD_SYNC_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/VibeSessionHub"

echo "==> [1/4] Building native macOS app: $APP_BUNDLE"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources" "$PROJECT_DIR/macos/bin"

/usr/bin/swiftc -O -framework AppKit -framework CoreGraphics "$PROJECT_DIR/macos/build_icon.swift" -o "$PROJECT_DIR/macos/bin/build_icon"

TMP_ICON_ROOT="$(mktemp -d)"
"$PROJECT_DIR/macos/bin/build_icon" \
  "$PROJECT_DIR/static/icons/logo-master.png" \
  "$TMP_ICON_ROOT/AppIcon.iconset" \
  "$PROJECT_DIR/static/icons/logo.png"
/usr/bin/iconutil -c icns "$TMP_ICON_ROOT/AppIcon.iconset" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
rm -rf "$TMP_ICON_ROOT"

/usr/bin/swiftc \
  -O \
  -framework AppKit \
  -framework CoreServices \
  "$PROJECT_DIR/macos/send_url_to_pid.swift" \
  -o "$PROJECT_DIR/macos/bin/send_url_to_pid"
cp "$PROJECT_DIR/macos/bin/send_url_to_pid" "$APP_BUNDLE/Contents/MacOS/send_url_to_pid"

/usr/bin/swiftc \
  -O \
  -framework AppKit \
  -framework WebKit \
  "$PROJECT_DIR/macos/VibeSessionHub.swift" \
  -o "$APP_BUNDLE/Contents/MacOS/VibeSessionHub"

printf '%s\n' "$PROJECT_DIR" > "$APP_BUNDLE/Contents/Resources/project_root.txt"

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key>
  <string>Vibe Session Hub</string>
  <key>CFBundleName</key>
  <string>Vibe Session Hub</string>
  <key>CFBundleIdentifier</key>
  <string>$PLIST_LABEL</string>
  <key>CFBundleVersion</key>
  <string>1.0.0</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>VibeSessionHub</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
  <key>CFBundleURLTypes</key>
  <array>
    <dict>
      <key>CFBundleURLName</key>
      <string>Vibe Session Hub Jump URL</string>
      <key>CFBundleURLSchemes</key>
      <array>
        <string>vibehub</string>
      </array>
    </dict>
  </array>
</dict>
</plist>
EOF

/usr/bin/codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null 2>&1 || true

echo "==> [2/4] Running initial incremental index & iCloud export"
/usr/bin/python3 -m hub.server --sync-once --cwd "$PROJECT_DIR" 2>/dev/null || (cd "$PROJECT_DIR" && /usr/bin/python3 -m hub.server --sync-once)

echo "==> [3/4] Installing LaunchAgent background service (127.0.0.1:17380)"
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
    <string>-m</string>
    <string>hub.server</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$PROJECT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
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
  /usr/bin/rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' --exclude '.claude' --exclude 'macos/bin' "$PROJECT_DIR/" "$ICLOUD_SYNC_DIR/app-source/"
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
echo "  Web URL     : http://127.0.0.1:17380"
