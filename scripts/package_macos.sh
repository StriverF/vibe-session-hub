#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Vibe Session Hub"
BUNDLE_ID="com.vibesessionhub.app"
MIN_MACOS="13.0"

DEFAULT_VERSION="1.0.0"
if [ -f "$PROJECT_DIR/VERSION" ]; then
  RAW_VER="$(tr -d '[:space:]' < "$PROJECT_DIR/VERSION")"
  if [ -n "$RAW_VER" ]; then
    DEFAULT_VERSION="${RAW_VER#v}"
  fi
fi

VERSION="$DEFAULT_VERSION"
OUTPUT_DIR="$PROJECT_DIR/dist"
ARCH="universal"
HUB_PORT="${VIBE_HUB_PORT:-17380}"
APP_ONLY=0
APP_OUTPUT=""
DEV_PROJECT_ROOT=""
UPDATE_WEB_LOGO=0
SIGN_IDENTITY="${MACOS_SIGN_IDENTITY:--}"
KEYCHAIN_PATH="${MACOS_KEYCHAIN_PATH:-}"
ENTITLEMENTS_PLIST="$PROJECT_DIR/macos/entitlements.plist"

usage() {
  cat <<'EOF'
Usage: scripts/package_macos.sh [options]

Build a standalone macOS Vibe Session Hub.app bundle and optional .dmg installer.

Options:
  --version <X.Y.Z>         Release version string (default: VERSION file)
  --output-dir <dir>        Directory for .app / .dmg / .sha256 output (default: ./dist)
  --app-output <path>       Explicit destination path for Vibe Session Hub.app
  --arch <mode>             universal | arm64 | x86_64 | native (default: universal)
  --port <1-65535>          Default Hub port stored in Info.plist (default: VIBE_HUB_PORT or 17380)
  --app-only                Build Vibe Session Hub.app only (skip .dmg creation)
  --dev-project-root <dir>  Write project_root.txt for local checkout dev mode (used by install.sh)
  --update-web-logo         Also regenerate static/icons/logo.png from logo-master.png
  --sign-identity <id>      codesign identity (default: "-" ad-hoc, or MACOS_SIGN_IDENTITY env)
  --keychain <path>         Optional keychain path for Developer ID codesign (or MACOS_KEYCHAIN_PATH env)
  --entitlements <path>     Entitlements plist for signing (default: macos/entitlements.plist)
  -h, --help                Show this help message
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version)
      VERSION="${2#v}"
      shift 2
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --app-output)
      APP_OUTPUT="$2"
      shift 2
      ;;
    --arch)
      ARCH="$2"
      shift 2
      ;;
    --port)
      HUB_PORT="$2"
      shift 2
      ;;
    --app-only)
      APP_ONLY=1
      shift
      ;;
    --dev-project-root)
      DEV_PROJECT_ROOT="$2"
      shift 2
      ;;
    --update-web-logo)
      UPDATE_WEB_LOGO=1
      shift
      ;;
    --sign-identity)
      SIGN_IDENTITY="$2"
      shift 2
      ;;
    --keychain)
      KEYCHAIN_PATH="$2"
      shift 2
      ;;
    --entitlements)
      ENTITLEMENTS_PLIST="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown argument '$1'" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$HUB_PORT" =~ ^[0-9]+$ ]] || [ "$HUB_PORT" -lt 1 ] || [ "$HUB_PORT" -gt 65535 ]; then
  echo "Error: invalid port '$HUB_PORT' (must be 1..65535)" >&2
  exit 2
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][A-Za-z0-9._-]+)?$ ]]; then
  echo "Error: invalid version '$VERSION' (expected semver like 1.0.0 or 1.2.0-rc.1)" >&2
  exit 2
fi

if [ ! -f "$ENTITLEMENTS_PLIST" ]; then
  echo "Error: entitlements plist not found at '$ENTITLEMENTS_PLIST'" >&2
  exit 2
fi

KEYCHAIN_ARGS=()
if [ -n "$KEYCHAIN_PATH" ]; then
  KEYCHAIN_ARGS+=(--keychain "$KEYCHAIN_PATH")
fi

