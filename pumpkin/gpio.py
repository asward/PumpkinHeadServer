"""Eyes (LED, optionally PWM-dimmed) and mouth (motor on one pin, or both ways via an H-bridge) outputs.

Mouth motion has two modes:
- timed: flap with fixed open/close drive times (no position feedback).
- center: an H-bridge plus a center switch that is closed while the jaw is on the closed side
  of center and open on the open side. Flapping drives through center, overshoots for a random
  time, and reverses, so it stays centered. Turning the mouth off drives back to the switch's
  edge and stops there.

Uses lgpio, which talks to /dev/gpiochipN and works on every Pi including the Pi 5.
Falls back to a logging mock when lgpio or the chip is unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import random
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
    # timed mode (no center switch)
    "flap": True,          # button/sequence "on" flaps open/closed instead of holding open
    "open_ms": 150,        # drive open this long
    "close_ms": 150,       # drive closed this long
    "end_close_ms": 150,   # H-bridge: drive closed this long when the mouth turns off, then stop. 0 = coast
    # center mode (H-bridge + center switch)
    "swing_min_ms": 60,    # after crossing center, keep going a random time in [min, max] then reverse
    "swing_max_ms": 200,
    "seek_timeout_ms": 1000,  # no center crossing within this while heading for it = stuck: stop
}


class Outputs:
    """Current eyes/mouth state. on_change(name, value, source) fires on every change.

    source is one of: manual, lipsync, sequence, safety. Construct and call from the event loop.
    """

    def __init__(self, cfg, on_change: Callable[[str, float, str], None], motion: dict | None = None,
                 on_status: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.on_change = on_change
        self.on_status = on_status or (lambda status: None)
        self.motion = motion if motion is not None else dict(MOTION_DEFAULTS)
        self.eyes = 0.0
        self.mouth = False
        self._hbridge = cfg.mouth_driver == "hbridge"
        self._dir = 0                # direction being driven now: 1 open, -1 close, 0 stopped
        self._level = 1.0            # swing size 0..1 (lip-sync sets it from the voice volume)
        self._flapping = False       # current "on" flaps (vs. holds open)
        self._seeking = False        # center mode: heading for center to stop there
        self._awaiting = False       # center mode: driving toward center, crossing not seen yet
        self._timer: asyncio.TimerHandle | None = None      # flap phase / end close / swing reversal
        self._timeout: asyncio.TimerHandle | None = None    # center crossing deadline
        self._max_timer: asyncio.TimerHandle | None = None  # mouth_max_on_ms cut-off
        self._rng = random.Random()
        self._at_center = False      # center mode: stopped on the switch edge by a seek
        self._stuck_flag = False     # center mode: last seek/flap timed out
        self.status = ""             # center mode: what the jaw is doing, for the UI
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._pins = self._make_pins()
        self.centered = self._hbridge and cfg.mouth_center_pin >= 0  # center mode
        if cfg.mouth_center_pin >= 0 and not self._hbridge:
            log.warning("PUMPKIN_MOUTH_CENTER_PIN needs PUMPKIN_MOUTH_DRIVER=hbridge; ignoring it")
        self.closed_side = False     # center mode: jaw is on the closed side of center
        if self.centered:
            pressed = self._pins.watch_low(cfg.mouth_center_pin, self._switch_from_thread)
            self.closed_side = pressed != cfg.mouth_center_invert
            self.status = self._compute_status()

    @property
    def mode(self) -> str:
        return "center" if self.centered else "timed"

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
        """1 = open, -1 = close (hbridge only), 0 = stop."""
        self._dir = direction
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

    # ---- mouth: shared
    def _ms(self, key: str) -> float:
        return max(float(self.motion.get(key, MOTION_DEFAULTS[key])), 0.0) / 1000

    def _later(self, secs: float, fn, *args) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(secs, fn, *args)

    def _cancel_timers(self) -> None:
        for t in (self._timer, self._timeout, self._max_timer):
            if t:
                t.cancel()
        self._timer = self._timeout = self._max_timer = None

    def _compute_status(self) -> str:
        if not self.centered:
            return ""
        if self._seeking:
            return "centering"
        if self._flapping and self.mouth:
            return "flapping"
        if self._stuck_flag:
            return "stuck"
        if self._at_center:
            return "centered"
        return "closed side" if self.closed_side else "open side"

    def _update_status(self) -> None:
        status = self._compute_status()
        if status != self.status:
            self.status = status
            self.on_status(status)

    def set_level(self, level: float) -> None:
        """Swing size for the next center-mode reversals, 0..1 (1 = up to swing_max_ms)."""
        self._level = min(max(float(level), 0.0), 1.0)

    def set_mouth(self, on: bool, source: str = "manual") -> None:
        on = bool(on)
        if on == self.mouth:
            return
        self.mouth = on
        self._cancel_timers()
        self._seeking = self._awaiting = False
        self._at_center = self._stuck_flag = False
        if source != "lipsync":
            self._level = 1.0
        if self.centered:
            if on:
                self._flapping = True
                self._toward_center()
            else:
                self._flapping = False
                self._seek_center()
        elif on:
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
        self._update_status()

    def halt(self, source: str = "safety") -> None:
        """Stop the mouth motor where it is (E-stop / stop all / stuck)."""
        self._cancel_timers()
        self._seeking = self._flapping = self._awaiting = False
        self._drive_mouth(0)
        if self.mouth:
            self.mouth = False
            self.on_change("mouth", 0.0, source)
        self._update_status()

    def _stop_motion(self) -> None:
        self._timer = None
        self._drive_mouth(0)

    # ---- mouth: timed mode
    def _open_phase(self) -> None:
        self._drive_mouth(1)
        if self._flapping:
            self._timer = self._later(self._ms("open_ms"), self._close_phase)

    def _close_phase(self) -> None:
        self._drive_mouth(-1 if self._hbridge else 0)
        self._timer = self._later(self._ms("close_ms"), self._open_phase)

    # ---- mouth: center mode
    def _toward_center(self) -> None:
        """Drive toward (and through) center from whichever side the jaw is on."""
        self._timer = None
        self._drive_mouth(1 if self.closed_side else -1)
        self._awaiting = True
        self._arm_timeout()

    def _seek_center(self) -> None:
        """Drive to the center edge and stop the moment the switch changes."""
        self._seeking = True
        self._toward_center()

    def _arm_timeout(self) -> None:
        if self._timeout:
            self._timeout.cancel()
        self._timeout = self._later(self._ms("seek_timeout_ms") or 1.0, self._stuck)

    def _stuck(self) -> None:
        self._timeout = None
        self._stuck_flag = True
        log.warning("mouth: center switch not reached within %d ms; stopping the motor (stuck, or switch "
                    "wired/inverted wrong?)", self.motion.get("seek_timeout_ms", 0))
        self.halt("safety")

    def _swing(self) -> float:
        lo, hi = self._ms("swing_min_ms"), self._ms("swing_max_ms")
        hi = max(hi, lo)
        return self._rng.uniform(lo, lo + (hi - lo) * self._level)

    def _switch_from_thread(self, pressed: bool) -> None:
        """lgpio callback thread. When seeking, stop on the crossing without waiting for the loop."""
        closed_side = pressed != self.cfg.mouth_center_invert
        if self._seeking and closed_side != self.closed_side:
            self._cut()
        if self._loop:
            self._loop.call_soon_threadsafe(self.center_changed, closed_side)

    def center_changed(self, closed_side: bool) -> None:
        if closed_side == self.closed_side:
            return
        self.closed_side = closed_side
        self._at_center = False
        self._on_crossing(closed_side)
        self._update_status()

    def _on_crossing(self, closed_side: bool) -> None:
        if not (self._dir and self._awaiting):
            return  # moved by hand, coasting, or bounce after this sweep's crossing
        # Only a crossing in the direction we're driving counts (opening reaches the open side).
        if (self._dir > 0) == closed_side:
            return
        self._awaiting = False
        if self._timeout:
            self._timeout.cancel()
            self._timeout = None
        if self._seeking:
            self._seeking = False
            self._drive_mouth(0)
            self._at_center = True
        elif self._flapping:
            self._timer = self._later(self._swing(), self._toward_center)

    def close(self) -> None:
        try:
            self._cancel_timers()
            self._drive_mouth(0)
            if self.cfg.eyes_pwm:
                self._pins.pwm(self.cfg.eyes_pin, 0)
            else:
                self._pins.write(self.cfg.eyes_pin, False)
        finally:
            self._pins.close()
