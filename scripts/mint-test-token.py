"""Get a real JWT for YOUR Better Auth user, to test the third mode by hand.

    uv run scripts/mint-test-token.py you@example.com [--sign-up]

Prompts for your password, signs in to Neon Auth, prints a JWT. The password
is read with getpass: not echoed, not stored, not left in shell history. Then:

    $env:TEST_JWT="<token>"                     # PowerShell
    export TEST_JWT=<token>                     # bash
    uv run scripts/test-three-modes.py

This is the manual path, and since ADR-0007 it is no longer the only one:
a connector can now sign in through the OAuth endpoints in auth.py. Keep this
script for testing, where clicking through a browser is the slow way to find
out whether verification works.

The sign-in itself is `auth.login`, imported rather than reimplemented --
Better Auth needs an Origin header and returns its session as a cookie, and
that quirk is worth knowing in exactly one place.
"""
import getpass
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

from auth import login          # noqa: E402  (needs .env loaded first)
from identity import AUTH_BASE  # noqa: E402

if not AUTH_BASE:
    sys.exit("NEON_AUTH_JWKS_URL is unset, or does not end in "
             "/.well-known/jwks.json - so the auth base URL cannot be derived "
             "from it.\nNeon console -> your project -> Settings -> Better Auth.")

args = [a for a in sys.argv[1:] if not a.startswith("-")]
if not args:
    sys.exit(__doc__)
email = args[0]
sign_up = "--sign-up" in sys.argv
password = getpass.getpass(f"password for {email}: ")

print(f"{'signing up' if sign_up else 'signing in'} at {AUTH_BASE} ...")
try:
    token, _session = login(email, password, sign_up)
except httpx.HTTPStatusError as exc:
    body = exc.response.text[:400]
    sys.exit(f"HTTP {exc.response.status_code}: {body}\n"
             + ("That email may already have an account - drop --sign-up."
                if sign_up else
                "Wrong password, or no account yet - add --sign-up."))

if not token:
    sys.exit("signed in, but no token came back.")

print("\n" + token + "\n")
print('PowerShell:  $env:TEST_JWT="<paste the token above>"')
print("bash:        export TEST_JWT=<paste the token above>")
