"""What the scout is allowed to do, as read from `trend_settings`.

Every value here was a constant in this package or a variable in Terraform.
They are now a row the owner edits, which changes one thing about how they have
to be handled: they arrive from outside and can be wrong.

Two properties matter, and they pull in opposite directions.

A run must not be stopped by a bad value. Scouting is the better part of an
hour of paced browser work and it is the only thing that fills the queue, so
refusing to start because `videos_per_hashtag` came back as 5000 would turn a
typo into a day with no ideas. Everything here is therefore clamped, never
rejected.

But a clamp is a silent correction, and silent corrections are how a setting
comes to mean something different from what the page says. So the bounds below
are the same bounds the CHECK constraints in 20260906150000 enforce, and those
constraints are what actually hold: a value that needs clamping here could not
have been written by the app at all. This is the second line, for a row
written before the constraints existed, edited in a console, or read from a
database this build has never migrated.

`DEFAULTS` is the third line, and the one that keeps the pipeline honest about
its own history: every default is the constant the code used before this
module existed, so a missing row, an unreadable table or a null column all
degrade to the behaviour of the build before this one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults -- each one the value this code used before it was tunable.
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE_ENABLED = True
# cron(0 6 * * ? *) in infra/schedules.tf, until this change.
DEFAULT_SCHEDULE_HOUR_UTC = 6
DEFAULT_SCHEDULE_MINUTE_UTC = 0
# 0 = Sunday .. 6 = Saturday, matching Postgres `extract(dow)`. The daily cron
# had no day restriction, so every day is the default.
DEFAULT_SCHEDULE_DAYS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

# The one default that is not previous behaviour: there was no age filter at
# all. See the migration header for why 30 rather than unlimited.
DEFAULT_MAX_VIDEO_AGE_DAYS = 30

DEFAULT_MIN_PLAYS = 0
# Google Trends reports interest as 0-100 against a term's own peak, so no
# floor is the right default: the *rise* is what this source measures, and the
# ratio check already covers it.
DEFAULT_MIN_INTEREST = 0
DEFAULT_MIN_ENGAGEMENT_RATE = 0.0
# Was `Signal.is_worth_surfacing`, a hardcoded `ratio >= 1.5`.
DEFAULT_MIN_OUTLIER_RATIO = 1.5
DEFAULT_CAPTION_BLOCKLIST: tuple[str, ...] = ()
# Was ScoutConfig.videos_per_hashtag, passed as 30 by the runner.
DEFAULT_VIDEOS_PER_HASHTAG = 30
# Was IDEAS_PER_RUN, a task environment variable.
DEFAULT_IDEAS_PER_RUN = 10

# Rotation and the wall-clock budget are both new, and both default to off so
# that a run behaves exactly as it did before.
DEFAULT_HASHTAGS_PER_RUN: int | None = None
DEFAULT_RUN_BUDGET_MINUTES: int | None = None
# Was BASELINE_SAMPLE in tiktok.py.
DEFAULT_BASELINE_SAMPLE_SIZE = 12
# Was MIN_DELAY_S / MAX_DELAY_S in tiktok.py.
DEFAULT_PACING_MIN_SECONDS = 2.0
DEFAULT_PACING_MAX_SECONDS = 5.0
# Was DEDUP_WINDOW_DAYS in runner.py.
DEFAULT_DEDUP_WINDOW_DAYS = 14
# Was `expire_ideas(older_than_days=7)`. Note that its docstring talks about a
# three-week-old idea while the parameter said seven days; seven is what
# actually ran, so seven is the default here.
DEFAULT_IDEA_EXPIRY_DAYS = 7
# Was IDEA_LLM_PROVIDER, a Terraform variable on the task definition. Empty
# here rather than "claude" so that an unset column still defers to the
# environment, which is where this value lives on a database without the
# migration.
DEFAULT_IDEA_PROVIDER = ""

# ---------------------------------------------------------------------------
# Bounds. Mirrors the CHECK constraints in 20260906150000 exactly; see the
# reasoning for each one there rather than duplicating it.
# ---------------------------------------------------------------------------

BOUNDS: dict[str, tuple[float, float]] = {
    "schedule_hour_utc": (0, 23),
    "schedule_minute_utc": (0, 59),
    "max_video_age_days": (1, 365),
    "min_plays": (0, 100_000_000),
    "min_interest": (0, 100),
    "min_engagement_rate": (0.0, 0.5),
    "min_outlier_ratio": (1.0, 50.0),
    "videos_per_hashtag": (5, 100),
    "ideas_per_run": (1, 25),
    "hashtags_per_run": (1, 100),
    "run_budget_minutes": (5, 240),
    "baseline_sample_size": (3, 30),
    "pacing_min_seconds": (1.0, 30.0),
    "pacing_max_seconds": (1.0, 60.0),
    "dedup_window_days": (1, 90),
    "idea_expiry_days": (1, 90),
}

BLOCKLIST_MAX_ENTRIES = 200
BLOCKLIST_MIN_WORD_LENGTH = 2
BLOCKLIST_MAX_WORD_LENGTH = 60

PROVIDERS = ("claude", "gemini")

# Where signals come from.
#
# `tiktok` is gone from this tuple, though `tiktok.py` is still in the tree. It
# drove a browser through TikTok-Api, which is refused on every feed and pinned
# at a release that was already five months old; offering it was offering a
# source that reports success and finds nothing. `apify` is how TikTok signals
# are read now -- a hosted scraper somebody else keeps working -- and it brings
# Instagram with it, which had no trend source at all.
#
# The module stays because the code is sound and the arms race is not settled
# forever. Restoring it is adding a string here.
SOURCES = ("apify", "google_trends", "youtube")
DEFAULT_TREND_SOURCE = "google_trends"

# The sources that measure videos rather than search demand.
#
# A tuple rather than `!= "google_trends"` at each site, because the question
# is asked in five places and the negation stops being true the moment a second
# search-demand source arrives. Everything that separates a view count from an
# interest score keys off this: which filters apply, which vocabulary is read,
# and which half of the Settings card is shown.
VIDEO_SOURCES = ("apify", "youtube")

# Which platforms the Apify source scrapes. Both by default: they answer the
# same question about different audiences, and a run that scouts neither is a
# source selected with nothing to do.
APIFY_PLATFORMS = ("tiktok", "instagram")
DEFAULT_APIFY_PLATFORMS: tuple[str, ...] = APIFY_PLATFORMS


def is_video_source(source: str) -> bool:
    """Whether this source measures videos, and so has authors and view counts."""
    return source in VIDEO_SOURCES


@dataclass(frozen=True)
class ScoutControls:
    """One run's worth of settings, already validated."""

    schedule_enabled: bool = DEFAULT_SCHEDULE_ENABLED
    schedule_hour_utc: int = DEFAULT_SCHEDULE_HOUR_UTC
    schedule_minute_utc: int = DEFAULT_SCHEDULE_MINUTE_UTC
    schedule_days: tuple[int, ...] = DEFAULT_SCHEDULE_DAYS

    max_video_age_days: int = DEFAULT_MAX_VIDEO_AGE_DAYS
    min_plays: int = DEFAULT_MIN_PLAYS
    min_interest: int = DEFAULT_MIN_INTEREST
    min_engagement_rate: float = DEFAULT_MIN_ENGAGEMENT_RATE
    min_outlier_ratio: float = DEFAULT_MIN_OUTLIER_RATIO
    caption_blocklist: tuple[str, ...] = DEFAULT_CAPTION_BLOCKLIST
    videos_per_hashtag: int = DEFAULT_VIDEOS_PER_HASHTAG
    ideas_per_run: int = DEFAULT_IDEAS_PER_RUN

    hashtags_per_run: int | None = DEFAULT_HASHTAGS_PER_RUN
    hashtag_cursor: int = 0
    run_budget_minutes: int | None = DEFAULT_RUN_BUDGET_MINUTES
    baseline_sample_size: int = DEFAULT_BASELINE_SAMPLE_SIZE
    pacing_min_seconds: float = DEFAULT_PACING_MIN_SECONDS
    pacing_max_seconds: float = DEFAULT_PACING_MAX_SECONDS
    dedup_window_days: int = DEFAULT_DEDUP_WINDOW_DAYS
    idea_expiry_days: int = DEFAULT_IDEA_EXPIRY_DAYS
    idea_provider: str = DEFAULT_IDEA_PROVIDER

    trend_source: str = DEFAULT_TREND_SOURCE
    # Google Trends only. Empty means worldwide, which is the honest default:
    # nothing in the pipeline knows where this business sells.
    trend_geo: str = ""
    # Apify only. Which platforms its scrapers are pointed at.
    apify_platforms: tuple[str, ...] = DEFAULT_APIFY_PLATFORMS

    @property
    def run_budget_seconds(self) -> float | None:
        if self.run_budget_minutes is None:
            return None
        return float(self.run_budget_minutes) * 60.0


