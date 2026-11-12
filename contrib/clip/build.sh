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
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
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
swiftc -O -framework Cocoa -framework ApplicationServices -framework ScreenCaptureKit -framework Vision -o "$APP/Contents/MacOS/paisty" "$HERE/PaisteMenu.swift" "$HERE/Onboard.swift"
sh "$HERE/icon.sh" "$APP"
echo "built $APP"
