"""Builds the configured `LLMClient`."""

from app.config import Settings
from app.llm.client import LLMClient, UnconfiguredLLMClient
from app.llm.openai_client import OpenAIClient
from app.logging import get_logger


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.openai_api_key is None:
        get_logger(__name__).warning("llm_not_configured")
        return UnconfiguredLLMClient()
    return OpenAIClient(
        settings.openai_api_key,
        settings.openai_model,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )
