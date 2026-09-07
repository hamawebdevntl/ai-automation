"""YouTube: the two-kinds-of-403 trap, and a baseline that fits in the quota.

The client's whole job beyond fetching is telling apart three failures that all
arrive as HTTP 403 -- an exhausted daily quota, a burst limit, and a key that
was never going to work. Getting the first two the wrong way round is the most
expensive mistake available here: retrying an exhausted quota spends
*tomorrow's* allowance, and giving up on a burst limit throws away a run that a
few seconds would have finished.

The scout's job is to search once per keyword and never search again, because
searches are the scarce resource and everything else is nearly free.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from pipeline.clients.youtube import (
    API_BASE,
    YouTubeClient,
    YouTubeError,
    YouTubeQuotaExceeded,
    YouTubeRateLimited,
    YouTubeRefused,
)
from pipeline.trends import youtube
from pipeline.trends.controls import ScoutControls


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(youtube.time, "sleep", lambda _s: None)


def client() -> YouTubeClient:
    return YouTubeClient(api_key="test-key")


def error_body(reason: str, message: str = "nope") -> dict[str, Any]:
    return {"error": {"code": 403, "message": message, "errors": [{"reason": reason}]}}


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class TestTheKeyIsAHeaderNotAQueryParameter:
    def test_an_absent_key_refuses_immediately(self):
        with pytest.raises(YouTubeError, match="YOUTUBE_API_KEY"):
            YouTubeClient(api_key="")

    def test_the_key_is_sent_as_a_header(self):
        assert client()._headers == {"X-goog-api-key": "test-key"}

    @respx.mock
    def test_the_key_never_appears_in_a_url(self):
        # This client puts the request into its error messages, and those end
        # up in CloudWatch and in `trend_runs.error`. A key in the query string
        # would be a credential written to the database on every failure.
        route = respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client().search_videos("invoice software", limit=10)

        assert "key=" not in str(route.calls[0].request.url)
        assert route.calls[0].request.headers["X-goog-api-key"] == "test-key"


class TestTheThreeWaysA403CanMean:
    @respx.mock
    def test_an_exhausted_quota_is_terminal(self):
        respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(403, json=error_body("quotaExceeded"))
        )
        with pytest.raises(YouTubeQuotaExceeded):
            client().search_videos("q", limit=5)

    @respx.mock
    def test_the_quota_message_says_when_it_resets(self):
        # Retrying is pointless for hours, so the message has to say so rather
        # than reading like a transient failure.
        respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(403, json=error_body("quotaExceeded"))
        )
        with pytest.raises(YouTubeQuotaExceeded, match="Pacific"):
            client().search_videos("q", limit=5)

    @respx.mock
    def test_a_burst_limit_is_retryable_and_not_the_quota(self):
        respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(403, json=error_body("rateLimitExceeded"))
        )
        with pytest.raises(YouTubeRateLimited) as caught:
            client().search_videos("q", limit=5)
        assert not isinstance(caught.value, YouTubeQuotaExceeded)

    @respx.mock
    def test_a_project_without_the_api_enabled_says_which_api(self):
        respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(403, json=error_body("accessNotConfigured"))
        )
        with pytest.raises(YouTubeRefused, match="YouTube Data API v3"):
            client().search_videos("q", limit=5)

    @respx.mock
    def test_a_body_without_the_expected_shape_still_produces_a_readable_error(self):
        # The reason is nested three deep. A body that is not shaped that way
        # must not become an IndexError.
        respx.get(f"{API_BASE}/search").mock(return_value=httpx.Response(400, text="plain text"))
        with pytest.raises(YouTubeError):
            client().search_videos("q", limit=5)


class TestSearching:
    @respx.mock
    def test_it_asks_only_for_videos_within_the_recency_window(self):
        route = respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client().search_videos("q", limit=10, published_after="2026-08-08T00:00:00Z")

        params = route.calls[0].request.url.params
        assert params["type"] == "video"
        assert params["publishedAfter"] == "2026-08-08T00:00:00Z"
        assert params["order"] == "viewCount"

    @respx.mock
    def test_it_never_asks_for_more_than_the_api_returns(self):
        # Asking for more than 50 is not an error; it silently returns 50,
        # which would look like a short channel rather than a paging bug.
        route = respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client().search_videos("q", limit=500)

        assert route.calls[0].request.url.params["maxResults"] == "50"

    @respx.mock
    def test_only_real_video_ids_come_back(self):
        respx.get(f"{API_BASE}/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"id": {"videoId": "a"}},
                        {"id": {"channelId": "not-a-video"}},
                        {"nonsense": True},
                    ]
                },
            )
        )
        assert client().search_videos("q", limit=10) == ["a"]


class TestTheBaselineAvoidsSearchingAgain:
    @respx.mock
    def test_the_uploads_playlist_is_asked_for_rather_than_derived(self):
        # There is a widely-repeated trick that turns a UC… channel id into its
        # uploads playlist by swapping the prefix for UU. It works today and is
        # documented nowhere Google commits to; this saves one unit per fifty
        # channels, which is not worth depending on a string format for.
        route = respx.get(f"{API_BASE}/channels").mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "UCabc",
                            "contentDetails": {"relatedPlaylists": {"uploads": "UUabc"}},
                        }
                    ]
                },
            )
        )
        assert client().uploads_playlists(["UCabc"]) == {"UCabc": "UUabc"}
        assert route.called

    @respx.mock
    def test_channels_are_batched_into_one_call(self):
        route = respx.get(f"{API_BASE}/channels").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client().uploads_playlists([f"UC{i}" for i in range(50)])

        assert len(route.calls) == 1

    @respx.mock
    def test_an_unreadable_playlist_yields_nothing_rather_than_raising(self):
        # A channel with uploads hidden, or a terminated account. No baseline
        # is a recorded outcome upstream, not an error.
        respx.get(f"{API_BASE}/playlistItems").mock(
            return_value=httpx.Response(404, json=error_body("playlistNotFound"))
        )
        assert client().playlist_video_ids("UUgone", 12) == []

    @respx.mock
    def test_a_quota_failure_while_reading_a_playlist_is_not_swallowed(self):
        # That one is not about this channel, and hiding it would let the run
        # keep spending calls that cannot succeed.
        respx.get(f"{API_BASE}/playlistItems").mock(
            return_value=httpx.Response(403, json=error_body("quotaExceeded"))
        )
        with pytest.raises(YouTubeQuotaExceeded):
            client().playlist_video_ids("UUabc", 12)


class TestReadingStatistics:
    @respx.mock
    def test_ids_are_batched_fifty_at_a_time(self):
        route = respx.get(f"{API_BASE}/videos").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        client().videos([str(i) for i in range(120)])

        assert len(route.calls) == 3

    @respx.mock
    def test_an_empty_list_costs_no_call(self):
        route = respx.get(f"{API_BASE}/videos").mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        assert client().videos([]) == []
        assert not route.called


# ---------------------------------------------------------------------------
# The scout
# ---------------------------------------------------------------------------


def video(
    video_id: str = "v1",
    channel: str = "UCa",
    views: Any = 50_000,
    likes: Any = 2_000,
    comments: Any = 100,
    published: str = "2026-09-05T00:00:00Z",
    title: str = "How we automated invoicing",
) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if views is not None:
        stats["viewCount"] = str(views)
    if likes is not None:
        stats["likeCount"] = str(likes)
    if comments is not None:
        stats["commentCount"] = str(comments)
    return {
        "id": video_id,
        "snippet": {"channelId": channel, "title": title, "publishedAt": published},
        "statistics": stats,
    }


class FakeYouTube:
    """Scripted answers, and a record of every call and its cost."""

    def __init__(
        self,
        searches: dict[str, list[str]] | None = None,
        videos_by_id: dict[str, dict[str, Any]] | None = None,
        history: dict[str, list[str]] | None = None,
        search_error: Exception | None = None,
    ):
        self._searches = searches or {}
        self._videos = videos_by_id or {}
        self._history = history or {}
        self._search_error = search_error
        self.search_calls: list[str] = []
        self.playlist_calls: list[str] = []
        self.video_calls: list[list[str]] = []

    def search_videos(self, query, *, limit, published_after="", order="viewCount"):
        self.search_calls.append(query)
        if self._search_error is not None:
            raise self._search_error
        return list(self._searches.get(query, []))

    def videos(self, video_ids):
        self.video_calls.append(list(video_ids))
        return [self._videos[v] for v in video_ids if v in self._videos]

    def uploads_playlists(self, channel_ids):
        return {c: f"UU{c[2:]}" for c in channel_ids}

    def playlist_video_ids(self, playlist_id, limit):
        self.playlist_calls.append(playlist_id)
        return list(self._history.get(playlist_id, []))


def scout(keywords=("invoice software",), controls=None, fake=None, should_stop=None):
    fake = fake or FakeYouTube()
    outcome = youtube.scout(
        youtube.YouTubeConfig(
            keywords=list(keywords),
            controls=controls or ScoutControls(),
            client=fake,
            should_stop=should_stop,
        )
    )
    return outcome, fake


class TestScoringAgainstTheChannelsOwnMedian:
    def test_a_video_beating_its_channels_median_becomes_a_signal(self):
        history_ids = ["h1", "h2", "h3"]
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={
                "v1": video("v1", views=90_000),
                **{h: video(h, views=10_000) for h in history_ids},
            },
            history={"UUa": history_ids},
        )
        outcome, _ = scout(fake=fake)

        assert len(outcome.signals) == 1
        assert outcome.signals[0].source == "youtube"
        assert outcome.signals[0].ratio > 1.0

    def test_the_keyword_is_the_search_term(self):
        fake = FakeYouTube(
            searches={"crm software": ["v1"]},
            videos_by_id={"v1": video("v1", views=90_000), "h1": video("h1", views=1_000)},
            history={"UUa": ["h1"]},
        )
        outcome, _ = scout(keywords=("crm software",), fake=fake)

        assert outcome.signals[0].keyword == "crm software"

    def test_a_channel_with_no_readable_history_is_counted_not_scored(self):
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1")},
            history={},
        )
        outcome, _ = scout(fake=fake)

        assert outcome.signals == []
        assert outcome.report.dropped.get("no_baseline") == 1


class TestAbsenceIsNotALowScore:
    def test_a_video_whose_statistics_are_hidden_is_not_blamed_on_a_filter(self):
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1", views=None)},
        )
        outcome, _ = scout(fake=fake)

        assert outcome.report.dropped.get("no_metrics") == 1
        assert "too_few_plays" not in outcome.report.dropped

    def test_hidden_likes_survive_when_no_engagement_bar_is_set(self):
        # YouTube reports no shares at all and an uploader can hide likes. With
        # the floor at zero none of that decides anything.
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1", views=90_000, likes=None), "h1": video("h1", views=1_000)},
            history={"UUa": ["h1"]},
        )
        outcome, _ = scout(controls=ScoutControls(min_engagement_rate=0.0), fake=fake)

        assert len(outcome.signals) == 1

    def test_hidden_likes_are_not_reported_as_low_engagement(self):
        # The bar cannot be applied to a number the uploader hid, and blaming
        # the engagement filter would point at the wrong setting.
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1", views=90_000, likes=None)},
        )
        outcome, _ = scout(controls=ScoutControls(min_engagement_rate=0.02), fake=fake)

        assert outcome.report.dropped.get("no_metrics") == 1
        assert "below_engagement" not in outcome.report.dropped

    def test_a_video_search_named_but_could_not_be_read_is_counted(self):
        # `search.list` can name a video that `videos.list` will not return --
        # deleted, private, or region-blocked between the two calls. A large
        # count here means the search finds things we cannot read.
        fake = FakeYouTube(searches={"invoice software": ["v1", "gone"]}, videos_by_id={})
        outcome, _ = scout(fake=fake)

        assert outcome.report.seen == 2
        assert outcome.report.dropped.get("no_metrics") == 2

    def test_an_unparseable_timestamp_does_not_turn_the_recency_filter_off(self):
        # `fromisoformat` could not read a trailing Z before Python 3.11 and
        # this package supports 3.10. Getting it wrong would make every video
        # look brand new and extrapolate every ratio.
        assert youtube._published("2026-09-05T00:00:00Z") is not None
        assert youtube._published("not a date") is None
        assert youtube._published(None) is None


class TestStayingInsideTheQuota:
    def test_one_search_per_keyword_and_no_more(self):
        fake = FakeYouTube(searches={})
        _, fake = scout(keywords=("a", "b", "c"), fake=fake)

        assert fake.search_calls == ["a", "b", "c"]

    def test_the_baseline_never_costs_another_search(self):
        # The whole reason the uploads playlist is used: searching within a
        # channel would spend one of the day's hundred searches per channel.
        history_ids = ["h1", "h2", "h3"]
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={
                "v1": video("v1", views=90_000),
                **{h: video(h, views=10_000) for h in history_ids},
            },
            history={"UUa": history_ids},
        )
        scout(fake=fake)

        assert fake.search_calls == ["invoice software"]
        assert fake.playlist_calls == ["UUa"]

    def test_a_channel_is_only_read_once_however_many_of_its_videos_appear(self):
        fake = FakeYouTube(
            searches={"invoice software": ["v1", "v2", "v3"]},
            videos_by_id={
                "v1": video("v1", views=90_000),
                "v2": video("v2", views=80_000),
                "v3": video("v3", views=70_000),
                "h1": video("h1", views=10_000),
            },
            history={"UUa": ["h1"]},
        )
        scout(fake=fake)

        assert fake.playlist_calls == ["UUa"]

    def test_no_baseline_is_read_for_a_cheaply_rejected_candidate(self):
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1", views=100)},
        )
        scout(controls=ScoutControls(min_plays=50_000), fake=fake)

        assert fake.playlist_calls == []

    def test_a_run_stops_short_of_the_daily_allowance(self):
        # The thing this protects is not this run: spending the whole allowance
        # would make the owner's next button press fail tomorrow, for a reason
        # nothing in today's report mentioned.
        keywords = [f"term{i}" for i in range(youtube.MAX_SEARCHES_PER_RUN + 5)]
        outcome, fake = scout(keywords=keywords)

        assert len(fake.search_calls) == youtube.MAX_SEARCHES_PER_RUN
        assert outcome.report.hashtags_skipped == 5

    def test_an_exhausted_quota_keeps_what_was_already_found(self):
        # A partial batch of real signals is worth more than an empty queue.
        history_ids = ["h1"]
        fake = FakeYouTube(
            searches={"a": ["v1"]},
            videos_by_id={"v1": video("v1", views=90_000), "h1": video("h1", views=1_000)},
            history={"UUa": history_ids},
        )
        outcome, _ = scout(keywords=("a",), fake=fake)
        assert len(outcome.signals) == 1

        exhausted = FakeYouTube(search_error=YouTubeQuotaExceeded("gone"))
        outcome, _ = scout(keywords=("a", "b"), fake=exhausted)

        assert outcome.report.quota_exhausted is True
        # Not the wall clock. Saying "your run was too short" about an
        # exhausted quota would recommend the one change that cannot help.
        assert outcome.report.budget_exhausted is False

    def test_an_ordinary_failure_does_not_end_the_run(self):
        fake = FakeYouTube(search_error=YouTubeError("one bad term"))
        outcome, fake = scout(keywords=("a", "b"), fake=fake)

        assert len(fake.search_calls) == 2
        assert len(outcome.report.failed_hashtags) == 2


class TestStoppingEarly:
    def test_a_stopped_run_drafts_nothing(self):
        outcome, fake = scout(keywords=("a", "b"), should_stop=lambda: True)

        assert outcome.report.cancelled is True
        assert fake.search_calls == []
        assert outcome.signals == []


class TestTheReportAddsUp:
    def test_every_candidate_leaves_through_exactly_one_stage(self):
        fake = FakeYouTube(
            searches={"invoice software": ["v1", "v2", "gone"]},
            videos_by_id={
                "v1": video("v1", views=90_000),
                "v2": video("v2", views=10),
                "h1": video("h1", views=1_000),
            },
            history={"UUa": ["h1"]},
        )
        outcome, _ = scout(controls=ScoutControls(min_plays=1_000), fake=fake)

        report = outcome.report
        video_drops = sum(c for stage, c in report.dropped.items() if stage != "duplicate")
        assert video_drops + len(outcome.signals) == report.seen

    def test_the_blocklist_is_matched_against_the_title(self):
        # Not the description, which is mostly affiliate links and chapter
        # lists -- matching a blocklist against all of it would reject nearly
        # everything for a reason the breakdown could not localise.
        fake = FakeYouTube(
            searches={"invoice software": ["v1"]},
            videos_by_id={"v1": video("v1", title="My free course on invoicing")},
        )
        outcome, _ = scout(controls=ScoutControls(caption_blocklist=("course",)), fake=fake)

        assert outcome.report.dropped.get("blocked_caption") == 1
