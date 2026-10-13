import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from lampmonitor.db import Database
from lampmonitor.monitor import Frame, Monitor
from lampmonitor.schemas import CameraIn, LampIn

from .conftest import LAMP_REGION, FakeClock, FakeNotifier, FakeSource, encode, mail_settings, panel

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


class Harness:
    def __init__(self, **lamp_overrides):
        self.db = Database(":memory:")
        self.db.save_settings(mail_settings(camera_offline_seconds=60))
        self.camera = self.db.create_camera(CameraIn(name="Panel room", source_type="http", url="http://cam/snap"))
        lamp = {
            **LAMP_REGION,
            "name": "Circuit 12",
            "threshold": 128,
            "hysteresis": 10,
            "confirm_seconds": 10,
            "email_to": "ops@example.com",
            **lamp_overrides,
        }
        self.lamp = self.db.create_lamp(self.camera.id, LampIn(**lamp))
        self.notifier = FakeNotifier()
        self.clock = FakeClock(0)
        self.source = FakeSource()
        self.monitor = Monitor(self.db, self.notifier, clock=self.clock, source_factory=lambda c, s: self.source)

    async def frame(self, t: float, lit: bool) -> None:
        self.clock.now = t
        rgb = panel(lit)
        frame = Frame(encode(rgb), "image/png", rgb, T0 + timedelta(seconds=t), t)
        await self.monitor.handle_frame(self.camera, self.db.get_settings(), frame)
        await self.monitor.wait_idle()

    async def failure(self, t: float) -> None:
        self.clock.now = t
        await self.monitor.handle_failure(self.camera, self.db.get_settings(), OSError("connection refused"))
        await self.monitor.wait_idle()

    def state(self) -> str:
        return self.db.get_lamp(self.lamp.id).state

    def subjects(self) -> list[str]:
        return [mail["subject"] for mail in self.notifier.sent]

    def events(self) -> list[str]:
        return [event["kind"] for event in reversed(self.db.list_events())]


def run(coro):
    return asyncio.run(coro)


def test_lamp_turning_off_sends_one_alert_after_confirmation():
    async def scenario():
        h = Harness()
        await h.frame(0, lit=True)
        await h.frame(10, lit=True)
        assert h.state() == "on"
        assert h.subjects() == []  # coming on at startup isn't alert-worthy

        await h.frame(20, lit=False)
        await h.frame(25, lit=False)
        assert h.state() == "on"
        assert h.subjects() == []

        await h.frame(30, lit=False)
        assert h.state() == "off"
        assert h.subjects() == ["OFF: Circuit 12"]
        mail = h.notifier.sent[0]
        assert mail["recipients"] == ["ops@example.com"]
        assert "has turned OFF" in mail["body"]
        assert mail["image"][:2] == b"\xff\xd8"  # annotated JPEG attached

        await h.frame(40, lit=False)
        assert h.subjects() == ["OFF: Circuit 12"]  # no repeat by default
        assert h.events() == ["lamp_on", "lamp_off", "email_sent"]

    run(scenario())


def test_flicker_does_not_alert():
    async def scenario():
        h = Harness()
        await h.frame(0, lit=True)
        await h.frame(10, lit=True)
        for t in range(20, 120, 10):
            await h.frame(t, lit=False)
            await h.frame(t + 5, lit=True)
        assert h.state() == "on"
        assert h.subjects() == []

    run(scenario())


def test_restore_notification_and_opt_out():
    async def scenario(notify_on_restore):
        h = Harness(notify_on_restore=notify_on_restore)
        for t in (0, 10):
            await h.frame(t, lit=False)
        for t in (20, 30):
            await h.frame(t, lit=True)
        assert h.state() == "on"
        return h

    h = run(scenario(True))
    assert h.subjects() == ["OFF: Circuit 12", "RESTORED: Circuit 12"]
    assert "first reading since monitoring started" in h.notifier.sent[0]["body"]
    h = run(scenario(False))
    assert h.subjects() == ["OFF: Circuit 12"]


def test_reminders_repeat_while_off():
    async def scenario():
        h = Harness(repeat_minutes=1)
        await h.frame(0, lit=False)
        await h.frame(10, lit=False)
        await h.frame(40, lit=False)
        assert h.subjects() == ["OFF: Circuit 12"]
        await h.frame(70, lit=False)
        await h.frame(100, lit=False)
        await h.frame(130, lit=False)
        assert h.subjects() == ["OFF: Circuit 12", "STILL OFF: Circuit 12", "STILL OFF: Circuit 12"]
        assert "Off for:" in h.notifier.sent[1]["body"]

    run(scenario())


def test_state_survives_restart_without_realerting():
    async def scenario():
        h = Harness()
        await h.frame(0, lit=False)
        await h.frame(10, lit=False)
        assert h.subjects() == ["OFF: Circuit 12"]
        # A new monitor (app restart) picks up the persisted OFF state.
        h.monitor = Monitor(h.db, h.notifier, clock=h.clock)
        await h.frame(100, lit=False)
        await h.frame(200, lit=False)
        assert h.subjects() == ["OFF: Circuit 12"]

    run(scenario())


def test_camera_offline_and_back_online():
    async def scenario():
        h = Harness()
        await h.frame(0, lit=True)
        await h.frame(10, lit=True)
        await h.failure(30)
        await h.failure(69)
        assert h.subjects() == []
        await h.failure(70)  # 60 s since the last frame
        assert h.subjects() == ["CAMERA OFFLINE: Panel room"]
        assert h.notifier.sent[0]["recipients"] == ["admin@example.com", "ops@example.com"]
        assert "Circuit 12" in h.notifier.sent[0]["body"]
        await h.failure(200)
        assert len(h.notifier.sent) == 1
        await h.frame(210, lit=True)
        assert h.subjects() == ["CAMERA OFFLINE: Panel room", "CAMERA ONLINE: Panel room"]
        assert h.monitor.status()["cameras"][0]["online"] is True

    run(scenario())


def test_failed_email_is_logged_as_event():
    async def scenario():
        h = Harness(confirm_seconds=0)
        h.notifier.error = RuntimeError("relay refused")
        await h.frame(0, lit=False)
        assert h.state() == "off"
        assert h.events() == ["lamp_off", "email_failed"]
        assert "relay refused" in h.db.list_events()[0]["message"]

    run(scenario())


def test_disabled_lamps_are_ignored():
    async def scenario():
        h = Harness(enabled=False, confirm_seconds=0)
        await h.frame(0, lit=False)
        assert h.state() == "unknown"
        assert h.subjects() == []

    run(scenario())


def test_polling_loop_fetches_frames_and_reports_status():
    async def scenario():
        h = Harness(confirm_seconds=0)
        h.monitor.clock = time.monotonic
        h.source.data = encode(panel(True))
        await h.monitor.start()
        try:
            for _ in range(100):
                await asyncio.sleep(0.01)
                if h.state() == "on":
                    break
        finally:
            await h.monitor.stop()
        status = h.monitor.status()
        assert status["cameras"][0]["online"] is True
        assert status["lamps"][0]["state"] == "on"
        assert status["lamps"][0]["value"] == pytest.approx(230)

    run(scenario())
