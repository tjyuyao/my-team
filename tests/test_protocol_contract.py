import pytest

from my_team.kernel.event_protocol import VOID
from my_team.kernel.event_validator import EventError, validate_event


def test_application_event_round_trip_and_void_sentinel():
    """Covers the smallest valid event and the process silence sentinel."""
    event = {"source": "alice", "target": "bob", "kind": "application", "payload": {"x": 1}}
    assert validate_event(event) is event
    assert VOID == "VOID"


@pytest.mark.parametrize(
    "event",
    [
        {"source": "", "target": "bob", "kind": "application", "payload": {}},
        {"source": "alice", "target": "", "kind": "application", "payload": {}},
        {"source": "alice", "target": "bob", "kind": "unknown", "payload": {}},
        {"source": "alice", "target": "bob", "kind": "system", "payload": {}},
        {
            "source": "alice",
            "target": "bob",
            "kind": "system",
            "payload": {"command": "install_device"},
        },
    ],
)
def test_invalid_events_are_rejected(event):
    """Rejects malformed protocol shapes before they can reach a process."""
    with pytest.raises(EventError):
        validate_event(event)
