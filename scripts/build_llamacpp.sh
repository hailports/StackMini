#!/bin/sh
# Unix wrapper around the Python build helper.
cd "$(dirname "$0")/.." || exit 1
exec python3 scripts/build_llamacpp.py "$@"
