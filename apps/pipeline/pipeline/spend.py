"""What a render costs, and whether there is budget left to start one.

Everything here is pure: a rate and a quantity in, dollars out. The rates
themselves, the ceilings and the verdict all live in Postgres --
`spend_rates`, `spend_caps` and `spend_block_reason()` -- so that Gate 1 in
the browser and the render step in the worker cannot come to different
conclusions about whether a style can be paid for. This module is only the
arithmetic that turns "0.04 per second" and "a 30-second reel" into $1.20.

Two decisions worth stating, because both are choices rather than accidents.

**Spend is recorded at submit, not at completion.** A ceiling that only learns
about money once the bill arrives cannot stop the next ten submits, and the
failure this feature exists to prevent is a runaway loop -- ten reels a day was
the plan; a loop does ten a minute. So a submitted render counts against the
cap immediately, and a generation that is later refused leaves an over-count
rather than a gap. Over-counting stops renders that would have been affordable;
under-counting bills for renders nobody authorised. Only one of those is
recoverable by an owner editing a number.

**A provider's own figure always wins.** `render_spend.source` records which
kind of number a row holds, because "$1.40, measured" and "$1.40, we think"
are different claims and the second must not be able to pass for the first.
HeyGen reports a wallet balance, so a presenter render is priced from the
delta; fal reports nothing at all, so its cost is derived from a per-second
rate and the duration we asked for.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Provider names, matching `spend_provider()` in SQL. The mapping from a
# style preset to one of these is not repeated here on purpose -- the pipeline
# reads it back from the `style_preset_spend` view, so there is one definition
# of "which provider does this style spend on" rather than two that can drift.
HEYGEN = "heygen"
FAL = "fal"
MPT = "mpt"

# The providers that can be asked what they charged. Everything else has to be
# derived from a rate, and cannot be submitted at all without one.
REPORTS_ITS_OWN_COST = frozenset({HEYGEN})

# The providers that make a resubmit free, and therefore bill once per
# production however many times the submit is retried:
#
#   * **MoneyPrinterTurbo**, because our fork accepts a caller-supplied task id
#     and we send the production id, so a resubmit returns the existing render.
#   * **HeyGen**, because `POST /v3/videos` takes an `Idempotency-Key` and we
#     send the production id, so a resubmit within 24 hours replays the original
#     response.
#
# fal is in neither camp: it has no idempotency key of any kind, so every
# successful `submit` is a fresh billed generation. One production can genuinely
# owe fal for two, which is why its charges are keyed by fal's own `request_id`
# in `render_spend.external_ref` while everything else keys on the production
# alone.
PROVIDER_DEDUPES_A_RESUBMIT = frozenset({HEYGEN, MPT})

# `render_spend.kind`.
KIND_RENDER = "render"
KIND_TTS = "tts"
KIND_TRANSCRIBE = "transcribe"

# From the drafting prompt in `llm.py`: "Aim for 30-60 seconds when read aloud:
# roughly 80-150 words per paragraph". That is the range the script is written
# to, and 2.5 words a second is what puts a script inside it -- so this is the
# same assumption the words were drafted under rather than a new one.
#
# Only ever used to price a render *before* it exists. Once HeyGen reports the
# finished video's duration, that figure replaces this one.
SPEAKING_WORDS_PER_SECOND = 2.5


class Unpriceable(RuntimeError):
    """No rate covers this provider and model.

    Raised rather than defaulted to zero. A render whose cost cannot be
    computed is a render that spends against a ceiling without moving it, which
    is the exact hole this feature closes -- and the realistic way in is an
    owner pointing `params.fal.model` at a premium tier, which is the case the
    issue costs at $1,500-5,400 a month.
    """


@dataclass(frozen=True)
class Rate:
    """One row of `spend_rates`."""

    provider: str
    model: str
    unit: str
    rate_usd: float

    @classmethod
    def from_row(cls, row: dict[str, object]) -> Rate:
        return cls(
            provider=str(row.get("provider") or ""),
            model=str(row.get("model") or ""),
            unit=str(row.get("unit") or ""),
            rate_usd=float(row.get("rate_usd") or 0.0),
        )

    def amount_usd(
        self,
        *,
        seconds: float | None = None,
        characters: int | None = None,
        clips: int | None = None,
    ) -> float:
        """Dollars for this much of whatever the rate is per.

        The caller passes every quantity it happens to know and this picks the
        one the unit asks for, so a change of unit in the database does not need
        a matching change at the call site. A unit whose quantity is missing is
        a caller bug rather than a free render, so it raises.
        """
        if self.unit == "render":
            return round(self.rate_usd, 4)
        if self.unit == "second":
            if seconds is None:
                raise Unpriceable(f"{self.provider}/{self.model} bills per second, with no duration")
            return round(self.rate_usd * max(0.0, float(seconds)), 4)
        if self.unit == "character":
            if characters is None:
                raise Unpriceable(f"{self.provider}/{self.model} bills per character, with no text")
            return round(self.rate_usd * max(0, int(characters)), 4)
        if self.unit == "clip":
            if clips is None:
                raise Unpriceable(f"{self.provider}/{self.model} bills per clip, with no clip count")
            return round(self.rate_usd * max(0, int(clips)), 4)
        raise Unpriceable(f"{self.provider}/{self.model} has unit {self.unit!r}, which means nothing here")


def speech_seconds(script: str) -> float:
    """How long a script takes to read aloud.

    Deliberately coarse. It prices a presenter render before HeyGen has made
    one, and a reel is thirty to sixty seconds -- so being a few seconds out
    moves the estimate by pennies, and the measured figure replaces it as soon
    as the render finishes.
    """
    words = len((script or "").split())
    if not words:
        return 0.0
    return round(words / SPEAKING_WORDS_PER_SECOND, 2)


def clip_count(seconds: float, clip_seconds: float) -> int:
    """How many clips cover a runtime, for a source billed per request."""
    if clip_seconds <= 0:
        return 0
    return max(1, math.ceil(seconds / clip_seconds)) if seconds > 0 else 0
