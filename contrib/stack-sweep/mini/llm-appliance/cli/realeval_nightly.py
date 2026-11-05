#!/usr/bin/env python3
"""04:40 re-score. Regressions land in runtime/realeval/ALARM.txt for the morning brief. No mail."""
import runpy
import sys
from pathlib import Path

p = Path.home() / "llm-appliance/runtime/realeval/eval.py"
sys.argv = [str(p), "--nightly"]
runpy.run_path(str(p), run_name="__main__")
