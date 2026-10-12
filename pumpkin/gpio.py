"""Eyes (LED, optionally PWM-dimmed) and mouth (motor on one pin, or both ways via an H-bridge) outputs.

Uses lgpio, which talks to /dev/gpiochipN and works on every Pi including the Pi 5.
Falls back to a logging mock when lgpio or the chip is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)


class _MockPins:
    name = "mock"

    def write(self, pin: int, on: bool) -> None:
        log.debug("gpio %d -> %d", pin, on)

    def pwm(self, pin: int, duty: float) -> None:
        log.debug("gpio %d -> pwm %.2f", pin, duty)

    def watch_low(self, pin: int, callback: Callable[[bool], None]) -> bool:
        """No input on the mock; reports the switch as released."""
        return False

    def close(self) -> None:
        pass


class _LgpioPins:
    name = "lgpio"

    def __init__(self, chip: int, pins: list[int], active_high: bool, pwm_hz: int):
        import lgpio
        self.lg = lgpio
        self.h = lgpio.gpiochip_open(chip)
        self.active_high = active_high
        self.pwm_hz = pwm_hz
        self._callbacks = []
        for pin in pins:
            lgpio.gpio_claim_output(self.h, pin, self._level(False))

    def watch_low(self, pin: int, callback: Callable[[bool], None]) -> bool:
        """Pulled-up input; callback(active) runs on lgpio's thread when the pin goes low (True)
        or back high (False). Returns whether it is low right now."""
        lg = self.lg
        lg.gpio_claim_alert(self.h, pin, lg.BOTH_EDGES, lg.SET_PULL_UP)
        lg.gpio_set_debounce_micros(self.h, pin, 2000)

        def fire(_chip, _gpio, level, _tick):
            if level in (0, 1):  # 2 = watchdog timeout
                callback(level == 0)
        self._callbacks.append(lg.callback(self.h, pin, lg.BOTH_EDGES, fire))
        return lg.gpio_read(self.h, pin) == 0

    def _level(self, on: bool) -> int:
        return int(on == self.active_high)

    def write(self, pin: int, on: bool) -> None:
        self.lg.gpio_write(self.h, pin, self._level(on))

    def pwm(self, pin: int, duty: float) -> None:
        if 0 < duty < 1:
            pct = duty * 100 if self.active_high else (1 - duty) * 100
            self.lg.tx_pwm(self.h, pin, self.pwm_hz, pct)
        else:
            self.lg.tx_pwm(self.h, pin, 0, 0)  # frequency 0 stops PWM
            self.write(pin, duty >= 1)

    def close(self) -> None:
        for cb in self._callbacks:
            cb.cancel()
        self.lg.gpiochip_close(self.h)


# Operator-tunable mouth motion; Services swaps in the persisted runtime["mouth"] dict.
MOTION_DEFAULTS = {
    "flap": True,         # button/sequence "on" flaps open/closed instead of holding open
    "open_ms": 150,       # flap: drive open this long
    "close_ms": 150,      # flap: drive closed this long (raise it if the jaw creeps open, lower if it sags)
    "end_close_ms": 150,  # H-bridge: drive closed this long when the mouth turns off, then stop. 0 = coast
    "backoff_cycles": 10,  # limit hit: drive away for this many flap cycles (open_ms + close_ms each)
}


class Outputs:
    """Current eyes/mouth state. on_change(name, value, source) fires on every change.

    source is one of: manual, lipsync, sequence, safety. Construct and call from the event loop.
    Lip-sync does its own flapping, so it always drives the mouth directly.
    """

    def __init__(self, cfg, on_change: Callable[[str, float, str], None], motion: dict | None = None):
        self.cfg = cfg
        self.on_change = on_change
        self.motion = motion if motion is not None else dict(MOTION_DEFAULTS)
        self.eyes = 0.0
        self.mouth = False
        self.limit = False           # limit switch currently pressed
        self._hbridge = cfg.mouth_driver == "hbridge"
        self._dir = 0                # direction being driven now: 1 open, -1 close, 0 stopped
        self._last_dir = 0           # last non-zero direction (where a coasting jaw is heading)
        self._limit_dir = 0          # direction that ran into the switch; refused while it is pressed
        self._backing_off = False
        self._flapping = False       # current "on" flaps (vs. holds open)
        self._timer: asyncio.TimerHandle | None = None      # flap phase / end close / back-off
        self._max_timer: asyncio.TimerHandle | None = None  # mouth_max_on_ms cut-off
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._pins = self._make_pins()
        if cfg.mouth_limit_pin >= 0:
            self.limit = self._pins.watch_low(cfg.mouth_limit_pin, self._limit_from_thread)
            if self.limit:
                log.warning("mouth limit switch is pressed at startup; drive the mouth off it")

    def _make_pins(self):
        cfg = self.cfg
        pins = [cfg.eyes_pin, cfg.mouth_pin]
        if self._hbridge:
            pins.append(cfg.mouth_close_pin)
            if cfg.mouth_enable_pin >= 0:
                pins.append(cfg.mouth_enable_pin)
        if cfg.gpio_backend in ("auto", "lgpio"):
            try:
                return _LgpioPins(cfg.gpio_chip, pins, cfg.active_high, cfg.eyes_pwm_hz)
            except Exception as e:
                if cfg.gpio_backend == "lgpio":
                    raise
                log.warning("GPIO unavailable (%s); using mock outputs", e)
        return _MockPins()

    @property
    def backend(self) -> str:
        return self._pins.name

    def set_eyes(self, value: float, source: str = "manual") -> None:
        value = min(max(float(value), 0.0), 1.0)
        if not self.cfg.eyes_pwm:
            value = 1.0 if value >= 0.5 else 0.0
        if value == self.eyes:
            return
        self.eyes = value
        if self.cfg.eyes_pwm:
            self._pins.pwm(self.cfg.eyes_pin, value)
        else:
            self._pins.write(self.cfg.eyes_pin, value >= 1)
        self.on_change("eyes", value, source)

    # ---- mouth: pin level
    def _cut(self) -> None:
        """Remove drive immediately: enable low if wired, otherwise the inputs."""
        if self._hbridge and self.cfg.mouth_enable_pin >= 0:
            self._pins.write(self.cfg.mouth_enable_pin, False)
        else:
            self._pins.write(self.cfg.mouth_pin, False)
            if self._hbridge:
                self._pins.write(self.cfg.mouth_close_pin, False)

    def _drive_mouth(self, direction: int) -> None:
        """1 = open, -1 = close (hbridge only), 0 = stop. While the limit switch is pressed,
        the direction that ran into it is refused; moving away is always allowed."""
        if direction and self.limit and direction == self._limit_dir and not self._backing_off:
            direction = 0
        self._dir = direction
        if direction:
            self._last_dir = direction
        if not self._hbridge:
            self._pins.write(self.cfg.mouth_pin, direction > 0)
            return
        a, b, en = self.cfg.mouth_pin, self.cfg.mouth_close_pin, self.cfg.mouth_enable_pin
        # Disable the bridge while the inputs change, and re-enable only once they're set.
        if en >= 0:
            self._pins.write(en, False)
        # Release the active input before raising the other so both are never high together.
        if direction > 0:
            self._pins.write(b, False)
            self._pins.write(a, True)
        elif direction < 0:
            self._pins.write(a, False)
            self._pins.write(b, True)
        else:
            self._pins.write(a, False)
            self._pins.write(b, False)
        if en >= 0 and direction:
            self._pins.write(en, True)

    # ---- mouth: motion
    def _ms(self, key: str) -> float:
        return max(float(self.motion.get(key, MOTION_DEFAULTS[key])), 0.0) / 1000

    def _cancel_timers(self) -> None:
        for t in (self._timer, self._max_timer):
            if t:
                t.cancel()
        self._timer = self._max_timer = None

    def _later(self, secs: float, fn, *args) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(secs, fn, *args)

    def _open_phase(self) -> None:
        self._drive_mouth(1)
        if self._flapping:
            self._timer = self._later(self._ms("open_ms"), self._close_phase)

    def _close_phase(self) -> None:
        self._drive_mouth(-1 if self._hbridge else 0)
        self._timer = self._later(self._ms("close_ms"), self._open_phase)

    def _stop_motion(self) -> None:
        self._timer = None
        self._backing_off = False
        self._drive_mouth(0)

    def set_mouth(self, on: bool, source: str = "manual") -> None:
        on = bool(on)
        if on == self.mouth:
            return
        if on and self._backing_off:
            log.info("mouth: ignoring open from %s while backing off the limit switch", source)
            return
        self.mouth = on
        self._cancel_timers()
        if on:
            self._flapping = (bool(self.motion.get("flap")) and source != "lipsync"
                              and self._ms("open_ms") > 0 and self._ms("close_ms") > 0)
            self._open_phase()
            # Max-on guards against stalling against the open stop; a flapping jaw isn't stalled.
            if self.cfg.mouth_max_on_ms and not self._flapping:
                self._max_timer = self._later(self.cfg.mouth_max_on_ms / 1000, self.set_mouth, False, "safety")
        elif self._hbridge and self._ms("end_close_ms"):
            self._drive_mouth(-1)
            self._timer = self._later(self._ms("end_close_ms"), self._stop_motion)
        else:
            self._drive_mouth(0)
        self.on_change("mouth", float(on), source)

    def halt(self, source: str = "safety") -> None:
        """Stop the mouth motor where it is, with no closing move (E-stop / stop all)."""
        self._cancel_timers()
        self._backing_off = False
        self._drive_mouth(0)
        if self.mouth:
            self.mouth = False
            self.on_change("mouth", 0.0, source)

    # ---- limit switch
    def _limit_from_thread(self, active: bool) -> None:
        """Runs on lgpio's callback thread: cut the drive first, then let the loop sort out the rest."""
        if active and not self._backing_off:
            self._cut()
        if self._loop:
            self._loop.call_soon_threadsafe(self.limit_changed, active)

    def limit_changed(self, active: bool) -> None:
        if active == self.limit:
            return
        self.limit = active
        log.info("mouth limit switch %s", "hit" if active else "released")
        self.on_change("mouth_limit", float(active), "safety")
        if not active:
            self._limit_dir = 0
            return
        if self._backing_off:
            return  # bounce while leaving the switch
        # Which way was the jaw going? Driven now, or (if coasting, e.g. sagging under its own
        # weight after a close) the last way it was driven. Single-pin mode only drives open.
        heading = self._dir or (self._last_dir if self._hbridge else 0)
        self._limit_dir = heading
        self._cancel_timers()
        secs = self.motion.get("backoff_cycles", 0) * (self._ms("open_ms") + self._ms("close_ms"))
        if self._hbridge and heading and secs > 0:
            self._backing_off = True
            self._drive_mouth(-heading)
            self._timer = self._later(secs, self._stop_motion)
        else:
            self._drive_mouth(0)
        if self.mouth:
            self.mouth = False
            self.on_change("mouth", 0.0, "safety")

    def close(self) -> None:
        try:
            self._cancel_timers()
            self._backing_off = False
            self._drive_mouth(0)
            if self.cfg.eyes_pwm:
                self._pins.pwm(self.cfg.eyes_pin, 0)
            else:
                self._pins.write(self.cfg.eyes_pin, False)
        finally:
            self._pins.close()
