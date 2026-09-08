"""Turn scored trend signals into Reel ideas for the owner to approve.

Uses structured outputs rather than parsing prose, so a malformed response is a
validation error at the boundary instead of a half-populated row in the
approval queue.

Two providers can answer, chosen by `IDEA_LLM_PROVIDER`. The system prompt, the
output schema and the row mapping below are shared between them, so the setting
changes which model drafts and nothing else -- which is the only reason ideas
from the two are comparable, and the only reason switching mid-week does not
make last week's queue mean something different.

The call itself goes through `pipeline.llm.structured`, which is the one place
that knows how to ask either provider to fill a schema. This module owns what
is asked -- the prompt, the schema, and how the answer becomes queue rows.

A run started from a description (see `interpret`) asks for a little more: each
idea also says how well it serves that description and how it connects to it.
That is a separate schema and an addendum to the prompt rather than two
optional fields, so the ordinary run's request is byte-identical to what it
was before descriptions existed.
"""

from __future__ import annotations

import logging
import os
from itertools import zip_longest
from typing import Any

from pydantic import BaseModel, Field

from pipeline import llm
from pipeline.trends.velocity import Signal

log = logging.getLogger(__name__)

CLAUDE = llm.CLAUDE
GEMINI = llm.GEMINI
PROVIDERS = (CLAUDE, GEMINI)

CLAUDE_MODEL = llm.CLAUDE_MODEL

# How many scored signals reach the model. Enough to choose between, few
# enough that the strongest are not buried in the middle of a long list.
MAX_SIGNALS = 40

# Both providers get the same ceiling. It is generous because both models think
# before answering and the thinking is billed against this budget; a batch of
# ten ideas is nowhere near it.
MAX_TOKENS = 16000

# Below this, an idea drafted against a description is dropped rather than
# queued. The model is told the same number, so this is the backstop behind an
# instruction rather than the primary filter -- and it is what makes the drop
# countable in the run's breakdown.
MIN_RELEVANCE = 40


class ReelIdea(BaseModel):
    """One row in the Gate 1 queue.

    Field names match `ideas` columns so the insert is a direct mapping.
    """

    title: str = Field(description="A short internal name for the idea, under 80 characters.")
    hook: str = Field(description="The opening line of the reel. Must earn the first two seconds.")
    angle: str = Field(description="What this reel argues or shows, in one or two sentences.")
    rationale: str = Field(
        description="Why now: which observed signal motivates this, and what makes it "
        "relevant to us specifically rather than generically topical."
    )


class IdeaBatch(BaseModel):
    ideas: list[ReelIdea]


class RelevantReelIdea(ReelIdea):
    """A `ReelIdea` drafted against a description, and scored against it.

    A subclass rather than two optional fields on `ReelIdea`, so that the
    ordinary run's schema is unchanged and a described run cannot come back
    with the score missing.
    """

    relevance: int = Field(
        ge=0,
        le=100,
        description="How directly this idea serves what the person described. 100 means "
        f"exactly what they asked for. Below {MIN_RELEVANCE} must not be proposed.",
    )
    connection: str = Field(
        description="One sentence, addressed to the person, saying how this idea connects "
        "to what they described. Name the thing they mentioned."
    )


class RelevantIdeaBatch(BaseModel):
    ideas: list[RelevantReelIdea]


SYSTEM = """You generate short-form video ideas for an organisation's own social channels.

These reels exist to start conversations with potential customers. Reach is not
the goal and neither is follower growth: one viewer who recognises their own
problem and gets in touch is worth more than ten thousand who found it
interesting.

You will be given observed trend signals. Each carries a ratio: how far that
piece of content outperformed its OWN author's median, not an absolute view
count. A high ratio means the format or subject is resonating, not that the
account is large.

Rules:
- Ground every idea in a supplied signal. Do not invent trends.
- Borrow the FORMAT that is working, not the audience it was aimed at. Many
  signals come from people talking to their peers. That same structure pointed
  at the buyer described in the brief is usually the opportunity; repeating the
  peer-facing subject usually is not.
- Write for one identifiable person from the brief's audience, in the words
  they would use about their own work rather than in ours. If the best possible
  viewer for an idea is a competitor, a developer or a hobbyist, leave it out.
- The organisation's own remit, as given in the brief below, comes first. A
  trending format we cannot speak to credibly is not an opportunity; say
  nothing rather than stretch.
- Never propose a claim the organisation would have to defend without evidence.
  Nothing medical, financial, legal or statistical unless the brief says it is
  ours to say.
- Hooks must be specific. "You won't believe" and "Here's why" are not hooks.
  The strongest ones name the viewer's own situation back to them.
- Prefer fewer strong ideas over filling a quota."""

