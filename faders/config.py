import json
from pathlib import Path

DEFAULT_CONFIG = {
    "port": None,
    "num_faders": 4,
    "extensions": {},
    # Explicit layer list, [{"name": ..., "extensions": {...}}, ...]. When
    # absent, every plugin in "extensions" becomes a layer of its own.
    "layers": None,
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


def layer_specs(cfg, layer_name_for):
    """
    [(layer name, {module: instances})] in switch order. `layer_name_for`
    maps a module name to its default layer name, for the automatic case.
    """
    layers = cfg.get("layers")
    if layers:
        return [(str(layer.get("name") or f"layer {i + 1}"),
                 layer.get("extensions", {}))
                for i, layer in enumerate(layers)]
    return [(layer_name_for(name), {name: instances})
            for name, instances in cfg.get("extensions", {}).items()]
