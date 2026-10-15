"""Walk the whole OAuth dance, end to end, with no password anywhere.

    uv run scripts/test-oauth-flow.py [base-url]

The guest path is a complete authorization_code flow -- discovery, dynamic
registration, authorize, PKCE, token exchange -- that happens to end in the
declared-guest credential instead of a JWT. That makes it the one route a
test can drive by itself, so every piece of the handshake except the password
form is checked here rather than taken on trust.

The checks that matter most are the ones that should FAIL:
  - an unregistered redirect_uri must not produce a redirect
  - a wrong PKCE verifier must not produce a token
  - a code must not work twice
Those are the failure modes that turn an authorization server into an
open door, and none of them show up on the happy path.
"""
import base64
import hashlib
import re
import secrets
import sys
import urllib.parse

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
REDIRECT = "http://localhost:9999/callback"
EVIL = "http://attacker.example/steal"

fails = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n       {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def pkce():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


c = httpx.Client(timeout=30, follow_redirects=False)

# ---- discovery -----------------------------------------------------------
r = c.get(f"{BASE}/.well-known/oauth-protected-resource")
meta = r.json() if r.status_code == 200 else {}
check("protected-resource metadata", r.status_code == 200
      and meta.get("resource", "").endswith("/mcp")
      and isinstance(meta.get("authorization_servers"), list), str(meta))

r = c.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
check("path-suffixed variant answers too", r.status_code == 200)

r = c.get(f"{BASE}/.well-known/oauth-authorization-server")
asm = r.json() if r.status_code == 200 else {}
check("authorization-server metadata",
      r.status_code == 200
      and asm.get("code_challenge_methods_supported") == ["S256"]
      and "authorization_code" in (asm.get("grant_types_supported") or []),
      str(asm))

# ---- dynamic client registration ----------------------------------------
r = c.post(f"{BASE}/register", json={"redirect_uris": [REDIRECT],
                                     "client_name": "flow-test"})
reg = r.json() if r.status_code in (200, 201) else {}
client_id = reg.get("client_id", "")
check("dynamic registration returns a client_id", bool(client_id), str(reg)[:120])

r = c.post(f"{BASE}/register", json={})
check("registration without redirect_uris is refused", r.status_code == 400,
      f"HTTP {r.status_code}")

# ---- authorize: the form, and the redirect_uri guard --------------------
verifier, challenge = pkce()
params = {"client_id": client_id, "redirect_uri": REDIRECT,
          "state": "xyz-state", "code_challenge": challenge,
          "code_challenge_method": "S256", "response_type": "code"}

r = c.get(f"{BASE}/authorize", params=params)
check("authorize renders a sign-in form",
      r.status_code == 200 and "<form" in r.text and "guest" in r.text.lower(),
      f"HTTP {r.status_code}")

r = c.get(f"{BASE}/authorize", params={**params, "redirect_uri": EVIL})
check("unregistered redirect_uri is refused, with no redirect",
      r.status_code >= 400 and "location" not in r.headers,
      f"HTTP {r.status_code}, Location={r.headers.get('location')!r}")

r = c.get(f"{BASE}/authorize", params={**params, "client_id": "made-up"})
check("unknown client_id is refused", r.status_code >= 400, f"HTTP {r.status_code}")

# ---- authorize: take the guest branch ------------------------------------
r = c.post(f"{BASE}/authorize", data={**params, "mode": "guest"})
location = r.headers.get("location", "")
query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
code = (query.get("code") or [""])[0]
check("guest sign-in redirects back with a code",
      r.status_code == 302 and location.startswith(REDIRECT) and bool(code),
      f"HTTP {r.status_code} -> {location[:80]}")
check("state is handed back unchanged", (query.get("state") or [""])[0] == "xyz-state")

r = c.post(f"{BASE}/authorize", data={**params, "redirect_uri": EVIL, "mode": "guest"})
check("guest branch re-checks redirect_uri on POST",
      r.status_code >= 400 and "location" not in r.headers, f"HTTP {r.status_code}")

# ---- token: PKCE must actually be checked --------------------------------
bad = {"grant_type": "authorization_code", "code": code,
       "redirect_uri": REDIRECT, "client_id": client_id,
       "code_verifier": secrets.token_urlsafe(64)}
r = c.post(f"{BASE}/token", data=bad)
check("wrong PKCE verifier is rejected", r.status_code == 400,
      f"HTTP {r.status_code} {r.text[:90]}")

# that attempt spent the code, so get a fresh one
r = c.post(f"{BASE}/authorize", data={**params, "mode": "guest"})
code = urllib.parse.parse_qs(
    urllib.parse.urlsplit(r.headers.get("location", "")).query).get("code", [""])[0]

good = {"grant_type": "authorization_code", "code": code,
        "redirect_uri": REDIRECT, "client_id": client_id,
        "code_verifier": verifier}
r = c.post(f"{BASE}/token", data=good)
tok = r.json() if r.status_code == 200 else {}
access = tok.get("access_token", "")
check("correct verifier exchanges the code for a token",
      r.status_code == 200 and bool(access), f"HTTP {r.status_code} {r.text[:90]}")

r = c.post(f"{BASE}/token", data=good)
check("the same code cannot be redeemed twice", r.status_code == 400,
      f"HTTP {r.status_code}")

r = c.post(f"{BASE}/token", data={"grant_type": "refresh_token",
                                  "refresh_token": tok.get("refresh_token", "")})
check("refresh works for the guest credential",
      r.status_code == 200 and r.json().get("access_token") == access,
      f"HTTP {r.status_code}")

r = c.post(f"{BASE}/token", data={"grant_type": "password"})
check("an unsupported grant is refused", r.status_code == 400)

c.close()

# ---- the token actually means something to the MCP server ----------------
import asyncio  # noqa: E402

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402


async def user_status(token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(f"{BASE}/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool("user_status", {})
            return out.isError, "\n".join(
                x.text for x in out.content if hasattr(x, "text"))


err, text = asyncio.run(user_status(access))
check("the issued token is accepted and maps to guest",
      not err and text.startswith("guest"), text[:100])

err, text = asyncio.run(user_status("eyJhbGciOiJFZERTQSJ9.forged.sig"))
check("a forged token is still an error, not a guest", err, text[:100])

print("-" * 64)
print(f"{len(fails)} failed" if fails else "ALL PASSED")
if fails:
    print("  " + "\n  ".join(fails))
sys.exit(1 if fails else 0)
