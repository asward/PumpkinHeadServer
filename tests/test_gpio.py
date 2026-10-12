import asyncio

from pumpkin.config import Settings
from pumpkin.gpio import MOTION_DEFAULTS, Outputs

HOLD = {"flap": False}  # hold-open behavior for tests that aren't about flapping


class Recorder:
    """Records pin levels; flags if both H-bridge inputs (27, 22) are ever high together,
    or if an input changes while the enable pin (23) is high."""
    name = "rec"

    def __init__(self):
        self.level = {}
        self.both_high = False
        self.changed_while_enabled = False

    def write(self, pin, on):
        if pin != 23 and self.level.get(23) and self.level.get(pin) != on:
            self.changed_while_enabled = True
        self.level[pin] = on
        if self.level.get(27) and self.level.get(22):
            self.both_high = True

    def pwm(self, pin, duty):
        pass

    def close(self):
        pass


def make(motion=None, changes=None, **kw):
    m = dict(MOTION_DEFAULTS, **(motion or {}))
    cb = (lambda *a: changes.append(a)) if changes is not None else (lambda *a: None)
    out = Outputs(Settings(gpio_backend="mock", **kw), cb, m)
    out._pins = rec = Recorder()
    return out, rec


def run(coro):
    asyncio.run(coro)


def ok(rec):
    return not rec.both_high and not rec.changed_while_enabled


# ---- single pin / hold open

def test_single_pin_on_off():
    async def t():
        out, rec = make(HOLD)
        out.set_mouth(True)
        assert rec.level == {27: True}
        out.set_mouth(False)
        assert rec.level == {27: False}
    run(t())


def test_hbridge_hold_open_then_close_then_stop():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=50), mouth_driver="hbridge")
        out.set_mouth(True)
        assert rec.level == {22: False, 27: True}
        out.set_mouth(False)
        assert rec.level == {22: True, 27: False}
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: False} and ok(rec)
    run(t())


def test_reopen_cancels_pending_stop():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=50), mouth_driver="hbridge")
        out.set_mouth(True)
        out.set_mouth(False)
        out.set_mouth(True)
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: True} and ok(rec)
    run(t())


def test_max_on_cuts_hold_open_but_not_flapping():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=30), mouth_driver="hbridge", mouth_max_on_ms=30)
        out.set_mouth(True)
        await asyncio.sleep(0.05)
        assert out.mouth is False and rec.level[22] is True
        flap, _ = make({"open_ms": 20, "close_ms": 20}, mouth_driver="hbridge", mouth_max_on_ms=30)
        flap.set_mouth(True)
        await asyncio.sleep(0.08)
        assert flap.mouth is True
        flap.halt()
    run(t())


def test_end_close_zero_just_stops():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=0), mouth_driver="hbridge")
        out.set_mouth(True)
        out.set_mouth(False)
        assert rec.level == {22: False, 27: False}
    run(t())


# ---- enable pin

def test_enable_pin_sequencing():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=50), mouth_driver="hbridge", mouth_enable_pin=23)
        out.set_mouth(True)
        assert rec.level == {22: False, 27: True, 23: True}
        out.set_mouth(False)
        assert rec.level == {22: True, 27: False, 23: True}
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: False, 23: False}
        out.set_mouth(True)
        out.close()
        assert rec.level[23] is False and ok(rec)
    run(t())


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


def test_enable_pin_claimed_only_for_hbridge():
    assert 23 in _claimed(mouth_driver="hbridge", mouth_enable_pin=23)
    assert 23 not in _claimed(mouth_enable_pin=23)
    assert _claimed(mouth_driver="hbridge") == [17, 27, 22]


# ---- flapping

def test_flap_alternates_with_asymmetric_timing():
    async def t():
        out, rec = make({"open_ms": 40, "close_ms": 80, "end_close_ms": 20}, mouth_driver="hbridge")
        out.set_mouth(True)
        assert rec.level == {22: False, 27: True}
        await asyncio.sleep(0.06)                 # past 40 ms open
        assert rec.level == {22: True, 27: False}
        await asyncio.sleep(0.04)                 # 100 ms: still inside the 80 ms close
        assert rec.level == {22: True, 27: False}
        await asyncio.sleep(0.04)                 # 140 ms: open again
        assert rec.level == {22: False, 27: True}
        out.set_mouth(False)
        assert rec.level == {22: True, 27: False}
        await asyncio.sleep(0.05)
        assert rec.level == {22: False, 27: False}
        await asyncio.sleep(0.1)
        assert rec.level == {22: False, 27: False} and ok(rec)
    run(t())


