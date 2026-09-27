"""Imports every ORM model so `Base.metadata` is complete (for Alembic and tests)."""

from app.actions.models import PendingAction
from app.cases.models import CaseFact, CaseMessage, CaseTransition, SupportCase
from app.db.base import Base
from app.email.models import OutboundEmail
from app.events.models import Event
from app.users.models import User

__all__ = [
    "Base",
    "CaseFact",
    "CaseMessage",
    "CaseTransition",
    "Event",
    "OutboundEmail",
    "PendingAction",
    "SupportCase",
    "User",
]
