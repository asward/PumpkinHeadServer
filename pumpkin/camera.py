"""Camera video (MJPEG) and microphone (PCM) back to the browser.

Each hub runs one capture process shared by all viewers, started on demand and stopped
when the last viewer leaves. Video comes from either:

- v4l2: a USB/UVC webcam read by ffmpeg. A camera that already outputs MJPEG is passed
  through without re-encoding, which keeps a Pi's CPU nearly idle.
- rpicam: a Raspberry Pi CSI (ribbon) camera read by rpicam-vid. On current Raspberry Pi
  OS these cameras go through libcamera; their /dev/video* nodes carry raw sensor data
  that ffmpeg's v4l2 input can't use.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

log = logging.getLogger(__name__)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


class VideoHub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.frame: bytes | None = None
        self.error: str | None = None
        self._seq = 0
        self._cond = asyncio.Condition()
        self._viewers = 0
        self._task: asyncio.Task | None = None
        self._capturing = False

    def _cmd(self) -> list[str]:
        c = self.cfg
        if c.camera_source == "rpicam":
            width, height = c.camera_size.lower().split("x")
            # --verbose 0: no per-frame status on stderr (stderr is only read after exit,
            # so chatty output would fill the pipe and stall capture).
            return ["rpicam-vid", "--nopreview", "--verbose", "0", "--timeout", "0",
                    "--codec", "mjpeg", "--width", width, "--height", height,
                    "--framerate", str(c.camera_fps), "--output", "-"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "v4l2", "-input_format", c.camera_input_format,
               "-video_size", c.camera_size, "-framerate", str(c.camera_fps),
               "-i", c.camera_device]
        if c.camera_input_format == "mjpeg":
            cmd += ["-c:v", "copy"]
        else:
            cmd += ["-c:v", "mjpeg", "-q:v", "7"]
        return cmd + ["-f", "mjpeg", "-"]

    async def frames(self):
        """Async iterator of JPEG frames for one viewer."""
        self._viewers += 1
        if not self._capturing:
            self._capturing = True
            self._task = asyncio.create_task(self._run())
        try:
            last = self._seq
            while True:
                async with self._cond:
                    await self._cond.wait_for(lambda: self._seq != last or not self._capturing)
                if self._seq == last:
                    return  # capture gave up (no ffmpeg)
                last = self._seq
                yield self.frame
        finally:
            self._viewers -= 1

    async def _run(self) -> None:
        try:
            await self._capture()
        finally:
            self._capturing = False
            async with self._cond:
                self._cond.notify_all()  # wake viewers so they notice capture ended

    async def _capture(self) -> None:
        cmd = self._cmd()
        # Keep libcamera's INFO logging off stderr for the same reason as --verbose 0.
        env = {**os.environ, "LIBCAMERA_LOG_LEVELS": os.environ.get("LIBCAMERA_LOG_LEVELS", "*:ERROR")}
        while self._viewers > 0:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            except FileNotFoundError:
                self.error = f"{cmd[0]} not installed"
                log.warning("camera: %s", self.error)
                return
            try:
                await self._pump(proc)
            finally:
                await _kill(proc)
                if proc.stderr:
                    err = (await proc.stderr.read()).decode(errors="replace").strip()
                    if err:
                        self.error = err.splitlines()[-1]
                        log.warning("camera %s: %s", cmd[0], self.error)
            if self._viewers > 0:
                await asyncio.sleep(2)

    async def _pump(self, proc) -> None:
        buf = bytearray()
        while self._viewers > 0:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    buf.clear()
                    break
                end = buf.find(b"\xff\xd9", start + 2)
                if end < 0:
                    del buf[:start]
                    break
                frame = bytes(buf[start:end + 2])
                del buf[:end + 2]
                self.error = None
                async with self._cond:
                    self.frame = frame
                    self._seq += 1
                    self._cond.notify_all()


class MicHub:
    CHUNK_MS = 40

    def __init__(self, cfg):
        self.cfg = cfg
        self.rate = cfg.listen_rate
        self._subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.camera_audio_device)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=25)
        self._subs.add(q)
        if self.enabled and (not self._task or self._task.done()):
            self._task = asyncio.create_task(self._run())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def _run(self) -> None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "alsa", "-i", self.cfg.camera_audio_device,
               "-ac", "1", "-ar", str(self.rate), "-f", "s16le", "-"]
        chunk = self.rate * 2 * self.CHUNK_MS // 1000
        while self._subs:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            except FileNotFoundError:
                log.warning("listen: ffmpeg not installed")
                return
            try:
                while self._subs:
                    data = await proc.stdout.readexactly(chunk)
                    for q in list(self._subs):
                        if q.full():
                            q.get_nowait()  # drop oldest to bound latency
                        q.put_nowait(data)
            except asyncio.IncompleteReadError:
                log.warning("listen: microphone capture stopped")
            finally:
                await _kill(proc)
            if self._subs:
                await asyncio.sleep(2)
