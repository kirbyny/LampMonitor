"""Background polling of cameras, lamp state tracking and alerting."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from . import imaging, messages
from .db import Database
from .detector import LampState, LampTracker, Transition
from .notifier import EmailNotifier
from .schemas import Camera, Lamp, Settings, parse_addresses
from .sources import FrameSource, build_source, source_key

log = logging.getLogger(__name__)

SYNC_INTERVAL = 30.0


@dataclass
class Frame:
    data: bytes
    media_type: str
    rgb: np.ndarray
    captured_at: datetime
    monotonic: float

    @property
    def width(self) -> int:
        return self.rgb.shape[1]

    @property
    def height(self) -> int:
        return self.rgb.shape[0]


def decode_frame(data: bytes, monotonic: float) -> Frame:
    rgb, media_type = imaging.decode(data)
    return Frame(data, media_type, rgb, datetime.now(timezone.utc), monotonic)


@dataclass
class CameraHealth:
    started: float
    online: bool | None = None  # None until the first frame arrives or the offline timeout passes
    last_frame: float | None = None
    last_frame_at: datetime | None = None
    offline_since: datetime | None = None
    last_error: str | None = None


def lamp_recipients(lamp: Lamp, settings: Settings) -> list[str]:
    """The lamp's own recipients, or the admin addresses if it has none."""
    return parse_addresses(lamp.email_to) or parse_addresses(settings.admin_emails)


def _describe(error: BaseException) -> str:
    return str(error) or error.__class__.__name__


