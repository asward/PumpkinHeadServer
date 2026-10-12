"""Syllable detection for lip-sync, without speech recognition.

Speech comes in syllables at roughly 4-6 per second, each a bump in the energy of the
speech band (about 300-3000 Hz). The detector measures that energy in 10 ms frames,
smooths it, and reports:

- ("onset", e): energy rose well above the last dip and above the threshold. Fire a jaw stroke.
- ("level", e): the syllable got louder; the stroke can swing wider.

Measuring only the speech band keeps rumble and the voice changer's growl from smearing the
bumps. Each event carries its sample offset so the caller can line it up with playback.
"""
from __future__ import annotations

import numpy as np

FRAME_MS = 10
BAND = (300.0, 3000.0)
RISE = 1.6   # onset when energy reaches this multiple of the dip before it
FALL = 0.6   # re-arm once energy falls below this fraction of the syllable's peak


class SyllableDetector:
    def __init__(self, sr: int):
        self.sr = sr
        self.n = max(16, sr * FRAME_MS // 1000)
        self.win = np.hanning(self.n).astype(np.float32)
        freqs = np.fft.rfftfreq(self.n, 1 / sr)
        self.band = (freqs >= BAND[0]) & (freqs <= BAND[1])
        # Parseval with a window: mean-square of the band-limited signal per frame.
        self.scale = 2.0 / (self.n * float(np.sum(self.win ** 2)))
        self.reset()

    def reset(self) -> None:
        self._rest = np.zeros(0, np.float32)
        self._consumed = 0          # samples consumed into frames, absolute
        self._received = 0          # samples received, absolute
        self.env = 0.0
        self._armed = True
        self._dip = 0.0
        self._peak = 0.0
        self._last_onset = -10**12  # absolute sample index

    def energy(self, frames: np.ndarray) -> np.ndarray:
        """Band RMS for each row of frames (shape: count x n)."""
        spec = np.fft.rfft(frames * self.win, axis=1)
        power = np.sum(np.abs(spec[:, self.band]) ** 2, axis=1) * self.scale
        return np.sqrt(power)

    def process(self, x: np.ndarray, threshold: float, gap_ms: float) -> list[tuple[int, str, float]]:
        """Feed samples. Returns (offset, kind, envelope) events; offset is the sample index,
        relative to the start of x, at the end of the frame that produced the event."""
        start = self._received
        self._received += len(x)
        buf = np.concatenate([self._rest, np.asarray(x, np.float32)])
        count = len(buf) // self.n
        self._rest = buf[count * self.n:]
        if not count:
            return []
        energies = self.energy(buf[:count * self.n].reshape(count, self.n))
        gap = int(self.sr * gap_ms / 1000)
        events = []
        for raw in energies:
            self._consumed += self.n
            raw = float(raw)
            # fast attack, ~30 ms release
            self.env = raw if raw > self.env else self.env * 0.7 + raw * 0.3
            e, at = self.env, self._consumed
            offset = at - start
            if self._armed:
                self._dip = min(self._dip, e)
                if e > threshold and e >= self._dip * RISE and at - self._last_onset >= gap:
                    self._armed = False
                    self._peak = e
                    self._last_onset = at
                    events.append((offset, "onset", e))
            else:
                if e > self._peak:
                    self._peak = e
                    events.append((offset, "level", e))
                elif e < self._peak * FALL:
                    self._armed = True
                    self._dip = e
        return events


def detect(x: np.ndarray, sr: int, threshold: float, gap_ms: float) -> list[tuple[int, str, float]]:
    """Whole-clip version: events with offsets from the start of x."""
    return SyllableDetector(sr).process(x, threshold, gap_ms)


def level(e: float, threshold: float) -> float:
    """Stroke size 0..1 from an envelope value: full swing at 5x the threshold."""
    return min(max((e / threshold - 1) / 4, 0.0), 1.0)
