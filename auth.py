"""auth.py — this server, acting as its own OAuth authorization server.

**Why this exists.** `identity.py` can verify a token, but nothing could give
a caller one. A claude.ai connector signs in by OAuth and nothing else: it
discovers an authorization server, registers itself, opens a browser, and
comes back with a bearer token. Without that dance the hosted Claude can only
ever be a guest, and the private shelves of ADR-0006 are a capability no
surface can reach.

**The shape, and the road not taken.** The obvious design is to point the
connector straight at Better Auth and let Neon be the authorization server.
That is not available: Neon's Managed Better Auth is an app sign-in service,
not an OAuth provider for third parties — it has no `/authorize`, issues no
authorization codes, and knows nothing about PKCE. So this server becomes the
authorization server, and Better Auth stays what it is: the thing that checks
a password. The connector never learns Better Auth exists; `identity.py` never
learns OAuth exists. Each side keeps its own vocabulary.

**The honest cost of that shape.** The sign-in form is served by us, so a
password crosses this process on its way to Neon. A redirect to a login page
we did not write would avoid that, and is what a production system should do
the moment the provider offers one. Until then the mitigation is narrow:
nothing here stores, logs or echoes the password, and the form posts to
exactly one place.

**Prototype-grade, deliberately.** Codes and client registrations live in
memory, which is correct for one Render instance and wrong for two. A restart
drops them: in-flight codes die (they were valid for five minutes anyway) and
registered clients must register again, which well-behaved clients do on the
first `invalid_client`. Moving these to Postgres is a table and an afternoon
— see ADR-0007 for why that afternoon has not been spent.
"""
import base64
import hashlib
import os
import secrets
import time
import urllib.parse
from html import escape

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from identity import AUTH_BASE, GUEST_TOKEN

# Better Auth quirks, probe-verified by the course's reference implementation
# (your-protected-notes/auth.py, 2026-10-08): the Origin header is REQUIRED or
# the call is rejected, the session arrives as a cookie rather than in the
# body, and GET /token trades that cookie for a short-lived EdDSA JWT.
SESSION_COOKIE = "__Secure-neon-auth.session_token"

CODE_TTL = 300     # an authorization code is in flight, not at rest
JWT_TTL = 900      # Better Auth's own lifetime, restated for the client

# Deliberately in-memory; see the module docstring.
_codes: dict = {}      # code -> {tokens, code_challenge, redirect_uri, client_id, exp}
_clients: dict = {}    # client_id -> {"redirect_uris": [...]}

_CARRY = ("client_id", "redirect_uri", "state", "code_challenge",
          "code_challenge_method", "response_type", "scope", "resource")


# ─── the public identity of this server ──────────────────────────────────

def issuer(request: Request) -> str:
    """The public base URL of THIS server.

    Every URL in the discovery documents is built from this, and a client that
    gets redirected somewhere the metadata did not name will refuse the token.
    Render sets RENDER_EXTERNAL_URL itself, so the normal deploy needs no
    configuration; PUBLIC_URL overrides it for a cloudflared tunnel; and the
    fall back to the request's own origin keeps localhost working with nothing
    in .env at all.
    """
    for name in ("PUBLIC_URL", "RENDER_EXTERNAL_URL"):
        value = (os.environ.get(name) or "").strip().rstrip("/")
        if value:
            return value
    return str(request.base_url).rstrip("/")


# ─── talking to Better Auth ──────────────────────────────────────────────

def _origin() -> str:
    parts = urllib.parse.urlsplit(AUTH_BASE)
    return f"{parts.scheme}://{parts.netloc}"


def _session_from(response) -> str:
    """Pull the session cookie out, whichever way it arrived."""
    got = response.cookies.get(SESSION_COOKIE)
    if got:
        return got
    for raw in response.headers.get_list("set-cookie"):
        head = raw.split(";")[0]
        if head.startswith(SESSION_COOKIE + "="):
            return head.split("=", 1)[1]
    return ""


def _jwt_from_session(client: httpx.Client, session: str) -> str:
    r = client.get(AUTH_BASE + "/token",
                   headers={"Origin": _origin(),
                            "Cookie": f"{SESSION_COOKIE}={session}"})
    r.raise_for_status()
    return (r.json() or {}).get("token", "")


def login(email: str, password: str, signup: bool):
    """(jwt, session). Raises httpx.HTTPStatusError when Better Auth refuses."""
    payload = {"email": email, "password": password}
    if signup:
        payload["name"] = email.split("@")[0]
    path = "/sign-up/email" if signup else "/sign-in/email"
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        r = client.post(AUTH_BASE + path, json=payload,
                        headers={"Origin": _origin()})
        r.raise_for_status()
        session = _session_from(r)
        if not session:
            raise RuntimeError("signed in, but no session cookie came back")
        return _jwt_from_session(client, session), session


# ─── the sign-in page ────────────────────────────────────────────────────

