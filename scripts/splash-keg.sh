#!/bin/sh
# Run one installed Splash Homebrew keg, not whatever opt/splash points at.
# Install as a copy or symlink named splash-<version> (e.g. bin/splash-1.7.0) and put that path in flags/splash_bin.
# Homebrew's own bin/splash execs /opt/homebrew/opt/splash/..., so after an upgrade it always runs the newest keg,
# which makes "point bin/splash back at the old keg" a non-rollback. This wrapper pins the keg by version.
V=${SPLASH_KEG_VERSION:-$(basename "$0" | sed -n "s/^splash-\([0-9][0-9.]*\)\(\.sh\)\{0,1\}$/\1/p")}
[ -n "$V" ] || { echo "splash-keg: name me splash-<version> or set SPLASH_KEG_VERSION" >&2; exit 2; }
K=/opt/homebrew/Cellar/splash/$V/libexec
[ -x "$K/python/bin/python3" ] || { echo "splash-keg: keg $V not installed" >&2; exit 3; }
export PYTHONDONTWRITEBYTECODE=1
exec "$K/python/bin/python3" -u "$K/install/launcher.py" "$@"
