# ADR-0006: Identity, and the guest commons

- Status: Accepted · 2026-10-15

**Decision**: the server resolves WHO is asking before it touches the store.
A verified Neon Auth token buys a private shelf keyed by the token's `sub`; no
token at all is a *guest*, who reads and writes a shared commons; a token that
fails to verify is an error. Every query against `facts` filters on
`user_id IS NOT DISTINCT FROM` the resolved identity.

## The four decisions inside that

**D1 — the identity is `sub`, never the email.** Emails get reassigned, change
spelling, and are editable by the person they identify. A key that the keyed
party can change is not a key. The email is looked up from
`neon_auth."user"` for display only, and a failure to find it degrades to
showing the raw id rather than refusing the call: the identity was already
proven by the signature, so a missing display name is cosmetic.

**D2 — guests share a commons; they are not refused.** The alternative was to
reject every unauthenticated call. Refusal is simpler to reason about and
strictly safer, and it is the right answer for an instrument holding anything
real. It was rejected here for one reason: this is a teaching instrument, and
a server that answers nothing until OAuth is wired up cannot be demonstrated,
explored, or handed to a classmate. The commons keeps the thing *usable while
being read*, which is most of its value right now.

The usual hard part of this decision did not arise. A commons normally means
deciding what happens to rows written before auth existed — leave them public,
claim them for an owner, or archive them. The store had been cleared on
2026-09-24 and held zero rows at migration time, so there was no legacy data to
dispose of and the `ALTER TABLE` carried no data consequence at all. **Nothing
in this ADR should be read as a judgment that publishing old private facts is
acceptable; the question simply was not on the table.** If facts are ever
restored from a backup taken before 2026-10-15, they will arrive with
`user_id IS NULL` and land on the commons — claim them deliberately before
restoring, not after.

**D3 — an invalid token raises; it never becomes a guest.** This is the one
that justifies the whole design. If a malformed or expired credential quietly
degraded to guest, then every broken sign-in would write private facts onto a
public shelf and report success. The failure would be invisible on the happy
path and discoverable only by reading the commons. `scripts/test-three-modes.py`
exists to assert this specific behaviour, and its second check is the only one
that would be worth keeping if the other two were deleted.

**D4 — `user_id` is `TEXT`, though `neon_auth."user".id` is `uuid`.** The JWT
`sub` arrives as a string, and TEXT keeps it a string the whole way, so no cast
sits on the hot path and no future provider with non-UUID subjects breaks the
schema. The cast happens once, in the email lookup, where it is cheap and
local.

## Where this diverges from the course's retrofit guide

The guide assumes Neon Auth is Stack Auth. Neon now ships **Better Auth**, and
three of the guide's steps do not transfer:

| guide | here |
| --- | --- |
| `algorithms=["ES256", "RS256"]` | `["EdDSA"]` — Better Auth signs with Ed25519 |
| `FROM neon_auth.users_sync` | `FROM neon_auth."user"` — `users_sync` does not exist |
| `STACK_PROJECT_ID` as JWT audience | no such id; `NEON_AUTH_ISSUER` optional instead |

The audience check is worth a note. Its job in the guide is to stop a token
minted for a different project from being accepted here. That protection
survives its removal, because the JWKS URL is already project-scoped: a foreign
token fails at the signature, before any claim is read. `NEON_AUTH_ISSUER` is
offered as a belt-and-braces `iss` check and left unset by default, because a
config value that is guessed rather than observed is worse than an absent one —
guess it wrong and you get either silent no-op or blanket rejection, and both
look like a broken token.

## Two scoping sites the mechanical recipe would have missed

"Add the filter to every query" is correct and insufficient, because two of
the fourteen sites leak through something other than a `SELECT`:

- **`find_duplicate` quotes what it finds.** Unscoped, re-saving a sentence
  someone else had already saved returns `80% overlap with #12 ('<their
  text>')`. The disclosure is in the *error string*, not in a result set, so
  no test of the happy path would ever see it.
- **`promote` and `forget` take a bare integer.** Ids are sequential and
  therefore guessable; without an ownership guard a guest could retire any
  user's fact by counting upward. Both now carry the same
  `IS NOT DISTINCT FROM` guard as the reads, and both report
  `No fact #N on <shelf>` — phrasing that neither confirms nor denies that the
  id exists elsewhere.

One SQL hazard is worth recording because it bit three times during the change:
the store's existing predicate is `expires_at IS NULL OR expires_at > now()`,
and `AND` binds tighter than `OR`. Appending the ownership filter without
wrapping the original in parentheses silently yields "unexpired, *or* expiring
later and mine" — a filter that returns the whole table while looking correct.
`memory.vocabulary` carries a comment to that effect, and the isolation check
demonstrates the leak rather than describing it.

## The honest costs

Every tool now resolves identity before doing anything, which adds a JWKS
lookup to the first authenticated call (the client is cached, so later calls
are signature verification only — microseconds). The email lookup adds one
query per authenticated call, which is real and could be cached or dropped if
it ever shows up in a profile.

Bigger: **claude.ai still has no way to send a token.** Nothing here mints or
carries one, so every call from a connector arrives as a guest. The commons is
not a stepping stone to that — it is what makes the gap survivable until OAuth
routes exist, and they are now the top item in `TODO.md`. Until then, "it works
when signed in" is a claim backed by `scripts/mint-test-token.py` and nothing
else.

**If wrong**: the column is nullable and nothing reads it but the filters. Drop
the filters and the server returns to one shared store with a vestigial column.
Going the other way — refusing guests outright — is one branch in
`current_identity` and a different default in `shelf()`.
