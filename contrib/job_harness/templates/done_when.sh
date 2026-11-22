#!/bin/sh
# Must exit non-zero unless the job is really done. Replace the checks below.
set -eu
cd "$(dirname "$0")"
test -s RESULT.md || { echo "RESULT.md missing"; exit 1; }
# example: python3 -m pytest -q tests/ || exit 1
echo "done_when green"
