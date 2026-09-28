"""Building the production webhook URL from APP_BASE_URL."""

import pytest

from app.telegram.webhook_setup import WEBHOOK_PATH, webhook_url


@pytest.mark.parametrize(
    "base",
    [
        "https://swordbot.up.railway.app",
        "https://swordbot.up.railway.app/",
        " https://swordbot.up.railway.app ",
    ],
)
def test_webhook_url_joins_the_path(base: str) -> None:
    assert webhook_url(base) == "https://swordbot.up.railway.app" + WEBHOOK_PATH


@pytest.mark.parametrize(
    "base",
    ["http://swordbot.example", "swordbot.example", "https://", "https://x.example/?a=1"],
)
def test_webhook_url_requires_a_plain_https_base(base: str) -> None:
    with pytest.raises(ValueError):
        webhook_url(base)


def test_the_route_serves_the_registered_path() -> None:
    from app.api.telegram import router

    assert WEBHOOK_PATH in {getattr(r, "path", None) for r in router.routes}
