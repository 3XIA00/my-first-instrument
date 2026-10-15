"""The three-mode check. Run it before believing auth works.

    uv run scripts/test-three-modes.py [url]

  no token      -> guest           (a mode, not an error)
  garbled token -> ERROR           (the one that matters)
  real token    -> your email      (needs mint-test-token.py first)

The third row is the point. If a garbled token comes back as "guest", the
server is silently misfiling a broken credential as anonymity: every failed
sign-in quietly writes to the public commons and nobody finds out until the
commons has someone's private facts in it. That is the bug this whole design
exists to prevent, and it is invisible unless you test for it on purpose.

A real token is yours to supply -- set TEST_JWT in the environment, or the
third check is skipped and says so.
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp")


async def status(headers):
    """Call user_status with the given headers; return its text or the error."""
    try:
        async with streamablehttp_client(URL, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool("user_status", {})
                text = "\n".join(
                    c.text for c in result.content if hasattr(c, "text")
                )
                return ("ERROR" if result.isError else "OK"), text
    except Exception as exc:                      # transport / handshake level
        return "ERROR", f"{type(exc).__name__}: {exc}"


async def main():
    print(f"target: {URL}\n")
    checks = []

    kind, text = await status({})
    ok = kind == "OK" and text.startswith("guest")
    checks.append(("no token -> guest", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] no token\n       {text}\n")

    kind, text = await status({"Authorization": "Bearer not-a-real-token"})
    ok = kind == "ERROR"
    checks.append(("garbled token -> error", ok))
    print(f"[{'PASS' if ok else 'FAIL'}] garbled token\n       {text}")
    if not ok:
        print("       ^ STOP. A bad credential must never degrade to guest.")
    print()

    token = (os.environ.get("TEST_JWT") or "").strip()
    if not token:
        print("[SKIP] real token - set TEST_JWT to run this one")
        print("       uv run scripts/mint-test-token.py\n")
    else:
        kind, text = await status({"Authorization": f"Bearer {token}"})
        ok = kind == "OK" and text.startswith("user")
        checks.append(("real token -> user", ok))
        print(f"[{'PASS' if ok else 'FAIL'}] real token\n       {text}\n")

    failed = [name for name, ok in checks if not ok]
    print("-" * 60)
    print(f"{len(checks) - len(failed)}/{len(checks)} passed"
          + (f" - FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
