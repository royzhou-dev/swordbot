"""Tool errors.

All are permanent: the LLM's output is validated against a schema that only
admits permitted tools, so reaching one of these means a code bug or a
bypassed check, and retrying cannot help. Messages carry tool names and field
locations, never argument values.
"""

from app.events.errors import PermanentEventError


class ToolError(PermanentEventError):
    """Base class for tool errors."""


class UnknownToolError(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool {name!r}")
        self.name = name


class ToolNotPermittedError(ToolError):
    """The tool exists but is not allowed in this step."""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} is not permitted here")
        self.name = name


class ToolApprovalRequiredError(ToolError):
    """A `REQUIRES_APPROVAL` tool was called without a valid approval record."""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} requires a valid approval")
        self.name = name


class ToolNeedsCaseError(ToolError):
    """A tool that works on a case ran without one."""

    def __init__(self, name: str) -> None:
        super().__init__(f"tool {name!r} needs a case")
        self.name = name


class InvalidToolArgumentsError(ToolError):
    def __init__(self, name: str, locations: list[str]) -> None:
        super().__init__(f"invalid arguments for tool {name!r} at: {', '.join(locations)}")
        self.name = name
        self.locations = locations
