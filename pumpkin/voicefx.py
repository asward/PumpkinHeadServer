"""Streaming voice changer: pitch shift -> ring modulator -> drive -> echo -> gain.

Everything is vectorised with numpy so it runs block-by-block in real time on a Pi.
The same class renders recorded voice lines offline, so a line sounds the same
whether it was spoken live or played back later.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np

RANGES = {
    "pitch": (-12.0, 12.0),     # semitones
    "ring_hz": (0.0, 400.0),    # ring-modulator carrier
    "ring_mix": (0.0, 1.0),
    "drive": (0.0, 1.0),        # soft-clip distortion
    "echo_ms": (0.0, 1000.0),   # 0 = off, otherwise at least 20 ms
    "echo_fb": (0.0, 0.9),
    "echo_mix": (0.0, 1.0),
    "gain": (0.0, 3.0),
}

PRESETS: dict[str, dict] = {
    "normal": {},
    "monster": {"pitch": -7, "drive": 0.35, "gain": 1.2},
    "demon": {"pitch": -12, "drive": 0.5, "echo_ms": 120, "echo_fb": 0.25, "echo_mix": 0.25},
    "witch": {"pitch": 5, "ring_hz": 30, "ring_mix": 0.3, "drive": 0.15},
    "robot": {"ring_hz": 80, "ring_mix": 0.8, "drive": 0.2},
    "ghost": {"pitch": 3, "echo_ms": 280, "echo_fb": 0.55, "echo_mix": 0.5, "gain": 0.9},
    "chipmunk": {"pitch": 9},
}


@dataclass
class FxParams:
    pitch: float = 0.0
    ring_hz: float = 0.0
    ring_mix: float = 0.0
    drive: float = 0.0
    echo_ms: float = 0.0
    echo_fb: float = 0.35
    echo_mix: float = 0.35
    gain: float = 1.0

    @classmethod
    def from_dict(cls, d: dict | None) -> FxParams:
        names = {f.name for f in fields(cls)}
        p = cls(**{k: float(v) for k, v in (d or {}).items() if k in names})
        for k, (lo, hi) in RANGES.items():
            setattr(p, k, min(max(getattr(p, k), lo), hi))
        if 0 < p.echo_ms < 20:
            p.echo_ms = 20.0
        return p

    @classmethod
    def preset(cls, name: str) -> FxParams:
        return cls.from_dict(PRESETS[name])

    def to_dict(self) -> dict:
        return asdict(self)


class VoiceFX:
    GRAIN_MS = 50
    MAX_ECHO_S = 1.0

    def __init__(self, sr: int, params: FxParams | None = None):
        self.sr = sr
        self.p = params or FxParams()
        self._win = int(sr * self.GRAIN_MS / 1000)
        self._hist = np.zeros(self._win + 2, np.float32)
        self._phase = 0.0
        self._ring_phase = 0.0
        self._ebuf = np.zeros(int(sr * self.MAX_ECHO_S) + 1, np.float32)
        self._epos = 0

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, np.float32)
        if not len(x):
            return x
        p = self.p
        y = self._pitch(x)
        if p.ring_hz > 0 and p.ring_mix > 0:
            y = self._ring(y)
        if p.drive > 0:
            k = 1 + p.drive * 19
            y = np.tanh(k * y) / np.tanh(k)
        if p.echo_ms > 0 and p.echo_mix > 0:
            y = self._echo(y)
        y = y * p.gain
        return np.clip(y, -1.0, 1.0).astype(np.float32)

    def tail_samples(self) -> int:
        """Extra silence to append when rendering a clip so effects can ring out."""
        tail = self._win
        if self.p.echo_ms > 0 and self.p.echo_mix > 0:
            # until the echo decays below ~-40 dB
            repeats = np.log(0.01) / np.log(max(self.p.echo_fb, 0.05))
            tail += int(self.sr * self.p.echo_ms / 1000 * min(repeats, 12))
        return tail

    # Two delay-line taps half a grain apart, each sweeping its delay at (1 - ratio)
    # samples per sample. A sin^2/cos^2 crossfade hides each tap's wrap-around.
    def _pitch(self, x: np.ndarray) -> np.ndarray:
        W = self._win
        H = len(self._hist)
        data = np.concatenate([self._hist, x])
        self._hist = data[-H:]
        if abs(self.p.pitch) < 0.01:
            return x
        ratio = 2.0 ** (self.p.pitch / 12.0)
        n = len(x)
        steps = (1.0 - ratio) * np.arange(n)
        d1 = (self._phase + steps) % W
        d2 = (d1 + W / 2) % W
        self._phase = (self._phase + (1.0 - ratio) * n) % W
        now = H + np.arange(n)
        g1 = np.sin(np.pi * d1 / W) ** 2
        return self._tap(data, now - d1) * g1 + self._tap(data, now - d2) * (1.0 - g1)

    @staticmethod
    def _tap(data: np.ndarray, pos: np.ndarray) -> np.ndarray:
        i0 = np.floor(pos).astype(np.int64)
        frac = pos - i0
        i1 = np.minimum(i0 + 1, len(data) - 1)
        return data[i0] * (1.0 - frac) + data[i1] * frac

    def _ring(self, x: np.ndarray) -> np.ndarray:
        w = 2 * np.pi * self.p.ring_hz / self.sr
        ph = self._ring_phase + w * np.arange(len(x))
        self._ring_phase = (self._ring_phase + w * len(x)) % (2 * np.pi)
        m = self.p.ring_mix
        return x * ((1 - m) + m * np.sin(ph))

    # Feedback echo y[n] = x[n] + fb * y[n-D]. Processing in chunks no longer than D
    # means every y[n-D] a chunk needs is already in the ring buffer.
    def _echo(self, x: np.ndarray) -> np.ndarray:
        L = len(self._ebuf)
        D = min(int(self.sr * self.p.echo_ms / 1000), L - 1)
        out = np.empty_like(x)
        for s in range(0, len(x), D):
            c = x[s:s + D]
            n = len(c)
            idx = (self._epos + np.arange(n)) % L
            delayed = self._ebuf[(idx - D) % L]
            self._ebuf[idx] = c + self.p.echo_fb * delayed
            self._epos = (self._epos + n) % L
            out[s:s + n] = c + self.p.echo_mix * delayed
        return out
