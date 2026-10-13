import asyncio
import json
import stat
import sys

import httpx
import pytest

from lampmonitor.protect import ProtectClient, ProtectError
from lampmonitor.schemas import Camera, Settings
from lampmonitor.sources import HttpSnapshotSource, ProtectSource, RtspSource, SourceError, build_source


def run(coro):
    return asyncio.run(coro)


def protect_transport(requests: list[httpx.Request], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status)
        if request.url.path.endswith("/cameras"):
            cameras = [{"id": "b2", "name": "Yard", "state": "CONNECTED"}, {"id": "a1", "name": "Electrical Room"}]
            return httpx.Response(200, json=cameras)
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    return httpx.MockTransport(handler)


def test_protect_lists_cameras_and_fetches_snapshots():
    requests = []

    async def scenario():
        client = ProtectClient("192.168.1.1", "key-123", transport=protect_transport(requests))
        try:
            cameras = await client.list_cameras()
            snapshot = await client.snapshot("a1")
        finally:
            await client.aclose()
        return cameras, snapshot

    cameras, snapshot = run(scenario())
    assert [c["name"] for c in cameras] == ["Electrical Room", "Yard"]
    assert snapshot == b"\xff\xd8jpeg"
    assert str(requests[0].url) == "https://192.168.1.1/proxy/protect/integration/v1/cameras"
    assert requests[1].url.path == "/proxy/protect/integration/v1/cameras/a1/snapshot"
    assert requests[1].url.params["highQuality"] == "true"
    assert all(r.headers["X-API-KEY"] == "key-123" for r in requests)


def test_protect_reports_bad_api_key():
    async def scenario():
        client = ProtectClient("https://unifi.local/", "bad", transport=protect_transport([], status=401))
        try:
            await client.list_cameras()
        finally:
            await client.aclose()

    with pytest.raises(ProtectError, match="rejected the API key"):
        run(scenario())


def test_protect_falls_back_when_camera_lacks_full_hd_snapshots():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params.get("highQuality"))
        if request.url.params.get("highQuality") == "true":
            # What Protect 7.2 returns for cameras with featureFlags.supportFullHdSnapshot = false.
            return httpx.Response(
                400, json={"error": "Camera does not support full HD snapshot", "name": "BAD_REQUEST"}
            )
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    async def scenario():
        client = ProtectClient("192.168.1.1", "key", transport=httpx.MockTransport(handler))
        try:
            return [await client.snapshot("cam1"), await client.snapshot("cam1")]
        finally:
            await client.aclose()

    assert run(scenario()) == [b"\xff\xd8jpeg", b"\xff\xd8jpeg"]
    assert requests == ["true", "false", "false"]  # the unsupported request isn't repeated


def test_protect_errors_include_protects_explanation():
    def handler(request):
        return httpx.Response(500, json={"error": "Camera is disconnected", "name": "SERVER_ERROR"})

    async def scenario():
        client = ProtectClient("192.168.1.1", "key", transport=httpx.MockTransport(handler))
        try:
            await client.snapshot("cam1")
        finally:
            await client.aclose()

    with pytest.raises(ProtectError, match="HTTP 500 for /cameras/cam1/snapshot: Camera is disconnected"):
        run(scenario())


def test_http_snapshot_source():
    def handler(request):
        if request.url.path == "/missing":
            return httpx.Response(404)
        return httpx.Response(200, content=b"image-bytes")

    async def scenario(path):
        source = HttpSnapshotSource(f"http://cam.local{path}", transport=httpx.MockTransport(handler))
        try:
            return await source.fetch()
        finally:
            await source.close()

    assert run(scenario("/snap.jpeg")) == b"image-bytes"
    with pytest.raises(SourceError, match="HTTP 404"):
        run(scenario("/missing"))


def fake_ffmpeg(tmp_path, body: str):
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!{sys.executable}\nimport sys\nargs = sys.argv[1:]\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_rtsp_source_reads_one_keyframe(tmp_path):
    record = tmp_path / "args.json"
    ffmpeg = fake_ffmpeg(
        tmp_path, f"open({str(record)!r}, 'w').write(__import__('json').dumps(args))\nsys.stdout.buffer.write(b'JPEG')"
    )
    url = "rtsps://192.168.1.1:7441/token?enableSrtp"
    assert run(RtspSource(url, ffmpeg=ffmpeg).fetch()) == b"JPEG"
    args = json.loads(record.read_text())
    assert args[args.index("-i") + 1] == url
    assert args[args.index("-skip_frame") + 1] == "nokey"


def test_rtsp_errors_hide_the_stream_token(tmp_path):
    ffmpeg = fake_ffmpeg(
        tmp_path, "sys.stderr.write(args[args.index('-i') + 1] + ': Connection refused\\n'); sys.exit(1)"
    )
    with pytest.raises(SourceError) as excinfo:
        run(RtspSource("rtsps://192.168.1.1:7441/SECRETTOKEN", ffmpeg=ffmpeg).fetch())
    assert "SECRETTOKEN" not in str(excinfo.value)
    assert "Connection refused" in str(excinfo.value)


def test_rtsp_timeout(tmp_path):
    ffmpeg = fake_ffmpeg(tmp_path, "import time; time.sleep(10)")
    with pytest.raises(SourceError, match="no frame"):
        run(RtspSource("rtsp://cam/stream", ffmpeg=ffmpeg, timeout=0.5).fetch())


def test_missing_ffmpeg_is_explained():
    with pytest.raises(SourceError, match="ffmpeg is not installed"):
        run(RtspSource("rtsp://cam/stream", ffmpeg="/nonexistent/ffmpeg").fetch())


def test_build_source_requires_protect_settings():
    camera = Camera(id=1, name="Cam", source_type="protect", protect_camera_id="abc")
    with pytest.raises(SourceError, match="not configured"):
        build_source(camera, Settings())
    source = build_source(camera, Settings(protect_host="10.0.0.1", protect_api_key="k"))
    assert isinstance(source, ProtectSource)
    run(source.close())
