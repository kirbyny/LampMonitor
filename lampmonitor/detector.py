"""Turns a stream of brightness readings into debounced ON/OFF lamp states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LampState(str, Enum):
    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Transition:
    previous: LampState
    current: LampState


class LampTracker:
    """Debounced on/off classifier for one lamp.

    A reading at or above ``threshold`` means ON and one below
    ``threshold - hysteresis`` means OFF. Readings in between keep the previous
    classification, so a lamp hovering near the threshold doesn't flap. A new
    state is only confirmed once it has been seen continuously for
    ``confirm_seconds``, which filters out flicker and brief obstructions.
    """

    def __init__(self, state: LampState = LampState.UNKNOWN) -> None:
        self.state = state
        self.value: float | None = None
        self.pending: LampState | None = None
        self.pending_since: float | None = None
        self.last_alert: float | None = None
        self._reading: LampState | None = None

    def classify(self, value: float, threshold: float, hysteresis: float) -> LampState:
        if value >= threshold:
            return LampState.ON
        if value < threshold - hysteresis:
            return LampState.OFF
        if self._reading is not None:
            return self._reading
        if self.state is not LampState.UNKNOWN:
            return self.state
        return LampState.ON if value >= threshold - hysteresis / 2 else LampState.OFF

    def update(
        self,
        value: float,
        now: float,
        *,
        threshold: float,
        hysteresis: float,
        confirm_seconds: float,
    ) -> Transition | None:
        """Record a reading taken at monotonic time ``now``; return the transition it confirms, if any."""
        self.value = value
        reading = self._reading = self.classify(value, threshold, hysteresis)
        if reading is self.state:
            self.pending = self.pending_since = None
            return None
        if reading is not self.pending:
            self.pending, self.pending_since = reading, now
        if now - self.pending_since < confirm_seconds:
            return None
        transition = Transition(self.state, reading)
        self.state = reading
        self.pending = self.pending_since = None
        return transition

    def reset_pending(self) -> None:
        """Forget any unconfirmed change, e.g. after a gap in observations."""
        self.pending = self.pending_since = None
        self._reading = None