def from_row(row: dict[str, Any] | None) -> ScoutControls:
    """Read the settings row, filling and clamping as needed.

    A null or absent column takes the default rather than being an error: the
    columns arrived in one migration but a value can be added to that migration
    later, and a pipeline reading a database mid-rollout should behave like the
    build before it rather than refuse.
    """
    row = row or {}
    controls = ScoutControls(
        schedule_enabled=_boolean(row.get("schedule_enabled"), DEFAULT_SCHEDULE_ENABLED),
        schedule_hour_utc=_int(row, "schedule_hour_utc", DEFAULT_SCHEDULE_HOUR_UTC),
        schedule_minute_utc=_int(row, "schedule_minute_utc", DEFAULT_SCHEDULE_MINUTE_UTC),
        schedule_days=_days(row.get("schedule_days")),
        max_video_age_days=_int(row, "max_video_age_days", DEFAULT_MAX_VIDEO_AGE_DAYS),
        min_plays=_int(row, "min_plays", DEFAULT_MIN_PLAYS),
        min_interest=_int(row, "min_interest", DEFAULT_MIN_INTEREST),
        min_engagement_rate=_float(row, "min_engagement_rate", DEFAULT_MIN_ENGAGEMENT_RATE),
        min_outlier_ratio=_float(row, "min_outlier_ratio", DEFAULT_MIN_OUTLIER_RATIO),
        caption_blocklist=normalise_blocklist(row.get("caption_blocklist")),
        videos_per_hashtag=_int(row, "videos_per_hashtag", DEFAULT_VIDEOS_PER_HASHTAG),
        ideas_per_run=_int(row, "ideas_per_run", DEFAULT_IDEAS_PER_RUN),
        hashtags_per_run=_optional_int(row, "hashtags_per_run"),
        hashtag_cursor=max(0, _int(row, "hashtag_cursor", 0, bounded=False)),
        run_budget_minutes=_optional_int(row, "run_budget_minutes"),
        baseline_sample_size=_int(row, "baseline_sample_size", DEFAULT_BASELINE_SAMPLE_SIZE),
        pacing_min_seconds=_float(row, "pacing_min_seconds", DEFAULT_PACING_MIN_SECONDS),
        pacing_max_seconds=_float(row, "pacing_max_seconds", DEFAULT_PACING_MAX_SECONDS),
        dedup_window_days=_int(row, "dedup_window_days", DEFAULT_DEDUP_WINDOW_DAYS),
        idea_expiry_days=_int(row, "idea_expiry_days", DEFAULT_IDEA_EXPIRY_DAYS),
        idea_provider=_provider(row.get("idea_provider")),
        trend_source=_source(row.get("trend_source")),
        trend_geo=str(row.get("trend_geo") or "").strip().upper(),
        apify_platforms=_platforms(row.get("apify_platforms")),
    )

    # Cross-field, so it cannot be done per-column above. Jitter needs a range
    # to sit in, and `random.uniform(5, 2)` does not raise -- it silently
    # returns values from the range it was not given.
    if controls.pacing_max_seconds < controls.pacing_min_seconds:
        log.warning(
            "pacing_max_seconds (%.1f) is below pacing_min_seconds (%.1f); using the minimum for both",
            controls.pacing_max_seconds,
            controls.pacing_min_seconds,
        )
        controls = replace(controls, pacing_max_seconds=controls.pacing_min_seconds)

    return controls


