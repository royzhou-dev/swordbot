"""`LLMClient` on the OpenAI Responses API. The only module that imports `openai`.

Every request sets `store=False`: calls are stateless and OpenAI keeps no stored
response to chain from. Structured calls send a strict JSON schema generated
from the Pydantic model, then validate the text in our code (`structured.py`),
so the repair retry sees exactly what the model returned.

Errors: the SDK's exception messages include the response body, so they are
mapped to typed errors carrying only the status and error code, raised `from
None`. The SDK retries brief failures itself (`max_retries`); longer outages
surface as `LLMTemporaryError` and the event worker's backoff takes over.
"""

import time
from typing import Any, NoReturn

import httpx2
import openai
from openai.lib._pydantic import to_strict_json_schema
from openai.types.responses import Response, ResponseInputParam
from openai.types.responses.response_format_text_config_param import (
    ResponseFormatTextConfigParam,
)
from pydantic import BaseModel, SecretStr

from app.llm.client import Message
from app.llm.errors import (
    LLMAuthenticationError,
    LLMQuotaError,
    LLMRefusalError,
    LLMRequestError,
    LLMTemporaryError,
)
from app.llm.structured import RawOutput, extract_with_repair
from app.logging import get_logger

log = get_logger(__name__)

_QUOTA_CODE = "insufficient_quota"


class OpenAIClient:
    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        *,
        timeout: float,
        max_retries: int,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._client = openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    async def complete(self, messages: list[Message], *, purpose: str) -> str:
        started = time.monotonic()
        response = await self._create(messages, text_format=None)
        _log_call(purpose, self._model, None, started, [response], attempts=1, repaired=False)
        raw = _raw_output(response)
        if raw.refused:
            raise LLMRefusalError("text")
        return raw.text

    async def extract_structured[T: BaseModel](
        self, messages: list[Message], schema: type[T], *, purpose: str
    ) -> T:
        started = time.monotonic()
        text_format = _text_format(schema)
        responses: list[Response] = []
        attempts = 0

        async def generate(prompt: list[Message]) -> RawOutput:
            nonlocal attempts
            attempts += 1
            response = await self._create(prompt, text_format=text_format)
            responses.append(response)
            return _raw_output(response)

        try:
            result = await extract_with_repair(generate, messages, schema)
        finally:
            _log_call(
                purpose,
                self._model,
                schema.__name__,
                started,
                responses,
                attempts=attempts,
                repaired=attempts > 1,
            )
        return result.value

    async def aclose(self) -> None:
        await self._client.close()

    async def _create(
        self, messages: list[Message], *, text_format: ResponseFormatTextConfigParam | None
    ) -> Response:
        payload: ResponseInputParam = [{"role": m.role, "content": m.content} for m in messages]
        try:
            if text_format is None:
                return await self._client.responses.create(
                    model=self._model, input=payload, store=False
                )
            return await self._client.responses.create(
                model=self._model, input=payload, store=False, text={"format": text_format}
            )
        except openai.APIError as exc:
            _raise_mapped(exc)


def _text_format(schema: type[BaseModel]) -> ResponseFormatTextConfigParam:
    return {
        "type": "json_schema",
        "name": schema.__name__,
        "schema": to_strict_json_schema(schema),
        "strict": True,
    }


def _raw_output(response: Response) -> RawOutput:
    refused = any(
        content.type == "refusal"
        for item in response.output
        if item.type == "message"
        for content in item.content
    )
    truncated = (
        response.status == "incomplete"
        and response.incomplete_details is not None
        and response.incomplete_details.reason == "max_output_tokens"
    )
    return RawOutput(text=response.output_text, refused=refused, truncated=truncated)


def _raise_mapped(exc: openai.APIError) -> NoReturn:
    # `from None` throughout: the SDK's messages include the provider's response body.
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        raise LLMTemporaryError(type(exc).__name__) from None
    if isinstance(exc, openai.APIStatusError):
        status, code = exc.status_code, exc.code
        if status == 429:
            if code == _QUOTA_CODE:
                raise LLMQuotaError(f"429: {code}") from None
            raise LLMTemporaryError(f"429: {code or 'rate limited'}") from None
        if status >= 500:
            raise LLMTemporaryError(f"{status}: {code or 'server error'}") from None
        if status in (401, 403):
            raise LLMAuthenticationError(f"{status}: {code or 'unauthorized'}") from None
        raise LLMRequestError(status, code) from None
    # Anything else from the SDK (for example an unparseable response) may be a blip.
    raise LLMTemporaryError(type(exc).__name__) from None


def _log_call(
    purpose: str,
    model: str,
    schema_name: str | None,
    started: float,
    responses: list[Response],
    *,
    attempts: int,
    repaired: bool,
) -> None:
    fields: dict[str, Any] = {
        "purpose": purpose,
        "model": model,
        # Requests made, including one that failed; usage covers the ones that answered.
        "calls": attempts,
        "repaired": repaired,
        "duration_ms": round((time.monotonic() - started) * 1000),
        # Not `input_tokens`: any key containing "token" is redacted.
        "usage": {
            "input": sum(r.usage.input_tokens for r in responses if r.usage),
            "output": sum(r.usage.output_tokens for r in responses if r.usage),
        },
    }
    if schema_name is not None:
        fields["schema"] = schema_name
    log.info("llm_call", **fields)
