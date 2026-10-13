"""Plain-text content of the emails LampMonitor sends. Each function returns (subject, body)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .schemas import Camera, Lamp, Settings


def format_time(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


def _table(rows: Iterable[tuple[str, str]]) -> list[str]:
    rows = list(rows)
    width = max(len(label) for label, _ in rows) + 2
    return [f"{label + ':':<{width}}{value}" for label, value in rows]


def _reading(lamp: Lamp, value: float | None) -> str:
    if value is None:
        return "no reading"
    off_below = max(0.0, lamp.threshold - lamp.hysteresis)
    return f"{value:.0f} (ON at {lamp.threshold:.0f} or above, OFF below {off_below:.0f})"


def _finish(settings: Settings, lines: list[str]) -> str:
    if settings.public_url:
        lines += ["", f"Dashboard: {settings.public_url}"]
    lines += ["", "-- ", "Sent by LampMonitor"]
    return "\n".join(lines)


def _notes(lamp: Lamp) -> list[str]:
    return ["", "Notes:", lamp.notes] if lamp.notes else []


def lamp_off(
    settings: Settings,
    camera: Camera,
    lamp: Lamp,
    value: float | None,
    when: datetime,
    *,
    first_reading: bool = False,
    off_since: datetime | None = None,
) -> tuple[str, str]:
    """Alert for a lamp that went dark, or a reminder (``off_since`` set) that it still is."""
    if off_since is not None:
        subject = f"STILL OFF: {lamp.name}"
        headline = f'"{lamp.name}" is still OFF.'
        rows = [
            ("Lamp", lamp.name),
            ("Camera", camera.name),
            ("Off since", format_time(off_since)),
            ("Off for", format_duration((when - off_since).total_seconds())),
        ]
    else:
        subject = f"OFF: {lamp.name}"
        headline = (
            f'"{lamp.name}" is OFF (first reading since monitoring started).'
            if first_reading
            else f'"{lamp.name}" has turned OFF.'
        )
        rows = [("Lamp", lamp.name), ("Camera", camera.name), ("Detected", format_time(when))]
    rows.append(("Brightness", _reading(lamp, value)))
    lines = [headline, "", *_table(rows), *_notes(lamp), "", "The attached snapshot shows the lamp outlined in red."]
    return subject, _finish(settings, lines)


def lamp_restored(
    settings: Settings,
    camera: Camera,
    lamp: Lamp,
    value: float | None,
    when: datetime,
    *,
    off_since: datetime | None,
) -> tuple[str, str]:
    rows = [("Lamp", lamp.name), ("Camera", camera.name), ("Restored", format_time(when))]
    if off_since is not None:
        rows.append(("Was off for", format_duration((when - off_since).total_seconds())))
    rows.append(("Brightness", _reading(lamp, value)))
    lines = [f'"{lamp.name}" is back ON.', "", *_table(rows), *_notes(lamp)]
    return f"RESTORED: {lamp.name}", _finish(settings, lines)


def camera_offline(
    settings: Settings,
    camera: Camera,
    lamps: list[Lamp],
    *,
    last_image: datetime | None,
    error: str | None,
    when: datetime,
) -> tuple[str, str]:
    rows = [
        ("Camera", camera.name),
        ("Last image", format_time(last_image) if last_image else "none since LampMonitor started"),
        ("Detected", format_time(when)),
        ("Last error", error or "unknown"),
    ]
    lines = [
        f'LampMonitor has lost contact with camera "{camera.name}".',
        "",
        "The camera may have lost power or network connectivity. If it shares a circuit",
        "with the lamps it watches, this can itself mean the power is out.",
        "",
        *_table(rows),
    ]
    if lamps:
        lines += ["", "These lamps are not being monitored until the camera returns:"]
        lines += [f"  - {lamp.name}" for lamp in lamps]
    return f"CAMERA OFFLINE: {camera.name}", _finish(settings, lines)


def camera_online(
    settings: Settings, camera: Camera, *, offline_since: datetime | None, when: datetime
) -> tuple[str, str]:
    rows = [("Camera", camera.name), ("Back online", format_time(when))]
    if offline_since is not None:
        rows.append(("Was offline for", format_duration((when - offline_since).total_seconds())))
    lines = [f'Camera "{camera.name}" is back online and lamp monitoring has resumed.', "", *_table(rows)]
    return f"CAMERA ONLINE: {camera.name}", _finish(settings, lines)


def lamp_test(
    settings: Settings, camera: Camera, lamp: Lamp, value: float | None, recipients: list[str]
) -> tuple[str, str]:
    rows = [
        ("Lamp", lamp.name),
        ("Camera", camera.name),
        ("Brightness now", _reading(lamp, value)),
        ("Alert recipients", ", ".join(recipients)),
    ]
    lines = [
        f'This is a test alert for "{lamp.name}".',
        "If the lamp turns off, an alert like this will be sent to the recipients below.",
        "",
        *_table(rows),
        *_notes(lamp),
    ]
    return f"TEST: {lamp.name}", _finish(settings, lines)


def email_check(settings: Settings) -> tuple[str, str]:
    return "Test email", _finish(settings, ["This is a test message. LampMonitor can send email alerts."])
