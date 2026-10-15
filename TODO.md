# TODO

*Design reasoning for everything here lives in `docs/DESIGN-memory.md`. This
file is just the queue.*

*Read "the store" below as **the caller's shelf**. Since ADR-0006 the server
knows who is asking: a signed-in caller gets a private shelf, a signed-out one
gets the shared guest commons, and every query filters on `user_id`. Nothing
in the items below changes direction because of that, but the counts, the tag
vocabulary and the "what is in here" questions are now all answered per shelf,
not per table.*

## 0. OAuth — done, with one piece deliberately not built

**Done 2026-10-15 (ADR-0007).** `auth.py` makes this server its own
authorization server: discovery, dynamic client registration, a sign-in page,
PKCE, code exchange and refresh. Better Auth checks the password; everything
else is ours. There is also a "Continue as guest" button, because OAuth has no
way to decline and a connector must come back holding *something*.

`scripts/test-oauth-flow.py` drives the whole handshake through the guest
branch — which needs no password — and passes 18 checks, including the three
that matter: an unregistered `redirect_uri` yields no redirect, a wrong PKCE
verifier yields no token, and a spent code cannot be spent twice.

**Confirmed on the deployed instance, 2026-10-15.** A real client ran the
entire handshake end to end: Claude Code's connector discovered the metadata,
opened the sign-in page, took a password, and came back with a token that
`user_status` resolves to the account that signed in. Facts saved afterwards
landed on that account's private shelf and `recall` returned them. The
password branch is no longer an argument; it is an observation.

ADR-0007 still says that branch is untested. That was true when it was
written, and ADRs here are not rewritten (ADR-0001), so this paragraph is the
update.

**The finding worth keeping.** Claude Code began the OAuth flow *without being
challenged for it*. This server answers unauthenticated calls with 200 and no
`WWW-Authenticate` header, so the working assumption was that no client would
ever attempt authorization — the guest commons would simply swallow everyone.
It turns out that client probes `/.well-known/oauth-protected-resource` of its
own accord. A 401 challenge is therefore not what makes sign-in reachable, at
least not here. That was worth being wrong about out loud: the guest default
and a discoverable sign-in are less in tension than they looked.

**Decided (2026-10-15): no 401 challenge.** claude.ai was the open question,
being a different client from Claude Code and under no obligation to share its
habits. It behaves the same way: the custom connector found the metadata on
its own, ran the OAuth flow unprompted, and `user_status` there reports the
same account Claude Code reports. Both surfaces this instrument actually has
reach the private shelf without ever being challenged for a credential.

So the challenge would buy nothing today, while costing the zero-friction
local flow — `claude mcp add` against localhost would gain a browser round
trip before its first call. Roughly thirty lines in `auth.py` plus a branch in
`__main__`, not written.

**What would bring it back:** a client that waits to be told. The symptom to
watch for is specific — a connector that attaches fine, reports `guest`, and
offers no authorization step anywhere in its UI. That is a client expecting a
`WWW-Authenticate` header it never receives, and it has no way out of the
commons on its own.

**Worth recording while it is fresh:** the same identity now reaches the same
private shelf from both Claude Code and claude.ai, and the claude.ai session
visibly *acted* on the stored preferences rather than merely listing them,
saying so in its own reply. ADR-0005 argued one database because a file would
give two memories that never learn from each other; this is the same argument
one level up, and the first evidence that these facts change behaviour instead
of just occupying rows.

**What is left.**

1. **Move codes and registrations into Postgres.** Only once there is a reason:
   a second instance, or restarts frequent enough that re-registering annoys
   someone. One table, an afternoon, and a new stored credential to reason
   about. Not before.

**Still honest about the shape.** We serve the sign-in form, so a password
crosses this process on its way to Neon. That is the cost of translating
between a connector that speaks only OAuth and a provider that does not speak
it at all; it goes away the day Neon exposes a hosted login we can redirect to,
and not before. ADR-0007 says so at greater length.

## 1. Tag-driven recall — let the model propose tags instead of guessing words

**The problem, observed live.** `recall("what do you know about me")` returned
nothing while the store held a fact about the user, because every word in that
phrasing is a stopword or absent from the fact text. Worse, the model then
filled the gap from a competing memory system and presented the result as one
seamless answer — the instrument *looked* like it worked. The failure was only
visible by opening Neon and counting rows.