# CFBundleVersion prefers dot-separated numeric components (strip prerelease suffix if any)
BUNDLE_VERSION="${VERSION%%-*}"

case "$ARCH" in
  universal|arm64|x86_64|native) ;;
  *)
    echo "Error: invalid --arch '$ARCH' (expected universal|arm64|x86_64|native)" >&2
    exit 2
    ;;
esac

if [ "$ARCH" = "native" ]; then
  HOST_ARCH="$(uname -m)"
  ARCH_LABEL="$HOST_ARCH"
else
  ARCH_LABEL="$ARCH"
fi

mkdir -p "$OUTPUT_DIR"
if [ -z "$APP_OUTPUT" ]; then
  APP_BUNDLE="$OUTPUT_DIR/$APP_NAME.app"
else
  APP_BUNDLE="$APP_OUTPUT"
fi

WORK_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

compile_swift_binary() {
  local out="$1"
  shift

  if [ "$ARCH" = "universal" ]; then
    local out_arm="$WORK_DIR/$(basename "$out").arm64"
    local out_x86="$WORK_DIR/$(basename "$out").x86_64"
    /usr/bin/swiftc -O -target "arm64-apple-macos${MIN_MACOS}" "$@" -o "$out_arm"
    /usr/bin/swiftc -O -target "x86_64-apple-macos${MIN_MACOS}" "$@" -o "$out_x86"
    /usr/bin/lipo -create "$out_arm" "$out_x86" -output "$out"
  elif [ "$ARCH" = "arm64" ]; then
    /usr/bin/swiftc -O -target "arm64-apple-macos${MIN_MACOS}" "$@" -o "$out"
  elif [ "$ARCH" = "x86_64" ]; then
    /usr/bin/swiftc -O -target "x86_64-apple-macos${MIN_MACOS}" "$@" -o "$out"
  else
    /usr/bin/swiftc -O "$@" -o "$out"
  fi
}

echo "==> [1/4] Building AppIcon.icns for v${VERSION} (${ARCH_LABEL})"
BUILD_ICON_BIN="$WORK_DIR/build_icon"
/usr/bin/swiftc -O -framework AppKit -framework CoreGraphics "$PROJECT_DIR/macos/build_icon.swift" -o "$BUILD_ICON_BIN"

ICONSET_DIR="$WORK_DIR/AppIcon.iconset"
ICNS_PATH="$WORK_DIR/AppIcon.icns"
if [ "$UPDATE_WEB_LOGO" -eq 1 ]; then
  "$BUILD_ICON_BIN" "$PROJECT_DIR/static/icons/logo-master.png" "$ICONSET_DIR" "$PROJECT_DIR/static/icons/logo.png"
else
  "$BUILD_ICON_BIN" "$PROJECT_DIR/static/icons/logo-master.png" "$ICONSET_DIR"
fi
/usr/bin/iconutil -c icns "$ICONSET_DIR" -o "$ICNS_PATH"

echo "==> [2/4] Compiling macOS native binaries (${ARCH_LABEL}, min macOS ${MIN_MACOS})"
SEND_URL_BIN="$WORK_DIR/send_url_to_pid"
APP_MAIN_BIN="$WORK_DIR/VibeSessionHub"

compile_swift_binary "$SEND_URL_BIN" -framework AppKit -framework CoreServices "$PROJECT_DIR/macos/send_url_to_pid.swift"
compile_swift_binary "$APP_MAIN_BIN" -framework AppKit -framework WebKit "$PROJECT_DIR/macos/BackendUpgradeLogic.swift" "$PROJECT_DIR/macos/AppUpdater.swift" "$PROJECT_DIR/macos/VibeSessionHub.swift"

echo "==> [3/4] Assembling standalone app bundle: $APP_BUNDLE"
rm -rf "$APP_BUNDLE"
mkdir -p \
  "$APP_BUNDLE/Contents/MacOS" \
  "$APP_BUNDLE/Contents/Resources/app/hub" \
  "$APP_BUNDLE/Contents/Resources/app/static"

