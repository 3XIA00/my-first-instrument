"""Who is asking — Neon Auth (Better Auth) identity resolution.

Four states now, and the distinctions between them are the whole point:

  absent token   -> (None, None): a GUEST. A mode, not an error.
  GUEST_TOKEN    -> (None, None): a DECLARED guest, who completed the OAuth
                    dance and chose the commons on purpose.
  valid JWT      -> (sub, email): a user, with a private shelf.
  anything else  -> raises. An invalid credential must never silently
                   degrade to guest, because that hides broken auth as
                   working anonymity — and files one person's facts onto
                   a commons everyone can read.

The declared guest exists because OAuth has no way to say "no thanks". A
connector must come back from the authorization dance holding a credential,
so a "continue as guest" button has to hand it something: not a JWT, because
there is no user, and not nothing, because the client would read that as a
failed sign-in. GUEST_TOKEN is that something. It is deliberately not a
secret, because it grants exactly what an unauthenticated caller already has.

The identity is the token's `sub`, never the email: emails change hands
and change spelling, and a key that can be reassigned is not a key.
Email is looked up for display only.

Neon now ships Better Auth rather than Stack Auth, so this diverges from
the course's retrofit guide in three places — algorithm, user table, and
the absent project id. ADR-0006 has the details.
"""
import os

from memory import db

JWKS_URL = (os.environ.get("NEON_AUTH_JWKS_URL") or "").strip()

# Optional. Better Auth has no Stack-style project id to use as an audience,
# so issuer is the equivalent check. Left unset, it is simply not verified —
# the JWKS URL is already project-scoped, so a token signed by someone else's
# project fails at the signature long before any claim is read.
ISSUER = (os.environ.get("NEON_AUTH_ISSUER") or "").strip()

# Better Auth signs with Ed25519. The guide's ES256/RS256 would reject every
# real token with "alg not allowed" — a failure that reads like a bad token.
ALGORITHMS = ["EdDSA"]

# The declared-guest credential (see the four states above). auth.py hands it
# out; this module is the only place that maps it back to guest mode.
GUEST_TOKEN = "guest-no-shelf"

# Better Auth's REST base, derived rather than configured: the JWKS URL minus
# its well-known suffix. One env var cannot drift out of sync with itself.
_SUFFIX = "/.well-known/jwks.json"
AUTH_BASE = JWKS_URL[: -len(_SUFFIX)] if JWKS_URL.endswith(_SUFFIX) else ""

_jwks = None


def _jwk_client():
    """One client, reused. PyJWKClient caches signing keys; building a new one
    per call would fetch the JWKS over the network on every single tool call."""
    global _jwks
    if _jwks is None:
        from jwt import PyJWKClient

        if not JWKS_URL:
            raise RuntimeError(
                "NEON_AUTH_JWKS_URL is not set. Locally: put it in .env. "
                "On Render: Environment -> Add environment variable. "
                "Neon console -> your project -> Settings -> Better Auth."
            )
        _jwks = PyJWKClient(JWKS_URL, cache_keys=True)
    return _jwks


def bearer_token(ctx) -> str | None:
    """The raw token from the Authorization header, or None if there isn't one.

    None means guest. An empty or malformed header is treated as absent here;
    anything that survives this function is a credential CLAIM, and a claim
    that fails to verify raises rather than falling back.
    """
    request = getattr(ctx.request_context, "request", None)  # starlette, under streamable-http
    header = request.headers.get("authorization", "") if request is not None else ""
    if not header.lower().startswith("bearer "):
        return None
    token = header.split(None, 1)[1].strip()
    return token or None


def _email(user_id: str) -> str | None:
    """Display name for a verified user. Never the key.

    `user` is a reserved word, hence the quoting; `id` is a uuid while our
    `facts.user_id` is text, hence the cast. A failure here is cosmetic — the
    identity is already proven — so it degrades to None instead of raising.
    """
    try:
        with db() as conn:
            row = conn.execute(
                'SELECT email FROM neon_auth."user" WHERE id::text = %s', (user_id,)
            ).fetchone()
        return row["email"] if row else None
    except Exception:
        return None


def current_identity(ctx):
    """Resolve the caller to (user_id, email), or (None, None) for a guest."""
    token = bearer_token(ctx)
    if token is None:
        return None, None
    if token == GUEST_TOKEN:
        return None, None          # chose the commons; a mode, never an error

    import jwt as pyjwt

    signing_key = _jwk_client().get_signing_key_from_jwt(token)
    claims = pyjwt.decode(
        token,
        signing_key.key,
        algorithms=ALGORITHMS,
        issuer=ISSUER or None,
        options={"verify_aud": False, "verify_iss": bool(ISSUER)},
    )
    user_id = claims["sub"]
    return user_id, _email(user_id)


def shelf(user_id) -> str:
    """How to name the caller's storage in tool output. Said out loud every
    time, because 'which shelf did that just go to' is the question this whole
    change exists to make answerable."""
    return "your shelf" if user_id else "the guest commons"
