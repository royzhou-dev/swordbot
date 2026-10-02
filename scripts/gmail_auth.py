"""One-time Gmail OAuth: get the refresh token for GMAIL_REFRESH_TOKEN (PLAN D6).

    uv run python scripts/gmail_auth.py            # consent in the browser, print the token
    uv run python scripts/gmail_auth.py --check    # check the configured token (sends nothing)

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from a "Desktop app" OAuth
client (README, "Gmail setup"). The script opens Google's consent page for the
`gmail.send` and `gmail.readonly` scopes, catches the redirect on the loopback
address in GOOGLE_REDIRECT_URI (default http://127.0.0.1:8080/), exchanges the
code with PKCE, and prints the refresh token. Put it in `.env` or your host's
secrets; never commit it.

`--check` refreshes an access token with the configured credentials and
reports whether it works and which of the two scopes it carries. It sends and
reads nothing.
"""

import argparse
import asyncio
import base64
import hashlib
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings, get_settings
from app.email.errors import GmailError
from app.email.gmail_client import (
    GMAIL_READ_SCOPE,
    GMAIL_SEND_SCOPE,
    GOOGLE_AUTH_URL,
    GOOGLE_TOKEN_URL,
    HttpGmailClient,
    build_gmail_client,
)

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8080/"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
CONSENT_TIMEOUT_SECONDS = 300


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="check the configured refresh token; sends nothing"
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="print the consent URL instead of opening it"
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.check:
        return asyncio.run(_check(settings))
    return _authorize(settings, open_browser=not args.no_browser)


# --- Getting a refresh token -------------------------------------------------------------


def _authorize(settings: Settings, *, open_browser: bool) -> int:
    client_id = (settings.google_client_id or "").strip()
    secret = settings.google_client_secret
    if not client_id or secret is None or not secret.get_secret_value().strip():
        print("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (README, 'Gmail setup').")
        return 2
    redirect_uri = settings.google_redirect_uri or DEFAULT_REDIRECT_URI
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname is None or parts.port is None:
        print(f"GOOGLE_REDIRECT_URI must look like {DEFAULT_REDIRECT_URI} (http, host, port).")
        return 2
    if parts.hostname not in LOOPBACK_HOSTS:
        print("GOOGLE_REDIRECT_URI must be a loopback address, e.g. http://127.0.0.1:8080/.")
        return 2

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    consent_url = (
        GOOGLE_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": f"{GMAIL_SEND_SCOPE} {GMAIL_READ_SCOPE}",
                # Offline access and a fresh consent make Google return a refresh token.
                "access_type": "offline",
                "prompt": "consent",
                "code_challenge": challenge.decode().rstrip("="),
                "code_challenge_method": "S256",
                "state": state,
            }
        )
    )

    print("Open this page, sign in with the Gmail account the bot uses, and allow both")
    print("permissions (sending email, and reading it to find order receipts):")
    print(f"\n  {consent_url}\n")
    if open_browser:
        webbrowser.open(consent_url)
    print(f"Waiting for Google to redirect to {redirect_uri} ...")
    query = _wait_for_redirect(parts.hostname, parts.port, parts.path or "/")
    if query is None:
        print("No answer from the consent page within 5 minutes. Run the script again.")
        return 1
    if query.get("state") != state:
        print("The redirect's state doesn't match this run, so it was ignored. Run again.")
        return 1
    if "error" in query:
        print(f"Google returned an error: {query['error']}")
        return 1
    code = query.get("code")
    if not code:
        print("The redirect had no authorization code. Run the script again.")
        return 1

    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": secret.get_secret_value().strip(),
        },
        timeout=15,
    )
    body = _json(response)
    if not response.is_success:
        print(f"The code exchange failed: HTTP {response.status_code} {body.get('error', '')}")
        return 1
    granted = str(body.get("scope", "")).split()
    if GMAIL_SEND_SCOPE not in granted:
        print("Access to send email wasn't granted. Run again and allow it on the consent page.")
        return 1
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        print(
            "Google returned no refresh token. Remove the app's access at "
            "https://myaccount.google.com/permissions and run again."
        )
        return 1

    if GMAIL_READ_SCOPE not in granted:
        print(
            "Note: access to read email wasn't granted, so the bot can send but can't look up "
            "order receipts. Run again and tick both boxes to change that."
        )
    print("\nDone. Add this line to .env (or your host's secrets). Keep it secret:\n")
    print(f"GMAIL_REFRESH_TOKEN={refresh_token}\n")
    print(
        "Reminder: the OAuth consent screen must be 'In production'. "
        "In 'Testing' this token stops working after 7 days."
    )
    if not settings.gmail_sender_address:
        print("Also set GMAIL_SENDER_ADDRESS to this account's address, for the From header.")
    return 0


def _wait_for_redirect(host: str, port: int, path: str) -> dict[str, str] | None:
    """Serve the loopback address until Google redirects there. Returns the query."""
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path != path:
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in parse_qs(url.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, format: str, *args: Any) -> None:
            # The default logs the request line, which contains the code.
            return None

    deadline = time.monotonic() + CONSENT_TIMEOUT_SECONDS
    with HTTPServer((host, port), Handler) as server:
        # One request per call: Google's redirect, or a stray one such as /favicon.ico.
        while not received:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            server.timeout = remaining
            server.handle_request()
    return received


# --- Checking the configured token -------------------------------------------------------


async def _check(settings: Settings) -> int:
    async with httpx.AsyncClient() as http:
        gmail = build_gmail_client(settings, http)
        if not isinstance(gmail, HttpGmailClient):
            print(
                "Gmail isn't configured: set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
                "GMAIL_REFRESH_TOKEN."
            )
            return 2
        try:
            await gmail.authorize()
            can_read = await gmail.can_read()
        except GmailError as exc:
            print(f"The refresh token doesn't work: {type(exc).__name__}: {exc}")
            print("If it says invalid_grant, run this script again without --check.")
            return 1
    print("OK: the refresh token works and has the gmail.send scope. Nothing was sent.")
    if can_read:
        print("OK: it also has the gmail.readonly scope, so order receipts can be looked up.")
    else:
        print(
            "Note: it lacks the gmail.readonly scope, so receipt lookup is off (sending still "
            "works). Run this script again without --check to get a token with both."
        )
    if not settings.gmail_sender_address:
        print("Note: GMAIL_SENDER_ADDRESS is not set, so Gmail will fill in the From header.")
    return 0


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


if __name__ == "__main__":
    sys.exit(main())
