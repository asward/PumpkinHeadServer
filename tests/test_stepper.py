import asyncio

import pytest

from pumpkin.stepper import MotionError, Stepper4, StepperError


async def connected():
    events = []
    s = Stepper4("mock", watchdog_ms=300, on_event=events.append)
    task = asyncio.create_task(s.run())
    for _ in range(100):
        if s.connected:
            break
        await asyncio.sleep(0.02)
    assert s.connected and s.version == "sim-1.0"
    return s, task, events


def test_move_completes_and_reports_position():
    async def main():
        s, task, events = await connected()
        done = await s.move(1, 200, 2000)
        assert await asyncio.wait_for(done, 2) == "DONE"
        assert (await s.status())[1]["pos"] == 200
        assert any(e["event"] == "DONE" for e in events)
        task.cancel()
    asyncio.run(main())


def test_stop_cancels_running_and_drops_queued():
    async def main():
        s, task, _ = await connected()
        a = await s.move(2, 10000, 100)
        b = await s.move(2, 10, 100)
        await s.command("STOP 2")
        with pytest.raises(MotionError) as ea:
            await asyncio.wait_for(a, 1)
        with pytest.raises(MotionError) as eb:
            await asyncio.wait_for(b, 1)
        assert (ea.value.event, eb.value.event) == ("CANCEL", "DROP")
        task.cancel()
    asyncio.run(main())


def test_errors_and_whitelist():
    async def main():
        s, task, _ = await connected()
        with pytest.raises(StepperError, match="BADMOTOR"):
            await s.motion("MOVE 9 10")
        with pytest.raises(StepperError, match="NOTALLOWED"):
            await s.send("WD 0")
        await s.command("ESTOP")
        with pytest.raises(StepperError, match="ESTOP"):
            await s.move(1, 10)
        await s.command("RESET")
        task.cancel()
    asyncio.run(main())


def test_keepalive_pings_keep_link_up():
    async def main():
        s, task, _ = await connected()
        await asyncio.sleep(0.5)  # several ping intervals at WD 300
        assert s.connected
        task.cancel()
    asyncio.run(main())