# ---------------------------------------------------------------------------
# Coercion. Each of these logs when it corrects something, because a clamped
# setting is a page telling the owner one thing while the run does another.
# ---------------------------------------------------------------------------


def _boolean(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"true", "t", "1", "yes"}


def _clamp(name: str, value: float) -> float:
    lo, hi = BOUNDS[name]
    if value < lo or value > hi:
        clamped = min(max(value, lo), hi)
        log.warning("%s=%s is outside %s-%s; using %s", name, value, lo, hi, clamped)
        return clamped
    return value


def _int(row: dict[str, Any], name: str, default: int, *, bounded: bool = True) -> int:
    raw = row.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(float(str(raw)))
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return int(_clamp(name, value)) if bounded else value


def _optional_int(row: dict[str, Any], name: str) -> int | None:
    """A nullable setting, where null means the feature is off."""
    raw = row.get(name)
    if raw is None or raw == "":
        return None
    try:
        value = int(float(str(raw)))
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; treating it as unset", name, raw)
        return None
    return int(_clamp(name, value))


def _float(row: dict[str, Any], name: str, default: float) -> float:
    raw = row.get(name)
    if raw is None or raw == "":
        return default
    try:
        # `str` first: numeric columns come back from PostgREST as strings, and
        # float(Decimal) and float("0.0400") both need to work.
        value = float(str(raw))
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return float(_clamp(name, value))


