"""your-first-instrument — a sense of time, and a sense of before.

Why time? Ask your Claude "how long have we been talking?" WITHOUT this
connected. It can only guess: no clock lives in a context window. This
server is the smallest honest fix — and the pattern generalizes.

The memory tools fix the next blindness along: a model has no sense of
*before*. Everything it learns about you dies when the conversation ends.
Every choice made there is argued in docs/DESIGN-memory.md.
"""
import os
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

import memory
from memory import SCOPES, db

mcp = FastMCP(
    "your-first-instrument",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

# Recalling a fact pushes its clock forward: a fact that keeps getting
# retrieved is demonstrably useful. Used memories persist, unused ones decay,
# and the store tunes itself without anyone deciding anything.
_REFRESH = """
    expires_at = CASE scope
        WHEN 'task' THEN now() + interval '7 days'
        WHEN 'term' THEN now() + interval '180 days'
        ELSE NULL END
"""


def _expiry(scope: str):
    ttl = SCOPES[scope]
    return datetime.now(timezone.utc) + ttl if ttl else None


# ─── a sense of now ──────────────────────────────────────────────────────

@mcp.tool()
def current_time() -> str:
    """The current date and time (UTC and local)."""
    now = datetime.now(timezone.utc)
    return f"UTC: {now.isoformat()} · local: {datetime.now().isoformat()}"


@mcp.tool()
def seconds_since(iso_timestamp: str) -> str:
    """Seconds elapsed since an ISO timestamp (e.g. '2026-09-10T17:15:00')."""
    then = datetime.fromisoformat(iso_timestamp)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - then
    return f"{delta.total_seconds():.0f} seconds ({delta})"


# ─── a sense of before ───────────────────────────────────────────────────

@mcp.tool()
def remember(
    fact: str,
    tags: list[str],
    scope: str,
    source: str,
    justification: str,
    new_tags: list[str] = [],
) -> str:
    """Save ONE fact so it survives past this conversation.

    Most conversations should produce ZERO calls to this tool. Typical is
    0-2 facts per session. Over-saving is far worse than under-saving: junk
    degrades every future recall, permanently, while a missed fact costs you
    once. When in doubt, don't call this.

    Two tests before you call:
      1. RE-DERIVATION — if this were lost, could you get it back cheaply?
         File contents: re-read the file, don't save. The user's advisor's
         name: can't be derived, must be told, save.
      2. STABILITY — will this still be TRUE when it's recalled? A stale fact
         is worse than no fact, because it misleads while looking like
         knowledge.

    SAVE — each category is also its tag, so one judgment does both jobs:
      identity     who they are, their context, their setup
      preferences  how they want things done
      corrections  where they told you that you were wrong
      decisions    a choice that was made, and why
      commitments  something dated they owe or expect
    DON'T SAVE: anything in a file · anything you produced yourself · task
      state ("we're on step 3") · transient system state ("tests pass") ·
      anything you INFERRED that the user didn't confirm.

    Args:
      fact: ONE self-contained sentence, under 200 characters. Write it so
        someone who never saw this conversation understands it — resolve
        pronouns ("he said" -> who?), relative dates ("next Friday" ->
        "2026-09-25"), and deixis ("this repo" -> the actual path).
      tags: 1-3 tags from the SAVE list above — identity, preferences,
        corrections, decisions, commitments. You already chose one of those
        categories when you decided this was worth saving; use it.
        Prefer a tag that exists over a new word that fits slightly better:
        a vocabulary of near-synonyms is what makes a store unsearchable.
        A tag outside the vocabulary is not saved — the tool replies with
        everything currently in use so you can pick from it and call again.
      new_tags: leave empty almost always. Only after the tool has refused
        an unfamiliar tag, and only if nothing in the list it showed you
        actually fits, repeat that tag here to confirm creating it.
      scope: how long this stays true.
        "task"   - true for the piece of work we're doing (7 days)
        "term"   - true for this semester or project (6 months)
        "stable" - true about the user or the world (no expiry)
      source: where this came from. "user_stated" (they said it),
        "user_confirmed" (you proposed it, they agreed), or "inferred" (you
        concluded it yourself — prefer to NOT save these at all).
      justification: why will this matter in a conversation that isn't this
        one? If you can't answer that in a sentence, don't save it.
    """
    fact = fact.strip()
    refusal = memory.validate(fact, scope, source)
    if refusal:
        return f"Not saved — {refusal}"

    with db() as conn:
        # "Not saved YET" — a refusal the model can act on reads as a step in
        # a process; "Not saved" reads as a dead end, and a tool that is
        # already told to prefer silence will take the excuse.
        tags, tag_refusal = memory.check_tags(conn, tags, new_tags)
        if tag_refusal:
            return f"Not saved yet — {tag_refusal}"

        dup, score = memory.find_duplicate(conn, fact)
        if dup:
            return (
                f"Not saved — {score:.0%} overlap with #{dup['id']} "
                f"({dup['text']!r}). Use promote() if it is more durable than "
                "you first thought, or forget() that one if this wording is "
                "genuinely better."
            )
        row = conn.execute(
            """INSERT INTO facts
                   (text, tags, scope, source, justification, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (fact, tags, scope, source, justification, _expiry(scope)),
        ).fetchone()
        stats = conn.execute(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE recall_count = 0) AS never
               FROM facts WHERE expires_at IS NULL OR expires_at > now()"""
        ).fetchone()

    ttl = SCOPES[scope]
    when = "never expires" if ttl is None else f"expires in {ttl.days} days"
    return (
        f"Saved as #{row['id']} ({scope}, {when}). "
        f"Store now holds {stats['total']} active facts; "
        f"{stats['never']} have never been recalled."
    )


@mcp.tool()
def recall(query: str, tag: str = "", limit: int = 5) -> str:
    """Search saved facts. Call this when a question touches the user's own
    life, preferences, projects or history — anything you would otherwise
    have to guess at, or ask them to repeat.

    Returns the best matches AND the shape of everything that matched, so you
    can tell when there is more to see and narrow down instead of guessing.

    Args:
      query: words to search for. Empty string returns the most recent facts.
      tag: optional — restrict to one tag. Use list_tags() to see what exists.
      limit: how many facts to return (default 5).
    """
    # OR the query's words rather than AND-ing them, and let tags match too.
    # plainto_tsquery() would demand EVERY word appear in the text, so
    # "penn course" finds nothing even when a fact says "Penn" and is tagged
    # "courses". Silent misses are the failure this tool exists to avoid;
    # ts_rank still floats the facts matching more words to the top.
    terms = memory.search_terms(query)
    where = ["(expires_at IS NULL OR expires_at > now())"]
    params: list = []
    if tag:
        where.append("%s = ANY(tags)")
        params.append(tag)
    if terms:
        where.append(
            "(to_tsvector('english', text) @@ to_tsquery('english', %s)"
            " OR text ILIKE %s OR tags && %s::text[])"
        )
        params += [" | ".join(terms), f"%{query.strip()}%", terms]
    clause = " AND ".join(where)

    with db() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM facts WHERE {clause}", params
        ).fetchone()["n"]
        if total == 0:
            scoped = f" with tag {tag!r}" if tag else ""
            return (f"No facts match {query!r}{scoped}. "
                    "Try list_tags() to see what is actually in the store.")

        breakdown = conn.execute(
            f"""SELECT unnest(tags) AS t, count(*) AS n FROM facts
                WHERE {clause} GROUP BY 1 ORDER BY n DESC LIMIT 8""",
            params,
        ).fetchall()

        if terms:
            rank_sql = ("ts_rank(to_tsvector('english', text),"
                        " to_tsquery('english', %s))")
            rank_params = [" | ".join(terms)]
        else:
            rank_sql, rank_params = "0", []
        rows = conn.execute(
            f"""SELECT id, text, tags, scope, created_at, recall_count,
                       {rank_sql} AS rank
                FROM facts WHERE {clause}
                ORDER BY rank DESC, created_at DESC LIMIT %s""",
            rank_params + params + [limit],
        ).fetchall()

        conn.execute(
            f"""UPDATE facts SET last_recalled = now(),
                    recall_count = recall_count + 1, {_REFRESH}
                WHERE id = ANY(%s)""",
            ([r["id"] for r in rows],),
        )

    header = f"{total} fact{'' if total == 1 else 's'} match"
    if tag:
        header += f" (tag={tag})"
    if breakdown:
        header += " — " + ", ".join(f"{b['t']}({b['n']})" for b in breakdown)
    if total > len(rows):
        header += (f"\nShowing {len(rows)} of {total}. Narrow with tag= or a "
                   "more specific query.")

    lines = []
    for r in rows:
        tags = ", ".join(r["tags"])
        stamp = r["created_at"].strftime("%Y-%m-%d")
        lines.append(
            f"#{r['id']} [{r['scope']}] {r['text']}\n"
            f"    {tags} · saved {stamp} · recalled {r['recall_count']}x"
        )
    return header + "\n\n" + "\n".join(lines)


