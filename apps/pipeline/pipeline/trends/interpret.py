"""Read a search description into something the scout can look for.

The saved keyword list is a standing brief. A description typed into the app
is a question asked once -- "I want to start a small home fitness brand for
busy parents" -- and the scout cannot search a sentence. This turns the
sentence into terms in the active source's vocabulary, and says back in one
line how it understood the request, so a misreading is visible before an hour
of scouting is spent on it.

One structured call, through `pipeline.llm.structured`, so the provider and
the key are whatever idea drafting already uses. The prompt and the schema
live here rather than in `llm.py` because they are about trends, as
`ideas.SYSTEM` is; `llm.py` owns the transport.

The terms come back normalised for the vocabulary they are meant for, mirroring
what the app does to a hashtag typed into Settings: what is stored on the run
is what was scouted, so a surprising batch can be traced back to an exact term.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from pipeline import llm
from pipeline.trends import sources
from pipeline.trends.controls import BOUNDS, ScoutControls

log = logging.getLogger(__name__)

# How many terms to ask for when the run carries no length of its own. Google
# Trends is paced at five seconds a term, YouTube spends a hundred quota units
# a search, and Apify bills per result per platform -- so a description that
# could fan out into twenty terms should not, by default. The run's own
# `override_hashtags_per_run` wins when the owner chose a length.
AI_TERMS_DEFAULT = 6
MAX_SUGGESTIONS = 3
# A sentence, a handful of terms and three rephrasings. Nowhere near this.
MAX_TOKENS = 2000


class Interpretation(BaseModel):
    """How the description was read. Shown back to the person, verbatim."""

    restatement: str = Field(
        description="One sentence restating what the person is working on and for whom, "
        "in plain words. It is shown back to them, so it should read as their own "
        "request rather than as a summary of it."
    )
    terms: list[str] = Field(
        description="What to search for, in the requested vocabulary. Fewer than the "
        "maximum is fine. Empty only when the description cannot be interpreted at all."
    )
    vague: bool = Field(
        description="True when the description is too short or too generic to search "
        "well -- a single word, or a broad field with no audience or product."
    )
    nudge: str | None = Field(
        default=None,
        description="When vague: one sentence naming the detail that would help most "
        "(who it is for, what it sells, what problem it solves, where). Null otherwise.",
    )
    suggestions: list[str] = Field(
        default_factory=list,
        description="Up to three complete rephrasings of the description that would "
        "broaden or sharpen the search, each usable as-is.",
    )


SYSTEM = """You turn a person's plain-language description of what they are working on into inputs a trend scout can search with, on behalf of an organisation whose remit is given in the brief.

Two vocabularies exist and they are not interchangeable:
- SEARCH TERMS are what somebody types into a search box: two to four words, lowercase, no operators, no hashtags, no quotes. "home workout for parents", not "#homeworkout".
- HASHTAGS are how a video is filed on TikTok or Instagram: one token, no spaces, no # sign. "busyparents", not "busy parents".
You will be told which one is wanted.

Rules:
- Restate the request in one sentence the person would nod at. Do not embellish it, and do not turn it into marketing copy.
- Terms must be what the described AUDIENCE actually searches for or tags right now, in their own words -- not the organisation's words for itself, and not the person's words for their goal. Someone starting a fitness brand for busy parents should get what busy parents search for, not "fitness brand".
- Stay inside the brief's remit. A term the organisation could not speak to credibly is not useful.
- Return at most the number asked for. Fewer is fine. Return none only if the description cannot be interpreted at all.
- Mark the description vague when it is roughly under eight words or names only a broad field. Then give one nudge sentence saying which detail would help most, and still return your best-effort terms -- a vague description is a reason to guess well, not to refuse.
- Offer up to three rephrasings, each a complete description that could be pasted in as-is, each broader or sharper than the original."""


def term_cap(controls: ScoutControls) -> int:
    """How many terms this run may ask for.

    The run's own length when the owner chose one -- `hashtags_per_run` already
    means "how many things to scout this run" -- and the default otherwise.
    Bounded by the same ceiling the settings page enforces, because the bound
    is about what a run can survive, not about which door the number came in.
    """
    chosen = controls.hashtags_per_run or AI_TERMS_DEFAULT
    return int(min(max(1, chosen), BOUNDS["hashtags_per_run"][1]))


def _vocabulary_sentence(vocabulary: str) -> str:
    if vocabulary == sources.HASHTAGS:
        return "hashtags (one token each, no spaces, no # sign)"
    return "search terms (two to four lowercase words each, as typed into a search box)"


def _prompt(description: str, niche_brief: str, source: sources.Spec, cap: int) -> str:
    noun = "hashtags" if source.vocabulary == sources.HASHTAGS else "search terms"
    return (
        f"# What we do\n{niche_brief.strip()}\n\n"
        f"# Active source\n{source.name} reads {_vocabulary_sentence(source.vocabulary)}.\n\n"
        f"# What the person is working on\n{description.strip()}\n\n"
        f"Return at most {cap} {noun}."
    )


def interpret(
    description: str,
    niche_brief: str,
    source: sources.Spec,
    *,
    cap: int,
    provider: str,
    model: str | None = None,
    client: Any | None = None,
) -> Interpretation:
    """One call. Terms come back normalised for `source.vocabulary` and cut to `cap`.

    Raises `llm.LlmError` when the model returns nothing usable. Deliberately
    not caught here: the alternative is scouting the saved list instead, which
    is a run that reports success and answers a question nobody asked.
    """
    raw = llm.structured(
        provider,
        SYSTEM,
        _prompt(description, niche_brief, source, cap),
        Interpretation,
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
    )
    terms = normalise_terms(raw.terms, source.vocabulary)[:cap]
    suggestions = [s.strip() for s in raw.suggestions if s and s.strip()][:MAX_SUGGESTIONS]
    nudge = (raw.nudge or "").strip() or None
    log.info(
        "read the description into %d %s for %s: %s",
        len(terms),
        source.vocabulary,
        source.name,
        ", ".join(terms) or "(nothing)",
    )
    return Interpretation(
        restatement=raw.restatement.strip(),
        terms=terms,
        vague=raw.vague,
        nudge=nudge,
        suggestions=suggestions,
    )


def normalise_terms(terms: list[str], vocabulary: str) -> list[str]:
    """Clean the model's terms the way the app cleans a typed one.

    Hashtags lose their `#`, their spaces and their case, because that is how
    the feed wants them (`normaliseHashtag` in the app does the same). Search
    terms keep their words and lose only surrounding and doubled whitespace,
    with duplicates dropped case-insensitively. Either way, what is stored on
    the run is exactly what was scouted.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        text = str(raw or "")
        if vocabulary == sources.HASHTAGS:
            clean = re.sub(r"\s+", "", text.strip().lstrip("#")).lower()
        else:
            clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def payload(
    interp: Interpretation,
    *,
    source: str,
    vocabulary: str,
    provider: str,
    model: str | None,
) -> dict[str, Any]:
    """The `trend_runs.interpretation` document.

    Carries which source and vocabulary the terms were made for, so the app
    can show a hashtag as a hashtag, and which model read the description, so
    a strange reading can be traced to the thing that produced it.
    """
    return {
        "restatement": interp.restatement,
        "terms": list(interp.terms),
        "vocabulary": vocabulary,
        "source": source,
        "vague": interp.vague,
        "nudge": interp.nudge,
        "suggestions": list(interp.suggestions),
        "provider": provider,
        "model": model,
    }
