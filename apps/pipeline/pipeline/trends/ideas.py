"""Turn scored trend signals into Reel ideas for the owner to approve.

Uses structured outputs rather than parsing prose, so a malformed response is a
validation error at the boundary instead of a half-populated row in the
approval queue.
"""

from __future__ import annotations

import logging

import anthropic
from pydantic import BaseModel, Field

from pipeline.trends.velocity import Signal

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"


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


SYSTEM = """You generate short-form video ideas for a non-profit's social channels.

You will be given observed trend signals. Each carries a ratio: how far that
piece of content outperformed its OWN author's median, not an absolute view
count. A high ratio means the format or subject is resonating, not that the
account is large.

Rules:
- Ground every idea in a supplied signal. Do not invent trends.
- The organisation's own remit comes first. A trending format we cannot speak
  to credibly is not an opportunity; say nothing rather than stretch.
- Never propose a claim the organisation would have to defend without evidence.
  Nothing medical, financial, legal or statistical unless the brief says it is
  ours to say.
- Hooks must be specific. "You won't believe" and "Here's why" are not hooks.
- Prefer fewer strong ideas over filling a quota."""


def generate_ideas(
    signals: list[Signal],
    niche_brief: str,
    *,
    count: int = 10,
    client: anthropic.Anthropic | None = None,
) -> list[ReelIdea]:
    """Draft ideas from signals.

    `niche_brief` describes what the organisation does, who it speaks to, and
    what it must not claim. It is required: without it the model has no basis
    for judging relevance and will produce generic content that wastes the
    owner's review time and the render budget.
    """
    if not niche_brief.strip():
        raise ValueError(
            "niche_brief is required. Idea generation without it produces generic "
            "content, and every idea costs the owner a review and possibly a render."
        )
    if not signals:
        return []

    client = client or anthropic.Anthropic()
    observed = "\n".join(
        f"- [{s.source}] ratio {s.ratio}x its author's median, engagement {s.engagement}, "
        f"{s.age_days:.1f} days old, keyword {s.keyword!r}: {s.title[:200]} ({s.source_url})"
        for s in signals[:40]
    )

    response = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"# What we do\n{niche_brief}\n\n"
                    f"# Observed signals\n{observed}\n\n"
                    f"Propose at most {count} reel ideas. Fewer is fine if the signals "
                    f"do not support more."
                ),
            }
        ],
        output_format=IdeaBatch,
    )

    batch = response.parsed_output
    if batch is None:
        log.error("idea generation returned no parseable batch (stop_reason=%s)", response.stop_reason)
        return []
    log.info("generated %d ideas from %d signals", len(batch.ideas), len(signals))
    return batch.ideas[:count]


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
