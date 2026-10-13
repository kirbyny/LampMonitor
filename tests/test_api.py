import base64

import pytest
from fastapi.testclient import TestClient

from lampmonitor.app import create_app
from lampmonitor.config import AppConfig
from lampmonitor.db import Database
from lampmonitor.monitor import Monitor

from .conftest import LAMP_REGION, FakeNotifier, FakeSource, encode, panel

LAMP = {**LAMP_REGION, "name": "Circuit 12", "email_to": "ops@example.com; oncall@example.com"}
CAMERA = {"name": "Panel room", "source_type": "http", "url": "http://cam.local/snap.jpeg"}


class Env:
    def __init__(self, tmp_path, password=None):
        self.db = Database(":memory:")
        self.notifier = FakeNotifier()
        self.source = FakeSource(encode(panel(True)))
        self.monitor = Monitor(self.db, self.notifier, source_factory=lambda camera, settings: self.source)
        config = AppConfig(data_dir=tmp_path, password=password)
        self.app = create_app(config, db=self.db, notifier=self.notifier, monitor=self.monitor, start_monitor=False)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def client(env):
    with TestClient(env.app) as client:
        yield client


def make_camera(client) -> dict:
    response = client.post("/api/cameras", json=CAMERA)
    assert response.status_code == 201, response.text
    return response.json()


def test_ui_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "LampMonitor" in response.text
    assert client.get("/app.js").status_code == 200


def test_settings_hide_secrets_and_keep_them_when_omitted(client):
    response = client.put(
        "/api/settings",
        json={"protect_host": " 192.168.1.1 ", "protect_api_key": "secret-key", "smtp_password": " pass word "},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["protect_host"] == "192.168.1.1"
    assert body["protect_api_key_set"] is True
    assert "protect_api_key" not in body and "smtp_password" not in body

    client.put("/api/settings", json={"smtp_host": "smtp.example.com"})
    settings = client.app.state.db.get_settings()
    assert settings.protect_api_key == "secret-key"
    assert settings.smtp_password == " pass word "  # passwords are not stripped


@pytest.mark.parametrize(
    "change",
    [{"admin_emails": "not-an-address"}, {"smtp_port": 0}, {"unknown_field": 1}, {"public_url": "example.com"}],
)
def test_invalid_settings_are_rejected(client, change):
    response = client.put("/api/settings", json=change)
    assert response.status_code == 422


def test_camera_crud_and_validation(client):
    camera = make_camera(client)
    assert camera["poll_interval"] == 5
    assert client.get("/api/cameras").json() == [camera]

    bad = client.post("/api/cameras", json={"name": "X", "source_type": "rtsp", "url": "http://nope"})
    assert bad.status_code == 422
    assert client.post("/api/cameras", json={"name": "X", "source_type": "protect"}).status_code == 422

    updated = client.put(f"/api/cameras/{camera['id']}", json={**CAMERA, "name": "Renamed", "enabled": False})
    assert updated.json()["name"] == "Renamed"
    assert client.delete(f"/api/cameras/{camera['id']}").status_code == 204
    assert client.get(f"/api/cameras/{camera['id']}").status_code == 404


def test_lamp_crud_and_validation(client):
    camera = make_camera(client)
    response = client.post(f"/api/cameras/{camera['id']}/lamps", json=LAMP)
    assert response.status_code == 201, response.text
    lamp = response.json()
    assert lamp["email_to"] == "ops@example.com, oncall@example.com"
    assert lamp["state"] == "unknown"
    assert lamp["metric"] == "peak"

    outside = client.post(f"/api/cameras/{camera['id']}/lamps", json={**LAMP, "x": 0.95, "w": 0.2})
    assert outside.status_code == 422
    bad_email = client.post(f"/api/cameras/{camera['id']}/lamps", json={**LAMP, "email_to": "ops@"})
    assert bad_email.status_code == 422

    updated = client.put(f"/api/lamps/{lamp['id']}", json={**LAMP, "threshold": 90, "name": "Freezer"})
    assert updated.json()["threshold"] == 90
    assert [lamp["name"] for lamp in client.get(f"/api/cameras/{camera['id']}/lamps").json()] == ["Freezer"]

    # Deleting the camera deletes its lamps.
    client.delete(f"/api/cameras/{camera['id']}")
    assert client.get(f"/api/lamps/{lamp['id']}").status_code == 404


def test_snapshot_and_measure(client, env):
    camera = make_camera(client)
    snapshot = client.get(f"/api/cameras/{camera['id']}/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.headers["content-type"] == "image/png"

    lit = client.post(f"/api/cameras/{camera['id']}/measure", json={**LAMP_REGION, "metric": "peak"}).json()
    dark = client.post(
        f"/api/cameras/{camera['id']}/measure", json={"x": 0.6, "y": 0.6, "w": 0.1, "h": 0.1}
    ).json()
    assert lit["value"] == 230
    assert dark["value"] == 30
    assert (lit["width"], lit["height"]) == (200, 100)


def test_snapshot_errors_are_reported(client, env):
    camera = make_camera(client)
    env.source.error = OSError("connection refused")
    response = client.get(f"/api/cameras/{camera['id']}/snapshot?fresh=1")
    assert response.status_code == 502
    assert "connection refused" in response.json()["detail"]


def test_test_alert_emails_lamp_recipients(client, env):
    client.put("/api/settings", json={"smtp_host": "smtp.example.com", "email_from": "lm@example.com"})
    camera = make_camera(client)
    lamp = client.post(f"/api/cameras/{camera['id']}/lamps", json=LAMP).json()
    response = client.post(f"/api/lamps/{lamp['id']}/test-alert")
    assert response.status_code == 200, response.text
    assert response.json() == {"sent_to": ["ops@example.com", "oncall@example.com"]}
    mail = env.notifier.sent[0]
    assert mail["subject"] == "TEST: Circuit 12"
    assert "230" in mail["body"]
    assert mail["image"] is not None


def test_status_lists_cameras_and_lamps(client):
    camera = make_camera(client)
    client.post(f"/api/cameras/{camera['id']}/lamps", json=LAMP)
    status = client.get("/api/status").json()
    assert [c["name"] for c in status["cameras"]] == ["Panel room"]
    assert status["lamps"][0]["state"] == "unknown"
    assert status["lamps"][0]["value"] is None


def test_password_protects_everything_but_health(tmp_path):
    env = Env(tmp_path, password="hunter2")
    with TestClient(env.app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 401
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/status", auth=("admin", "hunter2")).status_code == 200
        garbage = {"Authorization": "Basic " + base64.b64encode(b"\xff\xfe").decode()}
        assert client.get("/api/status", headers=garbage).status_code == 401
