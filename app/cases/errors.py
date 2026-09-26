import uuid

from app.cases.models import CaseStatus


class CaseError(Exception):
    """Base class for case-domain errors."""


class CaseNotFoundError(CaseError):
    def __init__(self, case_id: uuid.UUID) -> None:
        super().__init__(f"case {case_id} not found")
        self.case_id = case_id


class InvalidTransitionError(CaseError):
    def __init__(self, case_id: uuid.UUID, from_status: CaseStatus, to_status: CaseStatus) -> None:
        super().__init__(
            f"case {case_id}: transition {from_status.value} -> {to_status.value} is not allowed"
        )
        self.case_id = case_id
        self.from_status = from_status
        self.to_status = to_status


class ConcurrentCaseUpdateError(CaseError):
    """The case row changed after it was loaded. Reload and retry the unit of work."""

    def __init__(self, case_id: uuid.UUID) -> None:
        super().__init__(f"case {case_id} was modified concurrently")
        self.case_id = case_id


class InvalidFactError(CaseError, ValueError):
    """A fact key, value, or confidence failed validation."""


class CaseClosedError(CaseError):
    """The operation is not allowed on a resolved or cancelled case."""

    def __init__(self, case_id: uuid.UUID, status: CaseStatus) -> None:
        super().__init__(f"case {case_id} is {status.value}")
        self.case_id = case_id
        self.status = status
