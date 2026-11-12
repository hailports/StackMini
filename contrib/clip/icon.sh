#!/bin/sh
# Paperclip icon for the app bundle.
set -eu
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
APP=${1:?app}
mkdir -p "$APP/Contents/Resources"
DIR=$(mktemp -d /tmp/paisty-icon.XXXX)
swift "$HERE/mark.swift" "$DIR/icon.png"
SET="$DIR/paisty.iconset"
mkdir -p "$SET"
for spec in 16 32 128 256 512; do
  sips -z "$spec" "$spec" "$DIR/icon.png" --out "$SET/icon_${spec}x${spec}.png" >/dev/null
  dbl=$((spec * 2))
  sips -z "$dbl" "$dbl" "$DIR/icon.png" --out "$SET/icon_${spec}x${spec}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$APP/Contents/Resources/paisty.icns"
rm -rf "$DIR"
