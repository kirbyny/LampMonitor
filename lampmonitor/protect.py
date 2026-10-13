"""Client for the UniFi Protect Integration API (API-key authenticated)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class ProtectError(Exception):
    """UniFi Protect could not be reached or rejected a request."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _error_detail(response: httpx.Response) -> str:
    """Protect's own explanation from an error response, e.g. ': Camera does not support full HD snapshot'."""
    try:
        message = response.json().get("error")
    except (ValueError, AttributeError):
        message = response.text.strip()[:200]
    return f": {message}" if message else ""


def _base_url(host: str) -> str:
    host = host.strip().rstrip("/")
    if not host.lower().startswith(("http://", "https://")):
        host = f"https://{host}"
    return f"{host}/proxy/protect/integration/v1"


class ProtectClient:
    def __init__(
        self,
        host: str,
        api_key: str,
        *,
        verify_ssl: bool = False,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=_base_url(host),
            headers={"X-API-KEY": api_key},
            verify=verify_ssl,
            timeout=timeout,
            transport=transport,
        )
        # Cameras whose featureFlags.supportFullHdSnapshot is false reject highQuality=true.
        self._no_full_hd: set[str] = set()

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.get(path, **kwargs)
        except httpx.HTTPError as exc:
            raise ProtectError(f"cannot reach UniFi Protect: {exc or exc.__class__.__name__}") from exc
        status = response.status_code
        if status in (401, 403):
            raise ProtectError("UniFi Protect rejected the API key", status)
        if status == 404:
            detail = f"{_error_detail(response)} (wrong camera ID, or old Protect?)"
            raise ProtectError(f"UniFi Protect returned 404 for {path}{detail}", status)
        if status >= 400:
            raise ProtectError(f"UniFi Protect returned HTTP {status} for {path}{_error_detail(response)}", status)
        return response

    async def list_cameras(self) -> list[dict[str, str]]:
        data = (await self._get("/cameras")).json()
        cameras = [
            {"id": item["id"], "name": item.get("name") or item["id"], "state": item.get("state", "")}
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]
        return sorted(cameras, key=lambda camera: camera["name"].lower())

    async def snapshot(self, camera_id: str, *, high_quality: bool = True) -> bytes:
        path = f"/cameras/{quote(camera_id, safe='')}/snapshot"
        full_hd = high_quality and camera_id not in self._no_full_hd
        try:
            response = await self._get(path, params={"highQuality": "true" if full_hd else "false"})
        except ProtectError as exc:
            if not (full_hd and exc.status == 400):
                raise
            self._no_full_hd.add(camera_id)
            response = await self._get(path, params={"highQuality": "false"})
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise ProtectError(f"unexpected snapshot response ({content_type or 'no content type'})")
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()
