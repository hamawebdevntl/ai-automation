"""YouTube scouting: the one source that is an API rather than a scrape.

TikTok-Api lost its arms race and Apify rents somebody else's. This is neither
-- a documented, supported, versioned API -- and the price of that is a quota
instead of an uncertainty. What can go wrong here is knowable in advance, which
is a different kind of source to operate.

What it gives, and does not:

  * it gives *format with proof*, the same thing TikTok used to: a title, a
    view count, likes and comments, and a channel whose own recent uploads are
    cheap enough to read that a video can be scored against its own channel's
    median rather than against an absolute number.
  * it gives no share count at all. `engagement_rate` takes shares as a term,
    so the same engagement floor is marginally stricter here than on TikTok.
    Named in the run breakdown rather than left to look like a coincidence.
  * it gives search, not a feed. A keyword is a query, so this source reads
    `trend_keywords` -- the words a buyer types -- and not the hashtag list.

The two decisions worth knowing before reading the loop:

**Searches are the scarce resource, not units.** One search per keyword, and
the per-channel baselines deliberately never search. `MAX_SEARCHES_PER_RUN` is
a hard stop below the daily allowance, because the thing it protects is not
this run -- it is the owner's next button press, which would otherwise fail
tomorrow for a reason nothing recorded today.

**Blocked words are matched against the title only.** A YouTube description is
mostly boilerplate: affiliate links, chapter lists, "link to my free course
below" on channels whose videos are entirely fine. Matching a blocklist against
all of it would reject nearly everything for a reason the breakdown would
attribute to the blocklist without saying which word or where. The title is the
honest analogue of a TikTok caption, and it is what a viewer actually chose
from.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.trends import velocity as vel
from pipeline.trends.base import (
    Budget,
    ScoutOutcome,
    attribute_failure,
    filter_cheaply,
    opt_count,
    usable_stats,
)
from pipeline.trends.controls import BlocklistMatcher, ScoutControls
from pipeline.trends.report import ScoutReport

log = logging.getLogger(__name__)

# The ceiling on `search.list` calls in one run.
#
# Not a setting, because what it protects is not this run. The daily allowance
# is on the order of a hundred searches and does not reset until midnight
# US/Pacific -- which, for a schedule set in UTC, means a scheduled run and a
# manual one land on the same Pacific day and share it. A run that spent the
# whole allowance would leave the owner's next attempt failing for a reason
# nothing in today's report mentioned.
#
# Keywords past this go to `hashtags_skipped`, exactly as they do when the
# wall-clock budget expires, and rotation brings them round next time.
MAX_SEARCHES_PER_RUN = 20

# How many of a channel's recent uploads to read for its median.
#
# `controls.baseline_sample_size` is the real setting; this only caps what one
# `playlistItems` call can return anyway.
MAX_BASELINE_VIDEOS = 50

WATCH_URL = "https://www.youtube.com/watch?v={}"


@dataclass
class YouTubeConfig:
    keywords: list[str]
    controls: ScoutControls = field(default_factory=ScoutControls)
    # Injected by tests. The real one is built lazily so that importing this
    # module never needs a key.
    client: Any | None = None
    should_stop: Callable[[], bool] | None = None


def _client(config: YouTubeConfig) -> Any:
    if config.client is not None:
        return config.client
    from pipeline.clients.youtube import YouTubeClient

    return YouTubeClient()


@dataclass
class _Candidate:
    """One video, read out of the API's shape into the scoring's shape."""

    video_id: str
    channel_id: str
    keyword: str
    title: str
    age: float
    plays: int | None
    likes: int | None
    comments: int | None

    @property
    def url(self) -> str:
        return WATCH_URL.format(self.video_id)


def _published(raw: Any) -> datetime | None:
    """`snippet.publishedAt`, which is RFC-3339 with a trailing Z.

    The `Z` is the point of this function. `datetime.fromisoformat` did not
    accept it before Python 3.11, this package supports 3.10, and every
    timestamp this source reads ends in one -- so parsing it directly would fail
    on every video and `age_days` would answer YOUNG_DAYS for all of them,
    quietly turning the recency filter off and extrapolating every ratio.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _read(item: dict[str, Any], keyword: str) -> _Candidate | None:
    """Read one `videos.list` item, or None if it cannot be scored.

    A video with no id or no channel is not a candidate: the id is the URL the
    owner clicks and the channel is the denominator of the score. Neither can
    be substituted.
    """
    video_id = str(item.get("id") or "")
    snippet = item.get("snippet") or {}
    stats = item.get("statistics") or {}
    if not video_id or not isinstance(snippet, dict) or not snippet.get("channelId"):
        return None

    return _Candidate(
        video_id=video_id,
        channel_id=str(snippet["channelId"]),
        keyword=keyword,
        title=str(snippet.get("title") or "").strip(),
        age=vel.age_days(_published(snippet.get("publishedAt"))),
        # `statistics` counts arrive as strings, and the ones an uploader has
        # hidden are absent from the object entirely rather than zero. That is
        # observed behaviour rather than documented, which is exactly why these
        # go through `opt_count` and not `coerce_count`.
        plays=opt_count(stats.get("viewCount")) if isinstance(stats, dict) else None,
        likes=opt_count(stats.get("likeCount")) if isinstance(stats, dict) else None,
        comments=opt_count(stats.get("commentCount")) if isinstance(stats, dict) else None,
    )


