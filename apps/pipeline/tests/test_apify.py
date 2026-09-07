"""Apify: the client's status mapping, and the scout's two phases.

The client is tested with `respx` because the interesting part *is* the status
mapping -- a 403 that means "your credit is gone" and a 404 that means "you
wrote the actor id with a slash" are the two failures most likely to happen
first, and both must say so.

The scout is tested with a hand-rolled fake client, following `test_gtrends.py`:
the fake is both a stub and a spy, so what the scout *asked for* is assertable
without a network or a token.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from pipeline.clients.apify import (
    API_BASE,
    ApifyClient,
    ApifyError,
    ApifyRateLimited,
    ApifyRefused,
)
from pipeline.trends import apify
from pipeline.trends.controls import ScoutControls


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Neither the polling interval nor the pacing should cost test time."""
    monkeypatch.setattr(apify.time, "sleep", lambda _s: None)


def client() -> ApifyClient:
    return ApifyClient(api_key="test-token")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class TestTheTokenIsRequiredAtConstruction:
    def test_an_absent_token_refuses_immediately(self, monkeypatch):
        # Loud at construction rather than at import, so a deployment that
        # never selects Apify does not need a token at all.
        with pytest.raises(ApifyError, match="APIFY_TOKEN"):
            ApifyClient(api_key="")

    def test_the_token_travels_as_a_bearer_header(self):
        assert client()._headers == {"Authorization": "Bearer test-token"}


class TestTheActorIdIsPathEncoded:
    @respx.mock
    def test_a_slash_is_rewritten_as_a_tilde(self):
        # Apify's own convention, and the single easiest thing to get wrong:
        # `clockworks/tiktok-scraper` 404s where `clockworks~tiktok-scraper`
        # works. Rewritten here so a caller can write either.
        route = respx.post(f"{API_BASE}/acts/clockworks~tiktok-scraper/runs").mock(
            return_value=httpx.Response(201, json={"data": {"id": "run-1"}})
        )
        client().start_run("clockworks/tiktok-scraper", {"hashtags": ["crm"]})
        assert route.called


class TestWhatTheStatusesMean:
    @respx.mock
    def test_a_rate_limit_is_retryable_and_says_so(self):
        respx.get(f"{API_BASE}/actor-runs/run-1").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        )
        with pytest.raises(ApifyRateLimited) as caught:
            client().run_status("run-1")
        assert caught.value.retry_after == 7

    @respx.mock
    def test_a_rejected_token_names_the_header_it_belongs_in(self):
        respx.get(f"{API_BASE}/actor-runs/run-1").mock(
            return_value=httpx.Response(401, text="unauthorized")
        )
        with pytest.raises(ApifyRefused, match="Bearer"):
            client().run_status("run-1")

    @respx.mock
    def test_a_missing_actor_names_the_tilde_convention(self):
        # The most likely cause of a 404 here is the id, not the URL.
        respx.post(f"{API_BASE}/acts/nobody~nothing/runs").mock(
            return_value=httpx.Response(404, text="not found")
        )
        with pytest.raises(ApifyRefused, match="username~actor-name"):
            client().start_run("nobody~nothing", {})

    @respx.mock
    def test_any_other_failure_is_an_error_rather_than_a_refusal(self):
        # Retrying a 500 can help; retrying a 401 cannot. The distinction is
        # the exception class, so the caller does not parse messages.
        respx.get(f"{API_BASE}/actor-runs/run-1").mock(return_value=httpx.Response(500))
        with pytest.raises(ApifyError) as caught:
            client().run_status("run-1")
        assert not isinstance(caught.value, ApifyRefused)

    @respx.mock
    def test_a_run_that_names_no_id_is_an_error_not_a_silent_success(self):
        respx.post(f"{API_BASE}/acts/a~b/runs").mock(
            return_value=httpx.Response(201, json={"data": {}})
        )
        with pytest.raises(ApifyError, match="named no run id"):
            client().start_run("a~b", {})


