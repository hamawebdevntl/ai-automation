"""What every trend source shares.

Three sources now produce signals -- Apify's hosted scrapers, Google Trends and
the YouTube Data API -- and the value of the funnel they report into depends
entirely on their agreeing about it. A stage means "this many candidates died
here, and here is the setting that killed them"; if one source applies the
filters in a different order, or attributes a rejection to a different stage,
the breakdown stops being a diagnostic and becomes three separate ones sharing
a table.

So the order lives here rather than in each provider. It was previously private
to `tiktok.py`, which was fine while TikTok was the only video source and is
not fine now.

`ScoutOutcome` moved here for a smaller reason that was about to become a
larger one: it was defined in `tiktok.py`, so `gtrends.py` imported the TikTok
module in order to name the type it returns. That only worked because
`TikTokApi` is imported lazily inside the scout, and it would have quietly made
every new source depend on a module none of them use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pipeline.trends import velocity as vel
from pipeline.trends.controls import BlocklistMatcher, ScoutControls
from pipeline.trends.report import ScoutReport


@dataclass
class ScoutOutcome:
    """What a scout produced, and what it discarded getting there."""

    signals: list[vel.Signal] = field(default_factory=list)
    report: ScoutReport = field(default_factory=ScoutReport)


class Budget:
    """The wall-clock ceiling on scouting.

    A run cut short still drafts ideas from what it found; a run killed by the
    write-off sweeper produces nothing at all and leaves the button shut until
    somebody notices. So the budget stops the scout rather than the run, and
    says so in the report -- otherwise a budgeted run is indistinguishable from
    one whose later terms happened to be barren.
    """

    def __init__(self, seconds: float | None) -> None:
        self._deadline = (time.monotonic() + seconds) if seconds else None

    @property
    def exhausted(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline


def filter_cheaply(
    *,
    age: float,
    stats: dict[str, int],
    caption: str,
    controls: ScoutControls,
    blocklist: BlocklistMatcher,
) -> str | None:
    """The filters that cost nothing, or the stage that rejected this item.

    These run before any per-author baseline is fetched, and that ordering is
    the single largest thing deciding how long a run takes. Age, play count and
    the caption are already in the payload the source handed us. The outlier
    ratio is not: it is measured against the author's own median, which costs
    extra requests per author never seen before -- the dominant cost of a run by
    a wide margin, on every source that has authors.

    Checking the free filters first means tightening one makes a run *shorter*,
    which is the behaviour anyone adjusting them would assume.

    One consequence worth naming: an item rejected here contributes nothing to
    its author's baseline, so an author whose recent posts are all too old is
    no longer measured at all. That is the intended trade -- their median was
    only ever wanted in order to score an item we have already discarded.
    """
    # An unreadable timestamp is not evidence of age. `age_days` answers
    # YOUNG_DAYS for a missing one, which passes any limit of a week or more;
    # rejecting on a missing field would quietly discard whole feeds when a
    # source's payload shape changes.
    if age > controls.max_video_age_days:
        return "too_old"
    if stats["plays"] < controls.min_plays:
        return "too_few_plays"
    if blocklist and blocklist.matched(caption):
        return "blocked_caption"
    return None


def attribute_failure(signal: vel.Signal, controls: ScoutControls) -> str | None:
    """Which bar a scored signal failed, or None if it cleared them.

    Ratio first, deliberately, so that the stage counts and the survivors add
    up to `report.seen`. An item failing both bars is reported against the one
    it failed first -- double-counting would make the funnel stop summing, and a
    funnel that does not sum cannot be read.
    """
    if signal.clears(
        min_ratio=controls.min_outlier_ratio,
        min_engagement=controls.min_engagement_rate,
    ):
        return None
    return "below_ratio" if signal.ratio < controls.min_outlier_ratio else "below_engagement"


def opt_count(raw: object) -> int | None:
    """A counter, or None when the platform did not report one.

    `velocity.coerce_count` answers 0 for a missing value, which is right for
    TikTok-Api -- where the key is an untyped passthrough and absent means the
    library's shape changed -- and wrong for a hosted API, where "0 views" and
    "no view count published" are different facts and only one of them is a
    reason to apply a floor.

    Negatives come back as None too, and that is not defensive tidying: a
    hidden Instagram like count is published as `-1`. Passed through, it makes
    `engagement_rate` return a negative number, which fails every threshold and
    is reported as `below_engagement` -- a filter blamed for a value nobody
    published. That is the same class of mistake as measuring search interest
    against a view floor, one source along.
    """
    if raw is None:
        return None
    try:
        value = int(str(raw).strip() or 0)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def usable_stats(
    *,
    plays: int | None,
    likes: int | None,
    comments: int | None,
    shares: int | None,
    controls: ScoutControls,
) -> dict[str, int] | None:
    """The counters to score with, or None if the platform withheld what it takes.

    Two absences matter, and they matter differently.

    No view count is fatal to scoring: it is the denominator of the engagement
    rate and the numerator of the outlier ratio, and there is nothing to
    substitute. Instagram withholds it routinely and never had one for images.

    A missing interaction count is only fatal when the owner has set an
    engagement floor. With the floor at zero -- the default -- no bar is being
    applied, so carrying the absence as a zero changes no decision and keeps a
    perfectly good view-count signal. With a floor above zero it would decide
    the outcome, and a bar cannot be applied to a number that was not
    published.

    Returning None means "record this as `no_metrics`". It deliberately does
    not guess, and it deliberately does not blame a filter.
    """
    if plays is None:
        return None
    if controls.min_engagement_rate > 0 and (likes is None or comments is None):
        return None
    return {
        "plays": plays,
        "likes": likes or 0,
        "comments": comments or 0,
        # Instagram and the YouTube Data API report no share count at all, so
        # this is an absence carried as zero rather than a measurement. The
        # consequence is that the same engagement floor is marginally stricter
        # on those two than on TikTok, which reports shares. Named here because
        # it is invisible at the call site.
        "shares": shares or 0,
    }
