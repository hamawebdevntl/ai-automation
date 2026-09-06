"""The scout's filters, their order, and the report they produce.

Three separate promises are made here, and each of them is invisible when
broken.

The first is the report itself. Every filter the owner can tighten is a filter
they have to be able to watch working, or an empty queue stops being readable:
a quiet week, a bar set too high and a scraper being served captchas all used
to arrive as `signals = 0`. So the counts have to add up -- video-level
rejections plus what survived must equal what was seen -- because a breakdown
that does not add up invites loosening the filter that was not the problem.

The second is the order. Age, plays and the caption come free with the video;
the outlier ratio costs a paced request for the author's median. Checking the
free ones first is what makes tightening a filter shorten a run instead of
lengthening it, and it is measured here by counting baseline fetches rather
than by trusting the code to be arranged correctly.

The third is pacing, which is the only thing keeping the account in good
standing. The delays are deliberately not paid for cheaply-rejected videos, so
something has to guarantee the feed's own pagination is still spaced out.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from pipeline.trends import tiktok
from pipeline.trends.controls import ScoutControls
from pipeline.trends.report import VIDEO_STAGES

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def video(
    vid: str,
    *,
    author: str = "someone",
    plays: int = 100_000,
    likes: int = 5_000,
    comments: int = 500,
    shares: int = 100,
    age_days: float = 2.0,
    caption: str = "a caption",
) -> SimpleNamespace:
    """One item as TikTok-Api yields it, including its awkward parts.

    `stats` values are strings because statsV2 returns them that way and the
    library passes them straight through, and the caption lives only in the raw
    dict rather than as an attribute.
    """
    return SimpleNamespace(
        id=vid,
        author=SimpleNamespace(username=author),
        create_time=NOW - timedelta(days=age_days),
        stats={
            "playCount": str(plays),
            "diggCount": str(likes),
            "commentCount": str(comments),
            "shareCount": str(shares),
        },
        as_dict={"desc": caption},
    )


class FakeFeed:
    def __init__(self, items: list[Any], error: Exception | None = None) -> None:
        self.items = items
        self.error = error
        self.counts: list[int] = []

    def videos(self, count: int = 0):
        self.counts.append(count)
        items, error = self.items, self.error

        async def gen():
            if error:
                raise error
            for item in items:
                yield item

        return gen()


class FakeApi:
    """Enough of TikTokApi to drive `scout_hashtags`.

    Records what it was asked for: which feeds, with what counts, and how many
    author baselines were fetched. The last of those is the cost of a run, so
    it is the number the ordering tests assert on.
    """

    def __init__(
        self,
        feeds: dict[str, FakeFeed],
        baselines: dict[str, list[int]] | None = None,
        baseline_error: Exception | None = None,
    ) -> None:
        self.feeds = feeds
        self.baselines = baselines or {}
        self.baseline_error = baseline_error
        self.baseline_calls: list[str] = []
        self.baseline_counts: list[int] = []
        self.sessions = 0

    async def __aenter__(self) -> FakeApi:  # noqa: PYI034 - Self needs 3.11
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def create_sessions(self, **_kw: Any) -> None:
        self.sessions += 1

    def hashtag(self, name: str) -> FakeFeed:
        return self.feeds.get(name, FakeFeed([]))

    def user(self, username: str) -> FakeFeed:
        self.baseline_calls.append(username)
        counts = self.baselines.get(username, [50_000] * 12)
        feed = FakeFeed([video(f"{username}-b{i}", plays=c) for i, c in enumerate(counts)])
        feed = _CountRecordingFeed(feed, self.baseline_counts)
        if self.baseline_error:
            feed.inner.error = self.baseline_error
        return feed


class _CountRecordingFeed:
    def __init__(self, inner: FakeFeed, sink: list[int]) -> None:
        self.inner = inner
        self.sink = sink

    def videos(self, count: int = 0):
        self.sink.append(count)
        return self.inner.videos(count)


@pytest.fixture
def api_factory(monkeypatch):
    """Install a fake TikTokApi and silence the pacing delays.

    The delays are counted rather than waited on -- `paces` is how the pacing
    tests observe them without adding minutes to the suite.
    """
    holder: dict[str, Any] = {"api": None, "paces": 0}

    module = ModuleType("TikTokApi")
    module.TikTokApi = lambda *_a, **_kw: holder["api"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "TikTokApi", module)

    async def counted_pace(_controls) -> None:
        holder["paces"] += 1

    monkeypatch.setattr(tiktok, "_pace", counted_pace)

    def install(api: FakeApi) -> dict[str, Any]:
        holder["api"] = api
        return holder

    holder["install"] = install
    return holder


def scout(api: FakeApi, hashtags: list[str], controls: ScoutControls, api_factory) -> Any:
    api_factory["install"](api)
    return tiktok.scout(tiktok.ScoutConfig(hashtags=hashtags, controls=controls))


@pytest.fixture(autouse=True)
def frozen_age(monkeypatch):
    """Measure age from NOW, which is what the fixtures above are dated against.

    The original is captured before patching -- referring to `tiktok.vel.age_days`
    inside the replacement would call the replacement.
    """
    original = tiktok.vel.age_days
    monkeypatch.setattr(
        tiktok.vel, "age_days", lambda created, now=None: original(created, now=now or NOW)
    )


class TestTheCountsAddUp:
    def test_video_rejections_plus_survivors_equal_what_was_seen(self, api_factory):
        # The invariant the whole report rests on. Without it the breakdown is
        # decoration, and the owner cannot tell which filter to loosen.
        feed = FakeFeed(
            [
                video("1", age_days=400),                              # too old
                video("2", plays=10, likes=0, comments=0, shares=0),   # too few plays
                video("3", caption="free course today"),               # blocked
                video("4", author="flat", plays=50_000),               # below ratio
                video("5", plays=900_000),                             # survives
            ]
        )
        controls = ScoutControls(
            max_video_age_days=30,
            min_plays=1_000,
            caption_blocklist=("course",),
            min_outlier_ratio=1.5,
        )

        outcome = scout(FakeApi({"tag": feed}), ["tag"], controls, api_factory)
        report = outcome.report

        assert report.seen == 5
        video_dropped = sum(report.dropped.get(stage, 0) for stage in VIDEO_STAGES)
        assert video_dropped + len(outcome.signals) == report.seen

    def test_each_filter_is_attributed_to_its_own_stage(self, api_factory):
        feed = FakeFeed(
            [
                video("1", age_days=400),
                video("2", plays=10, likes=0, comments=0, shares=0),
                video("3", caption="join my course"),
            ]
        )
        controls = ScoutControls(
            max_video_age_days=30, min_plays=1_000, caption_blocklist=("course",)
        )

        report = scout(FakeApi({"tag": feed}), ["tag"], controls, api_factory).report

        assert report.dropped == {"too_old": 1, "too_few_plays": 1, "blocked_caption": 1}

    def test_a_video_failing_two_bars_is_counted_once(self, api_factory):
        # Double-counting would break the invariant and overstate whichever
        # filter happened to be checked twice.
        feed = FakeFeed([video("1", age_days=400, plays=1, caption="free course")])
        controls = ScoutControls(
            max_video_age_days=30, min_plays=1_000, caption_blocklist=("course",)
        )

        report = scout(FakeApi({"tag": feed}), ["tag"], controls, api_factory).report

        assert report.dropped == {"too_old": 1}
        assert report.total_dropped == 1

    def test_an_author_with_no_history_is_its_own_stage(self, api_factory):
        # Not a filter the owner can loosen, and a lot of it means the scrape
        # is being refused rather than the bar being high.
        api = FakeApi({"tag": FakeFeed([video("1", author="ghost")])}, baselines={"ghost": []})

        report = scout(api, ["tag"], ScoutControls(), api_factory).report

        assert report.dropped == {"no_baseline": 1}


class TestTheOrderOfTheFilters:
    def test_a_cheaply_rejected_video_costs_no_baseline_fetch(self, api_factory):
        # The point of the reordering. Thirty videos rejected on age used to
        # cost thirty author lookups, each with a paced delay in front of it.
        feed = FakeFeed([video(str(i), author=f"a{i}", age_days=400) for i in range(30)])
        api = FakeApi({"tag": feed})

        report = scout(api, ["tag"], ScoutControls(max_video_age_days=30), api_factory).report

        assert report.dropped["too_old"] == 30
        assert api.baseline_calls == []

    def test_a_baseline_is_fetched_once_per_author_not_once_per_video(self, api_factory):
        feed = FakeFeed([video(str(i), author="same", plays=900_000) for i in range(5)])
        api = FakeApi({"tag": feed})

        scout(api, ["tag"], ScoutControls(), api_factory)

        assert api.baseline_calls == ["same"]

    def test_tightening_a_free_filter_reduces_the_work(self, api_factory):
        items = [video(str(i), author=f"a{i}", plays=1_000 * i + 500) for i in range(1, 21)]
        api_loose = FakeApi({"tag": FakeFeed(list(items))})
        api_tight = FakeApi({"tag": FakeFeed(list(items))})

        scout(api_loose, ["tag"], ScoutControls(min_plays=0), api_factory)
        scout(api_tight, ["tag"], ScoutControls(min_plays=15_000), api_factory)

        assert len(api_tight.baseline_calls) < len(api_loose.baseline_calls)

    def test_the_baseline_sample_size_is_the_one_configured(self, api_factory):
        api = FakeApi({"tag": FakeFeed([video("1", plays=900_000)])})

        scout(api, ["tag"], ScoutControls(baseline_sample_size=5), api_factory)

        assert api.baseline_counts == [5]

    def test_the_video_count_asked_of_the_feed_is_the_one_configured(self, api_factory):
        feed = FakeFeed([video("1")])
        api = FakeApi({"tag": feed})

        scout(api, ["tag"], ScoutControls(videos_per_hashtag=17), api_factory)

        assert feed.counts == [17]


class TestPacing:
    def test_a_surviving_video_and_its_baseline_are_both_paced(self, api_factory):
        api = FakeApi({"tag": FakeFeed([video("1", plays=900_000)])})

        holder = api_factory
        scout(api, ["tag"], ScoutControls(), holder)

        # One for the baseline fetch, one for the video that survived.
        assert holder["paces"] == 2

    def test_pagination_is_still_spaced_out_when_everything_is_rejected(self, api_factory):
        # Cheap rejections deliberately skip the delay, so a hashtag whose
        # every video is rejected would otherwise page through the entire feed
        # as fast as the network allows -- exactly the pattern the pacing
        # exists to avoid.
        count = tiktok.PAGE_STRIDE * 3
        feed = FakeFeed([video(str(i), author=f"a{i}", age_days=400) for i in range(count)])

        holder = api_factory
        scout(FakeApi({"tag": feed}), ["tag"], ScoutControls(max_video_age_days=30), holder)

        assert holder["paces"] == 3

    def test_consecutive_hashtag_feeds_are_spaced_out(self, api_factory):
        # A tag that ends on a cheap rejection pays no delay, so without this
        # the next feed request follows the last one immediately.
        feeds = {
            "a": FakeFeed([video("1", age_days=400)]),
            "b": FakeFeed([video("2", age_days=400)]),
            "c": FakeFeed([video("3", age_days=400)]),
        }

        holder = api_factory
        scout(FakeApi(feeds), ["a", "b", "c"], ScoutControls(max_video_age_days=30), holder)

        # Two gaps between three feeds; nothing before the first.
        assert holder["paces"] == 2


class TestAFeedThatFails:
    def test_one_dead_hashtag_does_not_sink_the_run(self, api_factory):
        feeds = {
            "dead": FakeFeed([], error=RuntimeError("EmptyResponseException")),
            "alive": FakeFeed([video("1", plays=900_000)]),
        }

        outcome = scout(FakeApi(feeds), ["dead", "alive"], ScoutControls(), api_factory)

        assert len(outcome.signals) == 1

    def test_the_failure_is_recorded_rather_than_only_logged(self, api_factory):
        # The difference between "nothing was trending" and "we were turned
        # away" lives nowhere else. Bot detection arrives here as an exception
        # the library never retries.
        feeds = {"dead": FakeFeed([], error=RuntimeError("EmptyResponseException"))}

        report = scout(FakeApi(feeds), ["dead"], ScoutControls(), api_factory).report

        assert report.failed_hashtags == [
            {"hashtag": "dead", "error": "RuntimeError: EmptyResponseException"}
        ]

    def test_a_refused_run_is_distinguishable_from_a_quiet_one(self, api_factory):
        refused = scout(
            FakeApi({"t": FakeFeed([], error=RuntimeError("boom"))}), ["t"], ScoutControls(), api_factory
        ).report
        quiet = scout(FakeApi({"t": FakeFeed([])}), ["t"], ScoutControls(), api_factory).report

        assert refused.seen == quiet.seen == 0
        assert refused.failed_hashtags and not quiet.failed_hashtags


class TestTheRunBudget:
    def test_no_budget_scouts_every_hashtag(self, api_factory):
        feeds = {tag: FakeFeed([video(f"{tag}-1", plays=900_000)]) for tag in "abc"}

        report = scout(FakeApi(feeds), list("abc"), ScoutControls(), api_factory).report

        assert report.hashtags_scouted == list("abc")
        assert report.budget_exhausted is False

    def test_an_exhausted_budget_stops_partway_and_says_so(self, api_factory, monkeypatch):
        # A run cut short still drafts from what it found. A run killed by the
        # three-hour write-off produces nothing at all.
        #
        # The clock advances 40s per reading against a 5-minute budget, so the
        # run gets through several hashtags before it stops -- which is the
        # case worth testing. A clock that overshot the budget on its first
        # reading would test only that nothing happens.
        clock = iter(range(0, 100_000, 40))
        monkeypatch.setattr(tiktok.time, "monotonic", lambda: next(clock))
        feeds = {tag: FakeFeed([video(f"{tag}-1", plays=900_000)]) for tag in "abcdef"}

        outcome = scout(FakeApi(feeds), list("abcdef"), ScoutControls(run_budget_minutes=5), api_factory)
        report = outcome.report

        assert report.budget_exhausted is True
        # Stopped partway: some tags scouted, some skipped, and the two account
        # for the whole list.
        assert 0 < len(report.hashtags_scouted) < 6
        assert report.hashtags_skipped > 0
        assert len(report.hashtags_scouted) + report.hashtags_skipped == 6
        # And it kept what it found rather than throwing the session away.
        assert outcome.signals


class TestBeingStopped:
    """Cooperative cancellation.

    Stopping a run is a database write, so it takes effect whether or not
    anything in AWS is listening -- which is the point, since a dead dispatcher
    is the usual reason a run needs stopping. The cost is that the browser
    session carries on until something tells it. This is that something, and it
    is the half that still works when the dispatcher is the broken part.
    """

    def test_a_run_stopped_partway_gives_up_the_remaining_hashtags(self, api_factory):
        stopped = {"yet": False}

        def should_stop() -> bool:
            # Runs for one hashtag, then is cancelled.
            was = stopped["yet"]
            stopped["yet"] = True
            return was

        feeds = {tag: FakeFeed([video(f"{tag}-1", plays=900_000)]) for tag in "abcdef"}
        api_factory["install"](FakeApi(feeds))
        outcome = tiktok.scout(
            tiktok.ScoutConfig(hashtags=list("abcdef"), controls=ScoutControls(), should_stop=should_stop)
        )

        assert outcome.report.cancelled is True
        assert outcome.report.hashtags_scouted == ["a"]
        assert outcome.report.hashtags_skipped == 5

    def test_it_is_checked_between_hashtags_not_between_videos(self, api_factory):
        # A database round trip per video would add one to every video in the
        # run, on a path whose entire purpose is that it is cheap.
        calls = {"n": 0}

        def should_stop() -> bool:
            calls["n"] += 1
            return False

        feeds = {tag: FakeFeed([video(f"{tag}-{i}", plays=900_000) for i in range(5)]) for tag in "abc"}
        api_factory["install"](FakeApi(feeds))
        tiktok.scout(tiktok.ScoutConfig(hashtags=list("abc"), controls=ScoutControls(), should_stop=should_stop))

        assert calls["n"] == 3

    def test_a_run_with_nothing_watching_it_never_asks(self, api_factory):
        # A container started by hand has no row to be stopped.
        feeds = {"a": FakeFeed([video("a-1", plays=900_000)])}
        outcome = scout(FakeApi(feeds), ["a"], ScoutControls(), api_factory)

        assert outcome.report.cancelled is False


class TestTheBudgetIsAHardDeadline:
    """"Stop after N minutes" has to hold even when nothing is yielding.

    The checks inside the scout loops are a graceful stop: they finish the
    video in hand and record what was skipped. They can only fire between
    iterations, though, and every await in the scout can block indefinitely --
    creating a session launches a browser, and both the feed iterator and the
    author baseline wait on a signed request inside a live page. TikTok-Api
    sets no timeout on any of them and never retries a refused one.

    So a wedged session does not overrun the budget; it never reaches the line
    that would have enforced it. That is how a fifteen-minute run becomes an
    hour, and it is what the external deadline exists to prevent.

    The budgets here are fractions of a minute. `ScoutControls` is a plain
    dataclass and does not clamp -- clamping is `from_row`'s job, on the way in
    from the database -- so a test can ask for a deadline it can afford to wait
    for.
    """

    def test_a_feed_that_never_yields_is_still_stopped(self, api_factory):
        class HangingFeed:
            def videos(self, count: int = 0):
                async def gen():
                    await asyncio.sleep(3600)
                    yield None  # pragma: no cover - never reached

                return gen()

        api = FakeApi({})
        api.feeds = {"a": HangingFeed(), "b": HangingFeed()}

        started = time.monotonic()
        outcome = scout(api, ["a", "b"], ScoutControls(run_budget_minutes=0.02), api_factory)
        elapsed = time.monotonic() - started

        assert outcome.report.budget_exhausted is True
        # Stopped on the deadline, not after the hour the feed was going to take.
        assert elapsed < 10

    def test_what_it_found_before_hanging_survives_the_cancellation(self, api_factory):
        # A run cut short still drafts from what it has. Losing the partial
        # result to the cancellation would make the deadline as costly as the
        # hang it prevents.
        class HangingFeed:
            def videos(self, count: int = 0):
                async def gen():
                    await asyncio.sleep(3600)
                    yield None  # pragma: no cover - never reached

                return gen()

        api = FakeApi({"a": FakeFeed([video("a-1", plays=900_000)])})
        api.feeds = {"a": FakeFeed([video("a-1", plays=900_000)]), "b": HangingFeed()}

        outcome = scout(api, ["a", "b"], ScoutControls(run_budget_minutes=0.05), api_factory)

        assert outcome.report.budget_exhausted is True
        assert outcome.report.seen == 1
        assert len(outcome.signals) == 1
        assert outcome.report.hashtags_skipped >= 0

    def test_a_session_that_never_opens_is_stopped_too(self, api_factory):
        # The likeliest place to hang, and the one the in-loop checks cannot
        # reach at all: nothing has been scouted yet when it wedges.
        class HangingApi(FakeApi):
            async def create_sessions(self, **_kw):
                await asyncio.sleep(3600)

        started = time.monotonic()
        outcome = scout(HangingApi({}), ["a"], ScoutControls(run_budget_minutes=0.02), api_factory)
        elapsed = time.monotonic() - started

        assert outcome.report.budget_exhausted is True
        assert outcome.report.hashtags_scouted == []
        assert elapsed < 10

    def test_no_budget_still_waits_for_the_scout(self, api_factory):
        # The default. Nothing should acquire a deadline it was not given.
        outcome = scout(
            FakeApi({"a": FakeFeed([video("a-1", plays=900_000)])}),
            ["a"],
            ScoutControls(run_budget_minutes=None),
            api_factory,
        )
        assert outcome.report.budget_exhausted is False
        assert len(outcome.signals) == 1

    def test_a_real_failure_still_propagates(self, api_factory, monkeypatch):
        # The deadline must not turn a broken run into a merely empty one --
        # that is the distinction the whole report exists to preserve.
        class ExplodingApi(FakeApi):
            async def create_sessions(self, **_kw):
                raise RuntimeError("no browser")

        api_factory["install"](ExplodingApi({}))
        with pytest.raises(RuntimeError, match="no browser"):
            tiktok.scout(
                tiktok.ScoutConfig(hashtags=["a"], controls=ScoutControls(run_budget_minutes=5))
            )
