from lampmonitor.detector import LampState, LampTracker, Transition

PARAMS = {"threshold": 100, "hysteresis": 20, "confirm_seconds": 10}


def feed(tracker, readings):
    """Feed (time, value) pairs; return the transitions that were confirmed."""
    return [t for now, value in readings if (t := tracker.update(value, now, **PARAMS))]


def test_first_reading_confirms_after_delay():
    tracker = LampTracker()
    assert feed(tracker, [(0, 200), (5, 200)]) == []
    assert tracker.pending is LampState.ON
    assert feed(tracker, [(10, 200)]) == [Transition(LampState.UNKNOWN, LampState.ON)]
    assert tracker.state is LampState.ON
    assert tracker.pending is None


def test_off_requires_continuous_confirmation():
    tracker = LampTracker(LampState.ON)
    # Brief dip (someone walks past) is ignored.
    assert feed(tracker, [(0, 10), (5, 10), (8, 200), (12, 10), (21, 10)]) == []
    assert feed(tracker, [(22, 10)]) == [Transition(LampState.ON, LampState.OFF)]


def test_hysteresis_band_keeps_previous_state():
    tracker = LampTracker(LampState.ON)
    # 85 is below the threshold but not below threshold - hysteresis (80).
    assert feed(tracker, [(0, 85), (60, 85)]) == []
    assert tracker.state is LampState.ON

    tracker = LampTracker(LampState.OFF)
    assert feed(tracker, [(0, 85), (60, 85)]) == []
    assert tracker.state is LampState.OFF
    assert feed(tracker, [(70, 100), (80, 100)]) == [Transition(LampState.OFF, LampState.ON)]


def test_zero_confirmation_is_immediate():
    tracker = LampTracker(LampState.ON)
    assert tracker.update(0, 0, threshold=100, hysteresis=0, confirm_seconds=0) == Transition(
        LampState.ON, LampState.OFF
    )


def test_reset_pending_restarts_confirmation():
    tracker = LampTracker(LampState.ON)
    feed(tracker, [(0, 10)])
    tracker.reset_pending()
    assert feed(tracker, [(100, 10)]) == []
    assert feed(tracker, [(110, 10)]) == [Transition(LampState.ON, LampState.OFF)]
