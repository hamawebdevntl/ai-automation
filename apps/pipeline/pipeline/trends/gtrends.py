"""Google Trends scouting.

The scout's second source, and the first that is not an arms race. TikTok-Api
lost its one; this is a different bargain rather than a better position in the
same fight.

What Google Trends gives, and does not:

  * it gives *demand* -- what people typed into a search box, measured against
    its own recent history. That maps cleanly onto `Signal.ratio`, which was
    always relative rather than absolute, and onto `window_velocity`, which was
    written for exactly this shape of series before there was anything to feed
    it.
  * it gives *discovery*, through rising related queries: terms nobody thought
    to configure, surfacing because they moved. This is the part that most
    resembles browsing a hashtag feed.
  * it gives nothing about *format*. No captions, no hooks, no engagement, no
    author. A signal from here says the subject is live, and says nothing at
    all about how to open a video about it. That falls to the brief.

Two properties of pytrends shape everything below.

There is no official Google Trends API; pytrends scrapes the same endpoints the
website calls, so it is rate-limited without warning and answers 429 rather
than slowing down. Requests are therefore batched -- `build_payload` accepts
five terms at once, which is a fivefold reduction for free -- and paced, and a
429 ends that batch rather than the run.

And interest is returned on a 0-100 scale that is normalised *within a single
request*. Two terms are only comparable if they were asked for together, which
is why the batch is the unit of scoring here and why nothing compares a value
from one batch against a value from another.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pipeline.trends import velocity as vel
from pipeline.trends.controls import BlocklistMatcher, ScoutControls
from pipeline.trends.report import ScoutReport
from pipeline.trends.tiktok import ScoutOutcome

log = logging.getLogger(__name__)

# Terms per request. One, deliberately, and this is the most consequential
# decision in the file.
#
# Google accepts five and normalises interest to 0-100 *across the terms in the
# request*, so a batch is scored against its own biggest member. Measured on
# this niche, batching five buyer terms with "ai automation" pushed the rest to
# 1-3 out of 100, where a rise from 1 to 4 reads as a 3.7x breakout and is
# really just quantisation. The same terms asked for individually:
#
#   invoice software       mean 20.5  last 19  ratio 1.47
#   excel alternative      mean  4.3  last 14  ratio 2.93
#   bookkeeping software   mean 24.4  last  8  ratio 0.76
#
# -- which is a rising term, a falling term and a steady one, correctly told
# apart. Asked as a batch, all three were noise.
#
# A solo request normalises a term against its own history, which is precisely
# what `window_velocity` compares. The cost is one request per term instead of
# one per five; rotation and the pacing floor below are what keep that
# affordable.
TERMS_PER_REQUEST = 1

# The window each series covers. Three months is long enough for
# `window_velocity` to have a baseline worth comparing against, and short
# enough that a term which mattered last year does not drown one that matters
# this week.
TIMEFRAME = "today 3-m"

# How much of the window counts as "recent" when scoring. A quarter of three
# months is roughly the last three weeks -- long enough to be a trend rather
# than a spike, short enough to still be news.
RECENT_FRACTION = 0.25

# Rate limiting is not ours to negotiate, so a refused batch waits rather than
# retrying immediately. Two attempts: the first 429 is often a burst limit that
# a pause clears, and a second one means the run should move on rather than sit
# there spending its budget on a closed door.
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_S = 20.0


@dataclass
class GTrendsConfig:
    keywords: list[str]
    controls: ScoutControls = field(default_factory=ScoutControls)
    geo: str = ""
    # Injected by tests. The real one drives network requests and is built
    # lazily, because `pytrends` is only installed in the trends image.
    client: Any | None = None
    should_stop: Callable[[], bool] | None = None


def _client(config: GTrendsConfig) -> Any:
    if config.client is not None:
        return config.client
    from pytrends.request import TrendReq

    # Connect and read timeouts, because the default is none at all and a
    # hanging request would sit inside the run budget doing nothing.
    return TrendReq(hl="en-GB", tz=0, timeout=(10, 30))


# The shortest gap between Google Trends requests, whatever the settings say.
#
# The pacing settings were calibrated for TikTok, where the delay is camouflage
# and two seconds is defensible. Google's limit is not a matter of appearing
# human -- it answers 429 and stops talking. Two rapid requests were refused
# during development; eight seconds apart went through. Five is the floor, and
# the owner's setting applies above it.
MIN_PACING_S = 5.0


def _pace(controls: ScoutControls) -> None:
    """Jittered delay between requests.

    The same mechanism as the TikTok scout, for a different reason: not
    camouflage, but staying under a limit that answers 429 rather than slowing
    down. Synchronous because pytrends is.
    """
    low = max(controls.pacing_min_seconds, MIN_PACING_S)
    high = max(controls.pacing_max_seconds, low)
    time.sleep(random.uniform(low, high))


def _batches(keywords: list[str], size: int = TERMS_PER_REQUEST) -> list[list[str]]:
    return [keywords[i : i + size] for i in range(0, len(keywords), size)]


def _series(frame: Any, term: str) -> list[float]:
    """One term's interest over time, as plain floats.

    Defensive about the frame's shape: pytrends returns a pandas DataFrame
    whose columns depend on what Google answered, and a term with too little
    search volume is simply absent rather than zero.
    """
    if frame is None or getattr(frame, "empty", True):
        return []
    if term not in frame:
        return []
    values = []
    for raw in frame[term].tolist():
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values


def _source_url(term: str, geo: str) -> str:
    from urllib.parse import quote_plus

    geo_part = f"&geo={geo}" if geo else ""
    return f"https://trends.google.com/trends/explore?q={quote_plus(term)}{geo_part}"


def scout(config: GTrendsConfig) -> ScoutOutcome:
    """Measure search demand for the configured terms, and score what moved.

    Returns the same `ScoutOutcome` the TikTok scout does, so everything
    downstream -- spreading signals across keywords, drafting, duplicate
    suppression, the rejection report -- works without knowing which source
    produced them.
    """
    controls = config.controls
    blocklist = BlocklistMatcher(controls.caption_blocklist)
    report = ScoutReport()
    signals: list[vel.Signal] = []

    terms = [k.strip() for k in config.keywords if k and k.strip()]
    if not terms:
        return ScoutOutcome(signals=[], report=report)

    client = _client(config)
    deadline = (time.monotonic() + controls.run_budget_seconds) if controls.run_budget_seconds else None
    batches = _batches(terms)

    for index, batch in enumerate(batches):
        if config.should_stop is not None and config.should_stop():
            report.cancelled = True
            report.hashtags_skipped = sum(len(b) for b in batches[index:])
            log.info("run was stopped from the app; skipping %d terms", report.hashtags_skipped)
            break

        if deadline is not None and time.monotonic() >= deadline:
            report.budget_exhausted = True
            report.hashtags_skipped = sum(len(b) for b in batches[index:])
            log.warning("run budget reached; skipped %d terms", report.hashtags_skipped)
            break

        if index:
            _pace(controls)

        frame = _fetch(client, batch, config, report)
        if frame is None:
            continue

        for term in batch:
            report.hashtags_scouted.append(term)
            report.seen += 1

            # The blocklist reads as odd here until you remember it is the
            # owner's list of things they do not want to be seen talking about.
            # A term is the whole of what a Trends signal says, so it is the
            # only text there is to check.
            if blocklist and blocklist.matched(term):
                report.drop("blocked_caption")
                continue

            series = _series(frame, term)
            if len(series) < 4:
                # Too little volume for Google to report, which is not a low
                # score -- it is an absence, and the same absence the TikTok
                # scout recorded when an author had no readable history.
                report.drop("no_baseline")
                continue

            ratio = vel.window_velocity(series, recent_fraction=RECENT_FRACTION)
            latest = round(series[-1])

            if latest < controls.min_plays:
                # `min_plays` is a view count on a video source. Here it is the
                # term's current interest as a percentage of its own three-month
                # peak, because a solo request is normalised against that peak
                # and nothing else.
                #
                # Worth being explicit about what that cannot do: Google Trends
                # never reports absolute volume, so this cannot reject a term
                # nobody searches. It rejects a term that is currently far below
                # its own best, which is a different and still useful question.
                report.drop("too_few_plays")
                continue

            signal = vel.Signal(
                source="google_trends",
                source_url=_source_url(term, config.geo),
                title=term,
                keyword=term,
                plays=latest,
                ratio=ratio,
                # Google Trends has no notion of interaction, and inventing one
                # would let `min_engagement_rate` silently reject everything.
                engagement=0.0,
                # Nor of age: a series is current by construction.
                age_days=0.0,
            )

            if signal.ratio < controls.min_outlier_ratio:
                report.drop("below_ratio")
                continue

            signals.append(signal)

    signals.sort(key=lambda s: s.ratio, reverse=True)
    log.info("google trends: %d terms measured, %d moving", report.seen, len(signals))
    return ScoutOutcome(signals=signals, report=report)


def _fetch(client: Any, batch: list[str], config: GTrendsConfig, report: ScoutReport) -> Any:
    """One batch of up to five terms, with a bounded retry on a rate limit.

    A refused batch is recorded rather than raised. One rate-limited request
    must not cost the run the terms it had already measured -- and the record
    is what tells the owner afterwards that an empty result was a closed door
    rather than a quiet market.
    """
    for attempt in range(RETRY_ATTEMPTS):
        try:
            client.build_payload(batch, timeframe=TIMEFRAME, geo=config.geo)
            return client.interest_over_time()
        except Exception as exc:  # noqa: BLE001 - pytrends raises its own types
            name = type(exc).__name__
            last = attempt == RETRY_ATTEMPTS - 1
            if not last and "TooManyRequests" in name:
                log.warning("rate limited on %s; waiting %.0fs", batch, RETRY_BACKOFF_S)
                time.sleep(RETRY_BACKOFF_S)
                continue
            log.warning("batch %s failed (%s): %s", batch, name, exc)
            report.failed_hashtags.append(
                {"hashtag": ", ".join(batch), "error": f"{name}: {exc}"[:300]}
            )
            return None
    return None
