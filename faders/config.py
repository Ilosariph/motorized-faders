import json
from pathlib import Path

DEFAULT_CONFIG = {
    "port": None,
    "num_faders": 2,
    "extensions": {},
}


def load(path):
    """Load JSON config from `path`, filling in defaults for missing keys."""
    path = Path(path)
    with open(path, "r") as f:
        data = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    merged.setdefault("extensions", {})
    return merged
