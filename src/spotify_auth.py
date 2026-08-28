"""Spotify OAuth 2.0 PKCE flow with local token cache.

No client secret needed. Tokens cached to .tokens.json (gitignored)
and refreshed automatically when expired.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
REDIRECT_URI = "http://127.0.0.1:8888/callback"
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".tokens.json"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

SCOPES = " ".join([
    "user-library-read",
    "user-top-read",
    "user-read-recently-played",
    "playlist-modify-private",
    "playlist-read-private",
])


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None
    expected_state = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        if params.get("state", [None])[0] != _CallbackHandler.expected_state:
            _CallbackHandler.error = "state mismatch"
        elif "code" in params:
            _CallbackHandler.code = params["code"][0]
        else:
            _CallbackHandler.error = params.get("error", ["unknown"])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Authorized. You can close this tab." if _CallbackHandler.code else f"Error: {_CallbackHandler.error}"
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *args):
        pass  # silence request logging


def _save_tokens(tok: dict):
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    TOKEN_FILE.write_text(json.dumps(tok))


def _load_tokens() -> dict | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return None


def _refresh(tok: dict) -> dict | None:
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CLIENT_ID,
    })
    if resp.status_code != 200:
        return None
    new = resp.json()
    # Spotify may omit refresh_token on refresh; keep the old one
    new.setdefault("refresh_token", tok["refresh_token"])
    _save_tokens(new)
    return new


def _interactive_auth() -> dict:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    _CallbackHandler.expected_state = state

    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })
    url = f"{AUTH_URL}?{params}"

    server = http.server.HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Opening browser for Spotify authorization...")
    print(f"If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)

    deadline = time.time() + 300
    while _CallbackHandler.code is None and _CallbackHandler.error is None:
        if time.time() > deadline:
            server.shutdown()
            raise TimeoutError("Authorization timed out after 5 minutes")
        time.sleep(0.2)
    server.shutdown()

    if _CallbackHandler.error:
        raise RuntimeError(f"Authorization failed: {_CallbackHandler.error}")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": _CallbackHandler.code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    })
    resp.raise_for_status()
    tok = resp.json()
    _save_tokens(tok)
    return tok


def get_access_token() -> str:
    """Return a valid access token, refreshing or re-authing as needed."""
    if not CLIENT_ID:
        raise SystemExit(
            "SPOTIFY_CLIENT_ID not set. Copy .env.example to .env and fill it in."
        )
    tok = _load_tokens()
    if tok and time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if tok and "refresh_token" in tok:
        refreshed = _refresh(tok)
        if refreshed:
            return refreshed["access_token"]
    return _interactive_auth()["access_token"]
