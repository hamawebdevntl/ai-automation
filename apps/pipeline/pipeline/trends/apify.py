"""Apify scouting: TikTok and Instagram, through somebody else's scrapers.

This is the video source that replaced the browser-driven one. It answers the
question TikTok-Api used to answer -- which *format* is outperforming, with
engagement as proof -- which is the half Google Trends structurally cannot give.

Both platforms run through one source rather than two because they are the same
question asked of different audiences, and because the thing that differs
between them is a handful of field names, not the scoring. Which of them a run
scouts is a setting; each `Signal` records which platform it came from, so a
queue item can be traced back.

The shape of a run, and why it is in two phases:

Phase one scrapes each hashtag and applies the filters that cost nothing --
age, view floor, blocked captions. Phase two takes the authors that survived
and scrapes *their* recent posts, once per platform, to get the median every
`Signal.ratio` is measured against.

That order is the whole economy of a run. A video is worth having a baseline for
only after it has cleared the free filters, and batching every surviving author
into a single profile run means the expensive half costs one actor run per
platform instead of one per author. The alternative -- scoring a video against
the hashtag's median instead of its author's -- would have cost nothing at all,
and would have quietly changed what a signal means: a large account posting a
mediocre video beats a hashtag's median comfortably, which is precisely the
false positive `outlier_ratio` exists to prevent.

One caveat stated plainly: an actor's output shape is not a contract we
control. The field names below were checked against live output from all three
actors on 2026-09-08 and every first candidate matched, but that is a snapshot,
not a promise. Every read goes through `_pick`, which tries the plausible names
and records a rejection when none of them match. A renamed field costs this
source its signals and says so in the run breakdown; it does not raise.

Two things the same check corrected. The TikTok actor's date filter applies to
profile and search scrapes only -- on a hashtag scrape it is ignored without
charge, so recency is enforced locally by `too_old` and old videos are paid for.
And a profile scrape leads with the author's pinned videos, which are their
showcase rather than their norm, so the baseline asks for them to be left out.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any

from pipeline.clients.apify import TERMINAL_STATES, ApifyClient, ApifyError, ApifyRefused
from pipeline.trends import velocity as vel
from pipeline.trends.base import (
    Budget,
    ScoutOutcome,
    attribute_failure,
    filter_cheaply,
    opt_count,
    usable_stats,
)
from pipeline.trends.controls import APIFY_PLATFORMS, BlocklistMatcher, ScoutControls
from pipeline.trends.report import ScoutReport

log = logging.getLogger(__name__)

# How often to ask whether a run has finished.
#
# Apify runs take tens of seconds at best and many minutes at worst, so polling
# faster than this buys nothing and spends request quota. It is also the
# granularity at which a stopped run notices it has been stopped, which is why
# it is not longer.
POLL_INTERVAL_S = 5.0

# The ceiling on waiting for a single actor run.
#
# Separate from the run budget because it answers a different question: the
# budget is "how long may this whole scout take", this is "how long before I
# conclude that one actor is stuck". Without it, one wedged hashtag absorbs the
# entire budget and the remaining hashtags are never scouted.
RUN_TIMEOUT_S = 600.0

# The ceiling Apify itself puts on a run, passed when the run is started.
#
# Longer than RUN_TIMEOUT_S on purpose: the local deadline is the normal way a
# stuck run ends, and it records why in the report. This one exists for the case
# where nobody is left to enforce the local deadline -- a worker restarted
# mid-scout -- because the actors' own defaults are unlimited (TikTok) and seven
# days (Instagram profiles), and a run keeps billing until it ends.
REMOTE_TIMEOUT_S = RUN_TIMEOUT_S + 60.0

# How many times to ask again when a poll or a dataset read fails for a reason
# that waiting can fix.
#
# A poll asks about a run that carries on regardless, and a dataset read fetches
# results already paid for, so one 429 or one dropped connection should cost
# seconds rather than a hashtag. Past this the API is down rather than blinking,
# and giving up is the honest answer.
TRANSIENT_ATTEMPTS = 3

# Terminal states other than success. Derived rather than re-listed so that a
# state added to the client is not silently polled forever here.
FAILED_STATES = TERMINAL_STATES - {"SUCCEEDED"}

# How many of an author's recent posts to ask for when building their baseline.
#
# `controls.baseline_sample_size` is the real setting; this is only the ceiling
# we will ask an actor for, because a profile scrape's cost scales with it and a
# median over more than this is not a better median.
MAX_BASELINE_POSTS = 30


@dataclass
class PlatformSpec:
    """How one platform's actor is called, and how its output is read.

    A table rather than two code paths, because everything that genuinely
    differs between TikTok and Instagram is data: two actor ids and a list of
    field names. Keeping it as data is what stops the scoring from being
    written twice and drifting.
    """

    hashtag_actor: str
    profile_actor: str
    # Output field names, most likely first. See the module docstring on why
    # these are lists and not strings.
    plays: tuple[str, ...]
    likes: tuple[str, ...]
    comments: tuple[str, ...]
    shares: tuple[str, ...]
    created: tuple[str, ...]
    caption: tuple[str, ...]
    url: tuple[str, ...]
    author: tuple[str, ...]

    def hashtag_input(self, hashtag: str, limit: int) -> dict[str, Any]:
        """The actor's input for one hashtag.

        No date bound on either platform. Instagram's hashtag scraper has no
        date field in its schema, and TikTok's documents its date filter for
        profile and search scrapes only -- a live check confirmed it is ignored
        on a hashtag scrape, neither applied nor charged. So both pay for old
        posts and `too_old` drops them locally.
        """
        raise NotImplementedError

    def profile_input(self, authors: list[str], limit: int) -> dict[str, Any]:
        raise NotImplementedError

    def profile_url(self, author: str) -> str:
        raise NotImplementedError


@dataclass
class _TikTok(PlatformSpec):
    def hashtag_input(self, hashtag: str, limit: int) -> dict[str, Any]:
        # No `oldestPostDateUnified` here. It reads as though it would apply the
        # recency bar before we are charged, and an earlier version sent it for
        # that reason; the actor documents it for profile scrapes and a live
        # run confirmed it is ignored on hashtags. Sending it would mislead the
        # next reader about where `too_old` is enforced.
        return {
            "hashtags": [hashtag.lstrip("#")],
            "resultsPerPage": limit,
            # Every download flag off, explicitly. This actor bills per result
            # and these pull media we never read; a default flipping to true
            # upstream would show up as a bill rather than as a bug.
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadAvatars": False,
        }

    def profile_input(self, authors: list[str], limit: int) -> dict[str, Any]:
        return {
            "profiles": authors,
            "resultsPerPage": limit,
            # A profile page leads with the author's pinned videos, and with a
            # sample this small they were most of it: three of five for two of
            # three authors in the live check, one of them two 1.3M-view videos
            # from a year earlier. Pinned posts are the author's showcase, not
            # their norm, and a median built from them hides the next hit --
            # the exact opposite of what the baseline is for. Free to exclude.
            "excludePinnedPosts": True,
            # The same four as the hashtag scrape, for the same reason.
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSlideshowImages": False,
            "shouldDownloadAvatars": False,
        }

    def profile_url(self, author: str) -> str:
        return f"https://www.tiktok.com/@{author}"


# Reels, not posts, on both Instagram scrapes.
#
# "posts" is a hashtag page's top posts, and on a live check ten of ten were
# images or carousels: no play count, so every one was paid for and then
# dropped as `no_metrics`. "reels" returned ten videos, each with a play count.
# The scout measures views, so the only results worth being billed for are the
# ones that have them. The same holds for the baseline: an author's images
# cannot contribute to a median of view counts, so a "posts" profile scrape
# pays for items the median then skips.
INSTAGRAM_RESULTS_TYPE = "reels"


@dataclass
class _Instagram(PlatformSpec):
    def hashtag_input(self, hashtag: str, limit: int) -> dict[str, Any]:
        # `resultsLimit`, not `resultsPerPage`. The two actors genuinely differ,
        # which is the strongest available hint that they will differ again --
        # and the reason these inputs are a table rather than written inline.
        return {
            "hashtags": [hashtag.lstrip("#")],
            "resultsType": INSTAGRAM_RESULTS_TYPE,
            "resultsLimit": limit,
        }

    def profile_input(self, authors: list[str], limit: int) -> dict[str, Any]:
        return {
            "directUrls": [self.profile_url(a) for a in authors],
            "resultsType": INSTAGRAM_RESULTS_TYPE,
            "resultsLimit": limit,
        }

    def profile_url(self, author: str) -> str:
        return f"https://www.instagram.com/{author}/"


# Instagram does not report a share count anywhere in its public payload, so
# `shares` is empty rather than guessed. `engagement_rate` takes shares as a
# term, which means an Instagram post scores marginally lower than an
# identically-performing TikTok one and a single engagement floor is slightly
# stricter there. That is the honest reading -- the number is absent, not zero --
# and it is recorded here rather than papered over. A per-platform floor is the
# fix if it turns out to matter.
PLATFORMS: dict[str, PlatformSpec] = {
    "tiktok": _TikTok(
        hashtag_actor="clockworks~tiktok-scraper",
        profile_actor="clockworks~tiktok-scraper",
        plays=("playCount", "playsCount", "stats.playCount"),
        likes=("diggCount", "likesCount", "stats.diggCount"),
        comments=("commentCount", "commentsCount", "stats.commentCount"),
        shares=("shareCount", "sharesCount", "stats.shareCount"),
        created=("createTimeISO", "createTime"),
        caption=("text", "desc"),
        url=("webVideoUrl", "url"),
        author=("authorMeta.name", "authorMeta.uniqueId", "authorName"),
    ),
    "instagram": _Instagram(
        hashtag_actor="apify~instagram-hashtag-scraper",
        profile_actor="apify~instagram-scraper",
        plays=("videoPlayCount", "videoViewCount", "playCount"),
        likes=("likesCount", "likeCount"),
        comments=("commentsCount", "commentCount"),
        shares=(),
        created=("timestamp", "takenAt"),
        caption=("caption", "text"),
        url=("url", "postUrl"),
        author=("ownerUsername", "owner.username"),
    ),
}


@dataclass
class ApifyConfig:
    hashtags: list[str]
    # Defaults to both, matching the column default, so a caller with no
    # settings row to hand gets the same behaviour a fresh install gets.
    platforms: tuple[str, ...] = APIFY_PLATFORMS
    controls: ScoutControls = field(default_factory=ScoutControls)
    # Injected by tests. The real one is built lazily so that importing this
    # module never needs a token.
    client: Any | None = None
    should_stop: Callable[[], bool] | None = None


def _client(config: ApifyConfig) -> Any:
    if config.client is not None:
        return config.client
    return ApifyClient()


@dataclass
class _Item:
    """One scraped post, read into the shape the scoring needs.

    The counters are optional because "not published" is a different fact from
    zero, and only one of them is a filter's business. `base.usable_stats`
    decides which absences are fatal.
    """

    platform: str
    author: str
    url: str
    caption: str
    age: float
    plays: int | None
    likes: int | None
    comments: int | None
    shares: int | None
    # Which hashtag this was found under. Carried because it becomes
    # `Signal.keyword`, which `ideas._spread` groups by to stop one hashtag's
    # results filling a whole batch. Empty on a profile scrape, where the
    # posts were fetched by author and belong to no hashtag.
    hashtag: str = ""


def _pick(item: dict[str, Any], names: tuple[str, ...]) -> Any:
    """The first of these fields that is present, or None.

    Dotted names walk nested objects, because both actors nest some of what we
    need (`authorMeta.name`, `owner.username`) and flatten it in other versions
    of their output. Trying several names is how a renamed field becomes a
    counted rejection rather than a crash -- see the module docstring.
    """
    for name in names:
        value: Any = item
        for part in name.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value not in (None, ""):
            return value
    return None


def _parse_time(raw: Any) -> datetime | None:
    """A post's creation time, from any of the several shapes these actors use.

    ISO strings, epoch seconds, and epoch milliseconds all appear depending on
    actor and version. An unreadable value returns None, which `age_days`
    already treats as "young" rather than as "reject" -- deliberately, because a
    missing timestamp is not evidence of age.
    """
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(raw, (int, float)) and raw > 0:
        # Milliseconds if it is far too large to be seconds. The boundary is
        # generous: 10^11 seconds is the year 5138.
        seconds = float(raw) / 1000.0 if raw > 1e11 else float(raw)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _read(item: dict[str, Any], platform: str, hashtag: str = "") -> _Item | None:
    """Read one scraped post, or None if it cannot be identified.

    An item with no author or no URL is not a candidate: the author is the
    denominator of its score and the URL is what the owner clicks to see the
    format. Neither can be substituted, so this is the one place that discards
    rather than degrading.
    """
    spec = PLATFORMS[platform]
    author = _pick(item, spec.author)
    url = _pick(item, spec.url)
    if not author or not url:
        return None

    return _Item(
        platform=platform,
        hashtag=hashtag,
        author=str(author).lstrip("@"),
        url=str(url),
        caption=str(_pick(item, spec.caption) or "").strip(),
        age=vel.age_days(_parse_time(_pick(item, spec.created))),
        # `opt_count`, not `coerce_count`: these come from a hosted API where an
        # absent counter means the platform withheld it, and where Instagram
        # publishes -1 for a hidden like count. See `base.opt_count`.
        plays=opt_count(_pick(item, spec.plays)),
        likes=opt_count(_pick(item, spec.likes)),
        comments=opt_count(_pick(item, spec.comments)),
        shares=opt_count(_pick(item, spec.shares)),
    )


def _pace(controls: ScoutControls) -> None:
    """Jittered delay between actor runs.

    Far less important here than it was against TikTok directly -- Apify is a
    paid API being used as intended, not a scraper avoiding detection -- but the
    setting exists and a run that ignores it would surprise anyone who had
    turned it up.
    """
    time.sleep(random.uniform(controls.pacing_min_seconds, controls.pacing_max_seconds))


def _stopped(config: ApifyConfig, report: ScoutReport) -> bool:
    if config.should_stop is not None and config.should_stop():
        report.cancelled = True
        return True
    return False


def _retry(call: Callable[[], Any], *, what: str) -> Any:
    """`call()`, repeated through the failures that waiting can fix.

    A 429 waits as long as the server asked, anything else one poll interval.
    `ApifyRefused` is re-raised at once: a bad token does not become good by
    waiting. The last error is re-raised when the attempts are spent, so the
    caller decides what giving up means -- for a run in flight, aborting it.

    Not used for starting a run. A POST that times out is ambiguous about
    whether a run was created, and retrying it could start -- and bill -- two.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except ApifyRefused:
            raise
        except ApifyError as exc:
            if attempt >= TRANSIENT_ATTEMPTS:
                raise
            wait = getattr(exc, "retry_after", None) or POLL_INTERVAL_S
            log.warning("%s failed (%s); asking again in %.0fs", what, exc, wait)
            time.sleep(wait)


