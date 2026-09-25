# DESIGN — the memory instrument

*Working design notes for the `my_tool` replacement. Decisions here came out of
a design conversation; open questions are marked. See docs/adr/ for the
template's own choices.*

## The premise

The clock tool fixes one blindness: a model has no sense of *now*. This fixes
the next one: a model has no sense of *before*. Everything it learned about you
dies when the conversation ends.

## Storage format

**One self-contained sentence per fact.** Not phrases, not paragraphs.

- Phrases don't survive context loss (`"prefers uv"` — over what? who?).
- Paragraphs match every query, destroying retrieval precision.
- Cost is not the constraint: a sentence is ~25 tokens, 5 results is ~125.

**The rule, stated in the docstring:** *write each fact so someone who never saw
this conversation can understand it.* This forces the model to resolve pronouns,
relative dates (`"next Friday"` -> `2026-09-25`), and deixis (`"this repo"` ->
the path).

Corollaries:
- One fact per record. Facts glued together expire on different schedules and
  are recalled by different queries.
- Metadata lives in fields, not prose.

### Record shape

```
{
  "id": int,
  "text": str,            # one self-contained sentence
  "tags": [str],
  "scope": str,           # task | term | stable
  "source": str,          # user_stated | user_confirmed | inferred
  "created_at": iso8601,
  "expires_at": iso8601 | null,
  "last_recalled": iso8601 | null,
  "recall_count": int
}
```

## Scope and expiry — one dimension, three tiers

Named by *reason*, not duration. The model can answer "is this about the user,
about this project, or about today's task?" far more reliably than "does this
expire in 30 or 90 days?" Same durations, better compliance.

| scope    | expiry    | test                                    |
|----------|-----------|-----------------------------------------|
| `task`   | 7 days    | true for the piece of work we're doing  |
| `term`   | 6 months  | true for this semester / this project   |
| `stable` | never     | true about the user or the world        |

**Rejected:** a five-tier `day/week/month/3month/never` ladder. More options
means worse compliance — the model has to choose between "week" and "month"
with no principle to guide it, and will choose inconsistently.

### Two mechanics

**Refresh expiry on recall.** A fact that keeps getting retrieved is
demonstrably useful, so bump its clock. One line, and it makes the store
self-tuning: used memories persist, unused ones decay.

**Expire != delete.** Mark expired and exclude from recall, but keep the row.
Enables `expired_facts()` ("here's what I forgot, and why") and makes nothing
unrecoverable. Deletion is irreversible; exclusion isn't.

## Retrieval

The naive `query in fact` substring match is the thing that actually breaks as
the store grows — relevance degrades long before speed does. **The scaling
problem in a memory tool is retrieval quality, not storage.**

Three layers, all cheap:

