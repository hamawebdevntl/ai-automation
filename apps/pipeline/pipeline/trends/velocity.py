"""Baseline-relative trend scoring.

The mistake this avoids is chasing absolute view counts. A million views from a
channel that always gets a million is not a signal; fifty thousand from a
channel that normally gets five thousand is. So everything here is measured
against the source's *own* recent history.

ViralMint's implementation was the reference for the idea -- it splits a Google
Trends window and compares the recent portion to the earlier one. It is
AGPL-3.0 and a read-only reference, so this is our own code; only the approach
is borrowed, extended with per-author baselines and an age adjustment.

Pure functions throughout, so this is testable without touching a network.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

# A brand-new video has not had time to accumulate, so comparing its raw count
# against a baseline of mature videos understates it. Below this age we
# extrapolate; above it we take the count at face value.
YOUNG_DAYS = 7.0
# Guards against a single freak result dominating a batch. Also the ceiling on
# `min_outlier_ratio` in the settings: ratios are clamped here, so a threshold
# above this could never be met by anything.
MAX_RATIO = 50.0
# The bar when nobody has set one. Below 1.5x its own author's norm a video is
# not outperforming anything. This was the only bar there was until it became
# `trend_settings.min_outlier_ratio`; it survives as the default that keeps a
# run behaving the same way on a database that has no such column.
DEFAULT_MIN_RATIO = 1.5


@dataclass(frozen=True)
class Signal:
    """One scored candidate."""

    source: str
    source_url: str
    title: str
    keyword: str
    plays: int
    ratio: float
    engagement: float
    age_days: float

    @property
    def is_worth_surfacing(self) -> bool:
        """Whether this clears the default bar.

        Kept for callers with no settings to hand -- a script, a notebook, a
        test that cares about scoring rather than about configuration. The
        scheduled run does not use it: it calls `clears` with the owner's own
        threshold, which is the whole point of the setting existing.
        """
        return self.clears(min_ratio=DEFAULT_MIN_RATIO)

    def clears(self, *, min_ratio: float, min_engagement: float = 0.0) -> bool:
        """Whether this signal is worth a place in the batch.

        Both bars in one method because they are one decision and because
        reading them apart invites checking one and forgetting the other.

        A ratio at or below zero never clears, whatever the threshold: it means
        the author's own median could not be established, so the number is an
        absence rather than a low score. `min_ratio` is floored at 1.0 by both
        the column constraint and `controls`, so this only matters for a caller
        that built a Signal by hand.
        """
        if self.ratio <= 0:
            return False
        return self.ratio >= min_ratio and self.engagement >= min_engagement


def baseline(play_counts: list[int]) -> float:
    """The source's normal performance.

    Median rather than mean: one viral hit in an author's recent history would
    drag a mean up far enough to hide the next one, which is the exact opposite
    of what this is for.
    """
    usable = [c for c in play_counts if c and c > 0]
    if not usable:
        return 0.0
    if len(usable) < 3:
        # Too few samples for a median to mean much; be conservative and use
        # the largest, so we under-report rather than cry wolf.
        return float(max(usable))
    return float(statistics.median(usable))


def age_days(created: datetime | None, *, now: datetime | None = None) -> float:
    if created is None:
        return YOUNG_DAYS
    now = now or datetime.now(timezone.utc)
    # TikTok-Api builds create_time with datetime.fromtimestamp(), which is
    # naive local time. Treat a naive value as UTC rather than crashing.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 86400.0)


def outlier_ratio(plays: int, source_baseline: float, age: float) -> float:
    """How far above its source's norm this is, adjusted for how new it is."""
    if source_baseline <= 0:
        return 0.0
    ratio = plays / source_baseline
    if age < YOUNG_DAYS:
        # Extrapolate a young video to what it would reach at YOUNG_DAYS at its
        # current rate. Floored at half a day so a fresh upload cannot produce
        # an absurd multiplier.
        ratio *= YOUNG_DAYS / max(age, 0.5)
    return round(min(ratio, MAX_RATIO), 2)


def engagement_rate(plays: int, likes: int, comments: int, shares: int) -> float:
    """Interaction per view.

    A high view count with almost no interaction usually means paid promotion or
    a bot-inflated count, and is not something to imitate.
    """
    if plays <= 0:
        return 0.0
    return round((likes + comments + shares) / plays, 4)


def window_velocity(values: list[float], recent_fraction: float = 0.25) -> float:
    """Compare the tail of a series to the earlier part.

    Used for Google Trends interest-over-time, where there is no per-author
    notion at all -- only the keyword's own history.
    """
    if len(values) < 4:
        return 1.0
    split = max(1, int(len(values) * (1 - recent_fraction)))
    earlier, recent = values[:split], values[split:]
    base = sum(earlier) / len(earlier)
    current = sum(recent) / len(recent)
    if base <= 0:
        return 1.0 if current <= 0 else MAX_RATIO
    return round(min(current / base, MAX_RATIO), 2)


def coerce_count(raw: object) -> int:
    """TikTok's stats are an untyped passthrough.

    The library reads `statsV2` in preference to `stats`, and statsV2 returns
    its numbers as strings, so every count has to be coerced defensively.
    """
    if raw is None:
        return 0
    try:
        return int(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return 0
