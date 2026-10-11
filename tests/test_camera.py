from pumpkin.camera import VideoHub
from pumpkin.config import Settings


def test_v4l2_mjpeg_passthrough():
    cmd = VideoHub(Settings(camera_device="/dev/video2"))._cmd()
    assert cmd[0] == "ffmpeg"
    assert cmd[cmd.index("-i") + 1] == "/dev/video2"
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_rpicam_mjpeg_to_stdout():
    cmd = VideoHub(Settings(camera_source="rpicam", camera_size="1280x720", camera_fps=10))._cmd()
    assert cmd[0] == "rpicam-vid"
    assert cmd[cmd.index("--codec") + 1] == "mjpeg"
    assert cmd[cmd.index("--width") + 1] == "1280"
    assert cmd[cmd.index("--height") + 1] == "720"
    assert cmd[cmd.index("--framerate") + 1] == "10"
    assert cmd[cmd.index("--output") + 1] == "-"
    assert cmd[cmd.index("--verbose") + 1] == "0"