def _days(raw: Any) -> tuple[int, ...]:
    """The days the schedule may fire, de-duplicated and ordered.

    An empty result takes the default rather than meaning "never". A schedule
    that is enabled and can never be due is indistinguishable from a dispatcher
    that is not running, which is the confusion this whole change exists to
    remove -- `schedule_enabled = false` is how "never" is said.
    """
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_SCHEDULE_DAYS
    days = set()
    for item in raw:
        try:
            day = int(float(str(item)))
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6:
            days.add(day)
    if not days:
        log.warning("schedule_days is empty or unusable; falling back to every day")
        return DEFAULT_SCHEDULE_DAYS
    return tuple(sorted(days))


def _provider(raw: Any) -> str:
    """The drafting model, or empty to defer to the environment."""
    name = str(raw or "").strip().lower()
    if not name:
        return DEFAULT_IDEA_PROVIDER
    if name not in PROVIDERS:
        log.warning("idea_provider=%r is not a provider; deferring to the environment", raw)
        return DEFAULT_IDEA_PROVIDER
    return name


def _platforms(raw: Any) -> tuple[str, ...]:
    """Which platforms the Apify source scrapes, in a fixed order.

    Ordered by `APIFY_PLATFORMS` rather than by however the row happened to
    store them, so two installs with the same platforms selected scout them in
    the same sequence -- which matters because a run budget can expire partway
    through and "we ran out of time" should not mean a different platform each
    time.

    An empty or unusable list takes the default rather than meaning "none", for
    the same reason `schedule_days` does: a source that is selected but can
    never scout is indistinguishable from a runner that is not working, and
    "none" is said by choosing a different source.
    """
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_APIFY_PLATFORMS
    wanted = {str(item).strip().lower() for item in raw if item}
    unknown = wanted - set(APIFY_PLATFORMS)
    if unknown:
        log.warning(
            "apify_platforms contains %s, which is not scrapeable; ignoring",
            ", ".join(sorted(unknown)),
        )
    chosen = tuple(p for p in APIFY_PLATFORMS if p in wanted)
    if not chosen:
        log.warning("apify_platforms is empty or unusable; scouting all of %s", ", ".join(APIFY_PLATFORMS))
        return DEFAULT_APIFY_PLATFORMS
    return chosen


def _source(raw: Any) -> str:
    """Where signals come from, falling back rather than guessing.

    An unknown value takes the default instead of raising. A row written by a
    build newer than this one -- naming a source this code has never heard of
    -- should degrade to scouting something, not to refusing to run.
    """
    name = str(raw or "").strip().lower()
    if not name:
        return DEFAULT_TREND_SOURCE
    if name not in SOURCES:
        log.warning("trend_source=%r is not a known source; using %s", raw, DEFAULT_TREND_SOURCE)
        return DEFAULT_TREND_SOURCE
    return name


def normalise_blocklist(raw: Any) -> tuple[str, ...]:
    """Clean the blocklist for matching.

    Lowercased because matching is case-insensitive, de-duplicated so a repeat
    does not double-count a rejection, and short entries dropped: matching is
    whole-word, but a one-character word still appears in a great many
    captions, and the failure mode is a run that rejects everything for a
    reason the report attributes to the blocklist without saying which word.
    """
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_CAPTION_BLOCKLIST
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        word = str(item or "").strip().lower()
        if not word or word in seen:
            continue
        if len(word) < BLOCKLIST_MIN_WORD_LENGTH or len(word) > BLOCKLIST_MAX_WORD_LENGTH:
            log.warning("ignoring blocklist entry %r: it is not 2-60 characters", item)
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= BLOCKLIST_MAX_ENTRIES:
            log.warning("blocklist is longer than %d entries; ignoring the rest", BLOCKLIST_MAX_ENTRIES)
            break
    return tuple(out)


# ---------------------------------------------------------------------------
# Caption matching.
# ---------------------------------------------------------------------------

# A hashtag is one token to a reader and several words to a writer: nobody
# types "#free course", so #freecourse has to be searched as a run of text
# rather than as words. Everywhere else, whole-word matching -- "course"
# must not reject "racecourse", which is the trap that makes a blocklist
# quietly empty a queue.
_HASHTAG_TOKEN = re.compile(r"[#@]\w+", re.UNICODE)


