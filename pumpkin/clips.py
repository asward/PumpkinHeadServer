"""Voice-line library: <data_dir>/clips/<id>.wav, stored dry (mono int16) so the voice
changer can be re-chosen at playback."""
from __future__ import annotations

import asyncio
import re
import wave
from pathlib import Path

import numpy as np

from .audio import resample

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "untitled"


def check_id(id_: str) -> str:
    if not ID_RE.match(id_):
        raise FileNotFoundError(id_)
    return id_


def read_wav(path: Path, sr: int) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("only 16-bit WAV is supported without ffmpeg")
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
        return resample(x, w.getframerate(), sr)


class ClipStore:
    def __init__(self, data_dir: Path, sr: int):
        self.dir = data_dir / "clips"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sr = sr

    def path(self, id_: str) -> Path:
        return self.dir / f"{check_id(id_)}.wav"

    def exists(self, id_: str) -> bool:
        return ID_RE.match(id_) is not None and self.path(id_).exists()

    def unique_id(self, name: str) -> str:
        base = slugify(name)
        id_, i = base, 2
        while self.path(id_).exists():
            id_, i = f"{base}-{i}", i + 1
        return id_

    def duration(self, id_: str) -> float:
        try:
            with wave.open(str(self.path(id_)), "rb") as w:
                return w.getnframes() / w.getframerate()
        except (FileNotFoundError, wave.Error):
            return 0.0

    def list(self) -> list[dict]:
        return [{"id": p.stem, "duration": round(self.duration(p.stem), 2)}
                for p in sorted(self.dir.glob("*.wav"))]

    def load(self, id_: str) -> np.ndarray:
        return read_wav(self.path(id_), self.sr)

    def save(self, id_: str, x: np.ndarray) -> None:
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
        with wave.open(str(self.path(id_)), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sr)
            w.writeframes(pcm.tobytes())

    def delete(self, id_: str) -> None:
        self.path(id_).unlink()

    async def import_file(self, name: str, src: Path) -> str:
        """Convert any audio file ffmpeg understands (or a 16-bit WAV without it)."""
        id_ = self.unique_id(name)
        dst = self.path(id_)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                "-ac", "1", "-ar", str(self.sr), "-sample_fmt", "s16", str(dst),
                stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if not proc.returncode:
                return id_
            dst.unlink(missing_ok=True)
            reason = err.decode(errors="replace").strip()[:200]
        except FileNotFoundError:
            reason = "ffmpeg not installed"
        try:
            self.save(id_, read_wav(src, self.sr))
        except (wave.Error, ValueError, EOFError):
            raise ValueError(f"could not decode audio: {reason}") from None
        return id_
