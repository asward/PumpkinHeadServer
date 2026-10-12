# PumpkinHead Server

Command-and-control web app for an animatronic pumpkin, served by a Raspberry Pi inside the pumpkin.

- **Live**: see and hear through the pumpkin's USB camera. Hold-to-talk sends your voice through a configurable voice changer to the pumpkin's speaker.
- **Puppet**: eyes (GPIO LED, dimmable), mouth (GPIO motor, on/off), candy dispenser (stepper motor), and lip-sync that moves the mouth with the voice.
- **Record**: capture a "take" of your voice plus every eye, mouth, voice-line and candy action, saved as a voice line and a sequence.
- **Sequences**: timed scripts of eyes/mouth/voice/candy/motor events. Record them, generate them from templates, edit them as text, and play them back.
- **Autopilot**: plays chosen sequences at random intervals whenever nobody is puppeteering.

## Architecture

```
 Browser (web/)                                Raspberry Pi (pumpkin/)
 ───────────────                               ───────────────────────────────────────────
 mic ─AudioWorklet─ int16 PCM ── /ws/talk ───▶ voicefx ─▶ mixer ─▶ speaker (PortAudio)
                                                             └─ envelope ─▶ lip-sync ─▶ mouth GPIO
 <img> ◀── multipart MJPEG ──── /api/camera.mjpg ◀── ffmpeg ◀── /dev/video0 (MJPEG passthrough)
 AudioWorklet ◀─ int16 PCM ──── /ws/listen ◀──────── ffmpeg ◀── camera mic (ALSA)
 UI state ◀──── JSON events ─── /ws/events ◀──────── outputs, motors, playback, recording
 buttons ─────── REST ────────▶ /api/* ──▶ sequences / player ─▶ eyes+mouth GPIO (lgpio)
                                                              └▶ stepper4 board (USB serial)
```

| Module | Role |
| --- | --- |
| `pumpkin/stepper.py` | Async client for the stepper4 board: tagged commands, terminal events, watchdog pings, reconnects |
| `pumpkin/stepper_sim.py` | Board simulator (`PUMPKIN_SERIAL_PORT=mock`) |
| `pumpkin/gpio.py` | Eyes and mouth outputs via lgpio, with a mouth max-on safety cut-off |
| `pumpkin/voicefx.py` | Streaming voice changer: pitch, ring-mod "robot", growl, echo, gain, plus presets |
| `pumpkin/audio.py` | Speaker mixer (live voice + clips), jitter buffer, envelope for lip-sync |
| `pumpkin/recorder.py` | Take recorder that keeps voice and puppet actions in sync across push-to-talk gaps |
| `pumpkin/sequences.py` | Sequence format, validation, storage, and player |
| `pumpkin/generator.py` | Procedural sequence templates (blink, flicker, chatter, talk, trick-or-treat, scare) |
| `pumpkin/camera.py` | On-demand ffmpeg capture shared by all viewers |
| `pumpkin/services.py` | Wires everything together: lip-sync, autopilot, motor status, e-stop |
| `pumpkin/main.py` | FastAPI routes and WebSockets; serves `web/` |

Data lives in `PUMPKIN_DATA_DIR` (`/data` in the container): `clips/*.wav` (dry voice lines), `sequences/*.json`, `settings.json` (voice changer, lip-sync, autopilot), `certs/`.

## Wiring defaults

| Thing | Default | Setting |
| --- | --- | --- |
| Eyes LED (via transistor/MOSFET) | GPIO17 (pin 11), PWM-dimmed | `PUMPKIN_EYES_PIN`, `PUMPKIN_EYES_PWM` |
| Mouth motor (via driver/relay) | GPIO27 (pin 13), max on 3 s | `PUMPKIN_MOUTH_PIN`, `PUMPKIN_MOUTH_MAX_ON_MS` |
| Mouth motor via H-bridge | open: GPIO27 (pin 13) → IN1, close: GPIO22 (pin 15) → IN2 | `PUMPKIN_MOUTH_DRIVER=hbridge`, `PUMPKIN_MOUTH_CLOSE_PIN` |
| H-bridge enable (optional) | off (jumpered); e.g. GPIO23 (pin 16) → EN/ENA, high only while driving | `PUMPKIN_MOUTH_ENABLE_PIN` |
| Mouth flapping and timing | flap 150 ms open / 150 ms closed | UI → Puppet → Mouth motion |
| Mouth center switch (optional, H-bridge) | off; e.g. GPIO24 (pin 18) to GND, closed on the closed side of center | `PUMPKIN_MOUTH_CENTER_PIN`, `PUMPKIN_MOUTH_CENTER_INVERT` |
| Output polarity | active-high | `PUMPKIN_ACTIVE_HIGH` |
| stepper4 board | `/dev/ttyACM0`, candy on M1, 200 steps at 800 sps | `PUMPKIN_SERIAL_PORT`, `PUMPKIN_CANDY_*` |
| Camera | USB webcam on `/dev/video0`, 640x480 at 15 fps, MJPEG | `PUMPKIN_CAMERA_*` (`PUMPKIN_CAMERA_SOURCE=rpicam` for a Pi CSI camera) |
| Camera mic | off | `PUMPKIN_CAMERA_AUDIO_DEVICE` (find it with `arecord -L`) |
| Speaker | system default | `PUMPKIN_AUDIO_DEVICE` |

All settings are in `pumpkin/config.py`. `.env.example` lists the common ones.

## Run on a dev machine (no hardware)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
PUMPKIN_SERIAL_PORT=mock PUMPKIN_GPIO_BACKEND=mock .venv/bin/uvicorn pumpkin.main:app --reload
# open http://localhost:8000  (localhost counts as secure, so the mic works)
.venv/bin/python -m pytest
```

## Deploy on the Pi (Docker)

```sh
cp .env.example .env        # set pins, devices, and PUMPKIN_PASSWORD
docker compose up -d --build
# open https://pumpkin.local:8443 and accept the self-signed certificate
```

- **HTTPS is required** for browser microphone access from another device. The container makes a self-signed certificate on first start (`PUMPKIN_TLS=0` serves plain HTTP on 8080). Set `PUMPKIN_HOSTNAME` to the Pi's hostname before the first start.
- Every device in `docker-compose.yml` must exist or the container won't start. Remove any you don't have, and prefer `/dev/serial/by-id/...` for the motor board.
- Pi 5 on kernels older than 6.6.45 exposes the header as `gpiochip4`: set `PUMPKIN_GPIO_CHIP=4` and map that device.
- **Pi CSI (ribbon) camera:** set `PUMPKIN_CAMERA_SOURCE=rpicam` and `COMPOSE_FILE=docker-compose.yml:docker-compose.rpicam.yml` in `.env`, then rebuild. These cameras go through libcamera, so ffmpeg can't read their `/dev/video0`. The override runs the container privileged so libcamera can reach its media and DMA devices. Check the camera on the host first with `rpicam-hello --list-cameras`.
- Cross-build from a PC: `docker buildx build --platform linux/arm64 -t pumpkinhead .`

## Using it

Keyboard: **Space** hold to talk, **M** hold mouth, **E** toggle eyes, **D** dispense, **Esc** stop all.

Sequence editor format, one event per line (`//` comments):

```
0.00  eyes     1          // brightness 0-1
0.20  say      hello      // voice line id
0.50  mouth    1          // 1 open, 0 closed
3.00  dispense 1          // candy count
3.50  motor    MOVE 2 400 1000
```

A sequence's **Voice** is the live voice-changer setting, a preset, or "as recorded" (takes keep the setting used while recording). Lip-sync drives the mouth for any voice line, so you only need explicit `mouth` events for extra movement.

## Safety

- **E-STOP** sends the board's real-time `!` halt, then latches `ESTOP` (drivers off) until **Reset E-stop**. It also stops playback and turns off autopilot.
- The board's host watchdog (`WD`, 1 s by default) halts the motors if the server dies or the USB link drops.
- The mouth output turns itself off after `PUMPKIN_MOUTH_MAX_ON_MS` so a stuck button can't stall the motor.
- With a center switch the mouth flaps through center: after each crossing it keeps going for a random time between the UI's min and max swing, then reverses, so it stays centered. Lip-sync scales the swing with the voice volume. When the button is released or the voice goes quiet, the jaw drives back to center and stops the moment the switch changes (the enable pin drops straight from the GPIO callback). If the switch isn't reached within the stuck timeout, the motor stops. E-STOP and stop-all halt the mouth in place.
- **Lip-sync modes.** *Syllables* (default) fires one jaw stroke per syllable, found as bumps in the 300–3000 Hz speech band, sized by loudness, so the jaw keeps pace with the speaker and rests in pauses. Live talk is analysed as it arrives, before the jitter buffer; voice lines are analysed up front. Each stroke is scheduled *Lead* ms before its syllable is heard, to cover the motor's lag. *Follow volume* is the older open-while-loud behavior.
- **Open / Close buttons** drive the jaw while held and stop where it is on release (capped by `PUMPKIN_MOUTH_MAX_ON_MS`). Using them turns lip-sync off; turning lip-sync back on returns the jaw to rest (center) first.
- `PUMPKIN_MOUTH_MAX_ON_MS` cuts a mouth held open (flapping off) so a stuck button can't stall the motor.
- Raw motor commands from the UI and sequences are limited to the spec's command set. `WD` is reserved for the server.

## Known limits

- Live voice has about 100–150 ms of latency: browser capture, a 40 ms jitter buffer, and the speaker buffer.
- The pitch shifter is a cheap two-tap delay-line design. It sounds a little phasey, which suits a monster voice.
- Without headphones, the camera mic hears the pumpkin's speaker. "Mute listen while talking" stops the feedback loop.
- One sequence plays at a time. Starting another stops the current one.