def test_flap_single_pin():
    async def t():
        out, rec = make({"open_ms": 40, "close_ms": 40})
        out.set_mouth(True)
        assert rec.level == {27: True}
        await asyncio.sleep(0.06)
        assert rec.level == {27: False}
        await asyncio.sleep(0.04)
        assert rec.level == {27: True}
        out.set_mouth(False)
        assert rec.level == {27: False}
    run(t())


def test_lipsync_never_flaps():
    async def t():
        out, rec = make({"open_ms": 20, "close_ms": 20}, mouth_driver="hbridge")
        out.set_mouth(True, "lipsync")
        await asyncio.sleep(0.06)
        assert rec.level == {22: False, 27: True}
        out.halt()
    run(t())


def test_settings_change_applies_live():
    async def t():
        motion = dict(MOTION_DEFAULTS, open_ms=20, close_ms=20)
        out = Outputs(Settings(gpio_backend="mock"), lambda *a: None, motion)
        out._pins = rec = Recorder()
        motion["flap"] = False            # what runtime.update does to the shared dict
        out.set_mouth(True)
        await asyncio.sleep(0.05)
        assert rec.level == {27: True}
        out.halt()
    run(t())


# ---- limit switch

def test_limit_hit_while_driving_cuts_then_backs_off():
    async def t():
        changes = []
        out, rec = make(dict(HOLD, open_ms=10, close_ms=10, backoff_cycles=2.5), changes,
                        mouth_driver="hbridge", mouth_enable_pin=23, mouth_limit_pin=24)
        out.set_mouth(True)                              # driving open
        out._limit_from_thread(True)                     # switch thread: cut first
        assert rec.level[23] is False
        await asyncio.sleep(0)                           # loop handles the rest
        assert out.limit and out.mouth is False
        assert rec.level == {22: True, 27: False, 23: True}  # backing off: closing
        assert ("mouth", 0.0, "safety") in changes and ("mouth_limit", 1.0, "safety") in changes
        out.limit_changed(False)                         # releases mid back-off
        await asyncio.sleep(0.07)                        # 2.5 cycles x 20 ms = 50 ms
        assert rec.level == {22: False, 27: False, 23: False} and ok(rec)
    run(t())


def test_limit_hit_while_sagging_backs_off_upward():
    """Jaw closed and stopped, then sags onto the switch: back off by opening."""
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=10, open_ms=10, close_ms=10, backoff_cycles=2),
                        mouth_driver="hbridge", mouth_limit_pin=24)
        out.set_mouth(True)
        out.set_mouth(False)
        await asyncio.sleep(0.03)                        # close finished, motor stopped
        assert rec.level == {22: False, 27: False}
        out.limit_changed(True)                          # sags onto the switch
        assert rec.level == {22: False, 27: True}        # driving open, away from it
        await asyncio.sleep(0.06)
        assert rec.level == {22: False, 27: False} and ok(rec)
    run(t())


def test_still_pressed_after_backoff_can_drive_away_not_into():
    async def t():
        out, rec = make(dict(HOLD, end_close_ms=10, open_ms=10, close_ms=10, backoff_cycles=1),
                        mouth_driver="hbridge", mouth_limit_pin=24)
        out.set_mouth(True)
        out.set_mouth(False)                             # closing (-1)
        out.limit_changed(True)                          # hits bottom switch while closing
        await asyncio.sleep(0.04)                        # back-off done, switch still pressed
        assert out.limit
        out._drive_mouth(-1)                             # further down: refused
        assert rec.level == {22: False, 27: False}
        out.set_mouth(True)                              # mouth button: up, allowed
        assert out.mouth is True and rec.level == {22: False, 27: True}
        out.limit_changed(False)
        out.halt()
    run(t())


def test_limit_at_startup_does_not_block_either_way():
    async def t():
        out, rec = make(HOLD, mouth_driver="hbridge", mouth_limit_pin=24)
        out.limit = True                                 # as if pressed at startup
        out.set_mouth(True)
        assert rec.level[27] is True
        out.halt()
    run(t())


def test_single_pin_limit_when_coasting_does_not_block_open():
    async def t():
        out, rec = make(HOLD, mouth_limit_pin=24)
        out.set_mouth(True)
        out.set_mouth(False)                             # spring closes onto the switch
        out.limit_changed(True)
        out.set_mouth(True)
        assert rec.level == {27: True}
        out.halt()
    run(t())


def test_halt_stops_without_closing_move():
    async def t():
        out, rec = make({"open_ms": 20, "close_ms": 20}, mouth_driver="hbridge")
        out.set_mouth(True)
        out.halt()
        assert out.mouth is False and rec.level == {22: False, 27: False}
        await asyncio.sleep(0.08)
        assert rec.level == {22: False, 27: False}
    run(t())
