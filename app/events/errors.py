"""Event-processing errors.

The worker retries any exception with backoff unless it is a
`PermanentEventError`, which marks the event `dead` at once. Integration errors
that can never succeed on retry (bad credentials, a malformed payload) should
subclass it.
"""

from app.events.models import EventType


class EventError(Exception):
    """Base class for event-layer errors. Messages must not contain user content."""


class PermanentEventError(EventError):
    """Retrying cannot help. The event goes straight to `dead`."""


class InvalidEventPayloadError(PermanentEventError):
    def __init__(self, event_type: EventType, locations: list[str]) -> None:
        # Only field locations: the payload values may be user content.
        super().__init__(f"invalid {event_type.value} payload at: {', '.join(locations)}")
        self.event_type = event_type


class UnknownEventTypeError(PermanentEventError):
    def __init__(self, event_type: EventType) -> None:
        super().__init__(f"no handler registered for {event_type.value}")
        self.event_type = event_type


class LostClaimError(EventError):
    """The event is no longer claimed by this worker (its lease expired and it was reclaimed).

    The caller must roll back whatever it did for the event.
    """

    def __init__(self, event_id: int) -> None:
        super().__init__(f"event {event_id} is no longer claimed by this worker")
        self.event_id = event_id
