import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.events.models import EventSource, EventType


class NewEvent(BaseModel):
    """A normalized event, as produced by a transport adapter or a scheduler."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: uuid.UUID
    type: EventType
    source: EventSource
    # The source's id for this delivery. Redelivery with the same id is dropped.
    external_id: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    case_id: uuid.UUID | None = None
    # When to process the event. None means now. A future time schedules it.
    run_at: AwareDatetime | None = None

    @field_validator("payload")
    @classmethod
    def _payload_is_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON-serializable") from exc
        return value


@dataclass(frozen=True, slots=True)
class ClaimedEvent:
    """An immutable snapshot of an event the worker has claimed.

    Handlers get this rather than the ORM row, so they cannot change the
    event's queue state behind the service's back.
    """

    id: int
    user_id: uuid.UUID
    case_id: uuid.UUID | None
    type: EventType
    source: EventSource
    external_id: str
    payload: dict[str, Any]
    # Including this attempt.
    attempts: int
    claim_token: uuid.UUID
    created_at: datetime
