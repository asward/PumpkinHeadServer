"""Eyes (LED, optionally PWM-dimmed) and mouth (discrete motor) outputs.

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
        for pin in pins:
            lgpio.gpio_claim_output(self.h, pin, self._level(False))

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
        self.lg.gpiochip_close(self.h)


class Outputs:
    """Current eyes/mouth state. on_change(name, value, source) fires on every change.

    source is one of: manual, lipsync, sequence, safety.
    """

    def __init__(self, cfg, on_change: Callable[[str, float, str], None]):
        self.cfg = cfg
        self.on_change = on_change
        self.eyes = 0.0
        self.mouth = False
        self._mouth_timer: asyncio.TimerHandle | None = None
        self._pins = self._make_pins()

    def _make_pins(self):
        cfg = self.cfg
        if cfg.gpio_backend in ("auto", "lgpio"):
            try:
                return _LgpioPins(cfg.gpio_chip, [cfg.eyes_pin, cfg.mouth_pin], cfg.active_high, cfg.eyes_pwm_hz)
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

    def set_mouth(self, on: bool, source: str = "manual") -> None:
        on = bool(on)
        if on == self.mouth:
            return
        self.mouth = on
        self._pins.write(self.cfg.mouth_pin, on)
        if self._mouth_timer:
            self._mouth_timer.cancel()
            self._mouth_timer = None
        if on and self.cfg.mouth_max_on_ms:
            self._mouth_timer = asyncio.get_running_loop().call_later(
                self.cfg.mouth_max_on_ms / 1000, self.set_mouth, False, "safety")
        self.on_change("mouth", float(on), source)

    def close(self) -> None:
        try:
            self._pins.write(self.cfg.mouth_pin, False)
            if self.cfg.eyes_pwm:
                self._pins.pwm(self.cfg.eyes_pin, 0)
            else:
                self._pins.write(self.cfg.eyes_pin, False)
        finally:
            self._pins.close()