class TestReadingADataset:
    @respx.mock
    def test_items_come_back_as_a_list_and_the_limit_is_sent(self):
        route = respx.get(f"{API_BASE}/datasets/ds-1/items").mock(
            return_value=httpx.Response(200, json=[{"id": "1"}, {"id": "2"}])
        )
        items = client().dataset_items("ds-1", limit=30)
        assert items == [{"id": "1"}, {"id": "2"}]
        assert route.calls[0].request.url.params["limit"] == "30"

    @respx.mock
    def test_a_non_list_body_is_refused_rather_than_iterated(self):
        respx.get(f"{API_BASE}/datasets/ds-1/items").mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )
        with pytest.raises(ApifyError, match="not a list"):
            client().dataset_items("ds-1")

    @respx.mock
    def test_aborting_never_raises(self):
        # Called when the run is already leaving. Failing to abort costs money
        # but must not also cost the diagnostics the run was about to write.
        respx.post(f"{API_BASE}/actor-runs/run-1/abort").mock(
            return_value=httpx.Response(500, text="nope")
        )
        client().abort_run("run-1")


# ---------------------------------------------------------------------------
# The scout
# ---------------------------------------------------------------------------


def tiktok_item(
    author: str = "acct",
    plays: int = 10_000,
    video_id: str = "1",
    likes: int = 500,
    created: str = "2026-09-05T00:00:00.000Z",
) -> dict[str, Any]:
    return {
        "id": video_id,
        "text": "how we automated invoicing",
        "webVideoUrl": f"https://www.tiktok.com/@{author}/video/{video_id}",
        "playCount": plays,
        "diggCount": likes,
        "commentCount": 20,
        "shareCount": 10,
        "createTimeISO": created,
        "authorMeta": {"name": author},
    }


def instagram_item(
    author: str = "acct",
    plays: Any = 9000,
    post_id: str = "1",
    likes: Any = 400,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": post_id,
        "caption": "a reel about invoicing",
        "url": f"https://www.instagram.com/p/{post_id}/",
        "likesCount": likes,
        "commentsCount": 10,
        "timestamp": "2026-09-05T00:00:00.000Z",
        "ownerUsername": author,
    }
    if plays is not None:
        item["videoPlayCount"] = plays
    return item