cp "$APP_MAIN_BIN" "$APP_BUNDLE/Contents/MacOS/VibeSessionHub"
cp "$SEND_URL_BIN" "$APP_BUNDLE/Contents/MacOS/send_url_to_pid"
chmod +x "$APP_BUNDLE/Contents/MacOS/VibeSessionHub" "$APP_BUNDLE/Contents/MacOS/send_url_to_pid"
cp "$ICNS_PATH" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

# Embed standalone Python backend + static frontend assets inside the .app bundle
/usr/bin/rsync -a \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pyo' \
  --exclude '.DS_Store' \
  "$PROJECT_DIR/hub/" "$APP_BUNDLE/Contents/Resources/app/hub/"

/usr/bin/rsync -a \
  --exclude '.DS_Store' \
  "$PROJECT_DIR/static/" "$APP_BUNDLE/Contents/Resources/app/static/"

printf '%s\n' "$VERSION" > "$APP_BUNDLE/Contents/Resources/app/VERSION"
if [ -f "$PROJECT_DIR/CHANGELOG.md" ]; then
  cp "$PROJECT_DIR/CHANGELOG.md" "$APP_BUNDLE/Contents/Resources/app/CHANGELOG.md"
fi
GIT_SHORT_SHA="$(git -C "$PROJECT_DIR" rev-parse --short=7 HEAD 2>/dev/null || echo "nogit")"
BUILD_ID="${VERSION}-${GIT_SHORT_SHA}-$(date -u +%Y%m%d%H%M%S)"
printf '%s\n' "$BUILD_ID" > "$APP_BUNDLE/Contents/Resources/app/BUILD_ID"

if [ -n "$DEV_PROJECT_ROOT" ]; then
  printf '%s\n' "$DEV_PROJECT_ROOT" > "$APP_BUNDLE/Contents/Resources/project_root.txt"
fi

cat > "$APP_BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key>
  <string>${BUNDLE_ID}</string>
  <key>CFBundleVersion</key>
  <string>${BUNDLE_VERSION}</string>
  <key>CFBundleShortVersionString</key>
  <string>${VERSION}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleExecutable</key>
  <string>VibeSessionHub</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>LSMinimumSystemVersion</key>
  <string>${MIN_MACOS}</string>
  <key>LSApplicationCategoryType</key>
  <string>public.app-category.developer-tools</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>Vibe Session Hub 需要发送 AppleEvent 以将深链精准投递至对应的 Claude、Cursor、Codex 等 AI 编程客户端窗口（Used to open session deep links in the target AI coding client）。</string>
  <key>VibeHubPort</key>
  <integer>${HUB_PORT}</integer>
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

# Strip FinderInfo / quarantine / resource-fork detritus that can break codesign
/usr/bin/xattr -cr "$APP_BUNDLE"

# Sign inside-out (helper executable first, then outer .app bundle) without deprecated --deep
HELPER_BIN="$APP_BUNDLE/Contents/MacOS/send_url_to_pid"
if [ "$SIGN_IDENTITY" = "-" ]; then
  /usr/bin/codesign --force --sign - \
    --identifier "${BUNDLE_ID}.send-url-to-pid" \
    --entitlements "$ENTITLEMENTS_PLIST" \
    "$HELPER_BIN"
  /usr/bin/codesign --force --sign - \
    --entitlements "$ENTITLEMENTS_PLIST" \
    "$APP_BUNDLE"
else
  /usr/bin/codesign --force --options runtime --timestamp \
    ${KEYCHAIN_ARGS[@]+"${KEYCHAIN_ARGS[@]}"} \
    --sign "$SIGN_IDENTITY" \
    --identifier "${BUNDLE_ID}.send-url-to-pid" \
    --entitlements "$ENTITLEMENTS_PLIST" \
    "$HELPER_BIN"
  /usr/bin/codesign --force --options runtime --timestamp \
    ${KEYCHAIN_ARGS[@]+"${KEYCHAIN_ARGS[@]}"} \
    --sign "$SIGN_IDENTITY" \
    --entitlements "$ENTITLEMENTS_PLIST" \
    "$APP_BUNDLE"
fi

/usr/bin/codesign --verify --strict --verbose=2 "$HELPER_BIN"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

