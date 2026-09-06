"""Why a run came back with what it came back with.

`trend_runs` recorded four numbers: signals, drafted, inserted, suppressed. All
four are counted after scouting, so every way a run can end up empty arrived at
the same place -- `signals = 0` -- and the app had one sentence for three
completely different situations: a quiet week, filters set too tight, and a
scraper being served captchas.

That was survivable while the filters were constants nobody could change.
Filters the owner can tighten are filters the owner has to be able to watch
working, or the reasonable reading of an empty queue becomes "it's broken", and
the one state that really is broken stops being distinguishable.

So the counts are kept per stage, in the order the scout applies them, each
naming the setting responsible. Two invariants make the report answer the
question rather than just decorate it:

  * `seen` is counted before any filter runs. Filters can only ever reduce it,
    so `seen = 0` cannot be caused by any setting on the page -- it is always
    the scrape itself, and the app says so in those words.

  * every video that leaves the funnel is attributed to exactly one stage, so
    the video-level stage counts plus `surfaced` equal `seen`. A report that
    does not add up is worse than no report: it invites the owner to loosen a
    filter that was not the one rejecting anything.

The last stage is the exception, and is marked as such. Duplicate suppression
happens after drafting, so it is counted in ideas rather than in videos and
cannot be added to the others. It belongs in the report regardless -- from the
queue's point of view it is simply the final way something can fail to arrive
-- so each stage carries the `level` it is counted at, and the app totals the
two levels separately rather than presenting a sum that means nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline.trends.controls import ScoutControls

# The stages, in application order. The order is the diagnostic: the first
# stage with a large count is the one to loosen, and a stage can only reject
# what the stages above it passed.
#
# Keys are stable and stored; the labels travel with the payload so that a
# CloudWatch log line and a database row are both readable on their own, and so
# that a web build older than a new stage can still render it.
STAGES: tuple[tuple[str, str, str | None, str], ...] = (
    ("too_old", "Older than your recency limit", "max_video_age_days", "video"),
    ("too_few_plays", "Under your minimum view count", "min_plays", "video"),
    ("blocked_caption", "Caption contained a blocked word", "caption_blocklist", "video"),
    # Not a setting. The ratio is measured against the author's own median, so
    # an author whose recent videos cannot be read has no denominator and no
    # video of theirs can be scored. Given its own stage because it is the one
    # exclusion the owner cannot fix by loosening anything -- and because a
    # large count here is a scraping symptom, not a filter one.
    ("no_baseline", "Author's own history could not be read", None, "video"),
    ("below_ratio", "Did not outperform its author's median enough", "min_outlier_ratio", "video"),
    ("below_engagement", "Under your minimum engagement rate", "min_engagement_rate", "video"),
    # Counted in ideas, not videos -- see the module docstring. The `level`
    # is what stops the app adding it to the others.
    ("duplicate", "Too close to an idea already in the queue", "dedup_window_days", "idea"),
)

VIDEO_STAGES = tuple(key for key, _, _, level in STAGES if level == "video")

_SETTING_OF = {key: setting for key, _, setting, _level in STAGES}


@dataclass
class ScoutReport:
    """What the scout saw and dropped. Mutated during the run."""

    seen: int = 0
    dropped: dict[str, int] = field(default_factory=dict)
    hashtags_scouted: list[str] = field(default_factory=list)
    # A hashtag whose feed raised. Bot detection surfaces here as an
    # EmptyResponseException that TikTok-Api never retries, so this list is the
    # difference between "nothing was trending" and "we were turned away".
    failed_hashtags: list[dict[str, str]] = field(default_factory=list)
    # Set when the wall-clock budget ended scouting early. Without it, a
    # budgeted run looks like a run whose later hashtags were simply barren.
    budget_exhausted: bool = False
    # Set when an owner stopped the run from the app. Distinct from the budget
    # for the same reason `cancelled` is distinct from `failed`: one is a
    # limit being reached, the other is a person deciding.
    cancelled: bool = False
    hashtags_skipped: int = 0

    def drop(self, stage: str, count: int = 1) -> None:
        if stage not in _SETTING_OF:
            raise KeyError(f"unknown rejection stage {stage!r}; add it to STAGES")
        self.dropped[stage] = self.dropped.get(stage, 0) + count

    @property
    def total_dropped(self) -> int:
        """Videos rejected. Excludes the idea-level stages by construction."""
        return sum(count for stage, count in self.dropped.items() if stage in VIDEO_STAGES)


def payload(
    report: ScoutReport,
    controls: ScoutControls,
    *,
    surfaced: int,
    drafted: int,
    inserted: int,
    hashtags_configured: int,
) -> dict[str, Any]:
    """The `trend_runs.rejections` document.

    Every stage is included even at zero. A report that lists only what fired
    reads as an accusation; one that lists everything reads as a funnel, and
    the zeroes are how the owner sees that a filter they were about to loosen
    was not the problem.
    """
    stages = [
        {
            "key": key,
            "label": label,
            "level": level,
            "dropped": report.dropped.get(key, 0),
            "setting": setting,
            "value": _setting_value(controls, setting),
        }
        for key, label, setting, level in STAGES
    ]

    return {
        "seen": report.seen,
        "stages": stages,
        "surfaced": surfaced,
        "drafted": drafted,
        "inserted": inserted,
        "failed_hashtags": report.failed_hashtags,
        "budget_exhausted": report.budget_exhausted,
        "cancelled": report.cancelled,
        "hashtags_skipped": report.hashtags_skipped,
        # Both are recorded because they answer different questions: how many
        # tags were looked at, and whether the list is being rotated at all.
        "hashtags_scouted": list(report.hashtags_scouted),
        "hashtags_configured": hashtags_configured,
    }


def _setting_value(controls: ScoutControls, setting: str | None) -> Any:
    """The setting's value at the time of the run.

    Stored rather than looked up when the report is read, because settings
    change and a report is about a run that already happened. Without this, a
    breakdown read after a change explains the run using numbers that were not
    in force when it happened.
    """
    if setting is None:
        return None
    value = getattr(controls, setting, None)
    if isinstance(value, tuple):
        return list(value)
    return value


def summarise(document: dict[str, Any]) -> str:
    """One log line. What the owner sees in the app, for whoever reads the logs."""
    stages = document.get("stages") or []
    parts = [f"{s['dropped']} {s['key']}" for s in stages if s.get("dropped")]
    return (
        f"saw {document.get('seen', 0)} videos, "
        f"surfaced {document.get('surfaced', 0)}, "
        f"inserted {document.get('inserted', 0)}"
        + (f"; dropped {', '.join(parts)}" if parts else "; dropped nothing")
    )
