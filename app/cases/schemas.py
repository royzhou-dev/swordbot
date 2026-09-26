from pydantic import BaseModel, ConfigDict, Field


class CaseFieldsUpdate(BaseModel):
    """Operational case fields that may be changed directly.

    Fact-backed fields (merchant, order number, ...) are not accepted here; they
    change only through `set_fact` so they always carry provenance. Status
    changes only through the state machine.
    """

    model_config = ConfigDict(extra="forbid")

    gmail_thread_id: str | None = Field(default=None, max_length=64)
    auto_reply_enabled: bool = False
