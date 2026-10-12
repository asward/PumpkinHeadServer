"""Owns every subsystem and the background loops that tie them together."""
from __future__ import annotations

import asyncio
import logging
import random
import time

import numpy as np

from .audio import AudioEngine
from .bus import EventBus
from .camera import MicHub, VideoHub
from .clips import ClipStore
from .generator import TEMPLATES
from .gpio import Outputs
from .recorder import Recorder
from .runtime import RuntimeSettings
from .sequences import Player, SequenceStore, duration
from .stepper import Stepper4, StepperError
from .voicefx import PRESETS, FxParams

log = logging.getLogger(__name__)


class Services:
    def __init__(self, cfg):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        self.runtime = RuntimeSettings(cfg.data_dir / "settings.json")
        self.outputs = Outputs(cfg, self._on_output, self.runtime["mouth"],
                               lambda status: self.bus.publish("mouth_status", status=status))
        self.audio = AudioEngine(cfg)
        self.audio.set_fx(FxParams.from_dict(self.runtime["fx"]["params"]))
        self.clips = ClipStore(cfg.data_dir, cfg.sample_rate)
        self.seqs = SequenceStore(cfg.data_dir)
        self.recorder = Recorder(cfg.sample_rate)
        self.player = Player(self)
        self.video = VideoHub(cfg)
        self.mic = MicHub(cfg)
        self.stepper = (Stepper4(cfg.serial_port, cfg.serial_baud, cfg.watchdog_ms, self._on_stepper_event)
                        if cfg.serial_port else None)
        self.estopped = False
        self._dispense_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---- lifecycle
    async def start(self) -> None:
        self.audio.start()
        self.spawn(self._lipsync_loop())
        self.spawn(self._autopilot_loop())
        if self.stepper:
            self.spawn(self.stepper.run())
            self.spawn(self._status_loop())

    async def close(self) -> None:
        await self.player.stop()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.audio.close()
        self.outputs.close()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("background task failed", exc_info=task.exception())

    # ---- snapshots
    def state(self) -> dict:
        st = self.stepper
        return {
            "outputs": {"eyes": self.outputs.eyes, "mouth": self.outputs.mouth,
                        "mouth_mode": self.outputs.mode, "mouth_status": self.outputs.status},
            "backends": {"gpio": self.outputs.backend, "audio": self.audio.backend},
            "stepper": {"enabled": st is not None, "connected": bool(st and st.connected),
                        "version": st.version if st else None, "estopped": self.estopped},
            "candy": {"motor": self.cfg.candy_motor, "steps": self.cfg.candy_steps},
            "fx": self.runtime["fx"],
            "presets": PRESETS,
            "lipsync": self.runtime["lipsync"],
            "mouth": self.runtime["mouth"],
            "autopilot": self.runtime["autopilot"],
            "playing": self.player.current,
            "recording": self.recorder.status(),
            "camera": {"audio": self.mic.enabled, "error": self.video.error},
            "templates": TEMPLATES,
        }

    def sequence_list(self) -> list[dict]:
        return [{"id": s["id"], "name": s["name"], "events": len(s["events"]),
                 "duration": duration(s, self.clips.duration)} for s in self.seqs.list()]

    # ---- event hooks
    def _on_output(self, name: str, value: float, source: str) -> None:
        self.bus.publish("output", name=name, value=value, source=source)
        if self.recorder.active and source == "manual":
            self.recorder.add_event(name, value=value)

    def _on_stepper_event(self, ev: dict) -> None:
        if ev["event"] == "ESTOP":
            self.estopped = True
        elif ev["event"] in ("BOOT", "CONNECTED"):
            self.estopped = False
        self.bus.publish("stepper", **ev, connected=bool(self.stepper and self.stepper.connected),
                         estopped=self.estopped)

    # ---- actions
    async def dispense(self, count: int = 1) -> bool:
        st, cfg = self.stepper, self.cfg
        if not st or not st.connected:
            self.bus.publish("dispense", status="error", error="motors offline")
            return False
        async with self._dispense_lock:
            self.bus.publish("dispense", status="started", count=count)
            try:
                for _ in range(count):
                    done = await st.move(cfg.candy_motor, cfg.candy_steps, cfg.candy_speed)
                    await asyncio.wait_for(done, abs(cfg.candy_steps) / cfg.candy_speed * 3 + 5)
            except (StepperError, TimeoutError) as e:
                self.bus.publish("dispense", status="error", error=str(e) or "timeout")
                return False
            self.bus.publish("dispense", status="done", count=count)
            return True

    async def motor_command(self, cmd: str) -> list[str] | None:
        """Fire-and-forget form used by sequences: logs failures instead of raising."""
        if not self.stepper:
            return None
        try:
            return await self.stepper.send(cmd)
        except StepperError as e:
            log.warning("motor command %r failed: %s", cmd, e)
            return None

    async def stop_all(self) -> None:
        await self.player.stop()
        self.audio.stop()
        self.outputs.halt()

    async def estop(self) -> None:
        st = self.stepper
        if st:
            try:
                st.estop()                  # instant halt, no parser
                await st.command("ESTOP")   # then latch with drivers off
            except StepperError:
                pass
        self.estopped = True
        if self.runtime["autopilot"]["enabled"]:
            self.bus.publish("autopilot", **self.runtime.update("autopilot", {"enabled": False}))
        await self.stop_all()
        self.bus.publish("stepper", event="ESTOP", motor=0, tag=0, extra=[],
                         connected=bool(st and st.connected), estopped=True)

    async def reset(self) -> None:
        st = self.stepper
        if st and st.connected:
            try:
                await st.command("RESET")
            except StepperError as e:
                if e.code == "ESTOP":
                    raise ValueError("motor board refused RESET: its E-stop input is still active") from e
                if e.code != "OFFLINE":
                    raise
        # Offline: the board resets when the link reopens, which clears its own latch, so a dropped
        # link must not leave the server latched with no way out.
        self.estopped = False
        self.bus.publish("stepper", event="RESET", motor=0, tag=0, extra=[],
                         connected=bool(self.stepper and self.stepper.connected), estopped=False)

    def set_fx(self, params: dict, preset: str | None) -> dict:
        p = FxParams.from_dict(params)
        self.audio.set_fx(p)
        fx = self.runtime.update("fx", {"preset": preset, "params": p.to_dict()})
        self.bus.publish("fx", **fx)
        return fx

    # ---- recording
    def start_recording(self, name: str, live: bool) -> dict:
        if self.recorder.active:
            raise ValueError("already recording")
        self.recorder.start(name, live, self.outputs.eyes)
        self.bus.publish("recording", **self.recorder.status())
        return self.recorder.status()

    async def stop_recording(self, save: bool = True) -> dict:
        if not self.recorder.active:
            raise ValueError("not recording")
        name = self.recorder.name
        audio, events = self.recorder.stop()
        self.bus.publish("recording", **self.recorder.status())
        if not save:
            return {}
        sr = self.cfg.sample_rate
        clip_id = None
        loud = np.flatnonzero(np.abs(audio) > 0.01)
        if len(loud):
            # trim leading silence (keeping 50 ms) and start the line at that offset instead
            start = max(0, int(loud[0]) - sr // 20)
            clip_id = self.clips.unique_id(name)
            await asyncio.to_thread(self.clips.save, clip_id, audio[start:])
            events.append({"t": round(start / sr, 3), "type": "say", "clip": clip_id})
        seq = self.seqs.save({"id": self.seqs.unique_id(name), "name": name,
                              "fx": self.runtime["fx"]["params"], "events": events})
        self.bus.publish("library")
        return {"clip": clip_id, "sequence": seq}

    # ---- background loops
    async def _lipsync_loop(self) -> None:
        """Move the mouth with the voice.

        Timed mode: open while loud, flap during long sounds. Center mode: flap while talking with
        the swing scaled by volume, and drive back to center after hang_ms of quiet.
        """
        open_, changed, last_loud = False, 0.0, 0.0
        while True:
            await asyncio.sleep(0.02)
            ls = self.runtime["lipsync"]
            if not ls["enabled"]:
                if open_:
                    open_ = False
                    self.outputs.set_mouth(False, "lipsync")
                continue
            now = time.monotonic()
            env, thr = self.audio.env, ls["threshold"]
            if self.outputs.centered:
                if env > (thr * 0.6 if open_ else thr):
                    last_loud = now
                    self.outputs.set_level((env / thr - 1) / 4)  # full swing at 5x the threshold
                    if not open_:
                        open_ = True
                        self.outputs.set_mouth(True, "lipsync")
                elif open_ and now - last_loud > ls["hang_ms"] / 1000:
                    open_ = False
                    self.outputs.set_mouth(False, "lipsync")
                continue
            held = now - changed
            want = env > (thr * 0.6 if open_ else thr)
            if open_ and want and held > ls["max_open_ms"] / 1000:
                want = False
            if want != open_ and held >= ls["min_ms"] / 1000:
                open_, changed = want, now
                self.outputs.set_mouth(want, "lipsync")

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.bus.has_subscribers and self.stepper.connected:
                try:
                    self.bus.publish("stepper_status", motors=await self.stepper.status())
                except (StepperError, TimeoutError):
                    pass

    async def _autopilot_loop(self) -> None:
        """Play sequences on its own while nobody is puppeteering."""
        idx, next_at = 0, None
        while True:
            await asyncio.sleep(0.5)
            ap = self.runtime["autopilot"]
            busy = self.player.current or self.audio.live_active or self.recorder.active
            if not ap["enabled"] or not ap["sequences"] or busy:
                next_at = None
                continue
            now = time.monotonic()
            if next_at is None:
                next_at = now + random.uniform(ap["min_gap"], max(ap["min_gap"], ap["max_gap"]))
                continue
            if now < next_at:
                continue
            next_at = None
            ids = ap["sequences"]
            if ap["order"] == "cycle":
                sid, idx = ids[idx % len(ids)], idx + 1
            else:
                sid = random.choice(ids)
            try:
                await self.player.play(self.seqs.get(sid))
            except (FileNotFoundError, ValueError) as e:
                log.warning("autopilot: cannot play %s: %s", sid, e)