class FakeApify:
    """Answers with scripted datasets, and records what it was asked for."""

    def __init__(self, datasets: list[list[dict[str, Any]]], states: list[str] | None = None):
        self._datasets = list(datasets)
        self._states = list(states or [])
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.aborted: list[str] = []

    def start_run(self, actor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.started.append((actor_id, payload))
        return {"id": f"run-{len(self.started)}"}

    def run_status(self, run_id: str) -> dict[str, Any]:
        state = self._states.pop(0) if self._states else "SUCCEEDED"
        return {"status": state, "defaultDatasetId": f"ds-{run_id}"}

    def abort_run(self, run_id: str) -> None:
        self.aborted.append(run_id)

    def dataset_items(self, dataset_id: str, limit: int = 0) -> list[dict[str, Any]]:
        return self._datasets.pop(0) if self._datasets else []


def scout(
    hashtags=("crm",),
    platforms=("tiktok",),
    datasets=None,
    controls=None,
    states=None,
    should_stop=None,
):
    fake = FakeApify(datasets or [[], []], states)
    outcome = apify.scout(
        apify.ApifyConfig(
            hashtags=list(hashtags),
            platforms=tuple(platforms),
            controls=controls or ScoutControls(),
            client=fake,
            should_stop=should_stop,
        )
    )
    return outcome, fake


class TestScoringAgainstTheAuthorsOwnMedian:
    def test_a_video_beating_its_authors_median_becomes_a_signal(self):
        # Phase one finds the candidate; phase two reads the author's recent
        # posts to get the denominator.
        candidates = [tiktok_item(plays=30_000)]
        history = [
            tiktok_item(video_id="h1", plays=5_000),
            tiktok_item(video_id="h2", plays=6_000),
            tiktok_item(video_id="h3", plays=4_000),
        ]
        outcome, _ = scout(datasets=[candidates, history])

        assert len(outcome.signals) == 1
        signal = outcome.signals[0]
        assert signal.plays == 30_000
        assert signal.ratio > 1.0

    def test_the_signal_names_the_platform_not_the_vendor(self):
        # An idea traced back to "apify" would tell the owner which invoice it
        # came from rather than which feed to go and look at.
        candidates = [tiktok_item(plays=30_000)]
        history = [tiktok_item(video_id=f"h{i}", plays=5_000) for i in range(3)]
        outcome, _ = scout(datasets=[candidates, history])

        assert outcome.signals[0].source == "tiktok"

    def test_the_keyword_is_the_hashtag_it_was_found_under(self):
        # `ideas._spread` groups by keyword to stop one hashtag filling a
        # batch, so this has to be the hashtag and not the platform.
        candidates = [tiktok_item(plays=30_000)]
        history = [tiktok_item(video_id=f"h{i}", plays=5_000) for i in range(3)]
        outcome, _ = scout(hashtags=("exceltips",), datasets=[candidates, history])

        assert outcome.signals[0].keyword == "exceltips"

    def test_an_author_with_no_readable_history_is_counted_not_scored(self):
        # No denominator, so `outlier_ratio` would answer 0.0 and the video
        # would fail every bar. Its own stage, because it is the one exclusion
        # the owner cannot fix by loosening anything.
        outcome, _ = scout(datasets=[[tiktok_item(plays=30_000)], []])

        assert outcome.signals == []
        assert outcome.report.dropped.get("no_baseline") == 1


class TestAbsenceIsNotALowScore:
    def test_an_instagram_post_with_no_view_count_is_not_blamed_on_a_filter(self):
        # The mistake this prevents: reporting "under your minimum view count"
        # about a number Instagram never published.
        outcome, _ = scout(
            platforms=("instagram",),
            datasets=[[instagram_item(plays=None)], []],
        )

        assert outcome.report.dropped.get("no_metrics") == 1
        assert "too_few_plays" not in outcome.report.dropped
        assert outcome.signals == []

    def test_a_hidden_like_count_never_produces_negative_engagement(self):
        # Instagram publishes -1 for hidden likes. Passed through, it makes the
        # engagement rate negative, which fails every threshold and gets
        # reported as `below_engagement`.
        controls = ScoutControls(min_engagement_rate=0.01)
        outcome, _ = scout(
            platforms=("instagram",),
            controls=controls,
            datasets=[[instagram_item(likes=-1)], []],
        )

        assert outcome.report.dropped.get("no_metrics") == 1
        assert "below_engagement" not in outcome.report.dropped

    def test_hidden_likes_are_survivable_when_no_engagement_bar_is_set(self):
        # With the floor at its default of zero the absence decides nothing,
        # and a perfectly good view-count signal is worth keeping.
        history = [instagram_item(post_id=f"h{i}", plays=1_000) for i in range(3)]
        outcome, _ = scout(
            platforms=("instagram",),
            controls=ScoutControls(min_engagement_rate=0.0),
            datasets=[[instagram_item(likes=-1, plays=9_000)], history],
        )

        assert "no_metrics" not in outcome.report.dropped
        assert len(outcome.signals) == 1

    def test_an_item_missing_its_author_or_url_does_not_raise(self):
        # The field names here are the actors' documented output but were never
        # exercised against a live API. A renamed field has to become a counted
        # rejection, not a traceback three frames away.
        outcome, _ = scout(datasets=[[{}, {"playCount": 10}], []])

        assert outcome.report.dropped.get("no_metrics") == 2
        assert outcome.signals == []


class TestTheEconomyOfARun:
    def test_the_baseline_scrape_is_one_run_for_every_author(self):
        # One profile run per platform rather than one per author. Batching is
        # what makes scoring against an author's own median affordable.
        candidates = [
            tiktok_item(author="a", video_id="1", plays=30_000),
            tiktok_item(author="b", video_id="2", plays=40_000),
            tiktok_item(author="c", video_id="3", plays=50_000),
        ]
        _, fake = scout(datasets=[candidates, []])

        assert len(fake.started) == 2
        _actor, payload = fake.started[1]
        assert sorted(payload["profiles"]) == ["a", "b", "c"]

    def test_no_baseline_scrape_happens_when_nothing_survived(self):
        # An author is worth a baseline only once one of their videos has
        # cleared the free filters. Otherwise this is money for nothing.
        controls = ScoutControls(min_plays=1_000_000)
        _, fake = scout(controls=controls, datasets=[[tiktok_item(plays=10)], []])

        assert len(fake.started) == 1

    def test_the_recency_bar_is_pushed_into_the_actor(self):
        # The actor filters by date server-side, so this is the same bar
        # applied before we are charged for the result rather than after.
        _, fake = scout(controls=ScoutControls(max_video_age_days=30))

        _, payload = fake.started[0]
        assert "oldestPostDateUnified" in payload

    def test_media_downloads_are_explicitly_off(self):
        # This actor bills per result and these flags pull files we never read.
        _, fake = scout()

        _, payload = fake.started[0]
        assert payload["shouldDownloadVideos"] is False
        assert payload["shouldDownloadCovers"] is False

    def test_each_platform_is_scraped_with_its_own_actor(self):
        _, fake = scout(platforms=("tiktok", "instagram"), datasets=[[], [], [], []])

        actors = [actor for actor, _ in fake.started]
        assert "clockworks~tiktok-scraper" in actors[0]
        assert "instagram" in actors[1]

    def test_the_two_actors_take_differently_named_limits(self):
        # `resultsPerPage` on one and `resultsLimit` on the other. They really
        # do differ, which is why the inputs are a table and not inline.
        _, fake = scout(platforms=("tiktok", "instagram"), datasets=[[], [], [], []])

        assert "resultsPerPage" in fake.started[0][1]
        assert "resultsLimit" in fake.started[1][1]


class TestStoppingEarly:
    def test_stopping_mid_scrape_aborts_the_remote_run(self):
        # An Apify run keeps scraping and keeps billing after we stop waiting
        # for it, so the only way Stop actually stops the spending is to say
        # so. Cancelled after the run has started, which is the case that
        # costs money -- stopping before it starts has nothing to abort.
        calls = iter([False, True, True, True])

        outcome, fake = scout(
            states=["RUNNING", "RUNNING"],
            datasets=[[tiktok_item()], []],
            should_stop=lambda: next(calls, True),
        )

        assert outcome.report.cancelled is True
        assert fake.aborted == ["run-1"]

    def test_stopping_before_anything_starts_spends_nothing(self):
        outcome, fake = scout(should_stop=lambda: True)

        assert outcome.report.cancelled is True
        assert fake.started == []
        assert fake.aborted == []

    def test_a_failed_actor_run_is_recorded_and_does_not_end_the_run(self):
        # "We were turned away" has to be distinguishable from "nothing was
        # trending", which is the whole reason `failed_hashtags` exists.
        outcome, _ = scout(
            hashtags=("crm", "erp"),
            states=["FAILED", "SUCCEEDED", "SUCCEEDED"],
            datasets=[[tiktok_item()], []],
        )

        assert len(outcome.report.failed_hashtags) == 1
        assert outcome.report.hashtags_scouted == ["crm", "erp"]
        # The platform belongs in the failure record, where it says which
        # scrape was refused, rather than in the scouted list.
        assert outcome.report.failed_hashtags[0]["hashtag"] == "tiktok:crm"

    def test_a_hashtag_is_counted_once_however_many_platforms_scrape_it(self):
        # `hashtags_scouted` is compared against the configured list to decide
        # whether rotation is on, so counting a tag twice would report scouting
        # more hashtags than exist.
        outcome, _ = scout(
            hashtags=("crm", "erp"),
            platforms=("tiktok", "instagram"),
            datasets=[[], [], [], []],
        )

        assert outcome.report.hashtags_scouted == ["crm", "erp"]


class TestTheReportAddsUp:
    def test_every_candidate_leaves_through_exactly_one_stage(self):
        # The funnel's one invariant: video-level drops plus survivors equal
        # what was seen. A report that does not add up invites loosening a
        # filter that rejected nothing.
        controls = ScoutControls(min_plays=20_000)
        candidates = [
            tiktok_item(video_id="1", plays=30_000, author="a"),
            tiktok_item(video_id="2", plays=100, author="b"),
            {},
        ]
        history = [tiktok_item(video_id=f"h{i}", author="a", plays=5_000) for i in range(3)]
        outcome, _ = scout(controls=controls, datasets=[candidates, history])

        report = outcome.report
        video_drops = sum(
            count for stage, count in report.dropped.items() if stage != "duplicate"
        )
        assert video_drops + len(outcome.signals) == report.seen

    def test_a_duplicate_url_is_not_counted_twice(self):
        outcome, _ = scout(datasets=[[tiktok_item(), tiktok_item()], []])

        assert outcome.report.seen == 1


def test_a_config_with_no_scrapeable_platform_is_a_programming_error():
    # `controls._platforms` refuses to produce this, so reaching it means a
    # caller built the config by hand.
    with pytest.raises(ValueError, match="no scrapeable platforms"):
        apify.scout(apify.ApifyConfig(hashtags=["crm"], platforms=(), client=FakeApify([])))
