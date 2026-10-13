# LampMonitor

LampMonitor watches panel indicator lamps through UniFi cameras and sends an email when a lamp goes dark. It's meant for monitoring electrical circuits: point a camera at the pilot lights on a panel, draw a box around each lamp, and you'll be emailed if a circuit loses power.

- Define any number of lamp regions on each camera by dragging boxes over a live image.
- Calibrate each lamp's brightness threshold against a live reading.
- Give each lamp a friendly name, its own alert recipients, and notes to include in the email.
- Emails go out when a lamp turns off, optionally when it comes back on, and optionally as repeated reminders while it stays off. Each alert includes a snapshot with the lamp outlined.
- A camera that stops sending images is reported too. If the camera shares a circuit with the lamps it watches, that can itself mean the power is out.

## Quick start

### Docker (recommended)

```sh
# edit docker-compose.yml: set LAMPMONITOR_PASSWORD and TZ
docker compose up -d --build
```

Open `http://<host>:8080` and sign in as `admin` with the password you set.

### Without Docker

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). RTSP cameras also need `ffmpeg` on the `PATH`.

```sh
uv sync
LAMPMONITOR_PASSWORD=change-me uv run lampmonitor
```

## Setting it up

1. **Settings → Email.** Enter your SMTP server, a From address, and the admin recipients. Click **Save and send test email**.
2. **Settings → UniFi Protect** (only for the UniFi Protect source). Create an API key in UniFi OS under *Settings → Control Plane → Integrations*. Enter the console address and the key, then click **Save and test connection**.
3. **Cameras → Add a camera.** Choose an image source:

   | Source | Use when | Notes |
   |---|---|---|
   | **UniFi Protect** | Simplest setup; pick the camera from a list | Uses Protect's snapshot API. Recent Protect versions may return reduced-resolution snapshots (reportedly as low as 640×360). |
   | **RTSP / RTSPS stream** | Lamps are small or far away | Full resolution. In Protect, open the camera → *Settings → Advanced → RTSP*, turn on the **High** stream and copy its `rtsps://…` URL. Needs ffmpeg (included in the Docker image). |
   | **Snapshot URL** | Standalone (non-Protect) cameras, or anything that serves a still JPEG | Credentials can go in the URL: `http://user:pass@camera/snap.jpeg`. |

4. **Outline each lamp.** On the camera page, drag a box around the lamp's lens. Brightness is measured inside the oval that fits the box, so the panel around a round lamp is left out. Name the lamp, and set its recipients and notes.
5. **Calibrate the threshold.** The panel shows the live brightness and whether it currently reads ON or OFF. With the lamp lit, **Set the threshold to 60% of this reading** is a good start. The most reliable approach is to note the reading with the lamp both on and off (switch the circuit off briefly) and pick a value in between.
6. **Send test alert** confirms that the lamp's recipients get the email.

## How detection works

Every *poll interval* (5 s by default) LampMonitor takes a frame from each camera and measures each lamp:

- **Brightness** is the HSV *value* of each pixel, which is the brightest of red, green and blue. A lit red or green lamp therefore reads as bright as a white one. The scale runs from 0 to 255.
- **Measure:**
  - *Brightest spot* (default) uses the 90th percentile of the oval, so a loosely drawn box still reads correctly.
  - *Average* uses the mean over the whole oval.
- **Threshold and hysteresis.** The lamp is ON at or above the threshold, and OFF below *threshold − hysteresis*. Readings in between keep the previous state, so a lamp near the threshold doesn't flap.
- **Confirm after.** A change must hold for this many seconds before it counts. This filters out flicker, people walking past, and camera exposure adjustments.
- **Persistence.** States are stored, so restarting LampMonitor doesn't re-send alerts for lamps that were already off.

## Alerts

| Event | Recipients |
|---|---|
| Lamp turned OFF (or was off when first seen) | The lamp's recipients, or the admin recipients if it has none |
| Lamp still OFF (every *repeat* minutes, if set) | Same |
| Lamp back ON (if enabled for the lamp) | Same |
| Camera offline (no image for *Report a camera as offline after* seconds, default 120) | Admin recipients plus the recipients of every lamp on that camera |
| Camera back online | Same |

Every alert, sent email, and delivery failure is recorded on the **Events** page.

## Tips for reliable monitoring

- **Power the camera and network independently of the monitored circuits.** Use a PoE switch on a UPS. If they share a circuit, a power cut shows up as *camera offline* rather than *lamp off*. That still gets reported, but it's clearer when the camera stays up.
- **Keep the scene lit consistently, or turn off the camera's IR night mode.** IR mode changes how lamps look. Recalibrate if you change lighting.
- **Mount the camera so lamps are at least ~10 px across** in the image. Use the editor's zoom to check. For distant panels, use the RTSPS source or a zoom-lens camera.
- **Avoid glare.** Reflections on an unlit lens can read as bright. The live reading in the editor shows this immediately.

## Configuration

These environment variables configure the process. Everything else is set in the web UI and stored in `lampmonitor.db` in the data directory.

| Variable | Default | |
|---|---|---|
| `LAMPMONITOR_PASSWORD` | *(unset)* | Password for the web UI (HTTP Basic auth). **Set this.** Without it the UI is open to anyone who can reach it. |
| `LAMPMONITOR_USERNAME` | `admin` | Username for the web UI |
| `LAMPMONITOR_DATA_DIR` | `./data` (`/data` in Docker) | Where the database lives |
| `LAMPMONITOR_HOST` / `LAMPMONITOR_PORT` | `0.0.0.0` / `8080` | Listen address |
| `LAMPMONITOR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, … |
| `LAMPMONITOR_FFMPEG` | `ffmpeg` | Path to ffmpeg for RTSP cameras |
| `TZ` | | Time zone for times in emails |

**Security notes:**

- The Protect API key, SMTP password and RTSP URLs are stored unencrypted in the database. Protect the data directory accordingly.
- The API never returns the API key or SMTP password.
- Basic auth sends the password with every request, so to reach LampMonitor from outside a trusted network, put it behind an HTTPS reverse proxy.

## API

The web UI uses a JSON API. Interactive docs are at `/api/docs`. Useful endpoints:

- `GET /api/status`: live state of every camera and lamp, suitable for external dashboards.
- `GET /api/events`: event history.
- `GET /api/health`: liveness check that needs no authentication.

## Development

```sh
uv sync
uv run pytest
uv run lampmonitor
```

Code layout (`lampmonitor/`):

| Module | |
|---|---|
| `app.py` | FastAPI routes and static web UI (`static/`) |
| `monitor.py` | Per-camera polling tasks, state changes, alerts |
| `detector.py` | Debounce and hysteresis state machine |
| `imaging.py` | Decoding, brightness measurement, snapshot annotation |
| `sources.py`, `protect.py` | Frame sources: UniFi Protect, RTSP via ffmpeg, snapshot URL |
| `notifier.py`, `messages.py` | SMTP delivery and email text |
| `db.py`, `schemas.py` | SQLite storage and data models |
