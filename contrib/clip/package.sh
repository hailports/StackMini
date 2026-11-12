#!/bin/sh
# Build a drag-to-Applications disk image. No route, no key, no notes.
set -eu
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
SIGN="${PAISTY_SIGN:-7894BB24D88A0E407BA41F3BE6F65EAFE068DC80}"
if ! security find-identity -v -p codesigning 2>/dev/null | grep -q "$SIGN"; then
  SIGN="-"
fi
PY="${PAISTY_PYTHON_PREFIX:-}"
if [ -z "$PY" ]; then
  PY=$(ls -d "$HOME/.local/share/uv/python"/cpython-3.12*-macos-aarch64-none 2>/dev/null | tail -1)
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY/bin/python3" ]; then
  echo "Need a Python 3.12 prefix. Set PAISTY_PYTHON_PREFIX." >&2
  exit 1
fi
STAGE=$(mktemp -d /tmp/paisty-stage.XXXX)
DMGSRC=$(mktemp -d /tmp/paisty-dmg.XXXX)
APP="$STAGE/paisty.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/py/hailports/clip" "$APP/Contents/Resources/py/contrib/clip" "$APP/Contents/Resources/python"
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
cp "$ROOT/hailports/__init__.py" "$ROOT/hailports/llm.py" "$ROOT/hailports/chat_badge.py" "$ROOT/hailports/config.py" "$ROOT/hailports/promptui.py" "$APP/Contents/Resources/py/hailports/"
cp "$ROOT/hailports/clip/"*.py "$APP/Contents/Resources/py/hailports/clip/"
cp "$HERE/run" "$APP/Contents/Resources/py/contrib/clip/run"
chmod 755 "$APP/Contents/Resources/py/contrib/clip/run"
cp -R "$PY/bin" "$PY/lib" "$APP/Contents/Resources/python/"
find "$APP/Contents/Resources/python" -name '__pycache__' -type d -prune -exec rm -rf {} +
cat > "$APP/Contents/MacOS/paiste-run" <<'SH'
#!/bin/sh
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
RES="$HERE/../Resources"
export PYTHONPATH="$RES/py${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONNOUSERSITE=1
exec "$RES/python/bin/python3" "$RES/py/contrib/clip/run" "$@"
SH
chmod 755 "$APP/Contents/MacOS/paiste-run"
# The bundled interpreter must not see this Mac's route while the image is checked.
HOME=/tmp/paisty-empty-home-$$ "$APP/Contents/MacOS/paiste-run" onboard --show >/tmp/paisty-show-$$.json
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "locals" in d and "apis" in d' "/tmp/paisty-show-$$.json"
rm -f "/tmp/paisty-show-$$.json"
codesign --force --sign "$SIGN" --timestamp=none --deep "$APP"
mkdir -p "$DMGSRC/root"
cp -R "$APP" "$DMGSRC/root/paisty.app"
ln -s /Applications "$DMGSRC/root/Applications"
hdiutil create -volname paisty -srcfolder "$DMGSRC/root" -ov -format UDZO "$HOME/Downloads/paisty.dmg"
rm -rf "$STAGE" "$DMGSRC"
echo "wrote $HOME/Downloads/paisty.dmg"
