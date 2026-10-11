"""Async client for the stepper4 motor board (see "stepper4 Serial Interface Spec.md").

A reader thread splits incoming bytes into lines and hands them to the event loop.
Commands are sent one at a time under a lock: the board replies to every command
immediately, so serialising them costs nothing and makes the untagged data lines
(STA/INF/CFG) unambiguous. Motion commands also get a future that resolves on their
terminal EVT (DONE, or raises MotionError for CANCEL/DROP/FAIL/LIMIT/FAULT).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

TERMINAL_EVENTS = {"DONE", "CANCEL", "DROP", "FAIL", "LIMIT", "FAULT"}
MOTION_VERBS = {"MOVE", "GOTO", "JOG", "WAIT"}
# Verbs the UI and sequences may send. WD is owned by this client.
ALLOWED_VERBS = MOTION_VERBS | {
    "STOP", "SKIP", "HALT", "CLEAR", "SPEED", "ACCEL", "IDLE", "ZERO",
    "RELEASE", "STATUS", "INFO", "EN", "ESTOP", "RESET", "PING",
}
MAX_LINE = 63


class StepperError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MotionError(StepperError):
    """A motion command ended with something other than DONE."""

    def __init__(self, event: str, extra: list[str]):
        super().__init__(" ".join([event, *extra]))
        self.event = event
        self.extra = extra


def open_serial(port: str, baud: int):
    if port == "mock":
        from .stepper_sim import SimSerial
        return SimSerial()
    import serial
    return serial.Serial(port, baud, timeout=0.1, write_timeout=1)


def _consume(fut: asyncio.Future) -> None:
    # Nobody may await a motion future; retrieve the exception so asyncio doesn't warn.
    if not fut.cancelled():
        fut.exception()


class Stepper4:
    def __init__(self, port: str, baud: int = 115200, watchdog_ms: int = 1000,
                 on_event: Callable[[dict], None] | None = None,
                 opener: Callable = open_serial):
        self.port = port
        self.baud = baud
        self.watchdog_ms = watchdog_ms
        self.on_event = on_event or (lambda ev: None)
        self._opener = opener
        self.connected = False
        self.version: str | None = None
        self._ser = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self._write_lock = threading.Lock()
        self._tag = 0
        self._reply: asyncio.Future | None = None
        self._reply_tag = 0
        self._data: list[list[str]] = []
        self._motion: dict[int, asyncio.Future] = {}
        self._booted = asyncio.Event()
        self._dead = asyncio.Event()

    # ---- lifecycle -------------------------------------------------------

    async def run(self) -> None:
        """Connect, keep the watchdog fed, and reconnect forever."""
        self._loop = asyncio.get_running_loop()
        while True:
            try:
                await self._connect()
                await self._keepalive()
            except asyncio.CancelledError:
                self._close()
                raise
            except Exception as e:
                log.warning("stepper4 on %s: %s", self.port, e)
            self._close()
            await asyncio.sleep(3)

    async def _connect(self) -> None:
        self._booted.clear()
        self._dead.clear()
        ser = await asyncio.to_thread(self._opener, self.port, self.baud)
        self._ser = ser
        threading.Thread(target=self._reader, args=(ser,), daemon=True, name="stepper4-rx").start()
        # Opening the port resets the Uno, which announces itself with EVT BOOT.
        try:
            await asyncio.wait_for(self._booted.wait(), 4)
        except TimeoutError:
            log.info("stepper4: no EVT BOOT seen, continuing anyway")
        await self._init_device()

    async def _init_device(self) -> None:
        await self.command(f"WD {self.watchdog_ms}")
        self.connected = True
        log.info("stepper4 connected on %s (firmware %s)", self.port, self.version)
        self._emit("CONNECTED", 0, 0, [self.version or ""])

    async def _keepalive(self) -> None:
        interval = self.watchdog_ms / 3000 if self.watchdog_ms else 1.0
        while not self._dead.is_set():
            try:
                await asyncio.wait_for(self._dead.wait(), interval)
            except TimeoutError:
                await self.command("PING")
        raise ConnectionError("serial link lost")

    def _close(self) -> None:
        was_connected = self.connected
        self.connected = False
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        for fut in self._motion.values():
            if not fut.done():
                fut.set_exception(StepperError("OFFLINE"))
        self._motion.clear()
        if self._reply and not self._reply.done():
            self._reply.set_exception(StepperError("OFFLINE"))
        if was_connected:
            self._emit("DISCONNECTED", 0, 0, [])

    # ---- I/O -------------------------------------------------------------

    def _reader(self, ser) -> None:
        buf = bytearray()
        while self._ser is ser:
            try:
                chunk = ser.read(max(1, ser.in_waiting))
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while (i := buf.find(b"\n")) >= 0:
                line = buf[:i].decode("ascii", "replace").strip()
                del buf[:i + 1]
                if line:
                    self._post(self._on_line, line)
        self._post(self._lost, ser)

    def _post(self, fn, *args) -> None:
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass  # event loop already closed (shutdown)

    def _lost(self, ser) -> None:
        if ser is self._ser:
            self._dead.set()

    def _write(self, data: bytes) -> None:
        ser = self._ser
        if ser is None:
            raise StepperError("OFFLINE")
        with self._write_lock:
            ser.write(data)

    def _next_tag(self) -> int:
        self._tag = self._tag % 65535 + 1
        return self._tag

    async def _send(self, line: str, track: bool, timeout: float = 2.0):
        line = " ".join(line.split())
        async with self._lock:
            tag = self._next_tag()
            text = f"#{tag} {line}"
            if len(text) > MAX_LINE:
                raise StepperError("LINE_TOO_LONG")
            loop = asyncio.get_running_loop()
            reply = loop.create_future()
            done = None
            if track:
                done = loop.create_future()
                done.add_done_callback(_consume)
                self._motion[tag] = done
            self._reply, self._reply_tag, self._data = reply, tag, []
            try:
                self._write((text + "\n").encode("ascii"))
                data = await asyncio.wait_for(reply, timeout)
            except BaseException:
                if track:
                    self._motion.pop(tag, None)
                    done.cancel()
                raise
            finally:
                self._reply = None
        return data, done

    def _on_line(self, text: str) -> None:
        parts = text.split()
        head = parts[0].upper()
        if head in ("OK", "ERR"):
            rest = parts[1:]
            tag = None
            if rest and rest[0].startswith("#"):
                tag = int(rest[0][1:]) if rest[0][1:].isdigit() else None
                rest = rest[1:]
            fut = self._reply
            if fut and not fut.done() and tag in (None, self._reply_tag):
                if head == "OK":
                    fut.set_result(self._data)
                else:
                    fut.set_exception(StepperError(rest[0] if rest else "UNKNOWN"))
                self._data = []
            else:
                log.debug("stepper4: unmatched reply %r", text)
        elif head in ("STA", "INF", "CFG"):
            self._data.append(parts)
        elif head == "EVT":
            self._on_evt(parts)
        else:
            log.debug("stepper4: ignored %r", text)

    def _on_evt(self, parts: list[str]) -> None:
        try:
            typ, motor, tag = parts[1].upper(), int(parts[2]), int(parts[3])
        except (IndexError, ValueError):
            log.warning("stepper4: malformed event %r", " ".join(parts))
            return
        extra = parts[4:]
        if typ == "BOOT":
            self.version = extra[-1] if extra else None
            self._booted.set()
            if self.connected:
                log.warning("stepper4 rebooted; motor positions are unverified")
                self._loop.create_task(self._reinit())
        if typ in TERMINAL_EVENTS:
            fut = self._motion.pop(tag, None)
            if fut and not fut.done():
                if typ == "DONE":
                    fut.set_result(typ)
                else:
                    fut.set_exception(MotionError(typ, extra))
        self._emit(typ, motor, tag, extra)

    async def _reinit(self) -> None:
        try:
            await self._init_device()
        except Exception as e:
            log.warning("stepper4 re-init failed: %s", e)

    def _emit(self, typ: str, motor: int, tag: int, extra: list[str]) -> None:
        try:
            self.on_event({"event": typ, "motor": motor, "tag": tag, "extra": extra})
        except Exception:
            log.exception("stepper4 event handler failed")

    # ---- public API ------------------------------------------------------

    async def command(self, line: str) -> list[list[str]]:
        """Send an immediate command and return its data lines (split on spaces)."""
        data, _ = await self._send(line, track=False)
        return data

    async def motion(self, line: str) -> asyncio.Future:
        """Queue a motion command. Returns a future for its terminal event."""
        _, done = await self._send(line, track=True)
        return done

    async def send(self, line: str) -> list[str]:
        """Send an operator-supplied command, restricted to ALLOWED_VERBS."""
        words = line.split()
        verb = words[0].upper() if words else ""
        if verb not in ALLOWED_VERBS:
            raise StepperError("NOTALLOWED")
        data, _ = await self._send(line, track=verb in MOTION_VERBS)
        return [" ".join(p) for p in data]

    async def move(self, motor: int, steps: int, speed: int | None = None) -> asyncio.Future:
        return await self.motion(f"MOVE {motor} {steps}" + (f" {speed}" if speed else ""))

    async def status(self) -> dict[int, dict]:
        out = {}
        for p in await self.command("STATUS"):
            if p[0] == "STA" and len(p) >= 8:
                out[int(p[1])] = {
                    "state": p[2], "pos": int(p[3]), "rate": int(p[4]),
                    "queued": int(p[5]), "limit": p[6] == "1", "tag": int(p[7]),
                }
        return out

    def estop(self) -> None:
        """Real-time halt: the single byte '!' bypasses the line parser."""
        self._write(b"!")
