#!/bin/sh
# Build a drag-to-Applications disk image. No route, no key, no notes.
set -eu
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
ROOT=$(CDPATH= cd -- "$HERE/../.." && pwd)
SIGN="${PAISTY_SIGN:-}"
# PAISTY_SIGN_KEYCHAIN: the temporary keychain CI imported the Developer ID identity into.
KC="${PAISTY_SIGN_KEYCHAIN:-}"
# Empty PAISTY_SIGN must not be grepped: an empty pattern matches any find-identity
# output, including "0 valid identities found", and codesign --sign "" fails.
if [ -z "$SIGN" ] || ! security find-identity -v -p codesigning ${KC:+"$KC"} 2>/dev/null | grep -q -F -- "$SIGN"; then
  SIGN="-"
fi
# PAISTY_REQUIRE_SIGN=1 (CI release): never fall back to an ad-hoc build. Ad-hoc builds change
# identity on every update, so macOS drops the Accessibility and Screen Recording grants.
if [ "$SIGN" = "-" ] && [ "${PAISTY_REQUIRE_SIGN:-0}" = 1 ]; then
  echo "PAISTY_REQUIRE_SIGN=1 but no Developer ID identity matches PAISTY_SIGN" >&2
  exit 1
fi
# codesign messages can quote the certificate name; mask it so build logs never carry it.
cs() {
  out=$(codesign "$@" 2>&1) && rc=0 || rc=$?
  [ -n "$out" ] && printf '%s\n' "$out" | sed -E 's/(Developer ID [A-Za-z]+): [^"(]*/\1: <name> /g'
  return $rc
}
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
  <key>CFBundleURLTypes</key><array><dict><key>CFBundleURLName</key><string>com.hailports.paiste.activate</string><key>CFBundleURLSchemes</key><array><string>paisty</string></array></dict></array>
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
swiftc -O -framework Cocoa -framework ApplicationServices -framework ScreenCaptureKit -framework Vision -framework CryptoKit -framework IOKit -o "$APP/Contents/MacOS/paisty" "$HERE/PaisteMenu.swift" "$HERE/PastePath.swift" "$HERE/ReplyHint.swift" "$HERE/Onboard.swift" "$HERE/Updater.swift" "$HERE/Pulse.swift" "$HERE/License.swift" "$HERE/SetupGate.swift"
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
if [ "$SIGN" = "-" ]; then
  codesign --force --sign - --timestamp=none --deep "$APP"
else
  # Developer ID: sign inside out with the hardened runtime and a secure timestamp, as notarization
  # requires. Every Mach-O in the bundled Python first, then the helper and the app itself.
  KCARG=""
  [ -n "$KC" ] && KCARG="--keychain $KC"
  ENT="$HERE/python.entitlements"
  find "$APP/Contents/Resources/python" -type f | while IFS= read -r f; do
    if file -b "$f" | grep -q "Mach-O"; then
      case "$f" in
        */bin/python3*) cs --force $KCARG --sign "$SIGN" --options runtime --timestamp --entitlements "$ENT" "$f" || exit 1 ;;
        *) cs --force $KCARG --sign "$SIGN" --options runtime --timestamp "$f" || exit 1 ;;
      esac
    fi
  done
  cs --force $KCARG --sign "$SIGN" --options runtime --timestamp "$APP/Contents/MacOS/paiste-run"
  cs --force $KCARG --sign "$SIGN" --options runtime --timestamp "$APP"
  cs --verify --deep --strict "$APP"
  codesign -d -r- "$APP" 2>&1 | grep designated
fi
# Notarize and staple the app before it goes into the disk image and the update zip.
if [ "$SIGN" != "-" ] && [ -n "${PAISTY_NOTARY_KEY:-}" ]; then
  NZ=$(mktemp -d /tmp/paisty-notary.XXXX)
  ditto -c -k --keepParent "$APP" "$NZ/paisty.zip"
  xcrun notarytool submit "$NZ/paisty.zip" --key "$PAISTY_NOTARY_KEY" --key-id "$PAISTY_NOTARY_KEY_ID" \
    --issuer "$PAISTY_NOTARY_ISSUER" --wait --timeout 30m --output-format json > "$NZ/result.json"
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("notarization", d.get("status"), d.get("id"))' "$NZ/result.json"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])).get("status"); sys.exit(0 if s=="Accepted" else 1)' "$NZ/result.json" || {
    ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' "$NZ/result.json")
    [ -n "$ID" ] && xcrun notarytool log "$ID" --key "$PAISTY_NOTARY_KEY" --key-id "$PAISTY_NOTARY_KEY_ID" --issuer "$PAISTY_NOTARY_ISSUER" || true
    echo "notarization failed" >&2; exit 1; }
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  # -vv also prints the certificate name; keep only the verdict and the source.
  spctl -a -vv -t exec "$APP" 2>&1 | grep -v "^origin=" | tee /tmp/paisty-spctl.txt
  grep -q "source=Notarized Developer ID" /tmp/paisty-spctl.txt
  rm -rf "$NZ"
fi
mkdir -p "$DMGSRC/root"
cp -R "$APP" "$DMGSRC/root/paisty.app"
ln -s /Applications "$DMGSRC/root/Applications"
OUT="${PAISTY_OUT:-$HOME/Downloads}"
mkdir -p "$OUT"
hdiutil create -volname paisty -srcfolder "$DMGSRC/root" -ov -format UDZO "$OUT/paisty.dmg"
if [ "$SIGN" != "-" ]; then
  cs --force ${KC:+--keychain "$KC"} --sign "$SIGN" --timestamp "$OUT/paisty.dmg"
  if [ -n "${PAISTY_NOTARY_KEY:-}" ]; then
    xcrun notarytool submit "$OUT/paisty.dmg" --key "$PAISTY_NOTARY_KEY" --key-id "$PAISTY_NOTARY_KEY_ID" \
      --issuer "$PAISTY_NOTARY_ISSUER" --wait --timeout 30m
    xcrun stapler staple "$OUT/paisty.dmg"
    spctl -a -vv -t open --context context:primary-signature "$OUT/paisty.dmg" 2>&1 | grep -v "^origin=" | tee /tmp/paisty-spctl-dmg.txt
    grep -q "source=Notarized Developer ID" /tmp/paisty-spctl-dmg.txt
  fi
fi
# The self-update payload: the same signed app, zipped for ditto.
rm -f "$OUT/paisty-update.zip"
ditto -c -k --keepParent "$APP" "$OUT/paisty-update.zip"
printf '%s\n%s\n' "$VERSION" "$BUILD" > "$OUT/paisty-version.txt"
rm -rf "$STAGE" "$DMGSRC"
echo "wrote $OUT/paisty.dmg $OUT/paisty-update.zip ($VERSION build $BUILD)"
