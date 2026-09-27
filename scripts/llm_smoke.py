"""Check the LLM setup against the real OpenAI API.

    uv run python scripts/llm_smoke.py
    uv run python scripts/llm_smoke.py --text "Amazon sent me the wrong charger"

Extracts an `ExtractedIssue` from a complaint and prints it. The `llm_call` log
line shows the model, token usage and whether a repair retry was needed. Uses
OPENAI_API_KEY and OPENAI_MODEL. Each run costs one or two small API calls.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.llm.client import Message
from app.llm.errors import LLMError
from app.llm.factory import build_llm_client
from app.llm.schemas import ExtractedIssue
from app.logging import configure_logging

DEFAULT_COMPLAINT = (
    "My DoorDash order from last night was missing the large fries and a Coke. "
    "I'd like a refund for the missing items."
)

SYSTEM_PROMPT = (
    "You extract details of a customer-support problem from the user's message. "
    "Only report what the user actually said. Leave a field null if it was not stated."
)


async def _main(text: str) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    if settings.openai_api_key is None:
        print("error: set OPENAI_API_KEY in .env", file=sys.stderr)
        return 2

    client = build_llm_client(settings)
    try:
        issue = await client.extract_structured(
            [
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=text),
            ],
            ExtractedIssue,
            purpose="smoke_test",
        )
    except LLMError as exc:
        # Typed errors carry only a status and error code, never the key or prompt.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
    print(issue.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the LLM setup against the real API.")
    parser.add_argument("--text", default=DEFAULT_COMPLAINT, help="the complaint to extract")
    sys.exit(asyncio.run(_main(parser.parse_args().text)))
