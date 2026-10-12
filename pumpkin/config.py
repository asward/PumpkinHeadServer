"""Static, deploy-time configuration. Every field can be set with a PUMPKIN_<NAME> env var.

Things the operator tunes at runtime (voice changer, lip-sync, autopilot) live in
runtime.py instead, persisted to <data_dir>/settings.json.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUMPKIN_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./data")
    password: str = ""  # empty = no login

    # stepper4 board. "mock" = built-in simulator, "" = no motors.
    serial_port: str = "/dev/ttyACM0"
    serial_baud: int = 115200
    watchdog_ms: int = 1000
    candy_motor: int = 1
    candy_steps: int = 200
    candy_speed: int = 800

    # GPIO. backend: auto | lgpio | mock
    gpio_backend: str = "auto"
    gpio_chip: int = 0  # Pi 5 on kernels older than 6.6.45 uses 4
    eyes_pin: int = 17
    eyes_pwm: bool = True
    eyes_pwm_hz: int = 200
    # Mouth driver: single = one on/off pin (relay or MOSFET, spring closes the jaw)
    #               hbridge = two H-bridge inputs; mouth_pin drives open, mouth_close_pin drives closed
    mouth_driver: str = "single"
    mouth_pin: int = 27
    mouth_close_pin: int = 22  # hbridge only
    mouth_enable_pin: int = -1  # hbridge only: EN/ENA input, high only while driving. -1 = not wired (jumpered)
    # Center switch (hbridge only): to GND, internal pull-up; closed while the jaw is on the
    # closed side of center, open on the open side. -1 = none (timed flapping).
    # invert = true if yours is the other way round (closed on the open side).
    # Flap and centering timing are tuned in the UI (runtime "mouth" settings).
    mouth_center_pin: int = -1
    mouth_center_invert: bool = False
    active_high: bool = True
    mouth_max_on_ms: int = 3000  # safety cut-off for the mouth motor, 0 = none

    # Speaker. backend: auto | sounddevice | null
    audio_backend: str = "auto"
    audio_device: str = ""  # PortAudio name or index, "" = system default
    sample_rate: int = 48000

    # Camera. source: v4l2 (USB/UVC webcam via ffmpeg) | rpicam (Pi CSI camera via rpicam-vid)
    camera_source: str = "v4l2"
    camera_device: str = "/dev/video0"  # v4l2 only
    camera_size: str = "640x480"
    camera_fps: int = 15
    camera_input_format: str = "mjpeg"  # v4l2 only: mjpeg is passed through untouched; anything else is transcoded
    camera_audio_device: str = ""  # ALSA name, e.g. plughw:CARD=Camera,DEV=0; "" = no listen-back
    listen_rate: int = 16000


settings = Settings()