1. **Keyword scoring.** Split query into words, score facts by how many they
   contain, break ties by recency. ~10 lines, dramatically better than
   substring. Limitation: no semantics (`"advisor"` won't find `"supervisor"`).
2. **Tags as a hard pre-filter**, plus `list_tags()` so the model can read the
   map before drilling in. Two-step retrieval.
3. **Summary layer on truncation.** Return the *shape* of the match set, not
   just the top N:
   ```
   23 facts match "penn" - courses(12), people(7), admin(4).
   Showing 5 most recent. Narrow with tag= or a more specific query.
   ```

**`limit` is only truncation if the model can't tell it was truncated.** Honest
truncation plus a way to navigate is pagination with a map, not data loss.

**Out of scope, named deliberately:** embeddings / semantic search. The right
answer, but a ~90MB local model download. Knowing where the design stops is
itself a finding.

## Tool surface

```python
remember(fact, tags, scope)       # scope: task | term | stable
recall(query, tag=None, limit=5)  # scored, tagged, honest about truncation
list_tags()                       # the map
promote(fact_id, scope)           # this mattered more than I thought
forget(query)                     # explicit removal
expired_facts()                   # what decayed, for the demo
```

## Known limitation: the disk is ephemeral

Render's free tier has no persistent disk, so the JSON store resets on redeploy
and possibly on wake-from-nap. Three responses: run locally only, attach a
paid persistent disk, or accept it and write it up. **A memory instrument that
forgets is worth an ADR of its own.**

## The write-decision problem

*How does the model know a fact belongs in the tool at all, rather than just
living in the context window? And how do we stop it saving everything?*

### What we control

The save/don't-save decision happens inside the model's forward pass. There is
no hook and no callback. The only inputs we have are the tool name, the
docstring, the parameter names, and **what the tool returns** — because the
return value lands back in context and shapes the next call.

So the docstring is a prompt, not a description. Write it that way.

### Two tests, both falsifiable

"Would a future conversation be worse off without this?" is too soft — a model
can rationalize anything as potentially helpful. Sharper:

**Re-derivation:** *if I lost this, could I get it back cheaply?* File contents
— re-read the file, cost ~0, don't save. The user's advisor's name — can't be
derived, must be told, save. This is the strongest single test because it maps
onto why memory exists: memory is for what is expensive to reacquire, not for
what is useful.

**Stability:** *will this still be true when it's recalled?* "The build is
passing" is false within hours. Note the asymmetry — **a stale fact is worse
than no fact**, because it misleads while wearing the costume of knowledge.
That asymmetry is the whole argument for conservatism.

### Enumerated categories beat abstract principles

Models classify into a closed set far more reliably than they exercise
open-ended judgment. Don't ask for judgment; hand over a list.

**Save:** identity and context (program, machine, timezone, tooling) · stated
preferences · corrections the user made to you · decisions *and their
rationale* · dated commitments.

**Don't save:** anything in a file · anything you produced yourself · task
state ("we're on step 3") · transient system state ("tests pass") · **anything
you inferred that the user didn't confirm.**

That last exclusion is the important one. Models infer constantly ("user seems
to be a beginner"). Saving inferences as facts is how memory stores become
confidently wrong. **Save what was said, not what you concluded** — which is
why `source` is a field, not a nicety.

### Anti-greed, four layers

1. **Give the docstring a number.** "Be selective" is noise. "Most
   conversations should produce zero calls to this tool; typical is 0-2 facts
   per session" is an anchor, and models respond to anchors.

2. **Make saving cost something.** A `justification` parameter ("why will this
   matter in a conversation that isn't this one?") is a rubber duck — forcing
   articulation kills lazy saves. A `source` parameter lets us reject or
   downgrade `inferred` in code.

3. **Validate server-side.** We do not have to accept the write. The model
   proposes; our code disposes. All ~3 lines each:
   - reject near-duplicates (token overlap > 0.8) — the #1 greed failure is
     re-saving the same fact every session
   - cap length ~200 chars — enforces the one-sentence rule mechanically
   - blocklist "currently", "right now", "today we", "we're working on" —
     task state in disguise
   - (considered, rejected) rate limiting — blunt, and decay handles it

   **The docstring is a request; the validator is the rule.** Prose gets ~70%
   compliance; code gets 100%. Decide which rules matter, move those to code.

4. **Make the return value teach.** "Not saved — 91% overlap with #23" trains
   the next call. A silent accept trains nothing.

### The fork: gatekeeper or garbage collector?

Greed only hurts *because recall degrades*. With perfect retrieval, 10,000 junk
facts would cost nothing but disk. So policing writes is one strategy, not the
only one.

- **Gatekeeper** — strict on write, permanent once stored. Clean store, but it
  demands good judgment at the worst possible moment.
- **Garbage collector** — permissive on write, aggressive decay, expiry
  refreshed on recall. Greedy saves self-clean: anything never retrieved
  evaporates.

The argument for GC is epistemic: **at write time the model is being asked to
predict the future** — will this matter later? It cannot know. At recall time
there is *evidence*. Moving the decision from write-time prediction to
recall-time observation is better reasoning, and it means the system **tolerates
bad judgment instead of demanding good judgment.**

**DECISION: mostly garbage collector, with cheap mechanical gates on write.**
Short docstring (two tests + category lists), dedup and length enforced in code
because they are free, no rate limit, decay does the real pruning.

### Measuring it

Every record carries `recall_count` and `created_at`, so greed is an empirical
question, not an assertion:

```
memory_stats()
# 47 facts · 31 never recalled after 30 days (66%)
# by source: user_stated 12, inferred 29   <- saving its own guesses
# by scope: task 30, term 9, stable 8
```

That 66% is a finding. So is "inferred 29". Measuring the model's own saving
behavior is the most interesting thing this project can produce.

## Persistence: Neon (decided)

JSON on disk dies with Render's ephemeral filesystem. **Decision: Neon
serverless Postgres, free plan.**

The obvious reason is durability. The better reason: a file would give us *two*
memories — one on the laptop where the CLI Claude talks to it, one on Render
where claude.ai does — and neither would know what the other learned. One
database collapses them into one memory across both surfaces.

**Free plan, measured against this workload:** 0.5 GB storage per project
(~500,000 facts at ~1 KB/row with index overhead; we will have dozens) · 100
CU-hours/project/month (~400 hours awake at the 0.25 CU floor) · scale-to-zero
after 5 min idle · no card required, permanent plan, not a trial.

**Costs, honestly:** ~50-300 ms per call warm and 1-3 s on the first call after
suspend, where a file was ~0 ms · cold-start stacking, since Render free also
sleeps (~30 s) · a network failure mode a file never had · real added
complexity in connections, SQL and secrets.

**Alternatives rejected:** Render persistent disk (requires a paid instance) ·
Supabase (equivalent, no reason to prefer) · Turso (lower latency, less
standard SQL).

**Two doors this opens.** Postgres full-text search (`to_tsvector` / `ts_rank`)
replaces the hand-rolled keyword scorer in retrieval layer 1 — better ranking,
stemming included, none of our own code. And `pgvector` is supported, so the
"embeddings are out of scope" note above becomes "embeddings are a later
commit" rather than a 90 MB local model download.

### Provisioned

- Project `first-instrument`, region **AWS US West 2 (Oregon)** — deliberately
  matched to Render's free-tier default region; a mismatch would add ~70 ms to
  every query for nothing.
- Branch `production`, database `neondb`, role `neondb_owner`, Postgres 18.
- **Pooled** endpoint (`-pooler` in the hostname), which is what makes
  connect-per-call cheap.

### Connection handling: per call, not pooled

**DECISION: open one connection per tool call.** Neon suspends after 5 minutes
idle, which drops pooled connections; a long-lived pool would need reconnect
logic for a server that might see three calls an hour. Per-call costs ~50 ms
and cannot go stale.

### Secrets

`DATABASE_URL` in a gitignored `.env` locally, and set through Render's
Environment panel in deploy — same secret, two places, never in the repo.
`render.yaml` should declare it with `sync: false` so the variable is named but
its value never enters git.

*Note: the repo shipped without a `.gitignore`. Added one before the credential
existed, not after.*

## Built — what changed from the design

Three deviations, each forced by something that showed up in testing:

**`forget(fact_id)`, not `forget(query)`.** A query-shaped delete can match
more than intended, and the model cannot see what it is about to hit. Taking an
id means the fact has to be surfaced by `recall()` first, so nothing goes in
bulk and nothing goes unseen.

**OR-ed search terms, not AND-ed.** The first implementation used
`plainto_tsquery()`, which ANDs every word in the query. Testing found the
failure immediately: `recall("penn course")` returned NOTHING while the store
held *"Yuzhou is enrolled in CIS 7000 at Penn for Fall 2026"* tagged `penn`,
`courses`. Every word had to appear in the `text` column, and tags were not
searched at all. Now query words are OR-ed via `to_tsquery()`, tags are matched
with array overlap, and `ts_rank` still floats facts matching more words to the
top. **A silent miss is the exact failure this tool exists to prevent** — it
looks identical to having never saved the fact.

**The query regex doubles as the sanitiser.** `search_terms()` extracts
`[a-z0-9]+` only, so `&`, `|`, `!` and parentheses cannot reach `to_tsquery()`
as operators or syntax errors. `recall("what database did we pick?!")` is safe
by construction rather than by escaping.

### The limitation, now demonstrated rather than asserted

`recall("what database did we pick?!")` returns nothing, though the store holds
*"...stores facts in Neon Postgres, chosen so laptop and Render share one
memory."* Nothing is broken: "database" and "Postgres" share no stem, and
keyword search cannot know they are related. This is the semantic gap named
above, now with a reproducible example — and the argument for `pgvector`.

### Verified against the live database

All nine tools exercised end to end: the three validators refuse correctly
(banned phrase, over-length, bad scope), duplicate detection caught a reworded
repeat at 90% overlap, recall-refresh increments `recall_count` and pushes
expiry forward, and `forget()` -> `expired_facts()` -> `promote()` round-trips.
Test rows deleted afterwards; `facts` is empty and ready for real use.

## Tags: a seeded vocabulary with a confirm gate

### What went wrong first

The original `tags` parameter was documented as *"1-3 lowercase topic words
for later filtering, e.g. `["courses", "penn"]`"*. With one fact in the store,
the model had already produced this:

```
#12  ['education', 'penn']
#13  ['python', 'tooling', 'preferences']
#14  ['preferences', 'code-style']

identity 0          <- the tag that fits #12 was never used
5 seed tags, 5 added, every added tag a singleton
```

It took `penn` **verbatim from the example** and paraphrased `courses` into
`education`. **In a tool docstring an example is not an illustration, it is a
default** — whatever sits after `e.g.` is what the model reaches for.

### The controlled experiment sitting in the same call

Fact #12 was written by one tool call with three parameters:

| param | documented as | result |
|-------|---------------|--------|
| `scope` | closed list, enforced in code | correct (`stable`) |
| `source` | closed list, enforced in code | correct (`user_stated`) |
| `tags` | "topic words", free-form, one example | drifted |

Same tool, same invocation. The two parameters with a closed list came back
right; the one with an open list drifted immediately. n=1, so don't overclaim
it — but the prediction was made before the evidence arrived.

### The design

Seed vocabulary mirrors the SAVE categories in the docstring **one for one**,
so deciding a fact is worth saving also decides its tag. One judgment, not two:

```
identity     who they are, their context, their setup
preferences  how they want things done
corrections  where they told you that you were wrong
decisions    a choice that was made, and why
commitments  something dated they owe or expect
```

**Seeded, not fixed.** An unfamiliar tag is refused, and the refusal carries
the whole live vocabulary so the model can choose from it. If nothing fits, it
repeats the tag in `new_tags` to confirm. Reuse is free; coining costs a round
trip, and that asymmetry is the entire mechanism.

**Pay-on-failure.** The obvious alternative — require `list_tags()` before
every save — costs a round trip even when the model was going to choose
correctly. Refusing on the unknown case costs one only when there is a problem.
Expected price: ~300 tokens and one model turn, a handful of times early on,
decaying toward zero as the seeds cover the common cases.

**"Not saved yet", not "Not saved".** The real risk is not cost, it is
abandonment: a model already told to prefer silence will take a refusal as an
excuse to stop. The refusal is worded as a step in a process, with an
imperative, not as an error.

**No registry table.** The vocabulary is `SEED_TAGS` union the tags on
unexpired facts, so a tag stops existing when the last fact carrying it
expires. It decays like everything else here.

### Tag what the text can't tell you

A tension that only appeared once real facts existed: the seed vocabulary is
*category*-shaped (`identity`, `decisions`), but the model also produced
*topic*-shaped tags (`penn`, `python`). Two kinds of label in one namespace —
the same two-taxonomies problem that killed the `category` column, arriving
through a different door.

**The rule: tag what the text can't tell you; let search handle what it can.**
"Penn" and "Python" are already in the fact text and `recall` searches text, so
those tags are redundant. "This is an identity fact" appears nowhere in the
text and can never be inferred from it. Categories earn a tag; topics don't.
That is also *why* the seed list has the shape it does.

### Rendering

Newline-delimited, single space, no column alignment — padding costs tokens and
carries no information. The standard/added split is the part that does: it says
which tags are the taxonomy and which were exceptions someone had to justify.

Do not truncate the list when it grows. The model is being asked to choose, and
a hidden option cannot be chosen. If the list is too long to show, the answer is
fewer tags, not less display — and `memory_stats` reports singleton count so
that failure is visible rather than hoped against.

## OPEN — still to build

- No-dead-end recall: a keyword miss should return recent facts, not nothing
- Source label on recall results, so blending with a competing memory is visible
- `pgvector` for semantic recall — the demonstrated gap
