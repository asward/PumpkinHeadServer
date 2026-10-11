"""A small stepper4 simulator with a pyserial-like interface (PUMPKIN_SERIAL_PORT=mock).

Good enough to develop the UI and sequences without the board: it acknowledges every
command, runs each motor's queue on a timer, and reports DONE/CANCEL/DROP events.
"""
import queue
import threading
from collections import deque

VERSION = "sim-1.0"


class SimSerial:
    def __init__(self):
        self._out: queue.Queue[bytes] = queue.Queue()
        self._lock = threading.RLock()
        self._closed = False
        self.pos = {m: 0 for m in range(1, 5)}
        self.speed = {m: 1000 for m in range(1, 5)}
        self.queues: dict[int, deque] = {m: deque() for m in range(1, 5)}
        self.current: dict[int, tuple | None] = {m: None for m in range(1, 5)}  # (tag, timer, end_pos)
        self.estopped = False
        threading.Timer(0.3, self._emit, [f"EVT BOOT 0 0 {VERSION}"]).start()

    # ---- pyserial surface
    @property
    def in_waiting(self) -> int:
        return self._out.qsize()

    def read(self, n: int = 1) -> bytes:
        if self._closed:
            raise OSError("port closed")
        try:
            return self._out.get(timeout=0.1)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> int:
        text = data.decode("ascii")
        if "!" in text:
            self._halt_all()
            self._emit("EVT HALT 0 0")
            text = text.replace("!", "")
        for line in text.splitlines():
            if line.strip():
                self._handle(line)
        return len(data)

    def close(self) -> None:
        self._closed = True
        with self._lock:
            for m in self.current:
                if self.current[m]:
                    self.current[m][1].cancel()

    # ---- device behaviour
    def _emit(self, line: str) -> None:
        self._out.put((line + "\r\n").encode("ascii"))

    def _handle(self, line: str) -> None:
        parts = line.strip().upper().split()
        tag = 0
        if parts and parts[0].startswith("#"):
            tag = int(parts[0][1:])
            parts = parts[1:]
        t = f" #{tag}" if tag else ""
        verb, args = parts[0], parts[1:]
        with self._lock:
            try:
                if verb in ("MOVE", "GOTO", "JOG", "WAIT"):
                    if self.estopped:
                        return self._emit(f"ERR{t} ESTOP")
                    m = int(args[0].lstrip("M"))
                    if not 1 <= m <= 4:
                        return self._emit(f"ERR{t} BADMOTOR")
                    if len(self.queues[m]) + (self.current[m] is not None) >= 8:
                        return self._emit(f"ERR{t} QFULL")
                    self.queues[m].append((tag, verb, args[1:]))
                    self._emit(f"OK{t}")
                    self._kick(m)
                elif verb == "STATUS":
                    for m in range(1, 5):
                        cur = self.current[m]
                        state = "ESTOP" if self.estopped else ("RUN" if cur else "IDLE")
                        self._emit(f"STA {m} {state} {self.pos[m]} 0 {len(self.queues[m])} 0 {cur[0] if cur else 0}")
                    self._emit(f"OK{t}")
                elif verb == "INFO":
                    self._emit(f"INF stepper4 {VERSION}")
                    for m in range(1, 5):
                        self._emit(f"CFG {m} {self.speed[m]} 4000 0")
                    self._emit(f"OK{t}")
                elif verb in ("STOP", "HALT", "SKIP", "CLEAR"):
                    m = int(args[0].lstrip("M")) if args else 0
                    for mm in ([m] if m else range(1, 5)):
                        self._cancel(mm, current=verb != "CLEAR", queued=verb != "SKIP")
                    self._emit(f"OK{t}")
                elif verb == "ESTOP":
                    self.estopped = True
                    self._halt_all()
                    self._emit("EVT ESTOP 0 0")
                    self._emit(f"OK{t}")
                elif verb == "RESET":
                    self.estopped = False
                    self._emit(f"OK{t}")
                elif verb == "SPEED":
                    m, sps = int(args[0]), int(args[1])
                    for mm in ([m] if m else range(1, 5)):
                        self.speed[mm] = sps
                    self._emit(f"OK{t}")
                elif verb == "ZERO":
                    self.pos[int(args[0])] = int(args[1]) if len(args) > 1 else 0
                    self._emit(f"OK{t}")
                elif verb in ("ACCEL", "IDLE", "RELEASE", "EN", "WD", "PING"):
                    self._emit(f"OK{t}")
                else:
                    self._emit(f"ERR{t} UNKNOWN")
            except (IndexError, ValueError):
                self._emit(f"ERR{t} SYNTAX")

    def _kick(self, m: int) -> None:
        if self.current[m] or not self.queues[m]:
            return
        tag, verb, args = self.queues[m].popleft()
        speed = int(args[1]) if len(args) > 1 and verb in ("MOVE", "GOTO", "JOG") else self.speed[m]
        end = self.pos[m]
        if verb == "MOVE":
            end += int(args[0])
            secs = abs(int(args[0])) / speed
        elif verb == "GOTO":
            end = int(args[0])
            secs = abs(end - self.pos[m]) / speed
        elif verb == "WAIT":
            secs = int(args[0]) / 1000
        else:  # JOG runs until stopped
            secs = None
        timer = threading.Timer(secs if secs is not None else 1e9, self._finish, [m, tag])
        timer.daemon = True
        self.current[m] = (tag, timer, end)
        timer.start()

    def _finish(self, m: int, tag: int) -> None:
        with self._lock:
            cur = self.current[m]
            if not cur or cur[0] != tag:
                return
            self.pos[m] = cur[2]
            self.current[m] = None
            self._emit(f"EVT DONE {m} {tag}")
            self._kick(m)

    def _cancel(self, m: int, current: bool = True, queued: bool = True) -> None:
        cur = self.current[m]
        if current and cur:
            cur[1].cancel()
            self.current[m] = None
            self._emit(f"EVT CANCEL {m} {cur[0]}")
        if queued:
            while self.queues[m]:
                self._emit(f"EVT DROP {m} {self.queues[m].popleft()[0]}")
        if current and not queued:
            self._kick(m)

    def _halt_all(self) -> None:
        with self._lock:
            for m in range(1, 5):
                self._cancel(m)