_CSS = """
body{font-family:Georgia,serif;background:#faf8f3;display:flex;
     justify-content:center;padding-top:7vh;margin:0}
.card{background:#fff;border:1px solid #d8d2c4;border-radius:12px;
      padding:26px 30px;width:330px;box-shadow:0 1px 3px #0000000d}
h1{font-family:system-ui,sans-serif;font-size:1.05em;margin:0 0 2px;color:#2d3b2d}
p{font-size:.84em;color:#666;margin:6px 0 16px;line-height:1.45}
input,button{width:100%;box-sizing:border-box;padding:9px;margin:4px 0;
             border-radius:7px;font-size:.93em}
input{border:1px solid #cfc8b8;background:#fffefb}
button{border:none;background:#2d3b2d;color:#fff;font-weight:600;cursor:pointer}
.alt{background:#fff;color:#2d3b2d;border:1px solid #2d3b2d}
.guest{background:#fff;color:#777;border:1px dashed #aaa;font-weight:400}
.err{color:#a4342a;font-size:.84em;display:block;margin-top:6px}
"""


def _form(iss: str, params: dict, err: str = "") -> HTMLResponse:
    hidden = "\n".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(v or "")}">'
        for k, v in params.items())
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - your-first-instrument</title><style>{_CSS}</style></head><body>
<div class="card">
<h1>your-first-instrument</h1>
<p>Sign in and your saved facts are yours alone. Continue as a guest and they
go to a commons every signed-out caller can read and write.
{f'<span class="err">{escape(err)}</span>' if err else ''}</p>
<form method="post" action="{escape(iss)}/authorize">
{hidden}
<input name="email" type="email" placeholder="email" autocomplete="username" required>
<input name="password" type="password" placeholder="password"
       autocomplete="current-password" required>
<button name="mode" value="signin" type="submit">Sign in</button>
<button name="mode" value="signup" type="submit" class="alt">New here - sign up</button>
<button name="mode" value="guest" type="submit" class="guest"
        formnovalidate>Continue as guest</button>
