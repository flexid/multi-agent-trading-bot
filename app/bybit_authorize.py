"""Connect the bot to a Bybit AI subaccount through Bybit's OAuth flow (PKCE).

    python -m app.bybit_authorize              # print the link, wait, list AI subaccounts
    python -m app.bybit_authorize --use ID     # fetch that AI subaccount's key into .env
    python -m app.bybit_authorize --create     # create a new AI subaccount and fetch its key

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
PORT_RANGE = range(9876, 9887)
CALLBACK_TIMEOUT_S = 600


def _mask(key: str) -> str:
    return f"{key[:5]}...{key[-4:]}" if len(key) > 9 else "***"


def _unwrap(response: httpx.Response) -> Any:
    body = response.json()
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
    if time.time() - token["created_at"] >= token.get("expires_in", 86400):
        token = _refresh(token)
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
    minutes = CALLBACK_TIMEOUT_S // 60
    print(f"Open this link, log in to Bybit and click Authorize (valid {minutes} min):\n")
    print(f"{AUTHORIZE_URL}?{urlencode(params)}\n", flush=True)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not _Callback.done.wait(CALLBACK_TIMEOUT_S):
            raise SystemExit("timed out waiting for the authorization callback")
    finally:
        server.shutdown()
    if "error" in _Callback.result:
        raise SystemExit(f"authorization failed: {_Callback.result['error']}")

    response = httpx.post(
        f"{API_BASE}/oauth/v1/public/access_token",
        data={"client_id": CLIENT_ID, "code": _Callback.result["code"], "code_verifier": verifier},
        timeout=20,
    )
    token = _unwrap(response)
    if "access_token" not in token:
        raise SystemExit("token exchange returned no access token")
    token.setdefault("expires_in", 86400)
    token.setdefault("refresh_token_expires_in", 2592000)
    token["created_at"] = int(time.time())
    _save_token(token)
    print("Authorized. Token stored.\n")
    list_accounts(token)


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


def _write_env(key: str, secret: str) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    values = {
        "BYBIT_API_KEY": key,
        "BYBIT_API_SECRET": secret,
        "BYBIT_BASE_URL": TRADING_BASE_URL,
    }
    for name, value in values.items():
        line = f"{name}={value}"
        text, n = re.subn(rf"^{name}=.*$", line, text, flags=re.M)
        if n == 0:
            text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.touch(mode=0o600, exist_ok=True)
    ENV_PATH.write_text(text)


def fetch_credentials(*, sub_member_id: str | None, create: bool) -> None:
    token = _load_token()
    params = {"is_create": "true"} if create else {"sub_member_id": sub_member_id or ""}
    accounts = _ai_accounts(token, **params)
    account = next((a for a in accounts if a.get("api_key")), None)
    if account is None:
        raise SystemExit("response carried no API key; check the account id or try again")
    _write_env(str(account["api_key"]), str(account["api_secret"]))
    print(
        f"AI subaccount {account.get('sub_member_id')} {account.get('nickname') or ''}\n"
        f"API key {_mask(str(account['api_key']))} written to {ENV_PATH.name}, "
        f"base URL {TRADING_BASE_URL}\n"
        "Next: make selftest"
    )


def store_pasted_token(path: Path) -> None:
    """Cloud path: Bybit showed the access token on its success page instead of redirecting."""
    raw = path.read_text().strip()
    token: dict[str, Any]
    try:
        parsed = json.loads(raw)
        token = parsed.get("result", parsed) if isinstance(parsed, dict) else {}
    except ValueError:
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
        "--token-file", type=Path, help="file holding the access token shown by Bybit's page"
    )
    args = parser.parse_args(argv)
    if args.token_file:
        store_pasted_token(args.token_file)
    elif args.list:
        list_accounts(_load_token())
    elif args.use or args.create:
        fetch_credentials(sub_member_id=args.use, create=args.create)
    else:
        authorize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
