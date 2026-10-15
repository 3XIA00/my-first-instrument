# ADR-0007: We are our own authorization server

- Status: Accepted · 2026-10-15

**Decision**: this server implements the OAuth 2.1 authorization-server
endpoints itself (`auth.py`), and delegates only the password check to Neon's
Managed Better Auth. A connector discovers us, registers with us, sends its
user to a sign-in page we serve, and receives a token we issue. It never
learns that Better Auth exists.

## Why not simply point claude.ai at Better Auth

That was the design worth wanting, and it is not available. Neon's Managed
Better Auth is an *application sign-in service*: it has `/sign-in/email`,
`/sign-up/email` and `/token`, and that is the whole surface. There is no
`/authorize`, nothing issues authorization codes, and nothing has heard of
PKCE or dynamic client registration. An MCP client speaks only OAuth, so
something has to translate, and the only place to put that translation is
here.

The division that falls out is a clean one. `auth.py` knows OAuth and nothing
about shelves; `identity.py` knows tokens and nothing about OAuth; the tools
know neither. The seam is a bearer token, which is exactly what ADR-0006
already defined.

## The declared guest, and why OAuth forced it

ADR-0006 described three states. OAuth adds a fourth, because the protocol has
no way to say *no thanks*. A connector that opens a sign-in page must come
back holding a credential: hand it a JWT and we have lied about a user that
does not exist; hand it nothing and the client reads a failed sign-in and
offers to try again, forever. So "Continue as guest" issues `GUEST_TOKEN`, a
constant that `identity.py` maps back to guest mode.

It is deliberately not a secret, and that is the point rather than a weakness:
it grants exactly what an unauthenticated caller already has. The distinction
it preserves is between *absent* (guest), *declared* (guest, on purpose) and
*invalid* (an error) — and the third of those is the one ADR-0006 D3 exists to
defend. Making the declared guest a constant rather than a signed token keeps
that boundary legible: there is one string to look for, in one function.

## Where this departs from the reference implementation

`auth.py` is modelled on the course's `your-protected-notes/auth.py`, which
supplied three facts about Managed Better Auth that are not documented and
would have cost a day to rediscover: the `Origin` header is required, the
session comes back as a cookie rather than in the body, and `GET /token`
trades that cookie for a short-lived EdDSA JWT. Those are reproduced.

One thing is not. **The reference does not check `redirect_uri` against what
the client registered.** Client ids are public and registration is open, so
without that check anyone can start an authorization request with a real
client id and have the resulting code delivered to an address they control.
Here, `/authorize` refuses an unregistered redirect on both GET and POST, by
exact string match, and reports the refusal as a page rather than a redirect —
because the one thing never to do with an unvalidated redirect target is send
a browser to it. `/token` then re-checks that the `redirect_uri` and
`client_id` presented at redemption are the ones the code was minted for.

Two smaller departures: PKCE uses `secrets.compare_digest` rather than `==`,
and a missing `code_challenge` is a refusal rather than a skipped check — "no
challenge" must not be the cheap way past the challenge. The Better Auth base
URL is derived from `NEON_AUTH_JWKS_URL` instead of being a second environment
variable, so the two cannot drift apart.

## Prototype-grade, and exactly how

Authorization codes and client registrations live in dictionaries in the
process. This is correct for one Render instance and wrong for two, and the
free plan gives us one. On restart: in-flight codes are lost, which costs a
user one retry of a five-minute credential; registrations are lost, after
which a connector gets `invalid_client` and re-registers, which well-behaved
clients do automatically — the `/authorize` error page says so in words, for
the case where one does not.

Moving both to Postgres is one table and an afternoon. It has not been spent
because the thing it buys — surviving a restart and scaling past one instance
— is not yet a thing this instrument needs, and a stored credential is a new
liability to reason about. Revisit when the server stops being a single free
dyno, and not before.

## The honest costs

**A password crosses this process.** We serve the form, so the credential
passes through our handler on its way to Neon. Nothing stores, logs or echoes
it, and it posts to exactly one place, but that is mitigation and not absence.
A redirect to a login page we did not write is the correct answer and becomes
available the day Neon exposes one; until then this is the cost of the
translation layer, and it should be stated rather than buried.

**Tokens are short.** Better Auth JWTs last about fifteen minutes. Refresh is
the Better Auth session cookie, carried as `ba:<session>`, which means a
signed-in connector depends on that session staying alive — when it does not,
the user signs in again.

**Open registration.** Anyone can register a client. This grants nothing on
its own (every code still needs PKCE and a pre-registered redirect), but it is
unbounded memory, so the dict is capped and cleared rather than trusted.

## What is verified, and what is not

`scripts/test-oauth-flow.py` drives the entire handshake — discovery,
registration, authorize, PKCE, token exchange, refresh, and the issued token
against a real MCP call — through the guest branch, which needs no password.
It passes 18 checks, and three of them are the ones that matter: an
unregistered redirect produces no redirect, a wrong verifier produces no
token, and a spent code cannot be spent again.

**The password branch is not machine-tested.** Signing a real user in needs a
real credential, and no automated check here supplies one. Until a human has
clicked through the form and seen `user_status` report their email, "signed-in
users get private shelves" is a claim resting on `scripts/mint-test-token.py`
— which exercises the same `login()` function, and therefore proves the Better
Auth call works, but not the form, redirect and exchange wrapped around it.

**If wrong**: `auth.py` is mounted by one call at the bottom of `server.py`.
Delete that line and the server returns to exactly what ADR-0006 shipped —
tokens still verify, guests still work, and only the ability to obtain a token
in a browser goes away.
