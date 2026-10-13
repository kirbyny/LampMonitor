"""Where camera images come from: UniFi Protect, an RTSP(S) stream, or a snapshot URL."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Protocol

import httpx

from .protect import ProtectClient
from .schemas import Camera, Settings


class SourceError(Exception):
    """A frame could not be fetched."""


class FrameSource(Protocol):
    async def fetch(self) -> bytes:
        """Return one encoded image (JPEG, PNG, ...)."""

    async def close(self) -> None: ...


class ProtectSource:
    """Snapshots from the UniFi Protect Integration API."""

    def __init__(self, client: ProtectClient, camera_id: str) -> None:
        self._client = client
        self._camera_id = camera_id

    async def fetch(self) -> bytes:
        return await self._client.snapshot(self._camera_id)

    async def close(self) -> None:
        await self._client.aclose()


class HttpSnapshotSource:
    """A camera (or anything else) that serves a still image over HTTP.

    Credentials can be embedded in the URL (http://user:pass@host/snap.jpeg).
    Certificates aren't verified because cameras almost always use self-signed ones.
    """

    def __init__(
        self, url: str, *, timeout: float = 15.0, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True, transport=transport)

    async def fetch(self) -> bytes:
        try:
            response = await self._client.get(self._url)
        except httpx.HTTPError as exc:
            raise SourceError(f"snapshot request failed: {exc or exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise SourceError(f"snapshot URL returned HTTP {response.status_code}")
        return response.content

    async def close(self) -> None:
        await self._client.aclose()


_STREAM_URL_RE = re.compile(r"rtsps?://\S+", re.IGNORECASE)


class RtspSource:
    """Grabs one key frame per fetch from an RTSP(S) stream using ffmpeg.

    Key frames only (``-skip_frame nokey``) so we never analyse a half-decoded,
    smeared frame from the middle of a group of pictures.
    """

    def __init__(self, url: str, *, ffmpeg: str | None = None, timeout: float = 20.0) -> None:
        self._url = url
        self._ffmpeg = ffmpeg or os.environ.get("LAMPMONITOR_FFMPEG", "ffmpeg")
        self._timeout = timeout

    async def fetch(self) -> bytes:
        command = [
            self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-rtsp_transport", "tcp", "-skip_frame", "nokey",
            "-i", self._url,
            "-an", "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "pipe:1",
        ]  # fmt: skip
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SourceError("ffmpeg is not installed; it is required for RTSP cameras") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), self._timeout)
        except TimeoutError:
            raise SourceError(f"no frame from the stream within {self._timeout:.0f} s") from None
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode != 0 or not stdout:
            lines = stderr.decode(errors="replace").strip().splitlines()
            # ffmpeg often echoes the URL, which contains the stream's access token.
            detail = _STREAM_URL_RE.sub("<stream URL>", lines[-1]) if lines else f"exit code {process.returncode}"
            raise SourceError(f"ffmpeg could not read the stream: {detail}")
        return stdout

    async def close(self) -> None:
        pass


def build_source(camera: Camera, settings: Settings) -> FrameSource:
    if camera.source_type == "protect":
        if not (settings.protect_host and settings.protect_api_key):
            raise SourceError("UniFi Protect is not configured; add the console address and API key in Settings")
        client = ProtectClient(settings.protect_host, settings.protect_api_key, verify_ssl=settings.protect_verify_ssl)
        return ProtectSource(client, camera.protect_camera_id)
    if camera.source_type == "rtsp":
        return RtspSource(camera.url)
    return HttpSnapshotSource(camera.url)


def source_key(camera: Camera, settings: Settings) -> tuple:
    """Everything that, when changed, requires building a new source."""
    if camera.source_type == "protect":
        return (
            "protect",
            camera.protect_camera_id,
            settings.protect_host,
            settings.protect_api_key,
            settings.protect_verify_ssl,
        )
    return (camera.source_type, camera.url)
