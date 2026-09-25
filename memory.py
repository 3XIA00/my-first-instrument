"""The memory store — Postgres on Neon, one fact per row.

Why a database and not a JSON file: Render's free tier has an ephemeral
filesystem, so a file resets on every redeploy. The deeper reason is that a
file would give us TWO memories — one on the laptop where the CLI Claude talks
to it, one on Render where claude.ai does — and neither would know what the
other learned. One database collapses them into one memory.

Design rationale for every choice here lives in docs/DESIGN-memory.md.
"""
import os
import re
from contextlib import contextmanager
from datetime import timedelta

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()

# Scopes are named by REASON, not duration. The model can answer "is this about
# the user, about this project, or about today's task?" far more reliably than
# "does this expire in 30 or 90 days?" (DESIGN-memory.md, "Scope and expiry")
SCOPES = {
    "task": timedelta(days=7),
    "term": timedelta(days=180),
    "stable": None,
}
SOURCES = ("user_stated", "user_confirmed", "inferred")

# The tag vocabulary is seeded, not fixed. These mirror the SAVE categories in
# remember()'s docstring one-for-one, so deciding a fact is worth saving also
# decides its tag — one judgment instead of two. The model may add a tag, but
# only by confirming it, which costs a round trip. Reuse is free; coining is
# not, and that asymmetry is what keeps the vocabulary small.
SEED_TAGS = ("identity", "preferences", "corrections", "decisions",
             "commitments")
MAX_TAGS = 3

MAX_FACT_CHARS = 200
DUPLICATE_THRESHOLD = 0.8

# Task state wearing a disguise. The docstring is a request; this is the rule.
BANNED = re.compile(
    r"\b(currently|right now|at the moment|today we|we'?re working on|"
    r"just now|so far|this session)\b",
    re.I,
)


@contextmanager
def db():
    """One connection per call — deliberately.

    Neon scales to zero after 5 minutes idle, which drops pooled connections.
    A long-lived pool would need reconnect logic for a server that might see
    three calls an hour. Per-call costs ~50ms and cannot go stale.
    """
    # .strip() is not defensive padding — pasting a connection string into a
    # dashboard field routinely carries a trailing newline from the copy, and
    # libpq reads it as part of the LAST parameter: channel_binding becomes
    # "require\n" and every connection fails with a message that points at the
    # value rather than the whitespace.
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Locally: put it in .env. "
            "On Render: Environment -> Add environment variable."
        )
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def search_terms(query: str) -> list:
    """Query -> bare alphanumeric words, safe to hand to to_tsquery().

    The regex is also the sanitiser: anything that could be tsquery syntax
    (&, |, !, parentheses, quotes) simply isn't matched, so a user's stray
    punctuation can never become an operator or a syntax error.
    """
    return re.findall(r"[a-z0-9]+", query.lower())


def _similarity(a: str, b: str) -> float:
    """Jaccard overlap. Crude, but it catches the failure we actually see:
    the same fact re-saved in a slightly different sentence each session."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def validate(fact: str, scope: str, source: str) -> str | None:
    """Mechanical gates. Returns a refusal message, or None if the write is OK.

    These are the rules we enforce rather than request. Prose in a docstring
    gets ~70% compliance; code gets 100%.
    """
    if scope not in SCOPES:
        return f"scope must be one of {', '.join(SCOPES)} — got {scope!r}."
    if source not in SOURCES:
        return f"source must be one of {', '.join(SOURCES)} — got {source!r}."
    fact = fact.strip()
    if len(fact) < 10:
        return "Too short to be a self-contained fact."
    if len(fact) > MAX_FACT_CHARS:
        return (
            f"Too long ({len(fact)} chars, limit {MAX_FACT_CHARS}). Facts are ONE "
            "self-contained sentence. Split this into separate facts — they "
            "expire on different schedules and are recalled by different queries."
        )
    hit = BANNED.search(fact)
    if hit:
        return (
            f"Contains {hit.group(0)!r}, which marks this as task state rather "
            "than a durable fact. If it's only true right now, the context "
            "window already has it."
        )
    return None


def vocabulary(conn) -> dict:
    """Every tag currently carried by an unexpired fact, with its count.

    Derived from the facts themselves rather than a registry, so a tag stops
    existing once the last fact using it expires. The vocabulary decays like
    everything else here.
    """
    rows = conn.execute(
        """SELECT unnest(tags) AS t, count(*) AS n FROM facts
           WHERE expires_at IS NULL OR expires_at > now()
           GROUP BY 1"""
    ).fetchall()
    return {r["t"]: r["n"] for r in rows}


def render_vocabulary(counts: dict) -> str:
    """Newline-delimited, no column padding — alignment spaces cost tokens and
    carry no information. The standard/added split is the part that does: it
    tells the reader which tags are the taxonomy and which were exceptions."""
    lines = ["Standard tags:"]
    lines += [f"{t} {counts.get(t, 0)}" for t in SEED_TAGS]
    added = sorted((t for t in counts if t not in SEED_TAGS),
                   key=lambda t: (-counts[t], t))
    if added:
        lines += ["", "Added previously:"]
        lines += [f"{t} {counts[t]}" for t in added]
    return "\n".join(lines)


def check_tags(conn, tags: list, new_tags: list):
    """Normalise tags and gate unfamiliar ones. Returns (tags, refusal|None).

    Deliberately pay-on-failure: making the model call list_tags() before
    every save would cost a round trip even when it was going to choose
    correctly. This costs one only when there is actually a problem.
    """
    tags = [t.strip().lower() for t in tags if t and t.strip()]
    if not tags:
        return tags, ("no tags given. Choose 1-%d and call again.\n\n%s"
                      % (MAX_TAGS, render_vocabulary(vocabulary(conn))))
    if len(tags) > MAX_TAGS:
        return tags, f"{len(tags)} tags given; {MAX_TAGS} is the limit."

    confirmed = {t.strip().lower() for t in (new_tags or [])}
    counts = vocabulary(conn)
    known = set(SEED_TAGS) | set(counts)
    unknown = [t for t in tags if t not in known and t not in confirmed]
    if unknown:
        which = ", ".join(repr(t) for t in unknown)
        return tags, (
            f"{which} not in the vocabulary yet.\n\n"
            f"{render_vocabulary(counts)}\n\n"
            f"Pick from those and call again — a near-synonym of an existing "
            f"tag is what makes a store unsearchable. If this genuinely needs "
            f"a new tag, call again with new_tags={unknown} to confirm it."
        )
    return tags, None


def find_duplicate(conn, fact: str):
    rows = conn.execute(
        "SELECT id, text FROM facts WHERE expires_at IS NULL OR expires_at > now()"
    ).fetchall()
    for row in rows:
        score = _similarity(fact, row["text"])
        if score >= DUPLICATE_THRESHOLD:
            return row, score
    return None, 0.0