**The insight.** `recall` currently asks SQL to do semantics — match "about me"
against the words in a sentence. Postgres matches spelling, not meaning. But
the model is a semantic engine: show it `identity, preferences, decisions` and
it knows "about me" means `identity`. So don't solve the matching problem —
**move it to the component that can actually do it.** Retrieval becomes
selection from a menu, not search over text.

**What to build.**

- On a keyword miss, return the live tag vocabulary instead of "no match", so
  the dead end becomes the menu and the model can select on a second call.
  The vocabulary is per shelf, so a guest is offered the commons' tags and a
  signed-in user their own — which is correct, and also means the menu can be
  empty for a brand-new shelf where the old design could assume it never was.
- Keep free-text as the fast path. Do **not** force `list_tags()` before every
  recall — an extra required call on every lookup is friction the model will
  route around. Search first, menu on failure.
- The seed vocabulary and confirm gate (already built) are the prerequisite:
  a menu only works if someone controls what goes on it.

**Honest limit.** This makes recall *reliable*, not *semantic*.
`recall("what database did we pick")` still won't find "Postgres" unless
something is tagged `decisions` and the model thinks to look there. That is a
hard problem traded for a well-managed one — say so in the writeup rather than
claiming semantic search was solved.

## 2. No-dead-end recall

A memory that answers "nothing" while holding facts is broken. With a store
this size, if keyword matching finds nothing, return the most recent facts
anyway under an honest header:

```
No keyword match for "what do you know about me".
Your shelf holds 3 facts total — showing all of them:
```

Complements #1 rather than competing with it: small store → show everything;
large store → offer the menu. Navigation is pointless at three facts.

**Say which shelf.** "Holds 3 facts" is now ambiguous in the one case that
matters: a user who expected their own facts and is quietly being shown the
commons. The header has to name the shelf, not the table — that single word is
what turns a wrong-identity bug from invisible into obvious.

## 3. Source label on recall results

Claude Code and claude.ai both ship their own memory. When both are present the
model merges them into one answer with no seam, and provenance is lost — that
is exactly how the failure in #1 stayed invisible. Label results as coming from
this instrument so blending is visible to the user instead of hidden.

**Decided (2026-10-08): attribution at the point of use, not a banner at the
top.** A source label on `recall` output is read by the model, not the user; a
session-opening "loaded 4 facts" is identical every time and stops being read
by the third message. Neither sits next to the behavior it explains, which is
where the seam actually needs to be visible. The rule instead:

- One line after `briefing()`, only when something loaded — the gist, not a
  dump. Silent on an empty store.
