"""TikTok scouting.

TikTok-Api is a real browser driver, not an HTTP client: every session launches
Playwright, signs each request inside the page, and issues it via `fetch`. That
shapes everything here.

Three of its properties matter enough to design around:

  * It does no pacing whatsoever -- no sleeps, no token bucket, no concurrency
    cap -- and its iterators loop tightly, so pacing is entirely our problem.
  * `trending.videos()` hits the personalised FYP feed and has no cursor: it
    reissues the same request each iteration, so duplicates are expected. We
    prefer hashtag and sound feeds, which paginate properly.
  * Bot detection surfaces as an EmptyResponseException that the library
    raises immediately and never retries.

Every threshold below arrives in a `ScoutControls`, read from `trend_settings`.
The two things that used to be constants and now are not -- what qualifies as a
signal, and how long a run may take -- are the same two things that decide
whether this finishes at all, so the order the filters run in is load-bearing
and is described at `base.filter_cheaply`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pipeline.trends import velocity as vel

# The filter chain and the outcome type used to live here. They moved to
# `base.py` when a third source arrived: the order the filters run in is the
# funnel every source reports into, and three copies of it would drift.
# Imported under its old private name so the rest of this module reads unchanged.
from pipeline.trends.base import ScoutOutcome
from pipeline.trends.base import filter_cheaply as _filter_cheaply
from pipeline.trends.controls import BlocklistMatcher, ScoutControls
from pipeline.trends.report import ScoutReport

__all__ = ["ScoutConfig", "ScoutOutcome", "scout", "scout_hashtags"]

log = logging.getLogger(__name__)

# How many videos may be consumed between pacing delays when every one of them
# is being rejected by a cheap filter.
#
# The feed paginates roughly this many videos per request, so this is a stand-in
# for "once per page fetch". It exists because the cheap filters below
# deliberately do not sleep -- rejecting a video costs no request, so paying
# seconds for it is pure run length -- but the iterator's own next-page fetch
# does cost one, and a hashtag whose every video is rejected would otherwise
# page through the whole feed as fast as the network allows. That is exactly the
# request pattern the pacing exists to avoid.
PAGE_STRIDE = 30

# How long a cancelled scout gets to shut its browser down.
#
# Bounded because the cleanup path can hang for the same reason the scout can:
# it is talking to the same wedged Playwright session. A run that has already
# been stopped for overrunning must not be able to overrun again on its way
# out. Past this we log and move on; the container is about to exit anyway.
CLEANUP_GRACE_S = 30


@dataclass
class ScoutConfig:
    hashtags: list[str]
    ms_token: str | None = None
    headless: bool = True
    browser: str = "chromium"
    # Defaults to the previous constants, so a caller with no settings row to
    # hand -- a script, a test about scoring rather than configuration -- gets
    # the behaviour this module had before any of it was tunable.
    controls: ScoutControls = field(default_factory=ScoutControls)
    # Asked between hashtags: has an owner stopped this run?
    #
    # Cancelling frees the in-flight lock in Postgres immediately, which is
    # what lets the button work when the dispatcher is down. This is how the
    # browser session that lock was protecting finds out. Checked between
    # hashtags rather than between videos because it is a database round trip
    # and a hashtag is minutes long -- and because a half-scouted feed is not
    # worth finishing when nobody wants the result.
    should_stop: Callable[[], bool] | None = None


class _Budget:
    """The wall-clock ceiling on scouting.

    A run cut short still drafts ideas from what it found; a run killed by the
    three-hour write-off in `reconcile.py` produces nothing at all and leaves
    the button shut until a sweeper notices. So the budget stops the scout
    rather than the run, and says so in the report -- otherwise a budgeted run
    is indistinguishable from one whose later hashtags happened to be barren.
    """

    def __init__(self, seconds: float | None) -> None:
        self._deadline = (time.monotonic() + seconds) if seconds else None

    @property
    def exhausted(self) -> bool:
        return self._deadline is not None and time.monotonic() >= self._deadline


async def _pace(controls: ScoutControls) -> None:
    """Jittered delay between requests.

    Jittered rather than fixed because a metronomic request pattern is itself a
    bot signal. The range is a setting because it is the only thing standing
    between this and a request pattern no person produces -- and because the
    right value is discovered by being blocked, which is a thing to be able to
    react to without a deploy. Its floor is in the database, not here.
    """
    await asyncio.sleep(random.uniform(controls.pacing_min_seconds, controls.pacing_max_seconds))


def _stats(video: Any) -> dict[str, int]:
    """Read a video's counters defensively.

    The library assigns `statsV2 or stats` straight through without
    normalising, and statsV2 returns its numbers as strings. It never
    references playCount itself, so there is no guarantee any given key exists.
    """
    raw = getattr(video, "stats", None) or {}
    return {
        "plays": vel.coerce_count(raw.get("playCount")),
        "likes": vel.coerce_count(raw.get("diggCount")),
        "comments": vel.coerce_count(raw.get("commentCount")),
        "shares": vel.coerce_count(raw.get("shareCount")),
    }


def _caption(video: Any) -> str:
    # Not a typed attribute -- only present in the raw item dict.
    return str((getattr(video, "as_dict", None) or {}).get("desc") or "").strip()


def _url(video: Any) -> str:
    author = getattr(getattr(video, "author", None), "username", None) or "unknown"
    return f"https://www.tiktok.com/@{author}/video/{getattr(video, 'id', '')}"


async def scout_hashtags(config: ScoutConfig) -> ScoutOutcome:
    """Collect and score candidates from a set of hashtag feeds.

    Scores each video against its *author's* recent median rather than against
    anything absolute, so a big channel posting a normal video does not read as
    a trend.
    """
    from TikTokApi import TikTokApi  # imported lazily: only the trends image has it

    controls = config.controls
    blocklist = BlocklistMatcher(controls.caption_blocklist)
    budget = _Budget(controls.run_budget_seconds)
    report = ScoutReport()

    signals: list[vel.Signal] = []
    seen: set[str] = set()
    baselines: dict[str, float] = {}

    async def _drive() -> None:
        """The scouting itself, driven under an external deadline.

        Factored out so the budget can be enforced from outside it. The checks
        inside the loops below are the graceful stop -- they finish the video
        in hand and record what was skipped -- but they can only fire between
        iterations, and every await in here can block indefinitely: creating a
        session launches a browser, the feed iterator waits on a signed request
        inside a live page, and an author baseline is another such request.
        TikTok-Api sets no timeout on any of them and does not retry a refused
        one, so a wedged session simply never yields again.

        That is how a fifteen-minute run becomes an hour: not by overrunning,
        but by never reaching the line that would have stopped it.
        """
        async with TikTokApi() as api:
            await api.create_sessions(
                ms_tokens=[config.ms_token] if config.ms_token else None,
                num_sessions=1,
                sleep_after=3,
                headless=config.headless,
                browser=config.browser,
            )

            for index, tag in enumerate(config.hashtags):
                if config.should_stop is not None and config.should_stop():
                    report.cancelled = True
                    report.hashtags_skipped = len(config.hashtags) - index
                    log.info(
                        "run was stopped from the app; skipping %d of %d hashtags",
                        report.hashtags_skipped,
                        len(config.hashtags),
                    )
                    break

                if budget.exhausted:
                    # Recorded rather than logged and forgotten: the tags that were
                    # never looked at are the reason the report's totals are lower
                    # than the settings would predict.
                    report.budget_exhausted = True
                    report.hashtags_skipped = len(config.hashtags) - index
                    log.warning(
                        "run budget reached; skipped %d of %d hashtags",
                        report.hashtags_skipped,
                        len(config.hashtags),
                    )
                    break

                # The previous hashtag may have ended on a cheaply-rejected video,
                # which costs no delay, so the next feed request would otherwise
                # follow the last one immediately.
                if index:
                    await _pace(controls)

                report.hashtags_scouted.append(tag)
                consumed = 0
                try:
                    async for video in api.hashtag(name=tag).videos(count=controls.videos_per_hashtag):
                        if budget.exhausted:
                            report.budget_exhausted = True
                            break

                        vid = str(getattr(video, "id", "") or "")
                        if not vid or vid in seen:
                            continue          # the FYP feed in particular repeats
                        seen.add(vid)

                        report.seen += 1
                        consumed += 1
                        # Stands in for a page boundary. See PAGE_STRIDE.
                        if consumed % PAGE_STRIDE == 0:
                            await _pace(controls)

                        age = vel.age_days(getattr(video, "create_time", None))
                        stats = _stats(video)
                        caption = _caption(video)

                        rejected = _filter_cheaply(
                            age=age,
                            stats=stats,
                            caption=caption,
                            controls=controls,
                            blocklist=blocklist,
                        )
                        if rejected:
                            report.drop(rejected)
                            continue

                        author = getattr(video, "author", None)
                        username = getattr(author, "username", None) or "unknown"

                        if username not in baselines:
                            baselines[username] = await _author_baseline(api, author, controls)
                            await _pace(controls)

                        if baselines[username] <= 0:
                            # No denominator, so `outlier_ratio` would answer 0.0
                            # and the video would fail every threshold. Counted
                            # separately because it is not a filter the owner can
                            # loosen, and because a lot of it means the scrape is
                            # being refused rather than the bar being high.
                            report.drop("no_baseline")
                            continue

                        signal = vel.Signal(
                            source="tiktok",
                            source_url=_url(video),
                            title=caption[:280] or f"#{tag}",
                            keyword=tag,
                            plays=stats["plays"],
                            ratio=vel.outlier_ratio(stats["plays"], baselines[username], age),
                            engagement=vel.engagement_rate(**stats),
                            age_days=round(age, 2),
                        )

                        if not signal.clears(
                            min_ratio=controls.min_outlier_ratio,
                            min_engagement=controls.min_engagement_rate,
                        ):
                            # Attributed to one stage, ratio first, so that the
                            # stage counts and the survivors add up to `seen`. A
                            # video failing both bars is reported against the one
                            # it failed first.
                            report.drop(
                                "below_ratio"
                                if signal.ratio < controls.min_outlier_ratio
                                else "below_engagement"
                            )
                            continue

                        signals.append(signal)
                        # Only survivors pay the per-video delay now. With the
                        # filters at their defaults almost everything survives, so
                        # a default run paces as it always did; tightening a filter
                        # is what buys the time back.
                        await _pace(controls)
                except Exception as exc:  # noqa: BLE001
                    # An EmptyResponseException here means bot detection, and the
                    # library will not have retried it. One dead hashtag must not
                    # sink the whole run -- but it must not vanish either: a run
                    # that found nothing because every feed was refused has to be
                    # distinguishable from a quiet week, and this list is the only
                    # place that difference is recorded.
                    log.warning("hashtag %s failed (%s): %s", tag, type(exc).__name__, exc)
                    report.failed_hashtags.append(
                        {"hashtag": tag, "error": f"{type(exc).__name__}: {exc}"[:300]}
                    )
                    continue

    seconds = controls.run_budget_seconds
    if seconds is None:
        await _drive()
    else:
        # The hard deadline. `asyncio.wait` does not cancel on timeout, so the
        # task is cancelled explicitly and then given a bounded moment to
        # unwind -- a browser that has stopped responding must not be able to
        # extend the run through its own cleanup, which is the same failure
        # wearing a different hat.
        task = asyncio.create_task(_drive())
        _done, pending = await asyncio.wait({task}, timeout=seconds)

        if pending:
            task.cancel()
            await asyncio.wait({task}, timeout=CLEANUP_GRACE_S)
            if not task.done():
                log.error("the scout did not shut down within %ss of being cancelled", CLEANUP_GRACE_S)
            report.budget_exhausted = True
            report.hashtags_skipped = max(0, len(config.hashtags) - len(report.hashtags_scouted))
            log.warning(
                "run budget of %.0f minutes reached; stopped mid-scout with %d of %d hashtags done",
                seconds / 60,
                len(report.hashtags_scouted),
                len(config.hashtags),
            )
        else:
            # Re-raise anything the scout failed with. Swallowing it here would
            # turn a broken run into an empty one, which is the distinction the
            # whole report exists to preserve.
            task.result()

    signals.sort(key=lambda s: s.ratio, reverse=True)
    return ScoutOutcome(signals=signals, report=report)


async def _author_baseline(api: Any, author: Any, controls: ScoutControls) -> float:
    """The author's own recent median play count."""
    username = getattr(author, "username", None)
    if not username:
        return 0.0
    counts: list[int] = []
    try:
        async for video in api.user(username=username).videos(count=controls.baseline_sample_size):
            counts.append(_stats(video)["plays"])
    except Exception as exc:  # noqa: BLE001
        log.debug("no baseline for @%s: %s", username, exc)
        return 0.0
    return vel.baseline(counts)


def scout(config: ScoutConfig) -> ScoutOutcome:
    """Synchronous entry point for the scheduled task."""
    return asyncio.run(scout_hashtags(config))
