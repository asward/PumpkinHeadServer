"""Records a "take": the operator's dry voice plus every manual puppet action, timestamped."""
from __future__ import annotations

import time

import numpy as np


class Recorder:
    def __init__(self, sr: int):
        self.sr = sr
        self.active = False
        self.name = ""
        self.live = True
        self._t0 = 0.0
        self._chunks: list[np.ndarray] = []
        self._n = 0
        self.events: list[dict] = []

    def start(self, name: str, live: bool, eyes: float) -> None:
        self.active, self.name, self.live = True, name, live
        self._t0 = time.monotonic()
        self._chunks, self._n = [], 0
        self.events = [{"t": 0.0, "type": "eyes", "value": eyes}]

    def elapsed(self) -> float:
        return time.monotonic() - self._t0 if self.active else 0.0

    def add_audio(self, x: np.ndarray) -> None:
        # Place audio by arrival time, so gaps between push-to-talk presses stay
        # silent and the voice stays in sync with the recorded puppet events.
        behind = int(self.elapsed() * self.sr) - len(x) - self._n
        if behind > self.sr // 10:
            self._chunks.append(np.zeros(behind, np.float32))
            self._n += behind
        self._chunks.append(x)
        self._n += len(x)

    def add_event(self, type_: str, **fields) -> None:
        self.events.append({"t": round(self.elapsed(), 3), "type": type_, **fields})

    def stop(self) -> tuple[np.ndarray, list[dict]]:
        self.active = False
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
        self._chunks = []
        return audio, self.events

    def status(self) -> dict:
        return {"active": self.active, "name": self.name, "live": self.live, "elapsed": round(self.elapsed(), 1)}
