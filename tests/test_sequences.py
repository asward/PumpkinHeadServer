import numpy as np
import pytest

from pumpkin.generator import TEMPLATES, generate
from pumpkin.recorder import Recorder
from pumpkin.sequences import duration, normalize


def test_normalize_sorts_and_clamps():
    seq = normalize({"name": "Hello There", "events": [
        {"t": 2, "type": "eyes", "value": 5},
        {"t": 1, "type": "say", "clip": "hi"},
        {"t": 0, "type": "motor", "cmd": "move 1  100"},
    ]})
    assert seq["id"] == "hello-there"
    assert [e["t"] for e in seq["events"]] == [0, 1, 2]
    assert seq["events"][2]["value"] == 1.0
    assert seq["events"][0]["cmd"] == "move 1 100"


@pytest.mark.parametrize("bad", [
    {"t": -1, "type": "eyes", "value": 1},
    {"t": 0, "type": "explode"},
    {"t": 0, "type": "motor", "cmd": "WD 0"},
    {"t": 0, "type": "say", "clip": "../etc/passwd"},
])
def test_normalize_rejects(bad):
    with pytest.raises(ValueError):
        normalize({"name": "x", "events": [bad]})


def test_duration_includes_clip_length():
    seq = normalize({"name": "x", "events": [{"t": 1, "type": "say", "clip": "a"}]})
    assert duration(seq, lambda c: 2.5) == 3.5


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_generators_produce_valid_sequences(template):
    seq = normalize(generate(template, clips={"boo": 1.2, "hello": 2.0}, duration=8, seed=7, dispense=True))
    assert seq["events"]
    assert any(e["type"] == "dispense" for e in seq["events"])
    assert generate(template, clips={"boo": 1.2}, duration=8, seed=7) == generate(template, clips={"boo": 1.2}, duration=8, seed=7)


def test_recorder_pads_gaps_between_ptt_presses(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("pumpkin.recorder.time.monotonic", lambda: clock[0])
    r = Recorder(1000)
    r.start("take", live=True, eyes=1.0)
    clock[0] += 2.0                       # two seconds of silence, then speech
    r.add_audio(np.ones(500, np.float32))
    r.add_event("mouth", value=1)
    audio, events = r.stop()
    assert len(audio) == 2000 and audio[:1500].max() == 0 and audio[1500:].min() == 1
    assert events == [{"t": 0.0, "type": "eyes", "value": 1.0}, {"t": 2.0, "type": "mouth", "value": 1}]