def _pace(controls: ScoutControls) -> None:
    """Jittered delay between keywords.

    Not camouflage -- this is an authenticated API being used as intended -- but
    the setting exists and a source ignoring it would surprise anyone who had
    turned it up after being rate limited.
    """
    time.sleep(random.uniform(controls.pacing_min_seconds, controls.pacing_max_seconds))


def _baselines(
    client: Any,
    channel_ids: list[str],
    *,
    controls: ScoutControls,
    report: ScoutReport,
) -> dict[str, float]:
    """Each channel's own recent median view count.

    Three batched calls' worth of work and roughly one unit per channel: the
    uploads playlist id for fifty channels at a time, then that playlist, then
    the statistics for what it named. Deliberately never `search.list` -- see
    the module docstring.

    A channel this cannot read simply has no entry, and the caller records that
    as `no_baseline` rather than scoring the video against something borrowed.
    """
    from pipeline.clients.youtube import YouTubeError, YouTubeQuotaExceeded

    if not channel_ids:
        return {}

    playlists = client.uploads_playlists(channel_ids)
    wanted = max(3, min(controls.baseline_sample_size, MAX_BASELINE_VIDEOS))

    medians: dict[str, float] = {}
    for channel_id in channel_ids:
        playlist_id = playlists.get(channel_id)
        if not playlist_id:
            continue
        try:
            ids = client.playlist_video_ids(playlist_id, wanted)
            recent = client.videos(ids) if ids else []
        except YouTubeQuotaExceeded:
            # Not this channel's fault and not survivable. Stop building
            # baselines; whatever is already here still scores its videos.
            report.quota_exhausted = True
            log.warning("quota exhausted while reading channel baselines; stopping early")
            break
        except YouTubeError as exc:
            log.debug("no baseline for channel %s: %s", channel_id, exc)
            continue

        plays = [
            candidate.plays
            for candidate in (_read(item, "") for item in recent)
            if candidate is not None and candidate.plays is not None
        ]
        if plays:
            medians[channel_id] = vel.baseline(plays)
    return medians