if [ "$APP_ONLY" -eq 1 ]; then
  echo "==> [4/4] Skipping DMG creation (--app-only)"
  echo "Built standalone app: $APP_BUNDLE"
  exit 0
fi

DMG_NAME="Vibe-Session-Hub-v${VERSION}-macos-${ARCH_LABEL}.dmg"
DMG_PATH="$OUTPUT_DIR/$DMG_NAME"
SHA_PATH="${DMG_PATH}.sha256"

echo "==> [4/4] Packaging DMG installer: $DMG_PATH"
STAGE_DIR="$WORK_DIR/dmg-stage"
mkdir -p "$STAGE_DIR"
/usr/bin/ditto "$APP_BUNDLE" "$STAGE_DIR/$APP_NAME.app"
ln -s /Applications "$STAGE_DIR/Applications"

if [ "$SIGN_IDENTITY" = "-" ]; then
  FIRST_LAUNCH_NOTE=$'首次打开提示 / First Launch (Ad-hoc Signed Builds):\n如果 macOS 提示「无法验证开发者」或「已损坏」，请在终端执行以下命令后重试：\n  xattr -cr "/Applications/Vibe Session Hub.app"\n'
else
  FIRST_LAUNCH_NOTE=$'签名说明 / Code Signing:\n当前安装包已使用 Apple Developer ID 签名（开启 Hardened Runtime）。\nThis build is signed with an Apple Developer ID certificate (Hardened Runtime enabled).\n'
fi

cat > "$STAGE_DIR/安装说明-README.txt" <<EOF
Vibe Session Hub v${VERSION} (macOS ${MIN_MACOS}+ ${ARCH_LABEL})
================================================================

安装步骤 / Installation:
1. 将「Vibe Session Hub.app」拖入右侧「Applications」文件夹。
   Drag "Vibe Session Hub.app" into the "Applications" folder.
2. 双击启动应用；应用内置完整后端与前端，无需任何第三方依赖。
   Double-click to launch; the app is completely self-contained.
3. （可选）在顶部菜单栏点击「Vibe Session Hub -> 安装 / 更新开机后台服务」，
   即可在关闭窗口后继续保持后台增量索引与跨机跳转服务运行。

${FIRST_LAUNCH_NOTE}
自定义端口 / Custom Port:
默认监听 127.0.0.1:${HUB_PORT}。如需修改端口，可在 ~/.vibe-session-hub/preferences.json
中设置 {"port": 18380}，或通过环境变量 VIBE_HUB_PORT=18380 启动。

项目主页 / Repository:
https://github.com/StriverF/vibe-session-hub
EOF

rm -f "$DMG_PATH" "$SHA_PATH"

DMG_OK=0
for attempt in 1 2 3; do
  if /usr/bin/hdiutil create \
    -volname "$APP_NAME $VERSION" \
    -srcfolder "$STAGE_DIR" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=9 \
    "$DMG_PATH"; then
    DMG_OK=1
    break
  fi
  echo "hdiutil create attempt ${attempt} failed; retrying in 2s..." >&2
  sleep 2
done

if [ "$DMG_OK" -ne 1 ]; then
  echo "Error: failed to create DMG after 3 attempts" >&2
  exit 1
fi

if [ "$SIGN_IDENTITY" != "-" ]; then
  echo "==> Signing DMG installer with Developer ID"
  /usr/bin/codesign --force --timestamp \
    ${KEYCHAIN_ARGS[@]+"${KEYCHAIN_ARGS[@]}"} \
    --sign "$SIGN_IDENTITY" \
    "$DMG_PATH"
  /usr/bin/codesign --verify --verbose=2 "$DMG_PATH"
fi

/usr/bin/hdiutil verify "$DMG_PATH"

(
  cd "$OUTPUT_DIR"
  /usr/bin/shasum -a 256 "$DMG_NAME" > "$(basename "$SHA_PATH")"
)

echo ""
echo "Release packaging complete!"
echo "  App Bundle : $APP_BUNDLE"
echo "  DMG Image  : $DMG_PATH"
echo "  SHA-256    : $(cat "$SHA_PATH")"
