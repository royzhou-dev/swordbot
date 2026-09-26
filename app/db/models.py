"""Imports every ORM model so `Base.metadata` is complete (for Alembic and tests)."""

from app.cases.models import CaseFact, CaseTransition, SupportCase
from app.db.base import Base
from app.events.models import Event
from app.users.models import User

__all__ = ["Base", "CaseFact", "CaseTransition", "Event", "SupportCase", "User"]