def scout(config: YouTubeConfig) -> ScoutOutcome:
    """Search each keyword, score what it finds against each channel's median.

    Returns the same `ScoutOutcome` every source returns, so nothing downstream
    knows or cares that these signals came from an API rather than a feed.
    """
    from pipeline.clients.youtube import YouTubeError, YouTubeQuotaExceeded

    controls = config.controls
    blocklist = BlocklistMatcher(controls.caption_blocklist)
    budget = Budget(controls.run_budget_seconds)
    report = ScoutReport()
    client = _client(config)

    published_after = (
        (datetime.now(timezone.utc) - timedelta(days=controls.max_video_age_days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

    survivors: list[tuple[_Candidate, dict[str, int]]] = []
    searches = 0

    for index, keyword in enumerate(config.keywords):
        if config.should_stop is not None and config.should_stop():
            report.cancelled = True
            break
        if budget.exhausted:
            report.budget_exhausted = True
            report.hashtags_skipped += len(config.keywords) - index
            break
        if searches >= MAX_SEARCHES_PER_RUN:
            # The allowance, not the clock. Recorded as skipped rather than as
            # a failure: these keywords are fine and rotation will reach them.
            report.hashtags_skipped += len(config.keywords) - index
            log.info(
                "stopping at %d searches this run; %d keywords left for the next one",
                searches,
                len(config.keywords) - index,
            )
            break
        if index:
            _pace(controls)

        report.hashtags_scouted.append(keyword)
        try:
            searches += 1
            found = client.search_videos(
                keyword,
                limit=controls.videos_per_hashtag,
                published_after=published_after,
            )
            items = client.videos(found) if found else []
        except YouTubeQuotaExceeded as exc:
            # Terminal for the run. Everything already found is kept -- a
            # partial batch of real signals is worth more than an empty queue.
            report.quota_exhausted = True
            report.failed_hashtags.append({"hashtag": keyword, "error": str(exc)[:300]})
            log.warning("quota exhausted after %d searches; drafting from what was found", searches)
            break
        except YouTubeError as exc:
            report.failed_hashtags.append(
                {"hashtag": keyword, "error": f"{type(exc).__name__}: {exc}"[:300]}
            )
            continue

        # `search.list` can name a video that `videos.list` will not return --
        # deleted, made private, or region-blocked between the two calls. The
        # gap is counted rather than ignored, because a large one means the
        # search is finding things we cannot read rather than nothing at all.
        missing = len(found) - len(items)
        if missing > 0:
            report.seen += missing
            report.drop("no_metrics", missing)

        for item in items:
            candidate = _read(item, keyword)
            report.seen += 1
            if candidate is None:
                report.drop("no_metrics")
                continue

            stats = usable_stats(
                plays=candidate.plays,
                likes=candidate.likes,
                comments=candidate.comments,
                # YouTube reports no share count. Absent, not zero -- see the
                # module docstring and `base.usable_stats`.
                shares=None,
                controls=controls,
            )
            if stats is None:
                report.drop("no_metrics")
                continue

            rejected = filter_cheaply(
                age=candidate.age,
                stats=stats,
                # Title only, deliberately. See the module docstring.
                caption=candidate.title,
                controls=controls,
                blocklist=blocklist,
            )
            if rejected:
                report.drop(rejected)
                continue
            survivors.append((candidate, stats))

    signals: list[vel.Signal] = []

    # Only after the free filters, and only for channels still in the running.
    if survivors and not report.cancelled:
        channels = sorted({candidate.channel_id for candidate, _ in survivors})
        medians = _baselines(client, channels, controls=controls, report=report)

        for candidate, stats in survivors:
            median = medians.get(candidate.channel_id, 0.0)
            if median <= 0:
                # No denominator, so `outlier_ratio` would answer 0.0 and the
                # video would fail every threshold. Counted separately because
                # it is not a filter the owner can loosen.
                report.drop("no_baseline")
                continue

            signal = vel.Signal(
                source="youtube",
                source_url=candidate.url,
                title=candidate.title[:280] or candidate.keyword,
                keyword=candidate.keyword,
                plays=stats["plays"],
                ratio=vel.outlier_ratio(stats["plays"], median, candidate.age),
                engagement=vel.engagement_rate(**stats),
                age_days=round(candidate.age, 2),
            )

            failed = attribute_failure(signal, controls)
            if failed:
                report.drop(failed)
                continue
            signals.append(signal)

    signals.sort(key=lambda s: s.ratio, reverse=True)
    return ScoutOutcome(signals=signals, report=report)