- **Whenever a saved fact changes what the agent does** — a different tool, a
  refusal, a skipped step — say so and cite the fact id: *"Not committing —
  you corrected me on 2026-09-20 to never commit unasked (#7)."*
- Surface any conflict between a saved fact and what is on disk, and ask which
  is current, rather than silently trusting either.
- Stay silent for reference facts that merely informed an answer.

**Why the id and not just a label.** `recall` already prints `#id`, and the id
turns a mention into a control surface: when the agent cites #7 and #7 is
stale, `forget(7)` is right there. Provenance that carries an id is *operable*,
not merely visible — a stronger answer to this item than a source string.

**What to build.** The full four-part rule is judgment and belongs in the
skill. A one-line version of the middle two belongs in the `recall` docstring,
so it still travels to agents that never load a skill.

**Second axis, since ADR-0006.** Provenance now has two questions, not one:
which memory system, and which shelf. `user_status` answers the second on
demand, and `remember` names the shelf on every save, but `recall` still does
not — so facts read from the commons are indistinguishable from facts read
from your own shelf at the point where the model actually uses them. Same
argument as the one above, one level in.

## 4. Re-tag or retire pre-gate facts (only if any survive)

The vocabulary is derived from active facts, so tags written before the confirm
gate are grandfathered in and the gate will keep accepting them. The store was
cleared on 2026-09-24 and was still empty when `user_id` was added on
2026-10-15, so this is currently moot — it becomes live again if any fact
predating a future vocabulary change is still `stable`.

One way it comes back: a restore from a backup taken before 2026-10-15. Those
rows arrive with `user_id IS NULL` and land on the guest commons, so they would
need claiming *and* re-tagging, and the claiming has to happen first.

## 5. pgvector for semantic recall

The general solution to #1, and the point at which the menu becomes a fallback
rather than the mechanism. Needs an embedding model: Anthropic does not make
one and points to Voyage AI (`voyage-4-lite`, 1024-dim); `voyage-4-nano` is
Apache-2.0 on Hugging Face and runs locally. Neon supports the extension, so
this is `ALTER TABLE` plus a column, not a migration to a new store.

**Do not build this until the store has enough real facts to show that keyword
recall actually misses in practice.** Real failed queries are better evidence —
and better writeup material — than a hypothesis.

## 6. `briefing()` — the standing instructions, before the work

**The problem.** Facts come in two species and only one of them works on
demand. `identity`, `decisions`, `commitments` *answer* questions — recall them
when the question arrives. `preferences` and `corrections` *change how the
agent acts*, and nobody ever asks "what are my preferences?" You don't ask; you
notice the agent got it wrong. A correction recalled after the mistake is
worthless — its entire value is pre-emption. Those two tags are structurally
unable to work through `recall`.

**Why this cannot be left to the skill.** A skill fires by matching its
description against the request, and at the top of a session there is nothing
to match yet. Skills also reach only some clients (Claude, Codex), while tool
docstrings reach every agent that connects over MCP. Anything load-bearing for
cross-vendor alignment belongs in the server, not in a skill.

**What to build.**

- `briefing()` — one call returning every active fact tagged `preferences` and
  `corrections`, plus a count of what else is on that shelf so the agent knows
  the rest exists and is reachable by `recall`. No new query code needed:
  tag-only recall already works, because an empty `query` skips the text
  filter entirely.
- Update the docstrings so the instruction travels to agents that will never
  read a skill: `briefing()` announces itself as the first call of a session,
  and `recall`, `remember` and the server instructions point back at it. Phrase
  it as *before your first substantive action*, not *at session start* — an
  agent reads a docstring when it reaches for a tool, not when it says hello.
- Decide what an empty store returns. "No preferences saved yet" on every
  session is a ritual that teaches agents to skip the call. This stopped being
  an edge case. Guests are now a deliberate, one-click option rather than an
  accident of missing auth (#0), so the empty answer is one plenty of sessions
  will genuinely get, not a transitional embarrassment.

**Honest limit.** Nothing can make a model call this. It competes with every
other tool for attention at the one moment the agent is least sure what the
session is about. `memory_stats` should be able to answer the real question:
is `briefing()` landing before the first edit, or after the first mistake?

## 7. Write gate — ask every time (provisional baseline)

**Decided (2026-10-08).** Every `remember` is proposed and waits for a yes:
the agent shows the exact sentence, tags, scope and source, and writes nothing
until the user agrees. This holds for corrections the user just stated, not
only for inferences.

**Why, against the better argument.** The sharper design gates on `source` —
transcription needs no permission, inference does — and it is probably where
this ends up. But the store is new, the user is not yet confident judging these
sentences in the abstract, and a junk fact does its damage silently and on
machines nobody is watching. A gate that is too tight costs confirmations that
are visible and annoying; one that is too loose costs recall precision that is
invisible. Pay the visible cost first, and lift the gate once there is a real
store to judge the agent's proposals against.

**Keep the structure underneath.** The agent still declares `source` when it
proposes, so lifting the baseline later means deleting one block of the skill
rather than rediscovering the distinction.

**Watch for.** Whether the ask causes abandonment — the open question below,
now with a second gate stacked on the docstring's "ZERO calls" anchor. If
sessions start producing zero facts, this is the first thing to loosen.

## Open questions, not yet decided

- With a universal ask, does `source` collapse? If every fact is agreed to
  before it is written, nearly everything becomes `user_confirmed` and the
  field stops distinguishing anything. Define `source` by where the CONTENT
  came from — they stated it vs. the agent inferred it — not by whether a yes
  was given, or the schema loses the signal it was built for.


- Does the confirm gate cause **abandonment**? The docstring already says
  "most conversations should produce ZERO calls to this tool"; a refusal hands
  the model a second reason to stop. Watch whether refusals are followed by a
  retry or by silence.
- Does attribution go stale the way a banner does? It is rarer by design, but
  if a handful of preferences fire constantly the citations become their own
  wallpaper. Watch for the preference that earns a mention every single turn —
  that one probably belongs in CLAUDE.md, not in memory.
- `memory_stats` counts recalls, which is not the question. The real one is
  whether a recalled fact *changed the outcome* — and only attribution makes
  that observable. Worth considering whether the store can learn it at all, or
  whether it stays a thing you can only see by reading transcripts.
- Does `briefing()` land before the first edit, or after the first mistake?
  That single measurement decides whether the docstring instruction works.

- Is the "ZERO calls" anchor now too strong? It was tuned against over-saving
  before a competing memory was in the picture. A before/after comparison on
  that one line would be a real prompt-design experiment.
