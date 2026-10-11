"""Speaker output: mixes live push-to-talk audio with clip playback.

The output callback runs on PortAudio's thread. It also tracks an amplitude envelope
(`env`) that the lip-sync loop reads to flap the mouth.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from .voicefx import FxParams, VoiceFX

log = logging.getLogger(__name__)

BLOCK_MS = 10


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear resampling. Plenty for voice, and cheap."""
    if src == dst or not len(x):
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


class LiveBuffer:
    """Jitter buffer for network audio: primes 40 ms, caps latency at 250 ms."""

    def __init__(self, sr: int):
        self._buf = np.zeros(0, np.float32)
        self._lock = threading.Lock()
        self._prebuf = int(sr * 0.04)
        self._max = int(sr * 0.25)
        self._primed = False

    def write(self, x: np.ndarray) -> None:
        with self._lock:
            self._buf = np.concatenate([self._buf, x])
            if len(self._buf) > self._max:
                self._buf = self._buf[-self._prebuf * 2:]

    def read(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self._lock:
            if not self._primed:
                if len(self._buf) < self._prebuf:
                    return out
                self._primed = True
            k = min(n, len(self._buf))
            out[:k] = self._buf[:k]
            self._buf = self._buf[k:]
            if k < n:
                self._primed = False
        return out


@dataclass
class _Voice:
    data: np.ndarray
    tag: str
    done: asyncio.Future
    pos: int = 0


class AudioEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sr = cfg.sample_rate
        self.block = self.sr * BLOCK_MS // 1000
        self.live = LiveBuffer(self.sr)
        self.fx = VoiceFX(self.sr)
        self.env = 0.0
        self.last_live = 0.0  # monotonic time live audio last arrived
        self.backend = "null"
        self._voices: list[_Voice] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream = None
        self._running = False

    # ---- lifecycle
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.cfg.audio_backend in ("auto", "sounddevice"):
            try:
                import sounddevice as sd
                dev = self.cfg.audio_device
                self._stream = sd.OutputStream(
                    samplerate=self.sr, channels=1, dtype="float32", blocksize=self.block,
                    device=int(dev) if dev.isdigit() else (dev or None),
                    callback=self._callback, latency="low")
                self._stream.start()
                self.backend = "sounddevice"
                return
            except Exception as e:
                if self.cfg.audio_backend == "sounddevice":
                    raise
                log.warning("No audio output (%s); using a silent null sink", e)
        # Null sink: keep consuming audio in real time so lip-sync and clip timing still work.
        self._running = True
        threading.Thread(target=self._null_sink, daemon=True, name="audio-null").start()

    def close(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()

    def _null_sink(self) -> None:
        period = self.block / self.sr
        t = time.monotonic()
        while self._running:
            self._render(self.block)
            t += period
            time.sleep(max(0.0, t - time.monotonic()))

    def _callback(self, outdata, frames, _time, status) -> None:
        outdata[:, 0] = self._render(frames)

    def _render(self, frames: int) -> np.ndarray:
        mix = self.live.read(frames)
        finished = []
        with self._lock:
            for v in self._voices:
                n = min(frames, len(v.data) - v.pos)
                mix[:n] += v.data[v.pos:v.pos + n]
                v.pos += n
                if v.pos >= len(v.data):
                    finished.append(v)
            for v in finished:
                self._voices.remove(v)
        np.clip(mix, -1.0, 1.0, out=mix)
        rms = float(np.sqrt(np.mean(mix * mix)))
        # fast attack, ~60 ms release
        self.env = rms if rms > self.env else self.env * 0.85 + rms * 0.15
        for v in finished:
            self._loop.call_soon_threadsafe(_resolve, v.done, True)
        return mix

    # ---- live audio
    def set_fx(self, params: FxParams) -> None:
        self.fx = VoiceFX(self.sr, params)

    def push_live(self, pcm: bytes, rate: int, play: bool = True) -> np.ndarray:
        """Take one int16 mono chunk from the browser. Returns the dry samples at our rate."""
        x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0
        x = resample(x, rate, self.sr)
        if play:
            self.live.write(self.fx.process(x))
            self.last_live = time.monotonic()
        return x

    @property
    def live_active(self) -> bool:
        return time.monotonic() - self.last_live < 5

    # ---- clips
    def render(self, dry: np.ndarray, params: FxParams) -> np.ndarray:
        fx = VoiceFX(self.sr, params)
        return fx.process(np.concatenate([dry, np.zeros(fx.tail_samples(), np.float32)]))

    def play(self, data: np.ndarray, tag: str = "") -> asyncio.Future:
        """Start a clip. The future resolves True when it finishes, False if stopped."""
        done = asyncio.get_running_loop().create_future()
        with self._lock:
            self._voices.append(_Voice(data.astype(np.float32), tag, done))
        return done

    def stop(self, tag: str | None = None) -> None:
        with self._lock:
            stopped = [v for v in self._voices if tag is None or v.tag == tag]
            self._voices = [v for v in self._voices if v not in stopped]
        for v in stopped:
            _resolve(v.done, False)


def _resolve(fut: asyncio.Future, value) -> None:
    if not fut.done():
        fut.set_result(value)
