"""
Loads `pico/main.py` under CPython with the hardware stubbed out.

main.py calls main() at import time (it is a MicroPython boot script), so the
harness strips that trailing call and execs the rest, giving tests access to
the module's classes, constants and protocol handlers.
"""

import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import stubs  # noqa: E402

PICO_DIR = pathlib.Path(__file__).resolve().parent.parent / "pico"


def load_firmware():
    stubs.install()
    stubs.reset_clock()

    # Make `lib.*` importable as the firmware spells it.
    lib_pkg = types.ModuleType("lib")
    lib_pkg.__path__ = [str(PICO_DIR / "lib")]
    sys.modules["lib"] = lib_pkg
    for name in ("buttons", "fader_screen", "mcp3208", "ssd1306_spi"):
        sys.modules.pop("lib." + name, None)

    source = (PICO_DIR / "main.py").read_text()
    marker = "\nmain()\n"
    if not source.endswith(marker):
        raise AssertionError("main.py no longer ends with a bare main() call")
    source = source[: -len(marker)]

    module = types.ModuleType("firmware")
    module.__dict__["__name__"] = "firmware"
    exec(compile(source, str(PICO_DIR / "main.py"), "exec"), module.__dict__)
    return module
