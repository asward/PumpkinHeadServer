"""Procedurally generated sequences. Output is an ordinary (unsaved) sequence dict
that the operator can tweak in the editor before saving."""
from __future__ import annotations

import random

TEMPLATES = {
    "blink": "Eyes on with natural random blinks",
    "flicker": "Candle-like eye flicker",
    "chatter": "Teeth-chattering mouth with pulsing eyes",
    "talk": "Speak random voice lines with lively eyes",
    "trick_or_treat": "Flash eyes, speak a line, dispense candy",
    "scare": "Go dark, then burst to life",
}


class _Builder:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.events: list[dict] = []

    def add(self, t: float, type_: str, **fields) -> None:
        self.events.append({"t": round(t, 3), "type": type_, **fields})

    def blink(self, t0: float, t1: float) -> None:
        r = self.rng
        self.add(t0, "eyes", value=1.0)
        t = t0 + r.uniform(1.0, 4.0)
        while t < t1 - 0.3:
            for _ in range(2 if r.random() < 0.2 else 1):  # occasional double blink
                self.add(t, "eyes", value=0.0)
                t += r.uniform(0.08, 0.18)
                self.add(t, "eyes", value=1.0)
                t += 0.15
            t += r.uniform(1.5, 5.0)

    def flicker(self, t0: float, t1: float, lo: float = 0.35, hi: float = 1.0) -> None:
        r = self.rng
        t, v = t0, hi
        while t < t1:
            v = min(hi, max(lo, v + r.uniform(-0.3, 0.3)))
            self.add(t, "eyes", value=round(v, 2))
            t += r.uniform(0.04, 0.15)

    def chatter(self, t0: float, t1: float) -> None:
        r = self.rng
        t, open_ = t0, True
        while t < t1:
            self.add(t, "mouth", value=int(open_))
            open_ = not open_
            t += r.uniform(0.08, 0.18)
        self.add(t1, "mouth", value=0)

    def pulse(self, t0: float, t1: float, period: float = 0.5) -> None:
        t, on = t0, True
        while t < t1:
            self.add(t, "eyes", value=1.0 if on else 0.3)
            on = not on
            t += period / 2

    def say(self, t: float, clip: str, dur: float) -> float:
        self.add(t, "say", clip=clip)
        return t + dur


def generate(template: str, *, clips: dict[str, float], duration: float = 10.0,
             seed: int | None = None, dispense: bool = False, name: str | None = None) -> dict:
    """clips maps the voice lines to choose from to their durations (seconds)."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    seed = seed if seed is not None else random.randrange(1, 10_000)
    r = random.Random(seed)
    b = _Builder(r)
    names = list(clips)
    end = duration

    def pick() -> tuple[str, float] | None:
        if not names:
            return None
        c = r.choice(names)
        return c, clips[c]

    if template == "blink":
        b.blink(0, end)
    elif template == "flicker":
        b.flicker(0, end)
    elif template == "chatter":
        b.pulse(0, end)
        b.chatter(0.2, end - 0.2)
        if (c := pick()):
            b.say(0.2, *c)
    elif template == "talk":
        if not names:
            raise ValueError("talk needs at least one voice line")
        t, order = 0.3, r.sample(names, len(names))
        for i, c in enumerate(order):
            if i and t + clips[c] > duration:
                break
            t = b.say(t, c, clips[c]) + r.uniform(0.6, 1.5)
        end = max(duration, t)
        b.flicker(0, end, lo=0.6)
    elif template == "trick_or_treat":
        t = 0.0
        for _ in range(3):
            b.add(t, "eyes", value=1.0)
            b.add(t + 0.15, "eyes", value=0.0)
            t += 0.3
        b.add(t, "eyes", value=1.0)
        if (c := pick()):
            t = b.say(t + 0.2, *c)
        if dispense:
            b.add(t + 0.3, "dispense", count=1)
            dispense = False
        end = max(duration, t + 1.5)
        b.blink(t + 0.3, end)
    elif template == "scare":
        dark = r.uniform(1.5, 3.0)
        b.add(0, "eyes", value=0.0)
        b.add(0, "mouth", value=0)
        b.add(dark, "eyes", value=1.0)
        talk_end = dark + 1.5
        if (c := pick()):
            talk_end = max(talk_end, b.say(dark, *c))
        else:
            b.chatter(dark, talk_end)
        end = max(duration, talk_end + 1.0)
        b.flicker(talk_end, end, lo=0.5)

    if dispense:
        b.add(end, "dispense", count=1)
    b.add(end, "eyes", value=1.0)
    label = name or f"{template.replace('_', ' ')} {seed}"
    return {"name": label, "fx": None, "events": b.events}
