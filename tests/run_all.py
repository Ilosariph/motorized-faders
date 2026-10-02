#!/usr/bin/env python3
"""Run every test suite. Usage: python3 tests/run_all.py"""

import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
SUITES = ("test_firmware.py", "test_host.py", "test_integration.py")


def main():
    failed = []
    for suite in SUITES:
        print(f"\n{'=' * 60}\n{suite}\n{'=' * 60}")
        # Each suite runs in its own process: the firmware tests install fake
        # `machine` / `utime` modules, which must not leak into the host tests.
        result = subprocess.run([sys.executable, str(HERE / suite)])
        if result.returncode != 0:
            failed.append(suite)

    print(f"\n{'=' * 60}")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    print("all suites passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