def _collect(
    client: Any,
    actor: str,
    payload: dict[str, Any],
    *,
    limit: int,
    config: ApifyConfig,
    budget: Budget,
    report: ScoutReport,
    label: str,
) -> list[dict[str, Any]]:
    """Run one actor and return its dataset, or [] with the reason recorded.

    Aborts the remote run when the owner stops the scout or the budget expires.
    That is not tidiness: an Apify run keeps scraping and keeps billing after we
    stop waiting for it, so the only way Stop actually stops the spending is to
    say so.
    """
    try:
        run = client.start_run(actor, payload, timeout_s=REMOTE_TIMEOUT_S)
    except ApifyError as exc:
        log.warning("could not start %s for %s: %s", actor, label, exc)
        report.failed_hashtags.append({"hashtag": label, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return []

    run_id = str(run.get("id") or "")
    deadline = time.monotonic() + RUN_TIMEOUT_S

    while True:
        if _stopped(config, report) or budget.exhausted:
            if budget.exhausted:
                report.budget_exhausted = True
            client.abort_run(run_id)
            return []
        if time.monotonic() >= deadline:
            log.warning("%s for %s exceeded %.0fs; aborting it", actor, label, RUN_TIMEOUT_S)
            client.abort_run(run_id)
            report.failed_hashtags.append({"hashtag": label, "error": "the scraper did not finish in time"})
            return []

        try:
            status = _retry(partial(client.run_status, run_id), what=f"polling {label}")
        except ApifyError as exc:
            # The run is still going on Apify's side. Giving up on reading it
            # is not the same as stopping it, and only the second stops the bill.
            client.abort_run(run_id)
            report.failed_hashtags.append({"hashtag": label, "error": f"{type(exc).__name__}: {exc}"[:300]})
            return []

        state = str(status.get("status") or "")
        if state == "SUCCEEDED":
            dataset_id = str(status.get("defaultDatasetId") or "")
            if not dataset_id:
                report.failed_hashtags.append({"hashtag": label, "error": "the run produced no dataset"})
                return []
            try:
                return _retry(
                    partial(client.dataset_items, dataset_id, limit=limit),
                    what=f"fetching {label}",
                )
            except ApifyError as exc:
                report.failed_hashtags.append(
                    {"hashtag": label, "error": f"{type(exc).__name__}: {exc}"[:300]}
                )
                return []
        if state in FAILED_STATES:
            log.warning("%s for %s ended %s", actor, label, state)
            report.failed_hashtags.append({"hashtag": label, "error": f"the scraper ended {state}"})
            return []

        time.sleep(POLL_INTERVAL_S)


def _baselines(
    client: Any,
    platform: str,
    authors: list[str],
    *,
    config: ApifyConfig,
    budget: Budget,
    report: ScoutReport,
) -> dict[str, float]:
    """Each author's own recent median, in one actor run for all of them.

    This is the expensive half of a run, and batching is what makes it
    affordable: one profile scrape covering every author who survived the free
    filters, rather than one per author. An author the scrape does not return --
    a private account, a refused profile -- simply has no entry here, and the
    caller records that as `no_baseline` rather than scoring them against
    something borrowed.
    """
    if not authors:
        return {}

    spec = PLATFORMS[platform]
    per_author = max(1, min(config.controls.baseline_sample_size, MAX_BASELINE_POSTS))
    items = _collect(
        client,
        spec.profile_actor,
        spec.profile_input(authors, per_author),
        # Deliberately generous: the actor is asked for `per_author` posts each,
        # but it decides how it splits them, so capping the dataset at exactly
        # the product would risk truncating the last author's history.
        limit=per_author * len(authors) + len(authors),
        config=config,
        budget=budget,
        report=report,
        label=f"{platform} author history",
    )

    plays_by_author: dict[str, list[int]] = {}
    for raw in items:
        item = _read(raw, platform)
        # A post with no view count cannot contribute to a median of view
        # counts. Silently skipped rather than recorded: this is the author's
        # history, not a candidate, so it is not part of the funnel.
        if item is None or item.plays is None:
            continue
        plays_by_author.setdefault(item.author, []).append(item.plays)

    return {
        author: vel.baseline(plays)
        for author, plays in plays_by_author.items()
        if plays
    }


def scout(config: ApifyConfig) -> ScoutOutcome:
    """Scrape the configured hashtags on the configured platforms, and score.

    Returns the same `ScoutOutcome` every source returns, so nothing downstream
    knows or cares that these signals were rented rather than scraped here.
    """
    controls = config.controls
    blocklist = BlocklistMatcher(controls.caption_blocklist)
    budget = Budget(controls.run_budget_seconds)
    report = ScoutReport()
    client = _client(config)

    platforms = [p for p in config.platforms if p in PLATFORMS]
    if not platforms:
        # `controls._platforms` already refuses to produce this, so reaching it
        # means a caller built the config by hand.
        raise ValueError(f"no scrapeable platforms in {config.platforms!r}")

    signals: list[vel.Signal] = []
    # Hashtags, not hashtag-platform pairs.
    #
    # `report.hashtags_scouted` is compared against `hashtags_configured` to
    # say whether the list is being rotated, so counting a tag once per
    # platform would report scouting more tags than exist and turn the app's
    # "12 of 16, rotating" into nonsense. The platform belongs in the failure
    # records, where it identifies which scrape was refused, and not here.
    scouted: set[str] = set()

    for platform in platforms:
        spec = PLATFORMS[platform]
        # Kept per platform: an author is only comparable to their own posts on
        # the platform those posts are on. Each entry carries the counters it
        # was screened with, so scoring does not re-derive them.
        survivors: list[tuple[_Item, dict[str, int]]] = []
        seen_urls: set[str] = set()

        for index, tag in enumerate(config.hashtags):
            if _stopped(config, report):
                break
            if budget.exhausted:
                report.budget_exhausted = True
                # Only the tags no platform reached. Counted against the set so
                # that two platforms running out of time on the same tag is one
                # unscouted hashtag rather than two.
                report.hashtags_skipped = len(
                    [t for t in config.hashtags[index:] if t.lstrip("#") not in scouted]
                )
                break
            if index:
                _pace(controls)

            clean = tag.lstrip("#")
            label = f"{platform}:{clean}"
            if clean not in scouted:
                scouted.add(clean)
                report.hashtags_scouted.append(clean)
            raw_items = _collect(
                client,
                spec.hashtag_actor,
                spec.hashtag_input(tag, controls.videos_per_hashtag),
                limit=controls.videos_per_hashtag,
                config=config,
                budget=budget,
                report=report,
                label=label,
            )

            for raw in raw_items:
                item = _read(raw, platform, hashtag=clean)
                if item is None:
                    # No author or no URL. Not scoreable and not attributable to
                    # a filter, so it is counted where a missing metric is
                    # counted rather than blamed on a bar the owner set.
                    report.seen += 1
                    report.drop("no_metrics")
                    continue
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                report.seen += 1

                stats = usable_stats(
                    plays=item.plays,
                    likes=item.likes,
                    comments=item.comments,
                    shares=item.shares,
                    controls=controls,
                )
                if stats is None:
                    # Instagram routinely reports no play count, and publishes a
                    # hidden like count as -1. Recorded as its own stage: a bar
                    # cannot be applied to a number nobody published, and saying
                    # "under your minimum view count" about an absence points the
                    # owner at a setting that had nothing to do with it.
                    report.drop("no_metrics")
                    continue

                rejected = filter_cheaply(
                    age=item.age,
                    stats=stats,
                    caption=item.caption,
                    controls=controls,
                    blocklist=blocklist,
                )
                if rejected:
                    report.drop(rejected)
                    continue
                survivors.append((item, stats))

        if report.cancelled:
            break

        # Phase two. Only now, and only for authors whose posts cleared the
        # free filters -- see the module docstring on why the order is this way.
        authors = sorted({item.author for item, _ in survivors})
        if authors and not budget.exhausted:
            _pace(controls)
        medians = _baselines(
            client, platform, authors, config=config, budget=budget, report=report
        )

        for item, stats in survivors:
            median = medians.get(item.author, 0.0)
            if median <= 0:
                # No denominator, so `outlier_ratio` would answer 0.0 and the
                # post would fail every threshold. Counted separately because it
                # is not a filter the owner can loosen, and because a lot of it
                # means the profile scrape is being refused rather than the bar
                # being high.
                report.drop("no_baseline")
                continue

            signal = vel.Signal(
                # The platform, not the vendor. Apify is how we read TikTok, not
                # where the video is -- and an idea traced back to "apify" would
                # tell the owner which invoice it came from rather than which
                # feed to look at.
                source=item.platform,
                source_url=item.url,
                title=item.caption[:280] or f"#{item.hashtag}",
                keyword=item.hashtag,
                plays=stats["plays"],
                ratio=vel.outlier_ratio(stats["plays"], median, item.age),
                engagement=vel.engagement_rate(**stats),
                age_days=round(item.age, 2),
            )

            failed = attribute_failure(signal, controls)
            if failed:
                report.drop(failed)
                continue
            signals.append(signal)

    signals.sort(key=lambda s: s.ratio, reverse=True)
    return ScoutOutcome(signals=signals, report=report)
