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


# ---- center switch

CENTER = dict(mouth_driver="hbridge", mouth_center_pin=24)
FIXED = {"swing_min_ms": 30, "swing_max_ms": 30, "seek_timeout_ms": 1000}
OPEN, CLOSE, STOP = {22: False, 27: True}, {22: True, 27: False}, {22: False, 27: False}


def centered(motion=FIXED, closed_side=True, changes=None, **kw):
    out, rec = make(motion, changes, **dict(CENTER, **kw))
    out.closed_side = closed_side
    return out, rec


def test_center_mode_detected():
    assert centered()[0].mode == "center"
    assert make(mouth_center_pin=24)[0].mode == "timed"     # needs an H-bridge
    assert make(mouth_driver="hbridge")[0].mode == "timed"


def test_center_flap_crosses_overshoots_and_reverses():
    async def t():
        out, rec = centered()
        out.set_mouth(True)
        assert rec.level == OPEN                 # closed side: head for center by opening
        out.center_changed(False)                # crossed into the open side
        assert rec.level == OPEN                 # keep going for the swing
        await asyncio.sleep(0.05)
        assert rec.level == CLOSE                # reversed toward center
        out.center_changed(True)                 # crossed into the closed side
        await asyncio.sleep(0.05)
        assert rec.level == OPEN
        out.halt()
        assert ok(rec)
    run(t())


def test_center_bounce_counts_once_per_sweep():
    async def t():
        out, rec = centered()
        out.set_mouth(True)
        out.center_changed(False)                # crossing
        out.center_changed(True)                 # bounce back...
        out.center_changed(False)                # ...and forth: must not schedule a second reversal
        await asyncio.sleep(0.04)
        assert rec.level == CLOSE                # one reversal at ~30 ms
        await asyncio.sleep(0.04)
        assert rec.level == CLOSE                # no second reversal without a new crossing
        out.halt()
    run(t())


def test_release_seeks_center_and_stops_on_the_edge():
    async def t():
        out, rec = centered(closed_side=False)
        out.set_mouth(True)
        assert rec.level == CLOSE                # open side: head for center by closing
        out.set_mouth(False)
        assert rec.level == CLOSE                # still heading for center
        out.center_changed(True)                 # switch closes at center
        assert rec.level == STOP
        await asyncio.sleep(0.05)
        assert rec.level == STOP and ok(rec)
    run(t())


def test_release_from_closed_side_opens_to_center():
    async def t():
        out, rec = centered(closed_side=True)
        out.set_mouth(True)
        out.center_changed(False)                # now on the open side, swinging
        out.set_mouth(False)
        assert rec.level == CLOSE                # back toward center
        out.center_changed(True)
        assert rec.level == STOP
    run(t())


def test_switch_thread_cuts_enable_immediately_when_seeking():
    async def t():
        out, rec = centered(closed_side=False, mouth_enable_pin=23)
        out.set_mouth(True)
        out.set_mouth(False)                     # seeking center by closing
        assert rec.level[23] is True
        out._switch_from_thread(True)            # switch closes: callback thread
        assert rec.level[23] is False            # cut before the loop runs
        await asyncio.sleep(0)
        assert rec.level == {22: False, 27: False, 23: False} and ok(rec)
    run(t())


def test_switch_thread_does_not_cut_while_flapping():
    async def t():
        out, rec = centered(mouth_enable_pin=23)
        out.set_mouth(True)
        out._switch_from_thread(False)
        assert rec.level[23] is True
        out.halt()
    run(t())


def test_center_invert():
    async def t():
        out, rec = centered(closed_side=False, mouth_center_invert=True)
        out.set_mouth(True)
        out.set_mouth(False)                     # seeking by closing
        out._switch_from_thread(True)            # pressed = open side when inverted: not the edge
        await asyncio.sleep(0)
        assert rec.level == CLOSE
        out._switch_from_thread(False)           # released = closed side: the edge
        await asyncio.sleep(0)
        assert rec.level == STOP
    run(t())


def test_stuck_timeout_stops_motor():
    async def t():
        changes = []
        out, rec = centered(dict(FIXED, seek_timeout_ms=40), changes=changes)
        out.set_mouth(True)
        await asyncio.sleep(0.06)                # never crossed
        assert rec.level == STOP and out.mouth is False
        assert ("mouth", 0.0, "safety") in changes
    run(t())


def test_crossing_against_drive_direction_is_ignored():
    async def t():
        out, rec = centered(closed_side=False)
        out.set_mouth(True)                      # closing toward center
        out.center_changed(False)                # no change
        out.closed_side = True
        out.center_changed(False)                # "opened" while closing: hand-moved/noise, ignore
        await asyncio.sleep(0.05)
        assert rec.level == CLOSE
        out.halt()
    run(t())


def test_swing_scales_with_level():
    out, _ = centered({"swing_min_ms": 50, "swing_max_ms": 250})
    out.set_level(0)
    assert out._swing() == 0.05
    out.set_level(1)
    assert all(0.05 <= out._swing() <= 0.25 for _ in range(50))


def test_button_resets_level_after_lipsync():
    async def t():
        out, _ = centered()
        out.set_mouth(True, "lipsync")
        out.set_level(0.1)
        out.set_mouth(False, "lipsync")
        out.set_mouth(True, "manual")
        assert out._level == 1.0
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


def test_center_status_sequence():
    async def t():
        seen = []
        out, rec = centered(closed_side=False)
        out.on_status = seen.append
        out.status = out._compute_status()
        assert out.status == "open side"
        out.set_mouth(True)
        out.set_mouth(False)                     # seeking by closing
        out.center_changed(True)                 # reached center
        assert seen == ["flapping", "centering", "centered"]
        out.center_changed(False)                # nudged by hand
        assert out.status == "open side"
    run(t())


def test_center_status_stuck():
    async def t():
        seen = []
        out, rec = centered(dict(FIXED, seek_timeout_ms=30))
        out.on_status = seen.append
        out.set_mouth(True)
        await asyncio.sleep(0.05)
        assert seen[-1] == "stuck"
    run(t())


def test_timed_mode_has_no_status():
    out, _ = make()
    assert out.status == "" and out._compute_status() == ""
