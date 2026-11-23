#!/bin/sh
# Run one pinned Splash build, not whatever opt/splash points at.
# Install as a copy or symlink named splash-<version> (e.g. bin/splash-1.7.0) and put that path in flags/splash_bin.
# Homebrew's own bin/splash execs /opt/homebrew/opt/splash/..., so after an upgrade it always runs the newest keg,
# which makes "point bin/splash back at the old keg" a non-rollback. This wrapper pins the build by version.
# Lookup order: a private copy outside Homebrew (SPLASH_PRIVATE_ROOT/splash-<V>/libexec, which brew cleanup
# can never delete), then the Homebrew keg.
V=${SPLASH_KEG_VERSION:-$(basename "$0" | sed -n "s/^splash-\([0-9][0-9.]*\)\(\.sh\)\{0,1\}$/\1/p")}
[ -n "$V" ] || { echo "splash-keg: name me splash-<version> or set SPLASH_KEG_VERSION" >&2; exit 2; }
R=${SPLASH_PRIVATE_ROOT:-$HOME/claude-stack/opt}
for K in "$R/splash-$V/libexec" "/opt/homebrew/Cellar/splash/$V/libexec"; do
  [ -x "$K/python/bin/python3" ] && break
done
[ -x "$K/python/bin/python3" ] || { echo "splash-keg: build $V not installed (checked $R/splash-$V and the Homebrew keg)" >&2; exit 3; }
export PYTHONDONTWRITEBYTECODE=1
exec "$K/python/bin/python3" -u "$K/install/launcher.py" "$@"
