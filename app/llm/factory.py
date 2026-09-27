"""Builds the configured `LLMClient`."""

from app.config import Settings
from app.llm.client import LLMClient, UnconfiguredLLMClient
from app.llm.openai_client import OpenAIClient
from app.logging import get_logger


def build_llm_client(settings: Settings) -> LLMClient:
    # A blank `OPENAI_API_KEY=` line counts as unset. Passed on, it would make the
    # SDK raise while the app starts.
    key = settings.openai_api_key
    if key is None or not key.get_secret_value().strip():
        get_logger(__name__).warning("llm_not_configured")
        return UnconfiguredLLMClient()
    return OpenAIClient(
        key,
        settings.openai_model,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )
