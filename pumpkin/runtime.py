"""Operator-tunable settings, persisted to <data_dir>/settings.json."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from .voicefx import PRESETS, FxParams

log = logging.getLogger(__name__)

DEFAULTS = {
    "fx": {"preset": "monster", "params": FxParams.from_dict(PRESETS["monster"]).to_dict()},
    "lipsync": {"enabled": True, "threshold": 0.03, "min_ms": 90, "max_open_ms": 350},
    "autopilot": {"enabled": False, "sequences": [], "min_gap": 20, "max_gap": 60, "order": "random"},
}


class RuntimeSettings:
    def __init__(self, path: Path):
        self.path = path
        self.data = copy.deepcopy(DEFAULTS)
        try:
            saved = json.loads(path.read_text())
            for section, values in saved.items():
                if section in self.data and isinstance(values, dict):
                    self.data[section].update(values)
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            log.warning("ignoring unreadable %s: %s", path, e)

    def __getitem__(self, section: str) -> dict:
        return self.data[section]

    def update(self, section: str, values: dict) -> dict:
        self.data[section].update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)
        return self.data[section]
