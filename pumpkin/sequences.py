"""Sequences: timed lists of puppet events, stored as <data_dir>/sequences/<id>.json.

    {"id": "welcome", "name": "Welcome", "fx": null | "ghost" | {...params},
     "events": [{"t": 0.0, "type": "eyes", "value": 1.0},
                {"t": 0.2, "type": "say", "clip": "hello"},
                {"t": 0.5, "type": "mouth", "value": 1},
                {"t": 3.0, "type": "dispense", "count": 1},
                {"t": 3.5, "type": "motor", "cmd": "MOVE 2 400 1000"}]}

fx null means "use the live voice-changer setting at play time".
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .clips import ID_RE, check_id, slugify
from .stepper import ALLOWED_VERBS
from .voicefx import PRESETS, FxParams

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MAX_T = 3600.0


def normalize(seq: dict) -> dict:
    """Validate a sequence from the API or disk. Raises ValueError."""
    if not isinstance(seq, dict):
        raise ValueError("sequence must be an object")
    name = str(seq.get("name") or seq.get("id") or "untitled")[:80]
    id_ = str(seq.get("id") or slugify(name))
    if not ID_RE.match(id_):
        raise ValueError(f"bad id {id_!r}: use a-z, 0-9 and -")
    fx = seq.get("fx")
    if isinstance(fx, str):
        if fx not in PRESETS:
            raise ValueError(f"unknown fx preset {fx!r}")
    elif isinstance(fx, dict):
        fx = FxParams.from_dict(fx).to_dict()
    elif fx is not None:
        raise ValueError("fx must be null, a preset name or a params object")

    events = []
    for i, ev in enumerate(seq.get("events") or []):
        try:
            t = round(float(ev["t"]), 3)
            typ = ev["type"]
            if not 0 <= t <= MAX_T:
                raise ValueError("t out of range")
            if typ == "eyes":
                out = {"value": min(max(float(ev["value"]), 0.0), 1.0)}
            elif typ == "mouth":
                out = {"value": 1 if float(ev["value"]) >= 0.5 else 0}
            elif typ == "say":
                out = {"clip": check_id(str(ev["clip"]))}
            elif typ == "dispense":
                out = {"count": min(max(int(ev.get("count", 1)), 1), 10)}
            elif typ == "motor":
                cmd = " ".join(str(ev["cmd"]).split())
                if not cmd or cmd.split()[0].upper() not in ALLOWED_VERBS:
                    raise ValueError(f"motor command not allowed: {cmd!r}")
                out = {"cmd": cmd}
            else:
                raise ValueError(f"unknown type {typ!r}")
        except (KeyError, TypeError, ValueError, FileNotFoundError) as e:
            raise ValueError(f"event {i}: {e}") from None
        events.append({"t": t, "type": typ, **out})
    events.sort(key=lambda e: e["t"])
    return {"id": id_, "name": name, "fx": fx, "events": events}


def duration(seq: dict, clip_duration: Callable[[str], float]) -> float:
    end = 0.0
    for ev in seq["events"]:
        end = max(end, ev["t"] + (clip_duration(ev["clip"]) if ev["type"] == "say" else 0.0))
    return round(end, 3)


class SequenceStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "sequences"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, id_: str) -> Path:
        return self.dir / f"{check_id(id_)}.json"

    def unique_id(self, name: str) -> str:
        base = slugify(name)
        id_, i = base, 2
        while self.path(id_).exists():
            id_, i = f"{base}-{i}", i + 1
        return id_

    def get(self, id_: str) -> dict:
        return normalize(json.loads(self.path(id_).read_text()))

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(self.get(p.stem))
            except (ValueError, OSError) as e:
                log.warning("skipping %s: %s", p.name, e)
        return out

    def save(self, seq: dict) -> dict:
        seq = normalize(seq)
        self.path(seq["id"]).write_text(json.dumps(seq, indent=2))
        return seq

    def delete(self, id_: str) -> None:
        self.path(id_).unlink()


class Player:
    """Plays one sequence at a time; starting another stops the current one."""

    def __init__(self, svc: Services):
        self.svc = svc
        self.current: str | None = None
        self._task: asyncio.Task | None = None
        self._cache: OrderedDict = OrderedDict()  # rendered clips, keyed by (clip, mtime, fx)

    async def play(self, seq: dict) -> None:
        await self.stop()
        self.current = seq["id"]
        self._task = asyncio.create_task(self._run(seq))

    async def stop(self) -> None:
        task = self._task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def wait(self) -> None:
        if self._task:
            await asyncio.shield(self._task)

    def fx_for(self, seq: dict) -> FxParams:
        fx = seq.get("fx")
        if isinstance(fx, str):
            return FxParams.preset(fx)
        if isinstance(fx, dict):
            return FxParams.from_dict(fx)
        return FxParams.from_dict(self.svc.runtime["fx"]["params"])

    def render_clip(self, clip: str, fx: FxParams):
        clips = self.svc.clips
        key = (clip, clips.path(clip).stat().st_mtime, json.dumps(fx.to_dict(), sort_keys=True))
        if key not in self._cache:
            self._cache[key] = self.svc.audio.render(clips.load(clip), fx)
            while len(self._cache) > 16:
                self._cache.popitem(last=False)
        self._cache.move_to_end(key)
        return self._cache[key]

    async def _run(self, seq: dict) -> None:
        svc = self.svc
        eyes_before = svc.outputs.eyes
        try:
            fx = self.fx_for(seq)
            rendered = {}
            for clip in {e["clip"] for e in seq["events"] if e["type"] == "say"}:
                try:
                    rendered[clip] = await asyncio.to_thread(self.render_clip, clip, fx)
                except (FileNotFoundError, ValueError) as e:
                    log.warning("sequence %s: missing clip %s (%s)", seq["id"], clip, e)
            sr = svc.audio.sr
            dur = duration(seq, lambda c: len(rendered[c]) / sr if c in rendered else 0.0)
            svc.bus.publish("seq_started", id=seq["id"], duration=dur)

            loop = asyncio.get_running_loop()
            t0 = loop.time()
            for ev in seq["events"]:
                delay = t0 + ev["t"] - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._exec(ev, rendered)
            remaining = t0 + dur - loop.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        finally:
            svc.audio.stop("seq")
            svc.outputs.set_mouth(False, "sequence")
            svc.outputs.set_eyes(eyes_before, "sequence")
            self.current = None
            svc.bus.publish("seq_stopped", id=seq["id"])

    def _exec(self, ev: dict, rendered: dict) -> None:
        svc = self.svc
        typ = ev["type"]
        if typ == "eyes":
            svc.outputs.set_eyes(ev["value"], "sequence")
        elif typ == "mouth":
            svc.outputs.set_mouth(ev["value"], "sequence")
        elif typ == "say" and ev["clip"] in rendered:
            svc.audio.play(rendered[ev["clip"]], tag="seq")
        elif typ == "dispense":
            svc.spawn(svc.dispense(ev["count"]))
        elif typ == "motor":
            svc.spawn(svc.motor_command(ev["cmd"]))
