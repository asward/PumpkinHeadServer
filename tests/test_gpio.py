import asyncio

from pumpkin.config import Settings
from pumpkin.gpio import Outputs


class Recorder:
    name = "rec"

    def __init__(self):
        self.level = {}
        self.both_high = False

    def write(self, pin, on):
        self.level[pin] = on
        if self.level.get(27) and self.level.get(22):
            self.both_high = True

    def pwm(self, pin, duty):
        pass

    def close(self):
        pass


def make(**kw):
    out = Outputs(Settings(gpio_backend="mock", **kw), lambda *a: None)
    out._pins = rec = Recorder()
    return out, rec


def test_single_pin_on_off():
    async def run():
        out, rec = make()
        out.set_mouth(True)
        assert rec.level == {27: True}
        out.set_mouth(False)
        assert rec.level == {27: False}
    asyncio.run(run())


def test_hbridge_open_close_then_stop():
    async def run():
        out, rec = make(mouth_driver="hbridge", mouth_close_ms=50)
        out.set_mouth(True)
        assert rec.level == {22: False, 27: True}
        out.set_mouth(False)
        assert rec.level == {22: True, 27: False}
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: False}
        assert not rec.both_high
    asyncio.run(run())


def test_hbridge_reopen_cancels_close_stop():
    async def run():
        out, rec = make(mouth_driver="hbridge", mouth_close_ms=50)
        out.set_mouth(True)
        out.set_mouth(False)
        out.set_mouth(True)
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: True}  # still driving open
        assert not rec.both_high
    asyncio.run(run())


def test_hbridge_safety_cutoff_closes():
    async def run():
        out, rec = make(mouth_driver="hbridge", mouth_max_on_ms=30, mouth_close_ms=30)
        out.set_mouth(True)
        await asyncio.sleep(0.05)
        assert out.mouth is False and rec.level[22] is True
        await asyncio.sleep(0.05)
        assert rec.level == {22: False, 27: False}
    asyncio.run(run())


def test_hbridge_close_ms_zero_just_stops():
    async def run():
        out, rec = make(mouth_driver="hbridge", mouth_close_ms=0)
        out.set_mouth(True)
        out.set_mouth(False)
        assert rec.level == {22: False, 27: False}
    asyncio.run(run())


class OrderRecorder(Recorder):
    """Fails if an input pin changes while the enable pin (23) is high."""

    def __init__(self):
        super().__init__()
        self.changed_while_enabled = False

    def write(self, pin, on):
        if pin != 23 and self.level.get(23) and self.level.get(pin) != on:
            self.changed_while_enabled = True
        super().write(pin, on)


def test_hbridge_enable_pin():
    async def run():
        out = Outputs(Settings(gpio_backend="mock", mouth_driver="hbridge",
                               mouth_enable_pin=23, mouth_close_ms=50), lambda *a: None)
        out._pins = rec = OrderRecorder()
        out.set_mouth(True)
        assert rec.level == {22: False, 27: True, 23: True}
        out.set_mouth(False)
        assert rec.level == {22: True, 27: False, 23: True}
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: False, 23: False}
        out.set_mouth(True)
        out.close()
        assert rec.level[23] is False
        assert not rec.changed_while_enabled and not rec.both_high
    asyncio.run(run())


def test_enable_pin_claimed_only_for_hbridge():
    assert 23 in _claimed(mouth_driver="hbridge", mouth_enable_pin=23)
    assert 23 not in _claimed(mouth_enable_pin=23)
    assert _claimed(mouth_driver="hbridge") == [17, 27, 22]


def _claimed(**kw):
    import pumpkin.gpio as g
    seen = {}

    class FakeLg:
        name = "lgpio"

        def __init__(self, chip, pins, active_high, hz):
            seen["pins"] = pins

    orig = g._LgpioPins
    g._LgpioPins = FakeLg
    try:
        Outputs(Settings(gpio_backend="lgpio", **kw), lambda *a: None)
    finally:
        g._LgpioPins = orig
    return seen["pins"]