@mcp.tool()
def list_tags() -> str:
    """Every tag in the store with its count — the map of what is known.
    Read this before a broad recall() so you can filter instead of guess, and
    before remember() if you are unsure which tag a fact belongs under."""
    with db() as conn:
        counts = memory.vocabulary(conn)
    return memory.render_vocabulary(counts)


@mcp.tool()
def promote(fact_id: int, scope: str) -> str:
    """Extend a fact's life — it turned out to matter more than first thought.
    Also the way to rescue something listed by expired_facts().

    Args:
      fact_id: the #id shown by recall().
      scope: the new scope — "task", "term" or "stable".
    """
    if scope not in SCOPES:
        return f"scope must be one of {', '.join(SCOPES)} — got {scope!r}."
    with db() as conn:
        row = conn.execute(
            """UPDATE facts SET scope = %s, expires_at = %s
               WHERE id = %s RETURNING text""",
            (scope, _expiry(scope), fact_id),
        ).fetchone()
    if not row:
        return f"No fact #{fact_id}."
    ttl = SCOPES[scope]
    when = "never expires" if ttl is None else f"expires in {ttl.days} days"
    return f"#{fact_id} is now {scope} ({when}): {row['text']!r}"


@mcp.tool()
def forget(fact_id: int) -> str:
    """Retire a fact — wrong, outdated, or one that never should have been
    saved. Takes an id rather than a search, so nothing goes in bulk.

    This EXPIRES the fact rather than deleting it: excluded from recall, still
    listed by expired_facts(), recoverable with promote(). Deletion is
    irreversible; exclusion isn't.
    """
    with db() as conn:
        row = conn.execute(
            "UPDATE facts SET expires_at = now() WHERE id = %s RETURNING text",
            (fact_id,),
        ).fetchone()
    if not row:
        return f"No fact #{fact_id}."
    return f"Retired #{fact_id}: {row['text']!r} — recover it with promote()."


