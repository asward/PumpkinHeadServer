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


class Outputs:
    """Current eyes/mouth state. on_change(name, value, source) fires on every change.

    source is one of: manual, lipsync, sequence, safety. Construct and call from the event loop.
    """

    def __init__(self, cfg, on_change: Callable[[str, float, str], None]):
        self.cfg = cfg
        self.on_change = on_change
        self.eyes = 0.0
        self.mouth = False
        self.limit = False           # limit switch currently pressed
        self._hbridge = cfg.mouth_driver == "hbridge"
        self._dir = 0                # direction the motor is being driven: 1 open, -1 close, 0 stopped
        self._backing_off = False
        self._timer: asyncio.TimerHandle | None = None      # flap phase / close-then-stop / back-off
        self._max_timer: asyncio.TimerHandle | None = None  # mouth_max_on_ms cut-off
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._pins = self._make_pins()
        if cfg.mouth_limit_pin >= 0:
            self.limit = self._pins.watch_low(cfg.mouth_limit_pin, self._limit_from_thread)
            if self.limit:
                log.warning("mouth limit switch is pressed at startup; mouth disabled until released")

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
        """1 = open, -1 = close (hbridge only), 0 = stop. Refuses to move while the limit
        switch is pressed, except to back off it."""
        if direction and self.limit and not self._backing_off:
            direction = 0
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

    # ---- mouth: motion
    def _cancel_timers(self) -> None:
        for t in (self._timer, self._max_timer):
            if t:
                t.cancel()
        self._timer = self._max_timer = None

    def _later(self, secs: float, fn, *args) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(secs, fn, *args)

    def _half_cycle(self) -> float:
        return 0.5 / self.cfg.mouth_flap_hz

    def _open_phase(self) -> None:
        self._drive_mouth(1)
        if self.cfg.mouth_flap_hz > 0:
            self._timer = self._later(self._half_cycle(), self._close_phase)

    def _close_phase(self) -> None:
        self._drive_mouth(-1 if self._hbridge else 0)
        self._timer = self._later(self._half_cycle(), self._open_phase)

    def _stop_motion(self) -> None:
        self._timer = None
        self._backing_off = False
        self._drive_mouth(0)

    def set_mouth(self, on: bool, source: str = "manual") -> None:
        on = bool(on)
        if on == self.mouth:
            return
        if on and (self.limit or self._backing_off):
            log.info("mouth: ignoring open from %s while the limit switch is active", source)
            return
        self.mouth = on
        self._cancel_timers()
        if on:
            self._open_phase()
            if self.cfg.mouth_max_on_ms:
                self._max_timer = self._later(self.cfg.mouth_max_on_ms / 1000, self.set_mouth, False, "safety")
        elif self._hbridge and self.cfg.mouth_close_ms:
            self._drive_mouth(-1)
            self._timer = self._later(self.cfg.mouth_close_ms / 1000, self._stop_motion)
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
        if not active or self._backing_off:
            return  # leaving the switch while backing off is expected
        direction = self._dir
        self._cancel_timers()
        if self._hbridge and direction:
            cfg = self.cfg
            secs = (cfg.mouth_limit_backoff_cycles / cfg.mouth_flap_hz if cfg.mouth_flap_hz > 0
                    else cfg.mouth_close_ms / 1000)
            if secs > 0:
                self._backing_off = True
                self._drive_mouth(-direction)
                self._timer = self._later(secs, self._stop_motion)
            else:
                self._drive_mouth(0)
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
