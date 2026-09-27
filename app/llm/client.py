"""The `LLMClient` protocol: the one way the app talks to a language model.

Two calls are enough (PLAN D11): `complete` for free text such as a reply or
a summary, and `extract_structured` for anything that drives the workflow,
including each agent step, whose `AgentDecision` is itself a structured output.
Every call is stateless. The caller passes the full context, because the
database, not the provider, owns conversation state.

`purpose` is a short label ("intake_extract", "draft_email", ...) used only for logs.
"""

from typing import Literal, Protocol

from pydantic import BaseModel

from app.llm.errors import LLMAuthenticationError


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMClient(Protocol):
    async def complete(self, messages: list[Message], *, purpose: str) -> str: ...

    async def extract_structured[T: BaseModel](
        self, messages: list[Message], schema: type[T], *, purpose: str
    ) -> T:
        """Return `schema` validated from the model's output.

        Invalid output gets one repair retry, then `InvalidAgentDecisionError`.
        """
        ...

    async def aclose(self) -> None: ...


class UnconfiguredLLMClient:
    """Used when OPENAI_API_KEY is unset. Every call fails permanently."""

    def _fail(self) -> LLMAuthenticationError:
        return LLMAuthenticationError("OPENAI_API_KEY is not set")

    async def complete(self, messages: list[Message], *, purpose: str) -> str:
        raise self._fail()

    async def extract_structured[T: BaseModel](
        self, messages: list[Message], schema: type[T], *, purpose: str
    ) -> T:
        raise self._fail()

    async def aclose(self) -> None:
        return None
