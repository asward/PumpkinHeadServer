import numpy as np

from pumpkin.voicefx import PRESETS, FxParams, VoiceFX

SR = 48000


def tone(hz, secs=1.0):
    t = np.arange(int(SR * secs)) / SR
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def band_energy(x, lo, hi):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    hz = np.fft.rfftfreq(len(x), 1 / SR)
    return spec[(hz >= lo) & (hz <= hi)].sum()


# The two-tap shifter's crossfade adds sidebands 20 Hz either side of the target
# (1 / 50 ms grain), so compare energy around the target with energy around the source.
def test_pitch_shift_moves_frequency_an_octave():
    fx = VoiceFX(SR, FxParams(pitch=12))
    x = tone(220)
    y = np.concatenate([fx.process(x[i:i + 960]) for i in range(0, len(x), 960)])[SR // 4:]
    assert band_energy(y, 400, 480) > 20 * band_energy(y, 200, 240)


def test_pitch_down():
    y = VoiceFX(SR, FxParams(pitch=-12)).process(tone(440))[SR // 4:]
    assert band_energy(y, 190, 250) > 20 * band_energy(y, 420, 460)


def test_every_preset_is_finite_and_bounded_streaming():
    x = tone(300, 0.5)
    for name in PRESETS:
        fx = VoiceFX(SR, FxParams.preset(name))
        y = np.concatenate([fx.process(x[i:i + 128]) for i in range(0, len(x), 128)])
        assert y.shape == x.shape
        assert np.isfinite(y).all() and np.abs(y).max() <= 1.0


def test_echo_repeats_an_impulse():
    fx = VoiceFX(SR, FxParams(echo_ms=100, echo_fb=0.5, echo_mix=1.0))
    x = np.zeros(SR // 2, np.float32)
    x[0] = 1.0
    y = fx.process(x)
    d = SR // 10
    assert y[d] == 1.0 and abs(y[2 * d] - 0.5) < 1e-6


def test_params_are_clamped():
    p = FxParams.from_dict({"pitch": 99, "echo_ms": 5, "bogus": 1})
    assert p.pitch == 12 and p.echo_ms == 20