# Appended to SYSTEM only when a run was started from a description. Kept apart
# so that the ordinary run's request does not change by a character.
SEARCH_ADDENDUM = f"""This run was started by a person describing what they are working on. Their description is given below the brief.

- Every idea must serve that description, not merely the organisation's brief. The brief says what we may credibly speak to; the description says what this person wants right now.
- Score each idea's relevance from 0 to 100: how directly it serves what they described. Do not propose anything below {MIN_RELEVANCE}.
- Write `connection` to the person, in one sentence, naming the thing they mentioned that this idea speaks to.
- Fewer ideas, or none, is the right answer when the signals do not connect to the description. Say nothing rather than stretch."""


def _spread(signals: list[Signal], limit: int) -> list[Signal]:
    """Take `limit` signals without letting one hashtag crowd out the others.

    `signals` arrives sorted by ratio, so the obvious thing is to slice the top
    off the front. But ratio is measured against each author's own median, and
    one hashtag whose authors post inconsistently produces high ratios cheaply
    -- enough of them to fill the slice on its own. The batch then reads as one
    corner of the audience.

    That is worst exactly when the hashtags have been chosen to span several
    industries, which is the case by default: the spread we paid several
    minutes of scouting for would be thrown away here, at the last step before
    the model sees anything, and the queue would give no hint it had happened.

    So: strongest first within each keyword, one keyword at a time, round and
    round until full. The single best signal overall is still first.
    """
    by_keyword: dict[str, list[Signal]] = {}
    for signal in signals:
        by_keyword.setdefault(signal.keyword.lower(), []).append(signal)
    rounds = zip_longest(*by_keyword.values())
    return [s for round_ in rounds for s in round_ if s is not None][:limit]


def resolve_provider(name: str | None, *, gemini_api_key: str = "") -> str:
    """Normalise the configured provider and check its credential is present.

    Called before scouting rather than at the point of use. Scouting drives a
    real browser for several minutes, and finding out afterwards that the key
    for the selected provider is missing throws that whole run away for a
    reason that was knowable at the start.
    """
    provider = (name or CLAUDE).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(
            f"IDEA_LLM_PROVIDER={name!r} is not a provider. "
            f"Expected one of: {', '.join(PROVIDERS)}."
        )
    if provider == GEMINI and not gemini_api_key.strip():
        raise RuntimeError(
            "IDEA_LLM_PROVIDER=gemini but GEMINI_API_KEY is empty. "
            "A free key comes from https://aistudio.google.com/app/apikey."
        )
    if provider == CLAUDE and not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "IDEA_LLM_PROVIDER=claude but ANTHROPIC_API_KEY is not set. "
            "Set it, or switch to IDEA_LLM_PROVIDER=gemini."
        )
    return provider


def generate_ideas(
    signals: list[Signal],
    niche_brief: str,
    *,
    count: int = 10,
    provider: str = CLAUDE,
    model: str | None = None,
    client: Any | None = None,
    description: str | None = None,
) -> list[ReelIdea]:
    """Draft ideas from signals.

    `niche_brief` describes what the organisation does, who it speaks to, and
    what it must not claim. It is required: without it the model has no basis
    for judging relevance and will produce generic content that wastes the
    owner's review time and the render budget.

    `client` is the provider's own client, injected by tests. Which type it must
    be depends on `provider`, which is the price of one entry point for both.

    `description` is what the person who started this run said they were
    working on, when it was started that way. It changes the request in two
    ways: the model is told to serve it and to score each idea against it, and
    the answer comes back as `RelevantReelIdea`s, best fit first. Without it
    the request is exactly what it was before descriptions existed.
    """
    if not niche_brief.strip():
        raise ValueError(
            "niche_brief is required. Idea generation without it produces generic "
            "content, and every idea costs the owner a review and possibly a render."
        )
    if not signals:
        return []

    # Explicit rather than an else-falls-back-to-Claude: a typo in the provider
    # name should not quietly draft with the other model, which would surface
    # as an unexpected bill rather than as an error.
    if provider not in PROVIDERS:
        raise ValueError(
            f"unknown idea provider {provider!r}. Expected one of: {', '.join(PROVIDERS)}."
        )

    observed = "\n".join(
        f"- [{s.source}] ratio {s.ratio}x its author's median, engagement {s.engagement}, "
        f"{s.age_days:.1f} days old, keyword {s.keyword!r}: {s.title[:200]} ({s.source_url})"
        for s in _spread(signals, MAX_SIGNALS)
    )
    described = (description or "").strip()
    sections = [f"# What we do\n{niche_brief}"]
    if described:
        sections.append(f"# What the person is working on\n{described}")
    sections.append(f"# Observed signals\n{observed}")
    sections.append(
        f"Propose at most {count} reel ideas. Fewer is fine if the signals do not support more."
    )
    prompt = "\n\n".join(sections)

    system = SYSTEM + "\n\n" + SEARCH_ADDENDUM if described else SYSTEM
    schema: type[BaseModel] = RelevantIdeaBatch if described else IdeaBatch

    batch = _draft(prompt, system=system, schema=schema, provider=provider, model=model, client=client)
    if batch is None:
        return []

    ideas: list[ReelIdea] = list(batch.ideas)
    if described:
        # Best fit first, so the ceiling below keeps the strongest matches
        # rather than the first ones the model happened to write down.
        ideas.sort(key=lambda idea: getattr(idea, "relevance", 0), reverse=True)

    log.info(
        "generated %d ideas from %d signals via %s%s",
        len(ideas),
        len(signals),
        provider,
        " against a description" if described else "",
    )
    return ideas[:count]