class Monitor:
    """Runs one polling task per enabled camera and turns lamp state changes into emails."""

    def __init__(
        self,
        db: Database,
        notifier: EmailNotifier,
        *,
        source_factory: Callable[[Camera, Settings], FrameSource] = build_source,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.notifier = notifier
        self.source_factory = source_factory
        self.clock = clock
        self._running = False
        self._tasks: dict[int, asyncio.Task] = {}
        self._watchdog: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._health: dict[int, CameraHealth] = {}
        self._frames: dict[int, Frame] = {}
        self._trackers: dict[int, dict[int, LampTracker]] = {}  # camera id -> lamp id -> tracker

    # Lifecycle

    async def start(self) -> None:
        self._running = True
        self.sync()
        self._watchdog = asyncio.create_task(self._watch(), name="monitor-watchdog")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks.values())
        if self._watchdog is not None:
            tasks.append(self._watchdog)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._watchdog = None
        # Give in-flight alert emails a chance to go out before shutting down.
        if self._background:
            await asyncio.wait(list(self._background), timeout=10)

    async def wait_idle(self) -> None:
        """Wait for queued notifications to finish (used by tests)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(SYNC_INTERVAL)
            try:
                self.sync()
            except Exception:
                log.exception("Monitor sync failed")

    def sync(self) -> None:
        """Start polling newly enabled cameras and stop removed or disabled ones."""
        cameras = self.db.list_cameras()
        enabled = {camera.id for camera in cameras if camera.enabled}
        for camera_id, task in list(self._tasks.items()):
            if camera_id in enabled and not task.done():
                continue
            if task.done() and not task.cancelled() and task.exception() is not None:
                log.error("Camera %s worker crashed", camera_id, exc_info=task.exception())
            task.cancel()
            del self._tasks[camera_id]
        for camera_id in set(self._health) - enabled:
            del self._health[camera_id]
            self._trackers.pop(camera_id, None)
        for camera_id in set(self._frames) - {camera.id for camera in cameras}:
            del self._frames[camera_id]
        if not self._running:
            return
        for camera_id in enabled - set(self._tasks):
            self._tasks[camera_id] = asyncio.create_task(self._run(camera_id), name=f"camera-{camera_id}")

    def invalidate(self, camera_id: int) -> None:
        """Drop the cached frame for a camera, e.g. after its source changed."""
        self._frames.pop(camera_id, None)

    # Polling

    async def _run(self, camera_id: int) -> None:
        self._health[camera_id] = CameraHealth(started=self.clock())
        source: FrameSource | None = None
        key: tuple | None = None
        try:
            while True:
                camera = self.db.get_camera(camera_id)
                if camera is None or not camera.enabled:
                    return
                settings = self.db.get_settings()
                started = self.clock()
                try:
                    new_key = source_key(camera, settings)
                    if new_key != key:
                        if source is not None:
                            await source.close()
                        source, key = None, None
                        source = self.source_factory(camera, settings)
                        key = new_key
                    data = await source.fetch()
                    frame = await asyncio.to_thread(decode_frame, data, self.clock())
                except Exception as exc:
                    await self._guarded(self.handle_failure(camera, settings, exc))
                else:
                    await self._guarded(self.handle_frame(camera, settings, frame))
                await asyncio.sleep(max(0.5, camera.poll_interval - (self.clock() - started)))
        finally:
            if source is not None:
                await source.close()

    async def _guarded(self, step: Coroutine[Any, Any, None]) -> None:
        try:
            await step
        except Exception:
            log.exception("Error while processing camera data")

    async def handle_frame(self, camera: Camera, settings: Settings, frame: Frame) -> None:
        now = frame.monotonic
        self._frames[camera.id] = frame
        health = self._health.setdefault(camera.id, CameraHealth(started=now))
        if health.online is False:
            self._camera_back(camera, settings, health, frame)
        health.online = True
        health.last_frame = now
        health.last_frame_at = frame.captured_at
        health.offline_since = None
        health.last_error = None

        trackers = self._trackers.setdefault(camera.id, {})
        lamps = [lamp for lamp in self.db.list_lamps(camera.id) if lamp.enabled]
        for lamp in lamps:
            tracker = trackers.get(lamp.id)
            if tracker is None:
                tracker = trackers[lamp.id] = LampTracker(LampState(lamp.state))
            value = imaging.measure(frame.rgb, lamp, lamp.metric)
            change = tracker.update(
                value,
                now,
                threshold=lamp.threshold,
                hysteresis=lamp.hysteresis,
                confirm_seconds=lamp.confirm_seconds,
            )
            if change is not None:
                self._lamp_changed(camera, settings, lamp, tracker, change, frame)
            elif self._reminder_due(lamp, tracker, now):
                tracker.last_alert = now
                subject, body = messages.lamp_off(
                    settings, camera, lamp, value, frame.captured_at, off_since=lamp.state_since or frame.captured_at
                )
                self._notify(settings, lamp_recipients(lamp, settings), subject, body, camera, lamp, frame)
        # Forget lamps that were deleted or disabled.
        for lamp_id in set(trackers) - {lamp.id for lamp in lamps}:
            del trackers[lamp_id]

    def _reminder_due(self, lamp: Lamp, tracker: LampTracker, now: float) -> bool:
        if tracker.state is not LampState.OFF or lamp.repeat_minutes <= 0:
            return False
        if tracker.last_alert is None:
            # Already off when we started (state restored from the database): count from now.
            tracker.last_alert = now
            return False
        return now - tracker.last_alert >= lamp.repeat_minutes * 60

    def _lamp_changed(
        self,
        camera: Camera,
        settings: Settings,
        lamp: Lamp,
        tracker: LampTracker,
        change: Transition,
        frame: Frame,
    ) -> None:
        when = frame.captured_at
        value = tracker.value
        self.db.set_lamp_state(lamp.id, change.current.value, when)
        if change.current is LampState.OFF:
            log.warning("Lamp %r on %r is OFF (brightness %.0f)", lamp.name, camera.name, value)
            self.db.add_event(
                "lamp_off", f"{lamp.name} turned OFF", camera_id=camera.id, lamp_id=lamp.id, value=value
            )
            tracker.last_alert = frame.monotonic
            subject, body = messages.lamp_off(
                settings, camera, lamp, value, when, first_reading=change.previous is LampState.UNKNOWN
            )
            self._notify(settings, lamp_recipients(lamp, settings), subject, body, camera, lamp, frame)
        elif change.previous is LampState.OFF:
            log.info("Lamp %r on %r is back ON (brightness %.0f)", lamp.name, camera.name, value)
            self.db.add_event(
                "lamp_on", f"{lamp.name} is back ON", camera_id=camera.id, lamp_id=lamp.id, value=value
            )
            if lamp.notify_on_restore:
                subject, body = messages.lamp_restored(
                    settings, camera, lamp, value, when, off_since=lamp.state_since
                )
                self._notify(settings, lamp_recipients(lamp, settings), subject, body, camera, lamp, frame)
        else:
            self.db.add_event("lamp_on", f"{lamp.name} is ON", camera_id=camera.id, lamp_id=lamp.id, value=value)

    async def handle_failure(self, camera: Camera, settings: Settings, error: BaseException) -> None:
        now = self.clock()
        health = self._health.setdefault(camera.id, CameraHealth(started=now))
        first_failure = health.last_error is None
        health.last_error = _describe(error)
        log.log(logging.WARNING if first_failure else logging.DEBUG, "Camera %r: %s", camera.name, health.last_error)
        if health.online is False:
            return
        since = health.last_frame if health.last_frame is not None else health.started
        if now - since < settings.camera_offline_seconds:
            return
        health.online = False
        health.offline_since = datetime.now(timezone.utc)
        for tracker in self._trackers.get(camera.id, {}).values():
            tracker.reset_pending()
        self.db.add_event(
            "camera_offline", f"Camera {camera.name} is offline: {health.last_error}", camera_id=camera.id
        )
        lamps = [lamp for lamp in self.db.list_lamps(camera.id) if lamp.enabled]
        subject, body = messages.camera_offline(
            settings,
            camera,
            lamps,
            last_image=health.last_frame_at,
            error=health.last_error,
            when=health.offline_since,
        )
        self._notify(settings, self._camera_recipients(settings, lamps), subject, body, camera)

    def _camera_back(self, camera: Camera, settings: Settings, health: CameraHealth, frame: Frame) -> None:
        log.info("Camera %r is back online", camera.name)
        self.db.add_event("camera_online", f"Camera {camera.name} is back online", camera_id=camera.id)
        lamps = [lamp for lamp in self.db.list_lamps(camera.id) if lamp.enabled]
        subject, body = messages.camera_online(
            settings, camera, offline_since=health.offline_since, when=frame.captured_at
        )
        self._notify(settings, self._camera_recipients(settings, lamps), subject, body, camera)

    @staticmethod
    def _camera_recipients(settings: Settings, lamps: list[Lamp]) -> list[str]:
        addresses = parse_addresses(settings.admin_emails)
        for lamp in lamps:
            addresses += lamp_recipients(lamp, settings)
        return list(dict.fromkeys(addresses))

    # Notifications

    def _notify(
        self,
        settings: Settings,
        recipients: list[str],
        subject: str,
        body: str,
        camera: Camera,
        lamp: Lamp | None = None,
        frame: Frame | None = None,
    ) -> None:
        """Send an email in the background so a slow mail server never stalls monitoring."""

        async def deliver() -> None:
            lamp_id = lamp.id if lamp else None
            try:
                image = None
                if lamp is not None and frame is not None:
                    image = await asyncio.to_thread(imaging.annotate, frame.rgb, lamp, lamp.name)
                await self.notifier.send(settings, recipients, subject, body, image)
            except Exception as exc:
                log.error("Email %r not sent: %s", subject, exc)
                self.db.add_event(
                    "email_failed", f'Email "{subject}" not sent: {exc}', camera_id=camera.id, lamp_id=lamp_id
                )
            else:
                self.db.add_event(
                    "email_sent",
                    f'Emailed "{subject}" to {", ".join(recipients)}',
                    camera_id=camera.id,
                    lamp_id=lamp_id,
                )

        task = asyncio.create_task(deliver())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # Queries used by the API

    async def latest_frame(self, camera: Camera, *, max_age: float | None = None) -> Frame:
        """The most recent frame for ``camera``, fetching a new one if it's missing or too old."""
        frame = self._frames.get(camera.id)
        if frame is not None and (max_age is None or self.clock() - frame.monotonic <= max_age):
            return frame
        source = self.source_factory(camera, self.db.get_settings())
        try:
            data = await source.fetch()
        finally:
            await source.close()
        frame = await asyncio.to_thread(decode_frame, data, self.clock())
        self._frames[camera.id] = frame
        return frame

    def status(self) -> dict[str, Any]:
        now = self.clock()
        cameras = []
        for camera in self.db.list_cameras():
            health = self._health.get(camera.id)
            frame = self._frames.get(camera.id)
            cameras.append(
                {
                    "id": camera.id,
                    "name": camera.name,
                    "enabled": camera.enabled,
                    "source_type": camera.source_type,
                    "poll_interval": camera.poll_interval,
                    "online": health.online if health else None,
                    "last_frame_at": health.last_frame_at if health else None,
                    "last_error": health.last_error if health else None,
                    "width": frame.width if frame else None,
                    "height": frame.height if frame else None,
                }
            )
        lamps = []
        for lamp in self.db.list_lamps():
            tracker = self._trackers.get(lamp.camera_id, {}).get(lamp.id)
            pending = tracker.pending if tracker else None
            lamps.append(
                {
                    "id": lamp.id,
                    "camera_id": lamp.camera_id,
                    "name": lamp.name,
                    "enabled": lamp.enabled,
                    "state": lamp.state,
                    "state_since": lamp.state_since,
                    "value": tracker.value if tracker else None,
                    "threshold": lamp.threshold,
                    "hysteresis": lamp.hysteresis,
                    "confirm_seconds": lamp.confirm_seconds,
                    "pending": pending.value if pending else None,
                    "pending_seconds": now - tracker.pending_since if pending else None,
                }
            )
        return {"cameras": cameras, "lamps": lamps}