@mcp.tool()
def expired_facts(limit: int = 20) -> str:
    """What the store has forgotten, and when. Nothing here is gone — any of
    it can be brought back with promote()."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, text, scope, expires_at, recall_count FROM facts
               WHERE expires_at IS NOT NULL AND expires_at <= now()
               ORDER BY expires_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    if not rows:
        return "Nothing has expired yet."
    lines = []
    for r in rows:
        stamp = r["expires_at"].strftime("%Y-%m-%d")
        lines.append(
            f"#{r['id']} [{r['scope']}] {r['text']}\n"
            f"    expired {stamp} · was recalled {r['recall_count']}x"
        )
    return "\n".join(lines)


@mcp.tool()
def memory_stats() -> str:
    """How this memory is actually being used — including how much was saved
    and then never recalled once. Whether the instrument is being used
    greedily is an empirical question; this is how you answer it."""
    active_only = "expires_at IS NULL OR expires_at > now()"
    with db() as conn:
        s = conn.execute(
            f"""SELECT count(*) FILTER (WHERE {active_only}) AS active,
                       count(*) FILTER (WHERE expires_at IS NOT NULL
                           AND expires_at <= now()) AS expired,
                       count(*) FILTER (WHERE recall_count = 0
                           AND ({active_only})) AS never,
                       count(*) AS total
                FROM facts"""
        ).fetchone()
        by_scope = conn.execute(
            f"""SELECT scope AS k, count(*) AS n FROM facts
                WHERE {active_only} GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        by_source = conn.execute(
            f"""SELECT source AS k, count(*) AS n FROM facts
                WHERE {active_only} GROUP BY 1 ORDER BY n DESC"""
        ).fetchall()
        counts = memory.vocabulary(conn)

    if s["total"] == 0:
        return "No facts saved yet."
    pct = f" ({s['never'] / s['active']:.0%})" if s["active"] else ""
    scopes = ", ".join(f"{r['k']} {r['n']}" for r in by_scope) or "—"
    sources = ", ".join(f"{r['k']} {r['n']}" for r in by_source) or "—"
    # Tag hygiene as a number, not a hope. Added tags that stay at 1 are the
    # near-synonyms the confirm gate was meant to stop; if this climbs, the
    # seed vocabulary is wrong rather than the model being careless.
    added = [t for t in counts if t not in memory.SEED_TAGS]
    singles = [t for t in counts if counts[t] == 1]
    hygiene = (f"tags: {len(memory.SEED_TAGS)} standard, {len(added)} added"
               + (f" · {len(singles)} used only once" if singles else ""))
    return (
        f"{s['active']} active · {s['expired']} expired · {s['total']} ever\n"
        f"never recalled: {s['never']} of the active facts{pct}\n"
        f"by scope:  {scopes}\n"
        f"by source: {sources}\n"
        f"{hygiene}"
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
