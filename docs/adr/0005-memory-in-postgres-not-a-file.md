# ADR-0005: Memory lives in Postgres (Neon), not a file

- Status: Accepted · 2026-09-18

**Decision**: the memory instrument stores facts in Neon serverless Postgres,
free plan, reached through a `DATABASE_URL` env var. Not a JSON file on disk.

**Rationale**: the obvious reason is that ADR-0003 bought us a host whose
filesystem is ephemeral — a JSON file resets on every redeploy, so an
instrument built to remember would forget every time we shipped it. The better
reason only appears once you draw it: a file gives you *two* memories, one on
the laptop where the CLI Claude talks to it and one on Render where claude.ai
does, and neither knows what the other learned. One database collapses both
surfaces into one memory. The instrument stops being two instruments.

Neon specifically: 0.5 GB free is ~500,000 facts and we will have dozens; it
scales to zero so an idle instrument costs nothing; and it is ordinary
Postgres, so nothing learned here is Neon-specific. It also happens to carry
full-text search, which replaced a hand-rolled keyword scorer, and supports
`pgvector`, which is where semantic recall goes next.

**The honest cost (know it, don't fear it)**: a file read was ~0 ms; a query is
~50-300 ms warm and 1-3 s on the first call after Neon suspends. Stacked on
Render's own ~30 s wake, a cold first call is slow. We also inherit a failure
mode a file never had — the network — and real complexity in connections,
SQL and secrets. Connections are opened per call rather than pooled, because
Neon drops idle connections when it suspends and a server that sees three calls
an hour gains nothing from a pool it would have to teach to reconnect.

**The thing to watch**: the free plan is one project, and the connection string
is a password. It lives in a gitignored `.env` locally and in Render's
Environment panel in deploy — never in the repo. The template shipped without a
`.gitignore`; one was added *before* the credential existed, which is the only
ordering that actually protects anything.

**If wrong**: the store is one table behind six functions in `memory.py` and
`server.py`. Supabase, Turso or a paid Render disk would each be an afternoon's
work, and the tool surface the model sees would not change at all.
