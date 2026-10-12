"""End-to-end through the HTTP API with every backend simulated."""
import io
import time
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    from pumpkin import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "serial_port", "mock")
    monkeypatch.setattr(config.settings, "gpio_backend", "mock")
    monkeypatch.setattr(config.settings, "audio_backend", "null")
    from pumpkin.main import app
    with TestClient(app) as c:
        yield c


def wav_bytes(secs=0.5, sr=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        t = np.arange(int(sr * secs)) / sr
        w.writeframes((np.sin(2 * np.pi * 300 * t) * 12000).astype("<i2").tobytes())
    return buf.getvalue()


def wait_for(pred, secs=5):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_full_flow(client):
    assert wait_for(lambda: client.get("/api/state").json()["stepper"]["connected"])
    assert client.get("/").status_code == 200

    r = client.post("/api/clips", files={"file": ("boo.wav", wav_bytes())}, data={"name": "Boo!"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "boo"
    assert client.get("/api/clips").json()[0]["duration"] == pytest.approx(0.5, abs=0.01)

    seq = client.post("/api/generate", json={"template": "trick_or_treat", "duration": 2, "seed": 1, "dispense": True}).json()
    assert client.put("/api/sequences/tot", json=seq).status_code == 200
    assert client.get("/api/sequences").json()[0]["id"] == "tot"

    assert client.post("/api/sequences/tot/play").status_code == 200
    assert wait_for(lambda: client.get("/api/state").json()["playing"] == "tot", 2)
    assert wait_for(lambda: client.get("/api/state").json()["playing"] is None, 8)

    assert client.post("/api/eyes", json={"value": 0.5}).json() == {"eyes": 0.5}
    assert client.put("/api/fx", json={"preset": "ghost"}).json()["preset"] == "ghost"
    assert client.post("/api/stepper/cmd", json={"cmd": "WD 0"}).status_code == 409
    assert client.post("/api/stepper/cmd", json={"cmd": "STATUS"}).json()["data"][0].startswith("STA 1")


def test_record_take_over_websocket(client):
    client.post("/api/record/start", json={"name": "Greeting", "live": True})
    client.post("/api/eyes", json={"value": 1})
    with client.websocket_connect("/ws/talk") as ws:
        ws.send_text('{"rate": 16000}')
        t = np.arange(320) / 16000
        for _ in range(25):
            ws.send_bytes((np.sin(2 * np.pi * 200 * t) * 10000).astype("<i2").tobytes())
    out = client.post("/api/record/stop").json()
    assert out["clip"] == "greeting"
    types = [e["type"] for e in out["sequence"]["events"]]
    assert "say" in types and types.count("eyes") == 2


def test_estop_latches(client):
    assert wait_for(lambda: client.get("/api/state").json()["stepper"]["connected"])
    client.post("/api/estop")
    assert client.get("/api/state").json()["stepper"]["estopped"]
    assert client.post("/api/stepper/cmd", json={"cmd": "MOVE 1 10"}).json()["error"] == "ESTOP"
    client.post("/api/reset")
    assert client.post("/api/stepper/cmd", json={"cmd": "MOVE 1 10"}).status_code == 200


def test_manual_mouth_and_eyes(client):
    assert client.post("/api/mouth", json={"value": 1}).json() == {"mouth": True}
    assert client.post("/api/mouth", json={"value": 0}).json() == {"mouth": False}
    assert client.post("/api/eyes", json={"value": 1}).json() == {"eyes": 1.0}


def test_mouth_settings_roundtrip(client):
    r = client.put("/api/mouth", json={"flap": False, "open_ms": 120, "close_ms": 200})
    assert r.status_code == 200
    m = r.json()
    assert (m["flap"], m["open_ms"], m["close_ms"]) == (False, 120, 200)
    st = client.get("/api/state").json()
    assert st["mouth"]["close_ms"] == 200 and st["outputs"]["mouth_mode"] == "timed"
    assert st["outputs"]["mouth_status"] == ""
    assert client.put("/api/mouth", json={"swing_min_ms": 40, "swing_max_ms": 300}).json()["swing_max_ms"] == 300
    assert client.put("/api/lipsync", json={"hang_ms": 500}).json()["hang_ms"] == 500
    assert client.put("/api/mouth", json={"open_ms": 5}).status_code == 422


def test_reset_clears_estop_while_board_offline(client):
    from pumpkin import main
    assert client.post("/api/estop").status_code == 200
    assert client.get("/api/state").json()["stepper"]["estopped"] is True
    main.svc.stepper.connected = False            # board browned out / USB dropped
    assert client.post("/api/reset").status_code == 200
    assert client.get("/api/state").json()["stepper"]["estopped"] is False


def test_reset_explains_active_estop_input(client, monkeypatch):
    from pumpkin import main
    from pumpkin.stepper import StepperError
    client.post("/api/estop")

    async def refuse(line):
        raise StepperError("ESTOP")
    monkeypatch.setattr(main.svc.stepper, "command", refuse)
    monkeypatch.setattr(main.svc.stepper, "connected", True)
    r = client.post("/api/reset")
    assert r.status_code == 400 and "E-stop input" in r.json()["error"]
    assert client.get("/api/state").json()["stepper"]["estopped"] is True


def test_jog_endpoint_and_lipsync_mode(client):
    assert client.put("/api/lipsync", json={"mode": "follow"}).json()["mode"] == "follow"
    assert client.put("/api/lipsync", json={"mode": "words"}).status_code == 422
    assert client.post("/api/mouth/jog", json={"direction": 1}).status_code == 200
    assert client.get("/api/state").json()["lipsync"]["enabled"] is False
    assert client.post("/api/mouth/jog", json={"direction": 0}).status_code == 200
    assert client.post("/api/mouth/jog", json={"direction": 2}).status_code == 422
