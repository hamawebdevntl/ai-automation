"""Turn scored trend signals into Reel ideas for the owner to approve.

Uses structured outputs rather than parsing prose, so a malformed response is a
validation error at the boundary instead of a half-populated row in the
approval queue.

Two providers can answer, chosen by `IDEA_LLM_PROVIDER`. The system prompt, the
output schema and the row mapping below are shared between them, so the setting
changes which model drafts and nothing else -- which is the only reason ideas
from the two are comparable, and the only reason switching mid-week does not
make last week's queue mean something different.
"""

from __future__ import annotations

import logging
import os
from itertools import zip_longest
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

from pipeline.trends.velocity import Signal

log = logging.getLogger(__name__)

CLAUDE = "claude"
GEMINI = "gemini"
PROVIDERS = (CLAUDE, GEMINI)

CLAUDE_MODEL = "claude-opus-5"

# How many scored signals reach the model. Enough to choose between, few
# enough that the strongest are not buried in the middle of a long list.
MAX_SIGNALS = 40

# Both providers get the same ceiling. It is generous because both models think
# before answering and the thinking is billed against this budget; a batch of
# ten ideas is nowhere near it.
MAX_TOKENS = 16000


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
) -> list[ReelIdea]:
    """Draft ideas from signals.

    `niche_brief` describes what the organisation does, who it speaks to, and
    what it must not claim. It is required: without it the model has no basis
    for judging relevance and will produce generic content that wastes the
    owner's review time and the render budget.

    `client` is the provider's own client, injected by tests. Which type it must
    be depends on `provider`, which is the price of one entry point for both.
    """
    if not niche_brief.strip():
        raise ValueError(
            "niche_brief is required. Idea generation without it produces generic "
            "content, and every idea costs the owner a review and possibly a render."
        )
    if not signals:
        return []

    observed = "\n".join(
        f"- [{s.source}] ratio {s.ratio}x its author's median, engagement {s.engagement}, "
        f"{s.age_days:.1f} days old, keyword {s.keyword!r}: {s.title[:200]} ({s.source_url})"
        for s in _spread(signals, MAX_SIGNALS)
    )
    prompt = (
        f"# What we do\n{niche_brief}\n\n"
        f"# Observed signals\n{observed}\n\n"
        f"Propose at most {count} reel ideas. Fewer is fine if the signals "
        f"do not support more."
    )

    # Explicit rather than an else-falls-back-to-Claude: a typo in the provider
    # name should not quietly draft with the other model, which would surface
    # as an unexpected bill rather than as an error.
    if provider == GEMINI:
        batch = _draft_gemini(prompt, model=model, client=client)
    elif provider == CLAUDE:
        batch = _draft_claude(prompt, model=model, client=client)
    else:
        raise ValueError(
            f"unknown idea provider {provider!r}. Expected one of: {', '.join(PROVIDERS)}."
        )

    if batch is None:
        return []
    log.info(
        "generated %d ideas from %d signals via %s",
        len(batch.ideas),
        len(signals),
        provider,
    )
    return batch.ideas[:count]


def _draft_claude(prompt: str, *, model: str | None, client: Any | None) -> IdeaBatch | None:
    client = client or anthropic.Anthropic()
    response = client.messages.parse(
        model=model or CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=IdeaBatch,
    )
    batch = response.parsed_output
    if batch is None:
        log.error(
            "idea generation returned no parseable batch (stop_reason=%s)",
            response.stop_reason,
        )
    return batch


def _draft_gemini(prompt: str, *, model: str | None, client: Any | None) -> IdeaBatch | None:
    """Draft with Gemini.

    The SDK import and the settings lookup are both function-local. Only this
    lane needs `google-genai`, and a test injecting a fake client should not
    need the SDK installed or a key set to exercise the parsing below.

    `config` is passed as a plain dict rather than a `types.GenerateContentConfig`
    for the same reason: the SDK coerces it, and building one would drag the
    import back to module scope.
    """
    if client is None or model is None:
        from pipeline.config import settings

        cfg = settings()
        model = model or cfg.gemini_model
        if client is None:
            from google import genai

            client = genai.Client(api_key=cfg.gemini_api_key)

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config={
            "system_instruction": SYSTEM,
            "max_output_tokens": MAX_TOKENS,
            "response_mime_type": "application/json",
            "response_schema": IdeaBatch,
            # We pass no tools, so automatic function calling has nothing to
            # do -- but the SDK enables it by default and warns about it on
            # every call, which is noise in the trend task's logs and a false
            # lead for anyone reading them during an incident.
            "automatic_function_calling": {"disable": True},
        },
    )
    return _coerce_gemini(response)


def _coerce_gemini(response: Any) -> IdeaBatch | None:
    """Get an `IdeaBatch` out of a Gemini response, or log why there isn't one.

    `response.parsed` is the schema-validated object when the SDK could build
    one, but it is `None` whenever generation stopped early -- and a safety
    stop is a real possibility here, because the prompt carries scraped TikTok
    titles nobody vetted. That case must be logged rather than returned as an
    empty batch: a silent empty queue looks identical to a quiet trend week.
    """
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, IdeaBatch):
        return parsed
    if isinstance(parsed, dict):
        try:
            return IdeaBatch.model_validate(parsed)
        except ValidationError as exc:
            log.error("gemini returned a batch that does not fit the schema: %s", exc)
            return None

    text = getattr(response, "text", None)
    if not text:
        log.error("gemini returned no content (finish_reason=%s)", _finish_reason(response))
        return None
    try:
        return IdeaBatch.model_validate_json(text)
    except ValidationError as exc:
        log.error("gemini returned unparseable JSON: %s", exc)
        return None


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        return str(getattr(candidates[0], "finish_reason", "unknown"))
    return str(getattr(response, "prompt_feedback", "unknown"))


def to_rows(ideas: list[ReelIdea], signals: list[Signal], platforms: list[str]) -> list[dict]:
    """Map ideas onto `ideas` table rows.

    Each idea is attributed to the strongest signal that shares its keyword, so
    the queue can show the reviewer what prompted it and link out to the source.
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
        rows.append(
            {
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
        )
    return rows


def _label(ratio: float) -> str:
    from pipeline.models import velocity_label

    return velocity_label(ratio).value
