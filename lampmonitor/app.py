"""HTTP API and web UI."""

from __future__ import annotations

import asyncio
import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import imaging, messages
from .config import AppConfig
from .db import Database
from .monitor import Frame, Monitor, lamp_recipients
from .notifier import EmailNotifier, NotifierError
from .protect import ProtectClient, ProtectError
from .schemas import (
    SECRET_FIELDS,
    Camera,
    CameraIn,
    Lamp,
    LampIn,
    MeasureRequest,
    Settings,
    parse_addresses,
    settings_view,
)

STATIC_DIR = Path(__file__).parent / "static"


class TestEmailRequest(BaseModel):
    to: str = ""


def _authorized(header: str, username: str, password: str) -> bool:
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        user, _, given = base64.b64decode(token, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user_ok = secrets.compare_digest(user.encode(), username.encode())
    password_ok = secrets.compare_digest(given.encode(), password.encode())
    return user_ok and password_ok


def create_app(
    config: AppConfig | None = None,
    *,
    db: Database | None = None,
    notifier: EmailNotifier | None = None,
    monitor: Monitor | None = None,
    start_monitor: bool = True,
) -> FastAPI:
    config = config or AppConfig.from_env()
    db = db or Database(config.db_path)
    notifier = notifier or EmailNotifier()
    monitor = monitor or Monitor(db, notifier)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_monitor:
            await monitor.start()
        try:
            yield
        finally:
            await monitor.stop()

    app = FastAPI(
        title="LampMonitor",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.db = db
    app.state.monitor = monitor

    if config.password:

        @app.middleware("http")
        async def require_login(request: Request, call_next):
            if request.url.path == "/api/health" or _authorized(
                request.headers.get("authorization", ""), config.username, config.password
            ):
                return await call_next(request)
            return Response(
                "Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="LampMonitor"'}
            )

    def camera_or_404(camera_id: int) -> Camera:
        camera = db.get_camera(camera_id)
        if camera is None:
            raise HTTPException(404, "Camera not found")
        return camera

    def lamp_or_404(lamp_id: int) -> Lamp:
        lamp = db.get_lamp(lamp_id)
        if lamp is None:
            raise HTTPException(404, "Lamp not found")
        return lamp

    async def frame_for(camera: Camera, *, fresh: bool = False) -> Frame:
        max_age = 0 if fresh else max(15.0, camera.poll_interval * 3)
        try:
            return await monitor.latest_frame(camera, max_age=max_age)
        except Exception as exc:
            raise HTTPException(502, f"Could not get an image from {camera.name}: {exc}") from exc

    async def send_or_400(recipients: list[str], subject: str, body: str, image: bytes | None = None) -> None:
        try:
            await notifier.send(db.get_settings(), recipients, subject, body, image)
        except NotifierError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    # Settings

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return settings_view(db.get_settings())

    @app.put("/api/settings")
    async def update_settings(changes: dict[str, Any] = Body(...)) -> dict[str, Any]:
        merged = db.get_settings().model_dump()
        for key, value in changes.items():
            if key in SECRET_FIELDS and value is None:
                continue  # leave secrets unchanged unless a new value is supplied
            merged[key] = value
        try:
            settings = Settings.model_validate(merged)
        except ValidationError as exc:
            errors = exc.errors(include_url=False, include_context=False)
            raise RequestValidationError([{**error, "loc": ("body", *error["loc"])} for error in errors]) from exc
        db.save_settings(settings)
        return settings_view(settings)

    @app.post("/api/settings/test-email")
    async def send_test_email(request: TestEmailRequest) -> dict[str, list[str]]:
        settings = db.get_settings()
        recipients = parse_addresses(request.to) or parse_addresses(settings.admin_emails)
        subject, body = messages.email_check(settings)
        await send_or_400(recipients, subject, body)
        return {"sent_to": recipients}

    @app.get("/api/protect/cameras")
    async def protect_cameras() -> list[dict[str, str]]:
        settings = db.get_settings()
        if not (settings.protect_host and settings.protect_api_key):
            raise HTTPException(
                400, "UniFi Protect is not configured. Add the console address and API key in Settings."
            )
        client = ProtectClient(settings.protect_host, settings.protect_api_key, verify_ssl=settings.protect_verify_ssl)
        try:
            return await client.list_cameras()
        except ProtectError as exc:
            raise HTTPException(502, str(exc)) from exc
        finally:
            await client.aclose()

    # Cameras

    @app.get("/api/cameras")
    async def list_cameras() -> list[Camera]:
        return db.list_cameras()

    @app.post("/api/cameras", status_code=201)
    async def create_camera(data: CameraIn) -> Camera:
        camera = db.create_camera(data)
        monitor.sync()
        return camera

    @app.get("/api/cameras/{camera_id}")
    async def get_camera(camera_id: int) -> Camera:
        return camera_or_404(camera_id)

    @app.put("/api/cameras/{camera_id}")
    async def update_camera(camera_id: int, data: CameraIn) -> Camera:
        camera = db.update_camera(camera_id, data)
        if camera is None:
            raise HTTPException(404, "Camera not found")
        monitor.invalidate(camera_id)
        monitor.sync()
        return camera

    @app.delete("/api/cameras/{camera_id}", status_code=204)
    async def delete_camera(camera_id: int) -> Response:
        if not db.delete_camera(camera_id):
            raise HTTPException(404, "Camera not found")
        monitor.sync()
        return Response(status_code=204)

    @app.get("/api/cameras/{camera_id}/snapshot")
    async def snapshot(camera_id: int, fresh: bool = False) -> Response:
        frame = await frame_for(camera_or_404(camera_id), fresh=fresh)
        return Response(frame.data, media_type=frame.media_type, headers={"Cache-Control": "no-store"})

    @app.post("/api/cameras/{camera_id}/measure")
    async def measure(camera_id: int, region: MeasureRequest) -> dict[str, Any]:
        frame = await frame_for(camera_or_404(camera_id))
        return {
            "value": imaging.measure(frame.rgb, region, region.metric),
            "width": frame.width,
            "height": frame.height,
            "captured_at": frame.captured_at,
        }

    # Lamps

    @app.get("/api/lamps")
    async def list_all_lamps() -> list[Lamp]:
        return db.list_lamps()

    @app.get("/api/cameras/{camera_id}/lamps")
    async def list_lamps(camera_id: int) -> list[Lamp]:
        camera_or_404(camera_id)
        return db.list_lamps(camera_id)

    @app.post("/api/cameras/{camera_id}/lamps", status_code=201)
    async def create_lamp(camera_id: int, data: LampIn) -> Lamp:
        camera_or_404(camera_id)
        return db.create_lamp(camera_id, data)

    @app.get("/api/lamps/{lamp_id}")
    async def get_lamp(lamp_id: int) -> Lamp:
        return lamp_or_404(lamp_id)

    @app.put("/api/lamps/{lamp_id}")
    async def update_lamp(lamp_id: int, data: LampIn) -> Lamp:
        lamp_or_404(lamp_id)
        if not data.enabled:
            # A disabled lamp isn't watched, so its last known state goes stale.
            db.set_lamp_state(lamp_id, "unknown", None)
        return db.update_lamp(lamp_id, data)

    @app.delete("/api/lamps/{lamp_id}", status_code=204)
    async def delete_lamp(lamp_id: int) -> Response:
        if not db.delete_lamp(lamp_id):
            raise HTTPException(404, "Lamp not found")
        return Response(status_code=204)

    @app.post("/api/lamps/{lamp_id}/test-alert")
    async def test_lamp_alert(lamp_id: int) -> dict[str, list[str]]:
        lamp = lamp_or_404(lamp_id)
        camera = camera_or_404(lamp.camera_id)
        settings = db.get_settings()
        recipients = lamp_recipients(lamp, settings)
        value = image = None
        try:
            frame = await frame_for(camera)
            value = imaging.measure(frame.rgb, lamp, lamp.metric)
            image = await asyncio.to_thread(imaging.annotate, frame.rgb, lamp, lamp.name)
        except HTTPException:
            pass  # still useful to test email delivery without a snapshot
        subject, body = messages.lamp_test(settings, camera, lamp, value, recipients)
        await send_or_400(recipients, subject, body, image)
        db.add_event(
            "email_sent", f'Emailed "{subject}" to {", ".join(recipients)}', camera_id=camera.id, lamp_id=lamp.id
        )
        return {"sent_to": recipients}

    # Status and history

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return {**monitor.status(), "server_time": datetime.now(timezone.utc)}

    @app.get("/api/events")
    async def events(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return db.list_events(limit)

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