</form></div></body></html>"""
    return HTMLResponse(body)


def _oops(message: str, status: int = 400) -> HTMLResponse:
    """An error the human reads, shown INSTEAD of redirecting.

    Anything wrong with `client_id` or `redirect_uri` is reported here and
    never by bouncing the browser onward, because the one thing we must not do
    with an unvalidated redirect target is send someone to it.
    """
    return HTMLResponse(
        f'<!doctype html><meta charset="utf-8"><style>{_CSS}</style>'
        f'<div class="card"><h1>your-first-instrument</h1>'
        f'<p>{escape(message)}</p></div>', status_code=status)


# ─── PKCE and redirect validation ────────────────────────────────────────

def _pkce_ok(verifier: str, challenge: str) -> bool:
    """S256 only. We advertise exactly that, so a code issued without a
    challenge has nothing to verify against and is refused rather than waved
    through — 'no challenge' must not be the easy way past the check."""
    if not challenge or not verifier:
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


def _redirect_registered(client_id: str, redirect_uri: str) -> bool:
    """Is this exactly one of the URIs the client registered?

    The reference implementation this was built from omits this check, and the
    omission is the difference between "a client gets its own codes" and
    "anyone holding a public client_id can have the codes mailed to them."
    Exact string match, because prefix matching is how this goes wrong.
    """
    client = _clients.get(client_id)
    return bool(client) and redirect_uri in client["redirect_uris"]


def _issue_code(params: dict, tokens: dict) -> RedirectResponse:
    code = secrets.token_urlsafe(24)
    _codes[code] = {"tokens": tokens,
                    "code_challenge": params.get("code_challenge", ""),
                    "redirect_uri": params.get("redirect_uri", ""),
                    "client_id": params.get("client_id", ""),
                    "exp": time.time() + CODE_TTL}
    target = params["redirect_uri"]
    joiner = "&" if "?" in target else "?"
    query = urllib.parse.urlencode(
        {"code": code, "state": params.get("state", "")})
    return RedirectResponse(f"{target}{joiner}{query}", status_code=302)


# ─── routes ──────────────────────────────────────────────────────────────

def register_oauth_routes(mcp):
    """Mount the authorization-server endpoints onto the MCP app."""

    @mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
    async def as_metadata(request: Request):
        iss = issuer(request)
        return JSONResponse({
            "issuer": iss,
            "authorization_endpoint": f"{iss}/authorize",
            "token_endpoint": f"{iss}/token",
            "registration_endpoint": f"{iss}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        })

    @mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
    async def pr_metadata(request: Request):
        iss = issuer(request)
        return JSONResponse({"resource": f"{iss}/mcp",
                             "authorization_servers": [iss]})

    # RFC 9728 says the resource's path is appended to the well-known name.
    # Clients disagree about whether to do that, so answer both spellings.
    @mcp.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET"])
    async def pr_metadata_suffixed(request: Request):
        return await pr_metadata(request)

    @mcp.custom_route("/register", methods=["POST"])
    async def register(request: Request):
        """Dynamic client registration (RFC 7591), open by design.

        An open registration endpoint sounds alarming and is the normal posture
        for public clients: registering grants nothing on its own, since every
        code still needs PKCE and a pre-registered redirect URI. What it does
        buy an attacker is memory, so the dict is capped rather than trusted.
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        uris = [u for u in (body.get("redirect_uris") or []) if isinstance(u, str)]
        if not uris:
            return JSONResponse(
                {"error": "invalid_redirect_uri",
                 "error_description": "redirect_uris is required"}, status_code=400)
        if len(_clients) > 5000:
            _clients.clear()
        client_id = secrets.token_urlsafe(16)
        _clients[client_id] = {"redirect_uris": uris}
        return JSONResponse({
            "client_id": client_id,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }, status_code=201)

    @mcp.custom_route("/authorize", methods=["GET"])
    async def authorize_get(request: Request):
        params = {k: request.query_params.get(k, "") for k in _CARRY}
        if not params["redirect_uri"]:
            return _oops("That sign-in link is missing its redirect_uri.")
        if not _redirect_registered(params["client_id"], params["redirect_uri"]):
            return _oops(
                "This client is not registered with that redirect URI. If the "
                "server restarted, registrations were lost - disconnect and "
                "reconnect the connector and it will register again.")
        return _form(issuer(request), params)

    @mcp.custom_route("/authorize", methods=["POST"])
    async def authorize_post(request: Request):
        form = dict(await request.form())
        params = {k: str(form.get(k, "")) for k in _CARRY}
        iss = issuer(request)

        # Re-check on POST: the hidden fields came back from a browser and are
        # therefore input, not state we kept.
        if not _redirect_registered(params["client_id"], params["redirect_uri"]):
            return _oops("This client is not registered with that redirect URI.")

        if form.get("mode") == "guest":
            return _issue_code(params, {"access_token": GUEST_TOKEN,
                                        "refresh_token": GUEST_TOKEN})

        email = str(form.get("email", "")).strip()
        password = str(form.get("password", ""))
        signup = form.get("mode") == "signup"
        if not email or not password:
            return _form(iss, params, "Email and password are both required.")

        try:
            jwt_token, session = login(email, password, signup)
        except httpx.HTTPStatusError as exc:
            # Tell the human what actually happened. The reference learned this
            # the hard way in class: a generic "sign-in failed" turned an
            # account that already existed into what read as a rejection, and
            # the student retried the wrong button.
            status = exc.response.status_code
            try:
                code = (exc.response.json() or {}).get("code", "")
            except Exception:
                code = ""
            if signup and (status == 409 or "EXISTS" in code.upper()):
                message = ("Good news: that email already has an account. "
                           "Press Sign in instead.")
            elif signup:
                message = ("Sign-up failed. The password may be too short "
                           "(8+ characters), or the account already exists.")
            else:
                message = ("Sign-in failed: wrong password, or no account yet "
                           "- in which case use Sign up.")
            return _form(iss, params, message)
        except Exception:
            return _form(iss, params,
                         "Could not reach the authentication service. Try again.")

        if not jwt_token:
            return _form(iss, params,
                         "Signed in, but no token came back. Try again.")
        return _issue_code(params, {"access_token": jwt_token,
                                    "refresh_token": "ba:" + session})

    @mcp.custom_route("/token", methods=["POST"])
    async def token(request: Request):
        form = dict(await request.form())
        grant = form.get("grant_type")

        if grant == "authorization_code":
            # pop: a code is single use, and it is spent even if what follows
            # fails. A code that survives its own failed redemption is a code
            # an attacker gets to retry.
            record = _codes.pop(str(form.get("code", "")), None)
            if not record or record["exp"] < time.time():
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if str(form.get("client_id", "")) != record["client_id"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if str(form.get("redirect_uri", "")) != record["redirect_uri"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            if not _pkce_ok(str(form.get("code_verifier", "")),
                            record["code_challenge"]):
                return JSONResponse({"error": "invalid_grant",
                                     "error_description": "PKCE check failed"},
                                    status_code=400)
            issued = record["tokens"]
            guest = issued["access_token"] == GUEST_TOKEN
            return JSONResponse({
                "access_token": issued["access_token"],
                "token_type": "bearer",
                "expires_in": 31536000 if guest else JWT_TTL,
                "refresh_token": issued.get("refresh_token", ""),
            })

        if grant == "refresh_token":
            given = str(form.get("refresh_token", ""))
            if given == GUEST_TOKEN:
                return JSONResponse({"access_token": GUEST_TOKEN,
                                     "token_type": "bearer",
                                     "expires_in": 31536000,
                                     "refresh_token": GUEST_TOKEN})
            if given.startswith("ba:"):
                try:
                    with httpx.Client(timeout=30) as client:
                        fresh = _jwt_from_session(client, given[3:])
                except Exception:
                    fresh = ""
                if not fresh:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                return JSONResponse({"access_token": fresh,
                                     "token_type": "bearer",
                                     "expires_in": JWT_TTL,
                                     "refresh_token": given})
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
