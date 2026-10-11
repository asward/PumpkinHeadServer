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