def _draft(
    prompt: str,
    *,
    system: str,
    schema: type[BaseModel],
    provider: str,
    model: str | None,
    client: Any | None,
) -> Any | None:
    """One structured call, or None with the reason logged.

    `llm.structured` raises when the model returns nothing usable. Here that
    becomes an empty batch rather than a failed run, as it always has: a safety
    stop on a batch of scraped titles is a real possibility, and it should
    cost the run its ideas, not its report. It is logged at error rather than
    returned silently, because a silent empty queue looks identical to a quiet
    trend week.
    """
    try:
        return llm.structured(
            provider,
            system,
            prompt,
            schema,
            client=client,
            model=model,
            max_tokens=MAX_TOKENS,
            thinking=True,
        )
    except llm.LlmError as exc:
        log.error("idea generation returned nothing usable: %s", exc)
        return None


def split_relevant(
    ideas: list[ReelIdea], *, floor: int = MIN_RELEVANCE
) -> tuple[list[ReelIdea], list[ReelIdea]]:
    """(kept, dropped), by the relevance floor.

    An idea without a score is always kept: it was drafted against no
    description, so there is nothing to judge it against. The dropped list is
    returned rather than discarded so the run can count it as its own stage.
    """
    kept: list[ReelIdea] = []
    dropped: list[ReelIdea] = []
    for idea in ideas:
        relevance = getattr(idea, "relevance", None)
        (kept if relevance is None or relevance >= floor else dropped).append(idea)
    return kept, dropped


def to_rows(
    ideas: list[ReelIdea],
    signals: list[Signal],
    platforms: list[str],
    *,
    run_id: str | None = None,
) -> list[dict]:
    """Map ideas onto `ideas` table rows.

    Each idea is attributed to the strongest signal that shares its keyword, so
    the queue can show the reviewer what prompted it and link out to the source.

    `run_id` is the run that drafted them. When given, each row carries it,
    with the relevance score and connection sentence for ideas that have one.
    When it is not, the rows are shaped exactly as they were before those
    columns existed -- which is what lets a worker ahead of its database keep
    inserting rather than fail on a column PostgREST has never heard of.
    """
    by_keyword: dict[str, Signal] = {}
    for signal in sorted(signals, key=lambda s: s.ratio, reverse=True):
        by_keyword.setdefault(signal.keyword.lower(), signal)

    rows = []
    for idea in ideas:
        haystack = f"{idea.title} {idea.angle} {idea.rationale}".lower()
        match = next(
            (sig for kw, sig in by_keyword.items() if kw in haystack),
            signals[0] if signals else None,
        )
        row = {
            "title": idea.title[:200],
            "hook": idea.hook,
            "angle": idea.angle,
            "rationale": idea.rationale,
            "source": match.source if match else None,
            "source_url": match.source_url if match else None,
            "trend_keyword": match.keyword if match else None,
            "velocity_ratio": match.ratio if match else None,
            "velocity_label": _label(match.ratio) if match else None,
            "target_platforms": platforms,
            "status": "pending",
        }
        if run_id is not None:
            row["trend_run_id"] = run_id
            row["relevance"] = getattr(idea, "relevance", None)
            row["connection"] = (getattr(idea, "connection", None) or "").strip() or None
        rows.append(row)
    return rows


def _label(ratio: float) -> str:
    from pipeline.models import velocity_label

    return velocity_label(ratio).value
