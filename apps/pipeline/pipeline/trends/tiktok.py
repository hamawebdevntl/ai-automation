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
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any

from pipeline.trends import velocity as vel

log = logging.getLogger(__name__)

# Our own pacing, since the library has none.
MIN_DELAY_S = 2.0
MAX_DELAY_S = 5.0
# Videos to sample per author when establishing that author's baseline.
BASELINE_SAMPLE = 12


@dataclass
class ScoutConfig:
    hashtags: list[str]
    ms_token: str | None = None
    videos_per_hashtag: int = 30
    headless: bool = True
    browser: str = "chromium"


async def _pace() -> None:
    """Jittered delay between requests.

    Jittered rather than fixed because a metronomic request pattern is itself a
    bot signal.
    """
    await asyncio.sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))


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


async def scout_hashtags(config: ScoutConfig) -> list[vel.Signal]:
    """Collect and score candidates from a set of hashtag feeds.

    Scores each video against its *author's* recent median rather than against
    anything absolute, so a big channel posting a normal video does not read as
    a trend.
    """
    from TikTokApi import TikTokApi  # imported lazily: only the trends image has it

    signals: list[vel.Signal] = []
    seen: set[str] = set()
    baselines: dict[str, float] = {}

    async with TikTokApi() as api:
        await api.create_sessions(
            ms_tokens=[config.ms_token] if config.ms_token else None,
            num_sessions=1,
            sleep_after=3,
            headless=config.headless,
            browser=config.browser,
        )

        for tag in config.hashtags:
            try:
                async for video in api.hashtag(name=tag).videos(count=config.videos_per_hashtag):
                    vid = str(getattr(video, "id", "") or "")
                    if not vid or vid in seen:
                        continue          # the FYP feed in particular repeats
                    seen.add(vid)

                    author = getattr(video, "author", None)
                    username = getattr(author, "username", None) or "unknown"

                    if username not in baselines:
                        baselines[username] = await _author_baseline(api, author)
                        await _pace()

                    stats = _stats(video)
                    age = vel.age_days(getattr(video, "create_time", None))
                    signals.append(
                        vel.Signal(
                            source="tiktok",
                            source_url=_url(video),
                            title=_caption(video)[:280] or f"#{tag}",
                            keyword=tag,
                            plays=stats["plays"],
                            ratio=vel.outlier_ratio(stats["plays"], baselines[username], age),
                            engagement=vel.engagement_rate(**stats),
                            age_days=round(age, 2),
                        )
                    )
                    await _pace()
            except Exception as exc:  # noqa: BLE001
                # An EmptyResponseException here means bot detection, and the
                # library will not have retried it. One dead hashtag must not
                # sink the whole run.
                log.warning("hashtag %s failed (%s): %s", tag, type(exc).__name__, exc)
                continue

    signals.sort(key=lambda s: s.ratio, reverse=True)
    return [s for s in signals if s.is_worth_surfacing]


async def _author_baseline(api: Any, author: Any) -> float:
    """The author's own recent median play count."""
    username = getattr(author, "username", None)
    if not username:
        return 0.0
    counts: list[int] = []
    try:
        async for video in api.user(username=username).videos(count=BASELINE_SAMPLE):
            counts.append(_stats(video)["plays"])
    except Exception as exc:  # noqa: BLE001
        log.debug("no baseline for @%s: %s", username, exc)
        return 0.0
    return vel.baseline(counts)


def scout(config: ScoutConfig) -> list[vel.Signal]:
    """Synchronous entry point for the scheduled task."""
    return asyncio.run(scout_hashtags(config))
