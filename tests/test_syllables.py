import asyncio

import numpy as np

from pumpkin.config import Settings
from pumpkin.syllables import SyllableDetector, detect, level

SR = 48000


def bumps(rate=4.5, secs=2.0, carrier=800.0, amp=0.3):
    t = np.arange(int(SR * secs)) / SR
    am = np.clip(np.sin(2 * np.pi * rate * t), 0, None) ** 2
    return (amp * am * np.sin(2 * np.pi * carrier * t)).astype(np.float32)


def onsets(events):
    return [o for o, k, _ in events if k == "onset"]


def test_one_onset_per_syllable():
    assert len(onsets(detect(bumps(), SR, 0.03, 120))) == 9


def test_ignores_rumble_outside_speech_band():
    assert onsets(detect(bumps(carrier=60, amp=0.5), SR, 0.03, 120)) == []


def test_quiet_below_threshold():
    assert onsets(detect(bumps(amp=0.01), SR, 0.03, 120)) == []


def test_min_gap_merges_fast_bumps():
    fast = len(onsets(detect(bumps(rate=8), SR, 0.03, 60)))
    slow = len(onsets(detect(bumps(rate=8), SR, 0.03, 300)))
    assert slow < fast


def test_streaming_matches_whole_clip():
    x = bumps()
    whole = onsets(detect(x, SR, 0.03, 120))
    d, got, pos = SyllableDetector(SR), [], 0
    for n in [777, 1500, 333, 4000] * 100:
        chunk = x[pos:pos + n]
        if not len(chunk):
            break
        got += [pos + o for o in onsets(d.process(chunk, 0.03, 120))]
        pos += n
    assert got == whole


def test_level_scale():
    assert level(0.03, 0.03) == 0 and level(0.15, 0.03) == 1 and 0 < level(0.06, 0.03) < 1


def _services(tmp_path, **kw):
    from pumpkin.services import Services
    cfg = Settings(gpio_backend="mock", audio_backend="null", serial_port="", data_dir=tmp_path, **kw)
    return Services(cfg)


def test_clip_schedules_strokes_with_lead(tmp_path):
    async def t():
        svc = _services(tmp_path)
        await svc.start()
        strokes = []
        svc.outputs.stroke = lambda lv: strokes.append(asyncio.get_running_loop().time())
        svc.set_lipsync({"lead_ms": 0})
        t0 = asyncio.get_running_loop().time()
        svc.audio.play(bumps(secs=0.5))
        await asyncio.sleep(0.1)
        assert len(strokes) == 1                 # first syllable only, others still pending
        svc.set_lipsync({"lead_ms": 50})
        svc.audio.stop()                          # cancels pending strokes
        await asyncio.sleep(0.5)
        assert len(strokes) == 1
        assert strokes[0] - t0 < 0.08
        await svc.close()
    asyncio.run(t())


def test_live_voice_triggers_strokes(tmp_path):
    async def t():
        svc = _services(tmp_path)
        await svc.start()
        strokes = []
        svc.outputs.stroke = lambda lv: strokes.append(lv)
        pcm = (bumps(secs=0.5) * 32767).astype("<i2").tobytes()
        svc.audio.push_live(pcm, SR)
        await asyncio.sleep(0.6)
        assert len(strokes) >= 2
        await svc.close()
    asyncio.run(t())


def test_jog_disables_lipsync_and_reenable_centers(tmp_path):
    async def t():
        svc = _services(tmp_path, mouth_driver="hbridge", mouth_center_pin=24)
        await svc.start()
        assert svc.runtime["lipsync"]["enabled"]
        svc.jog_mouth(1)
        assert not svc.runtime["lipsync"]["enabled"] and svc.outputs.status == "opening"
        svc.jog_mouth(0)
        svc.outputs.closed_side = False
        svc.set_lipsync({"enabled": True})
        assert svc.outputs.status == "centering"
        await svc.close()
    asyncio.run(t())


def test_follow_mode_does_not_schedule_strokes(tmp_path):
    async def t():
        svc = _services(tmp_path)
        await svc.start()
        svc.set_lipsync({"mode": "follow"})
        strokes = []
        svc.outputs.stroke = lambda lv: strokes.append(lv)
        svc.audio.play(bumps(secs=0.3))
        await asyncio.sleep(0.4)
        assert strokes == []
        await svc.close()
    asyncio.run(t())
