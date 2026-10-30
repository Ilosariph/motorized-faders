"""
faders.run — CLI entrypoint.

Usage:
    python -m faders.run [--config faders/config.json] [--port /dev/ttyACM0]

Discovers every module in `faders/extensions/` and instantiates the ones
mentioned in the config file via their `register(host, instance_cfg)` hook.

Runs until SIGINT/SIGTERM. The Pico may be absent at startup or unplugged at
any time: the host waits for it and restores state when it comes back.
"""

import argparse
import importlib
import logging
import pkgutil
import signal
import sys
from pathlib import Path

from . import config as config_mod
from . import extensions as extensions_pkg
from .core import FaderHost


def _discover_extension_modules():
    """Import every .py file under faders/extensions/ and return {name: module}."""
    modules = {}
    for info in pkgutil.iter_modules(extensions_pkg.__path__):
        if info.ispkg:
            continue
        modules[info.name] = importlib.import_module(
            f"{extensions_pkg.__name__}.{info.name}"
        )
    return modules


def main(argv=None):
    parser = argparse.ArgumentParser(description="Motorized fader host")
    default_config = Path(__file__).with_name("config.json")
    parser.add_argument("--config", default=str(default_config))
    parser.add_argument("--port", default=None,
                        help="Override serial port (else auto-detect by USB VID)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Log every volume write, SET: and state edge")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = config_mod.load(args.config)
    port = args.port or cfg.get("port")
    host = FaderHost(port=port, num_faders=cfg.get("num_faders", 4))

    modules = _discover_extension_modules()
    enabled = cfg.get("extensions", {})

    for ext_name, instances in enabled.items():
        mod = modules.get(ext_name)
        if mod is None:
            sys.stderr.write(f"[faders] config references unknown extension '{ext_name}'\n")
            continue
        if not hasattr(mod, "register"):
            sys.stderr.write(f"[faders] extension '{ext_name}' has no register()\n")
            continue
        # Accept list-of-instances or single dict for convenience.
        if isinstance(instances, dict):
            instances = [instances]
        for inst_cfg in instances:
            mod.register(host, inst_cfg)

    sys.stderr.write(
        f"[faders] {len(host._extensions)} extension instance(s) loaded\n"
    )
    # systemd stops with SIGTERM; shut down like Ctrl-C so extensions clean up.
    signal.signal(signal.SIGTERM, lambda *_: host.stop())
    host.run()


if __name__ == "__main__":
    main()