class BlocklistMatcher:
    """Whole-word caption matching, with hashtags treated as runs of text.

    Compiled once per run rather than per video: a blocklist of two hundred
    words against thirty videos a tag across sixteen tags is a lot of regex
    construction to repeat for a filter that is supposed to be the cheap one.
    """

    def __init__(self, words: Iterable[str]) -> None:
        self.words = tuple(words)
        # One alternation rather than a pattern per word, longest first so the
        # reported match is the most specific one.
        #
        # Lookarounds rather than `\b`, and that is not a style choice. `\b`
        # asserts a transition between a word and a non-word character, so it
        # can only ever match next to a word character -- which means a
        # blocked entry that ends in punctuation never matches anything at
        # all. `\bc\+\+\b` cannot match "c++ here", because the space after
        # the plus signs is not a word character for the boundary to sit
        # against. A blocklist entry that silently matches nothing is the worst
        # failure available here: the owner adds a word, sees the count stay
        # at zero, and concludes the feature does not work.
        #
        # `(?<!\w) ... (?!\w)` asks the question actually wanted -- that the
        # match is not butted against more word characters -- which rejects
        # "racecourse" for "course" and accepts "#course", "c++" and "20%".
        self._pattern = (
            re.compile(
                r"(?<!\w)(?:"
                + "|".join(re.escape(w) for w in sorted(self.words, key=len, reverse=True))
                + r")(?!\w)",
                re.IGNORECASE | re.UNICODE,
            )
            if self.words
            else None
        )

    def __bool__(self) -> bool:
        return bool(self.words)

    def matched(self, caption: str) -> str | None:
        """The blocked word this caption contains, or None."""
        if not self._pattern or not caption:
            return None

        hit = self._pattern.search(caption)
        if hit:
            return hit.group(0).lower()

        # Nothing matched as a word, so look inside hashtags and handles, where
        # the words were run together when they were typed.
        for token in _HASHTAG_TOKEN.findall(caption.lower()):
            for word in self.words:
                if word in token:
                    return word
        return None


def with_run_overrides(controls: ScoutControls, run: dict[str, Any] | None) -> ScoutControls:
    """Apply a single run's requested length over the saved settings.

    Only the two costs are overridable -- how long the run may take, and how
    many hashtags it scouts. Neither changes what qualifies as a signal, so two
    runs of different lengths still produce ideas judged by the same bars. That
    is the reason the list stops here: a button that could also loosen the
    outlier ratio would make the queue mean different things on different days.

    Null means "use the saved setting", which is what a scheduled run always
    sends. Values are clamped exactly as the settings are, because the settings
    page's bounds are about what the pipeline can survive, not about which form
    the number was typed into.
    """
    if not run:
        return controls

    budget = _optional_int(
        {"run_budget_minutes": run.get("override_run_budget_minutes")}, "run_budget_minutes"
    )
    tags = _optional_int(
        {"hashtags_per_run": run.get("override_hashtags_per_run")}, "hashtags_per_run"
    )

    changes: dict[str, Any] = {}
    if budget is not None:
        changes["run_budget_minutes"] = budget
    if tags is not None:
        changes["hashtags_per_run"] = tags
    if not changes:
        return controls

    log.info("this run overrides %s", ", ".join(f"{k}={v}" for k, v in changes.items()))
    return replace(controls, **changes)


def rotate(hashtags: list[str], cursor: int, per_run: int | None) -> tuple[list[str], int]:
    """The slice of the hashtag list this run should scout, and where to resume.

    Rotation exists because run length is roughly linear in the number of tags:
    sixteen tags at around four minutes each is over an hour of deliberate
    pacing, and the owner reviews one batch a day regardless. Scouting six a
    run covers the whole list every three days at a quarter of the session
    length.

    A cursor rather than a timestamp per tag, because the list is edited freely
    in the app and per-tag state would have to be reconciled against it on
    every save. The cursor is taken modulo the current length instead, so
    adding or removing a tag shifts where the next run starts and nothing
    breaks.

    Wraps, so `per_run` larger than the list scouts the whole list once rather
    than repeating tags -- the alternative is paying twice for one feed.
    """
    if not hashtags:
        return [], 0
    if per_run is None or per_run >= len(hashtags):
        return list(hashtags), 0

    start = cursor % len(hashtags)
    selected = [hashtags[(start + i) % len(hashtags)] for i in range(per_run)]
    return selected, (start + per_run) % len(hashtags)
