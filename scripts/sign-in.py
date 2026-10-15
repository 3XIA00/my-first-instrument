"""Click through the real sign-in page, end to end, and see who you become.

    uv run scripts/sign-in.py [base-url]

Registers a client, opens your browser at the authorize endpoint, catches the
redirect on localhost:9999, exchanges the code, and calls `user_status` with
the token it got back. You type your password into your own browser; nothing
here ever sees it.

This is the one check `test-oauth-flow.py` cannot make. That script drives the
guest branch, which proves the handshake; this one proves the branch where a
password is actually checked and a private shelf is actually handed out. Run
it once after any change to auth.py or identity.py.

Pressing "Continue as guest" on the form works here too, and is a quick way to
confirm the plumbing without an account.

Pass --no-browser to have the URL printed instead of opened.
"""
import asyncio
import base64
import hashlib
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

_args = [a for a in sys.argv[1:] if not a.startswith("-")]
BASE = (_args[0] if _args else "http://localhost:8000").rstrip("/")
# --no-browser prints the URL instead of opening one: for WSL, a remote
# shell, or anywhere the default browser is not where you want to sign in.
OPEN_BROWSER = "--no-browser" not in sys.argv
PORT = 9999
REDIRECT = f"http://localhost:{PORT}/callback"

caught: dict = {}


class Catch(BaseHTTPRequestHandler):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        caught.update({k: v[0] for k, v in query.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        done = "code" in caught
        self.wfile.write(
            b"<!doctype html><meta charset='utf-8'>"
            b"<body style='font-family:Georgia,serif;padding:3em'>"
            + (b"<h2>Done.</h2><p>Go back to your terminal.</p>" if done
               else b"<h2>No code came back.</h2><p>Check the terminal.</p>")
            + b"</body>")

    def log_message(self, *_):
        pass                                      # the terminal is for us


def main():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    with httpx.Client(timeout=30, follow_redirects=False) as c:
        r = c.post(f"{BASE}/register",
                   json={"redirect_uris": [REDIRECT], "client_name": "sign-in.py"})
        r.raise_for_status()
        client_id = r.json()["client_id"]

        url = f"{BASE}/authorize?" + urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": REDIRECT,
            "state": secrets.token_urlsafe(8), "code_challenge": challenge,
            "code_challenge_method": "S256", "response_type": "code"})

        server = HTTPServer(("localhost", PORT), Catch)
        threading.Thread(target=server.handle_request, daemon=True).start()

        print(("opening your browser:" if OPEN_BROWSER else "open this URL:")
              + f"\n  {url}\n")
        print("sign in, sign up, or press Continue as guest.")
        if OPEN_BROWSER:
            webbrowser.open(url)

        for _ in range(600):                      # ~5 minutes, the code's life
            if caught:
                break
            threading.Event().wait(0.5)
        server.server_close()

        if "code" not in caught:
            sys.exit(f"no code came back; got {caught or 'nothing'}")
        print("got a code, exchanging it ...")

        r = c.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": caught["code"],
            "redirect_uri": REDIRECT, "client_id": client_id,
            "code_verifier": verifier})
        if r.status_code != 200:
            sys.exit(f"token exchange failed: HTTP {r.status_code} {r.text[:300]}")
        token = r.json()["access_token"]

    async def ask():
        async with streamablehttp_client(
                f"{BASE}/mcp", headers={"Authorization": f"Bearer {token}"}
        ) as (rd, wr, _):
            async with ClientSession(rd, wr) as s:
                await s.initialize()
                out = await s.call_tool("user_status", {})
                return "\n".join(x.text for x in out.content if hasattr(x, "text"))

    status = asyncio.run(ask())
    print("\n" + "-" * 60)
    print(status)
    print("-" * 60)
    if status.startswith("user"):
        print("\nThat is the password branch working: a private shelf, keyed to you.")
    else:
        print("\nThat is the guest commons. Run it again and sign in to get a shelf.")


if __name__ == "__main__":
    main()
