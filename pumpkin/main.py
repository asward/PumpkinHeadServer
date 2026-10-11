"""HTTP + WebSocket API, and the static web UI.

WebSockets:
  /ws/events  server -> browser JSON events (state changes, motor events, playback)
  /ws/talk    browser -> server int16 mono PCM from the operator's mic (first a JSON {"rate": n})
  /ws/listen  server -> browser int16 mono PCM from the camera mic (first a JSON {"rate": n})
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import PasswordMiddleware
from .config import settings
from .generator import generate
from .sequences import normalize
from .services import Services
from .stepper import StepperError
from .voicefx import PRESETS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

svc: Services


@asynccontextmanager
async def lifespan(_app):
    global svc
    svc = Services(settings)
    await svc.start()
    try:
        yield
    finally:
        await svc.close()


app = FastAPI(title="PumpkinHead", lifespan=lifespan)
app.add_middleware(PasswordMiddleware, password=settings.password)


@app.exception_handler(StepperError)
async def _stepper_error(_req: Request, e: StepperError):
    return JSONResponse({"error": e.code}, status_code=409)


@app.exception_handler(ValueError)
async def _value_error(_req: Request, e: ValueError):
    return JSONResponse({"error": str(e)}, status_code=400)


@app.exception_handler(FileNotFoundError)
async def _not_found(_req: Request, e: FileNotFoundError):
    return JSONResponse({"error": f"not found: {e}"}, status_code=404)


def _stepper():
    if not svc.stepper:
        raise HTTPException(503, "no motor board configured")
    return svc.stepper


# ---- request bodies -------------------------------------------------------

class Value(BaseModel):
    value: float


class Count(BaseModel):
    count: int = Field(1, ge=1, le=10)


class Command(BaseModel):
    cmd: str


class Fx(BaseModel):
    preset: str | None = None
    params: dict | None = None


class Lipsync(BaseModel):
    enabled: bool | None = None
    threshold: float | None = Field(None, gt=0, le=1)
    min_ms: int | None = Field(None, ge=20, le=1000)
    max_open_ms: int | None = Field(None, ge=50, le=5000)


class Autopilot(BaseModel):
    enabled: bool | None = None
    sequences: list[str] | None = None
    min_gap: float | None = Field(None, ge=0, le=3600)
    max_gap: float | None = Field(None, ge=0, le=3600)
    order: str | None = Field(None, pattern="^(random|cycle)$")


class RecordStart(BaseModel):
    name: str = Field("take", min_length=1, max_length=80)
    live: bool = True


class Generate(BaseModel):
    template: str
    duration: float = Field(10, gt=0, le=600)
    seed: int | None = None
    clips: list[str] = []
    dispense: bool = False
    name: str | None = None


# ---- state & puppet -------------------------------------------------------

@app.get("/api/state")
def get_state():
    return svc.state()


@app.post("/api/eyes")
def set_eyes(body: Value):
    svc.outputs.set_eyes(body.value)
    return {"eyes": svc.outputs.eyes}


@app.post("/api/mouth")
def set_mouth(body: Value):
    svc.outputs.set_mouth(body.value >= 0.5)
    return {"mouth": svc.outputs.mouth}


@app.post("/api/dispense")
async def dispense(body: Count):
    _stepper()
    if svc.recorder.active:
        svc.recorder.add_event("dispense", count=body.count)
    svc.spawn(svc.dispense(body.count))
    return {"ok": True}


@app.post("/api/stop")
async def stop_all():
    await svc.stop_all()
    return {"ok": True}


@app.post("/api/estop")
async def estop():
    await svc.estop()
    return {"ok": True}


@app.post("/api/reset")
async def reset():
    await svc.reset()
    return {"ok": True}


# ---- motors ---------------------------------------------------------------

@app.post("/api/stepper/cmd")
async def stepper_cmd(body: Command):
    return {"data": await _stepper().send(body.cmd)}


@app.get("/api/stepper/status")
async def stepper_status():
    return await _stepper().status()


# ---- voice changer & lip-sync --------------------------------------------

@app.put("/api/fx")
def put_fx(body: Fx):
    if body.preset and body.params is None:
        if body.preset not in PRESETS:
            raise ValueError(f"unknown preset {body.preset!r}")
        return svc.set_fx(PRESETS[body.preset], body.preset)
    return svc.set_fx(body.params or {}, body.preset)


@app.put("/api/lipsync")
def put_lipsync(body: Lipsync):
    ls = svc.runtime.update("lipsync", body.model_dump(exclude_none=True))
    svc.bus.publish("lipsync", **ls)
    return ls


@app.put("/api/autopilot")
def put_autopilot(body: Autopilot):
    ap = svc.runtime.update("autopilot", body.model_dump(exclude_none=True))
    svc.bus.publish("autopilot", **ap)
    return ap


# ---- voice lines ----------------------------------------------------------

@app.get("/api/clips")
def list_clips():
    return svc.clips.list()


@app.post("/api/clips")
async def upload_clip(file: UploadFile = File(...), name: str = Form("")):
    with tempfile.NamedTemporaryFile(suffix=Path(file.filename or "").suffix) as tmp:
        while chunk := await file.read(1 << 20):
            tmp.write(chunk)
        tmp.flush()
        clip = await svc.clips.import_file(name or Path(file.filename or "clip").stem, Path(tmp.name))
    svc.bus.publish("library")
    return {"id": clip, "duration": round(svc.clips.duration(clip), 2)}


@app.get("/api/clips/{clip}.wav")
def clip_audio(clip: str):
    return FileResponse(svc.clips.path(clip), media_type="audio/wav")


@app.post("/api/clips/{clip}/play")
async def play_clip(clip: str):
    fx = svc.player.fx_for({"fx": None})
    data = await asyncio.to_thread(svc.player.render_clip, clip, fx)
    if svc.recorder.active:
        svc.recorder.add_event("say", clip=clip)
    svc.audio.play(data, tag="clip")
    return {"ok": True}


@app.delete("/api/clips/{clip}")
def delete_clip(clip: str):
    svc.clips.delete(clip)
    svc.bus.publish("library")
    return {"ok": True}


# ---- sequences ------------------------------------------------------------

@app.get("/api/sequences")
def list_sequences():
    return svc.sequence_list()


@app.get("/api/sequences/{seq_id}")
def get_sequence(seq_id: str):
    return svc.seqs.get(seq_id)


@app.put("/api/sequences/{seq_id}")
def put_sequence(seq_id: str, seq: dict):
    seq = svc.seqs.save({**seq, "id": seq_id})
    svc.bus.publish("library")
    return seq


@app.delete("/api/sequences/{seq_id}")
def delete_sequence(seq_id: str):
    svc.seqs.delete(seq_id)
    svc.bus.publish("library")
    return {"ok": True}


@app.post("/api/sequences/{seq_id}/play")
async def play_sequence(seq_id: str):
    await svc.player.play(svc.seqs.get(seq_id))
    return {"ok": True}


@app.post("/api/play")
async def play_unsaved(seq: dict):
    """Play a sequence straight from the editor without saving it."""
    await svc.player.play(normalize({**seq, "id": seq.get("id") or "preview"}))
    return {"ok": True}


@app.post("/api/generate")
def generate_sequence(body: Generate):
    pool = body.clips or [c["id"] for c in svc.clips.list()]
    clips = {c: svc.clips.duration(c) for c in pool if svc.clips.exists(c)}
    return normalize(generate(body.template, clips=clips, duration=body.duration, seed=body.seed,
                              dispense=body.dispense, name=body.name))


# ---- recording ------------------------------------------------------------

@app.post("/api/record/start")
def record_start(body: RecordStart):
    return svc.start_recording(body.name, body.live)


@app.post("/api/record/stop")
async def record_stop():
    return await svc.stop_recording()


@app.post("/api/record/cancel")
async def record_cancel():
    await svc.stop_recording(save=False)
    return {"ok": True}


# ---- camera ---------------------------------------------------------------

@app.get("/api/camera.mjpg")
async def camera():
    async def body():
        async for jpg in svc.video.frames():
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
    return StreamingResponse(body(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


# ---- websockets -----------------------------------------------------------

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    await ws.accept()
    q = svc.bus.subscribe()
    try:
        await ws.send_text(json.dumps({"type": "state", **svc.state()}))
        while True:
            await ws.send_text(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        svc.bus.unsubscribe(q)


@app.websocket("/ws/talk")
async def ws_talk(ws: WebSocket):
    await ws.accept()
    rate = 48000
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text"):
                rate = int(json.loads(msg["text"]).get("rate", rate))
            elif msg.get("bytes"):
                rec = svc.recorder
                dry = svc.audio.push_live(msg["bytes"], rate, play=not rec.active or rec.live)
                if rec.active:
                    rec.add_audio(dry)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/listen")
async def ws_listen(ws: WebSocket):
    await ws.accept()
    if not svc.mic.enabled:
        await ws.close(code=4404, reason="no camera microphone configured")
        return
    q = svc.mic.subscribe()
    try:
        await ws.send_text(json.dumps({"rate": svc.mic.rate}))
        while True:
            await ws.send_bytes(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        svc.mic.unsubscribe(q)


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
