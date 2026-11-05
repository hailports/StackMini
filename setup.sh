#!/bin/sh
# macOS and Linux wrapper. The installer itself is Python.
cd "$(dirname "$0")" || exit 1
exec python3 setup.py "$@"
