#!/usr/bin/env python3
"""Run the tray's unit tests (tests/test_desktop_tray.py, tests/test_desktop_rename.py) and turn
failures into CI annotations.

The workflow runs this on every OS before building: the Windows and macOS
branches of desktop/ (locks, registry, ctypes wrappers) get exercised on the
systems they are written for. Annotations, because CI logs need a login.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

suite = unittest.defaultTestLoader.loadTestsFromNames(["tests.test_desktop_tray", "tests.test_desktop_rename"])
result = unittest.TextTestRunner(verbosity=1).run(suite)
for test, trace in result.failures + result.errors:
    last = [line.strip() for line in trace.strip().splitlines()[-4:]]
    print(f"::error::{test.id()}: " + " | ".join(last), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
