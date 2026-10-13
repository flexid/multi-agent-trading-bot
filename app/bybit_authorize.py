"""Connect the bot to a Bybit AI subaccount through Bybit's OAuth flow (PKCE).

    python -m app.bybit_authorize              # print the link, wait, list AI subaccounts
    python -m app.bybit_authorize --use ID     # fetch that AI subaccount's key into .env
    python -m app.bybit_authorize --create     # create a new AI subaccount and fetch its key
    python -m app.bybit_authorize --create --for kwikton   # one of the arena's three traders
    python -m app.bybit_authorize --code CODE  # Bybit showed a code instead of redirecting

``--for`` writes the key under the trader's own names (COMPETITION_KWIKTON_API_KEY and
_SECRET) instead of dorkbot's BYBIT_API_KEY, so each trader keeps its own sub-account.

The flow follows Bybit's published agent docs: authorize on www.bybit.com, exchange the
code at api2.bybit.com, then read the AI subaccount's HMAC API key. Tokens are stored
outside the repo with mode 600. Nothing secret is ever printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from app.config import ENV_PATH

CLIENT_ID = "ai-agent"
AUTHORIZE_URL = "https://www.bybit.com/oauth"
API_BASE = "https://api2.bybit.com"
TRADING_BASE_URL = "https://api.bybit.com"
TOKEN_PATH = Path.home() / ".config" / "multi-agent-trading-bot" / "bybit_oauth.json"
PENDING_PATH = TOKEN_PATH.with_name(
    "bybit_oauth_pending.json"
)  # the PKCE verifier of the open link
PORT_RANGE = range(9876, 9887)
CALLBACK_TIMEOUT_S = 600


def _mask(key: str) -> str:
    return f"{key[:5]}...{key[-4:]}" if len(key) > 9 else "***"


def _unwrap(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        raise SystemExit(
            f"Bybit answered HTTP {response.status_code} without JSON "
            f"({response.text[:80]!r}); the token is probably no longer valid"
        ) from None
    if not isinstance(body, dict):
        return body
    code = body.get("retCode", body.get("ret_code", 0))
    if code not in (0, None):
        msg = body.get("retMsg") or body.get("ret_msg") or ""
        raise SystemExit(f"Bybit error {code}: {msg}")
    return body.get("result", body)


def _load_token() -> dict[str, Any]:
    if not TOKEN_PATH.exists():
        raise SystemExit("no token stored; run without --use/--create first")
    token: dict[str, Any] = json.loads(TOKEN_PATH.read_text())
    age = time.time() - token["created_at"]
    if age >= token.get("expires_in", 86400):
        try:
            token = _refresh(token)
        except SystemExit as exc:
            raise SystemExit(
                f"the stored token is {age / 3600:.0f} hours old and could not be refreshed "
                f"({exc}).\nRun `make bybit-authorize` again, approve in the browser, and wait "
                "for 'Authorized. Token stored.' before --use or --create."
            ) from None
    return token


def _save_token(token: dict[str, Any]) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    TOKEN_PATH.touch(mode=0o600, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token))


def _refresh(token: dict[str, Any]) -> dict[str, Any]:
    response = httpx.post(
        f"{API_BASE}/oauth/v1/public/refresh_token",
        data={"client_id": CLIENT_ID, "refresh_token": token["refresh_token"]},
        timeout=20,
    )
    fresh = _unwrap(response)
    token = {**token, **fresh, "created_at": int(time.time())}
    _save_token(token)
    return token


# --- step 1: authorize -------------------------------------------------------


class _Callback(BaseHTTPRequestHandler):
    state = ""
    result: dict[str, str] = {}
    done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlparse(self.path)
        if url.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if query.get("state") != _Callback.state:
            status, text = 403, "Authorization failed: state mismatch."
        elif "code" in query:
            _Callback.result = {"code": query["code"]}
            status, text = 200, "Authorization successful. You can close this page."
        else:
            _Callback.result = {"error": query.get("error", "authorization_failed")}
            status, text = 400, "Authorization failed."
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())
        if _Callback.result:
            _Callback.done.set()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # the query string carries the code; keep it out of the log


def _bind() -> ThreadingHTTPServer:
    # Threaded: a browser's idle keep-alive connection must not block the real callback.
    for port in PORT_RANGE:
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), _Callback)
        except OSError:
            continue
    raise SystemExit(f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


def authorize() -> None:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    _Callback.state = secrets.token_hex(8)
    server = _bind()
    redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "ai-account",
        "state": _Callback.state,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    # Kept for `--code`: when Bybit shows the code on its page instead of redirecting, the
    # exchange still needs this link's verifier.
    PENDING_PATH.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    PENDING_PATH.touch(mode=0o600, exist_ok=True)
    PENDING_PATH.write_text(json.dumps({"verifier": verifier, "created_at": int(time.time())}))
    minutes = CALLBACK_TIMEOUT_S // 60
    print(f"Open this link, log in to Bybit and click Authorize (valid {minutes} min):\n")
    print(f"{AUTHORIZE_URL}?{urlencode(params)}\n", flush=True)
    print(
        "Keep this terminal open: Bybit sends the browser back here. If Bybit shows a code\n"
        "on its own page instead, stop this with Ctrl-C and run:\n"
        "  make bybit-authorize ARGS='--code THECODE'\n",
        flush=True,
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not _Callback.done.wait(CALLBACK_TIMEOUT_S):
            raise SystemExit("timed out waiting for the authorization callback")
    finally:
        server.shutdown()
    if "error" in _Callback.result:
        raise SystemExit(f"authorization failed: {_Callback.result['error']}")

    _exchange(_Callback.result["code"], verifier)


def _exchange(code: str, verifier: str) -> None:
    response = httpx.post(
        f"{API_BASE}/oauth/v1/public/access_token",
        data={"client_id": CLIENT_ID, "code": code, "code_verifier": verifier},
        timeout=20,
    )
    token = _unwrap(response)
    if "access_token" not in token:
        raise SystemExit("token exchange returned no access token")
    token.setdefault("expires_in", 86400)
    token.setdefault("refresh_token_expires_in", 2592000)
    token["created_at"] = int(time.time())
    _save_token(token)
    PENDING_PATH.unlink(missing_ok=True)
    print("Authorized. Token stored.\n")
    list_accounts(token)


def exchange_pasted_code(code: str) -> None:
    """Bybit showed the authorization code on its page instead of redirecting: exchange it
    with the verifier of the link that was last printed."""
    if not PENDING_PATH.exists():
        raise SystemExit(
            "no open authorization: run `make bybit-authorize` first, approve in the browser, "
            "then pass the code Bybit shows"
        )
    pending = json.loads(PENDING_PATH.read_text())
    if time.time() - pending["created_at"] > CALLBACK_TIMEOUT_S:
        raise SystemExit(
            "the last link is older than ten minutes; run `make bybit-authorize` again"
        )
    _exchange(code.strip(), pending["verifier"])


# --- step 2: pick an AI subaccount -------------------------------------------


def _ai_accounts(token: dict[str, Any], **params: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{API_BASE}/oauth/v1/resource/restrict/ai_accounts",
        params=params,
        headers={"Authorization": f"Bearer {token['access_token']}"},
        timeout=20,
    )
    result = _unwrap(response)
    if isinstance(result, dict):
        # Bybit wraps the list: {"accounts": [...]}; a single account comes back bare.
        result = result.get("accounts", [result])
    rows = result if isinstance(result, list) else [result]
    return [row for row in rows if isinstance(row, dict) and row.get("sub_member_id")]


def list_accounts(token: dict[str, Any]) -> None:
    accounts = _ai_accounts(token)
    if not accounts:
        print("No AI subaccounts yet. Create one with:  make bybit-authorize ARGS=--create")
        return
    print("AI subaccounts:")
    for row in accounts:
        print(f"  {row.get('sub_member_id')}  {row.get('nickname') or ''}")
    print("\nPick one with:  make bybit-authorize ARGS='--use <id>'")
    if len(accounts) < 5:
        print("Or create a new one with:  make bybit-authorize ARGS=--create")


TRADERS = ("kwikton", "kwekton", "kwakton")  # the arena's traders (arena-site/README.md)


def env_names(trader: str | None) -> dict[str, str]:
    """The .env names a key is written under: dorkbot's own, or a trader's."""
    if trader is None:
        return {"key": "BYBIT_API_KEY", "secret": "BYBIT_API_SECRET", "url": "BYBIT_BASE_URL"}
    if trader not in TRADERS:
        raise SystemExit(f"unknown trader {trader!r}; one of {', '.join(TRADERS)}")
    p = f"COMPETITION_{trader.upper()}"
    return {"key": f"{p}_API_KEY", "secret": f"{p}_API_SECRET", "url": "BYBIT_BASE_URL"}


def _write_env(key: str, secret: str, trader: str | None = None) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    names = env_names(trader)
    values = {
        names["key"]: key,
        names["secret"]: secret,
        names["url"]: TRADING_BASE_URL,
    }
    for name, value in values.items():
        line = f"{name}={value}"
        text, n = re.subn(rf"^{name}=.*$", line, text, flags=re.M)
        if n == 0:
            text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.touch(mode=0o600, exist_ok=True)
    ENV_PATH.write_text(text)


def fetch_credentials(
    *, sub_member_id: str | None, create: bool, trader: str | None = None
) -> None:
    token = _load_token()
    names = env_names(trader)  # validates the trader before anything is created
    params = {"is_create": "true"} if create else {"sub_member_id": sub_member_id or ""}
    accounts = _ai_accounts(token, **params)
    account = next((a for a in accounts if a.get("api_key")), None)
    if account is None:
        raise SystemExit("response carried no API key; check the account id or try again")
    _write_env(str(account["api_key"]), str(account["api_secret"]), trader)
    who = f"arena trader {trader}" if trader else "dorkbot"
    print(
        f"AI subaccount {account.get('sub_member_id')} {account.get('nickname') or ''} for {who}\n"
        f"API key {_mask(str(account['api_key']))} written to {ENV_PATH.name} as {names['key']}, "
        f"base URL {TRADING_BASE_URL}\n"
        + (
            "Next: bind the key to the server's IP in Bybit, then the arena self-test"
            if trader
            else "Next: make selftest"
        )
    )


def store_pasted_token(path: Path) -> None:
    """Cloud path: Bybit showed the access token on its success page instead of redirecting."""
    raw = path.read_text().strip()
    token: dict[str, Any]
    try:
        parsed = json.loads(raw)
        token = parsed.get("result", parsed) if isinstance(parsed, dict) else {}
    except ValueError:
        if raw.count(".") != 2:  # an access token is a JWT; a short string is the code
            raise SystemExit(
                "that is an authorization code, not a token: use  "
                "make bybit-authorize ARGS='--code THECODE'"
            ) from None
        token = {"access_token": raw}
    if not token.get("access_token"):
        raise SystemExit("file holds no access token")
    token.setdefault("expires_in", 86400)
    token["created_at"] = int(time.time())
    _save_token(token)
    path.unlink()
    print("Token stored; pasted file removed.\n")
    list_accounts(token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--use", metavar="ID", help="AI subaccount id to fetch the key for")
    group.add_argument("--create", action="store_true", help="create a new AI subaccount")
    group.add_argument("--list", action="store_true", help="list AI subaccounts (stored token)")
    group.add_argument(
        "--code", metavar="CODE", help="the code Bybit showed instead of redirecting"
    )
    group.add_argument(
        "--token-file", type=Path, help="file holding the access token shown by Bybit's page"
    )
    parser.add_argument(
        "--for",
        dest="trader",
        metavar="TRADER",
        help="an arena trader: kwikton, kwekton or kwakton",
    )
    args = parser.parse_args(argv)
    if args.trader and not (args.use or args.create):
        parser.error("--for goes with --use or --create")
    if args.code:
        exchange_pasted_code(args.code)
    elif args.token_file:
        store_pasted_token(args.token_file)
    elif args.list:
        list_accounts(_load_token())
    elif args.use or args.create:
        fetch_credentials(sub_member_id=args.use, create=args.create, trader=args.trader)
    else:
        authorize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
