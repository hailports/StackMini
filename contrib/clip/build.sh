#!/bin/sh
# Build the Paiste menu-bar app. It is not installed and it is not started.
set -eu
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
APP="$HERE/paisty.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>__VERSION__</string>
  <key>CFBundleVersion</key><string>__BUILD__</string>
  <key>PaistyFeedURL</key><string>__FEED__</string>
  <key>PaistyEdPublicKey</key><string>__PUBKEY__</string>
  <key>PaistyFeedToken</key><string>__FEEDTOKEN__</string>
  <key>CFBundleExecutable</key><string>paisty</string>
  <key>CFBundleIdentifier</key><string>com.hailports.paiste</string>
  <key>CFBundleName</key><string>paisty</string>
  <key>CFBundleDisplayName</key><string>paisty</string>
  <key>CFBundleIconFile</key><string>paisty</string>
  <key>LSUIElement</key><true/>
  <key>NSScreenCaptureUsageDescription</key><string>paisty reads the front window when the app does not expose its text.</string>
</dict>
</plist>
PLIST
VERSION="${PAISTY_VERSION:-1.1.$(git -C "$HERE" rev-list --count HEAD 2>/dev/null || echo 0)}"
BUILD="${PAISTY_BUILD:-$(git -C "$HERE" rev-list --count HEAD 2>/dev/null || echo 1)}"
FEED="${PAISTY_FEED:-https://www.paisty.com/api/update/paisty-update.json}"
FEEDTOKEN="${PAISTY_FEED_TOKEN:-}"
PUBKEY="${PAISTY_ED_PUBLIC_KEY:-$(cat "$HERE/update-public-key.txt" 2>/dev/null || true)}"
PLIST_PATH="$APP/Contents/Info.plist"
sed -e "s|__VERSION__|$VERSION|" -e "s|__BUILD__|$BUILD|" -e "s|__FEED__|$FEED|" -e "s|__PUBKEY__|$PUBKEY|" -e "s|__FEEDTOKEN__|$FEEDTOKEN|" "$PLIST_PATH" > "$PLIST_PATH.tmp" && mv "$PLIST_PATH.tmp" "$PLIST_PATH"
swiftc -O -framework Cocoa -framework ApplicationServices -framework ScreenCaptureKit -framework Vision -framework CryptoKit -o "$APP/Contents/MacOS/paisty" "$HERE/PaisteMenu.swift" "$HERE/PastePath.swift" "$HERE/ReplyHint.swift" "$HERE/Onboard.swift" "$HERE/Updater.swift" "$HERE/Pulse.swift"
sh "$HERE/icon.sh" "$APP"
echo "built $APP"
