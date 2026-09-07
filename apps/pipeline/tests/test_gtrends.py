"""Scouting search demand instead of video formats.

This source exists because the other one stopped working, and its risks are
different rather than smaller. Three things matter here.

Google rate-limits without warning and answers 429 rather than slowing down,
so requests are batched five terms at a time and a refused batch must cost that
batch rather than the run.

Interest is normalised *within a single request*, so two terms are only
comparable when they were asked for together. Nothing here may compare a value
from one batch against a value from another.

And a term with too little search volume is absent from the response rather
than zero. That is an absence, not a low score, and reporting it as a low score
would tell the owner to loosen a filter that had nothing to do with it.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.trends import gtrends
from pipeline.trends.controls import ScoutControls


class FakeFrame:
    """Enough of a pandas DataFrame for the scout: `in` and `[col].tolist()`."""

    def __init__(self, columns: dict[str, list[float]]) -> None:
        self._columns = columns
        self.empty = not columns

    def __contains__(self, key: str) -> bool:
        return key in self._columns

    def __getitem__(self, key: str) -> Any:
        values = self._columns[key]
        return type("Col", (), {"tolist": lambda self, v=values: list(v)})()


class FakeTrends:
    """Records what was asked for, and answers with what it was told to."""

    def __init__(self, series: dict[str, list[float]], errors: list[Exception] | None = None) -> None:
        self.series = series
        self.errors = errors or []
        self.payloads: list[dict[str, Any]] = []
        self._batch: list[str] = []

    def build_payload(self, kw_list, timeframe: str = "", geo: str = "", **_kw: Any) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self._batch = list(kw_list)
        self.payloads.append({"kw": list(kw_list), "timeframe": timeframe, "geo": geo})

    def interest_over_time(self) -> FakeFrame:
        return FakeFrame({k: self.series[k] for k in self._batch if k in self.series})


class TooManyRequestsError(Exception):
    """Named to match what pytrends raises; the scout matches on the name."""


def rising(n: int = 20) -> list[float]:
    """A series whose recent quarter clearly beats its earlier three."""
    return [10.0] * (n * 3 // 4) + [40.0] * (n - n * 3 // 4)


def flat(n: int = 20) -> list[float]:
    return [20.0] * n


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Neither the pacing nor the rate-limit backoff should cost test time."""
    monkeypatch.setattr(gtrends.time, "sleep", lambda _s: None)


def scout(client: FakeTrends, keywords: list[str], controls: ScoutControls | None = None, geo: str = ""):
    return gtrends.scout(
        gtrends.GTrendsConfig(
            keywords=keywords, controls=controls or ScoutControls(), geo=geo, client=client
        )
    )


class TestScoring:
    def test_a_rising_term_becomes_a_signal(self):
        client = FakeTrends({"invoice software": rising()})

        outcome = scout(client, ["invoice software"])

        assert len(outcome.signals) == 1
        signal = outcome.signals[0]
        assert signal.source == "google_trends"
        assert signal.keyword == "invoice software"
        assert signal.ratio > 1.5

    def test_a_flat_term_does_not_clear_the_bar(self):
        client = FakeTrends({"crm software": flat()})

        outcome = scout(client, ["crm software"])

        assert outcome.signals == []
        assert outcome.report.dropped == {"below_ratio": 1}

    def test_the_owner_s_own_ratio_threshold_is_used(self):
        client = FakeTrends({"a": rising(), "b": rising()})

        strict = scout(client, ["a"], ScoutControls(min_outlier_ratio=50.0))
        assert strict.signals == []

        loose = scout(FakeTrends({"a": rising()}), ["a"], ScoutControls(min_outlier_ratio=1.2))
        assert len(loose.signals) == 1

    def test_engagement_and_age_are_zero_rather_than_invented(self):
        # Google Trends has neither. Inventing a value would let
        # `min_engagement_rate` or the recency limit silently reject
        # everything, and the report would blame a filter the owner set for a
        # different source entirely.
        outcome = scout(FakeTrends({"a": rising()}), ["a"])

        assert outcome.signals[0].engagement == 0.0
        assert outcome.signals[0].age_days == 0.0

    def test_the_source_url_points_at_the_term(self):
        outcome = scout(FakeTrends({"invoice software": rising()}), ["invoice software"], geo="GB")

        url = outcome.signals[0].source_url
        assert "invoice+software" in url
        assert "geo=GB" in url


class TestAbsenceIsNotALowScore:
    def test_a_term_google_has_no_data_for_is_its_own_stage(self):
        # Absent from the response, not zero. Counting it as a weak signal
        # would point the owner at the ratio threshold, which had nothing to do
        # with it.
        client = FakeTrends({})

        outcome = scout(client, ["something nobody searches"])

        assert outcome.signals == []
        assert outcome.report.dropped == {"no_baseline": 1}

    def test_a_series_too_short_to_score_is_the_same_absence(self):
        client = FakeTrends({"a": [1.0, 2.0]})

        assert scout(client, ["a"]).report.dropped == {"no_baseline": 1}


class TestFiltersThatStillApply:
    def test_the_interest_floor_is_its_own_setting(self):
        client = FakeTrends({"a": [1.0] * 15 + [5.0] * 5})

        outcome = scout(client, ["a"], ScoutControls(min_interest=50, min_outlier_ratio=1.0))

        assert outcome.signals == []
        assert outcome.report.dropped == {"too_few_plays": 1}

    def test_a_view_count_floor_cannot_reject_a_search_term(self):
        # The bug this replaced. A view count is unbounded and a sensible video
        # floor is six figures; Trends interest is 0-100 against the term's own
        # peak. Sharing one column meant min_plays=198000 rejected every term
        # that will ever exist -- and the first live run dropped twelve of
        # fifteen that way, with Google having answered all fifteen.
        client = FakeTrends({"a": rising()})

        outcome = scout(client, ["a"], ScoutControls(min_plays=198_000))

        assert len(outcome.signals) == 1

    def test_a_blocked_word_rejects_the_term(self):
        client = FakeTrends({"free course": rising()})

        outcome = scout(client, ["free course"], ScoutControls(caption_blocklist=("course",)))

        assert outcome.report.dropped == {"blocked_caption": 1}


class TestStayingInsideTheRateLimit:
    def test_each_term_is_asked_for_on_its_own(self):
        # Google normalises interest across the terms in a request, so a batch
        # is scored against its biggest member and everything else becomes
        # noise. A solo request normalises a term against its own history,
        # which is what window_velocity actually compares.
        client = FakeTrends({f"t{i}": flat() for i in range(12)})

        scout(client, [f"t{i}" for i in range(12)])

        assert [len(p["kw"]) for p in client.payloads] == [1] * 12

    def test_pacing_never_goes_below_the_google_floor(self, monkeypatch):
        # The pacing settings were calibrated for TikTok, where a short delay
        # is defensible camouflage. Google answers 429 rather than slowing
        # down, so its floor is not the owner's to lower.
        waited: list[float] = []
        monkeypatch.setattr(gtrends.time, "sleep", lambda s: waited.append(s))
        client = FakeTrends({"a": flat(), "b": flat()})

        scout(client, ["a", "b"], ScoutControls(pacing_min_seconds=1.0, pacing_max_seconds=1.0))

        assert waited and min(waited) >= gtrends.MIN_PACING_S

    def test_a_rate_limited_batch_is_retried_once_then_recorded(self):
        client = FakeTrends(
            {"a": rising()},
            errors=[TooManyRequestsError("429"), TooManyRequestsError("429")],
        )

        outcome = scout(client, ["a"])

        assert outcome.signals == []
        assert len(outcome.report.failed_hashtags) == 1
        assert "TooManyRequests" in outcome.report.failed_hashtags[0]["error"]

    def test_a_retry_that_succeeds_keeps_the_batch(self):
        client = FakeTrends({"a": rising()}, errors=[TooManyRequestsError("429")])

        outcome = scout(client, ["a"])

        assert len(outcome.signals) == 1
        assert outcome.report.failed_hashtags == []

    def test_one_refused_batch_does_not_cost_the_others(self):
        client = FakeTrends(
            {f"t{i}": rising() for i in range(10)},
            errors=[RuntimeError("boom")],
        )

        outcome = scout(client, [f"t{i}" for i in range(10)])

        # The first term is lost, the other nine are scouted.
        assert len(outcome.signals) == 9
        assert len(outcome.report.failed_hashtags) == 1

    def test_a_refused_run_is_distinguishable_from_a_quiet_one(self):
        # The distinction the whole report exists for, carried over intact from
        # the video source.
        refused = scout(FakeTrends({}, errors=[RuntimeError("nope")]), ["a"]).report
        quiet = scout(FakeTrends({"a": flat()}), ["a"]).report

        assert refused.failed_hashtags and not quiet.failed_hashtags
        assert refused.seen == 0
        assert quiet.seen == 1

    def test_the_timeframe_and_region_are_passed_through(self):
        client = FakeTrends({"a": rising()})

        scout(client, ["a"], geo="GB-ENG")

        assert client.payloads[0]["timeframe"] == gtrends.TIMEFRAME
        assert client.payloads[0]["geo"] == "GB-ENG"


class TestStoppingEarly:
    def test_being_stopped_gives_up_the_remaining_batches(self):
        stopped = {"yet": False}

        def should_stop() -> bool:
            was = stopped["yet"]
            stopped["yet"] = True
            return was

        client = FakeTrends({f"t{i}": rising() for i in range(15)})
        outcome = gtrends.scout(
            gtrends.GTrendsConfig(
                keywords=[f"t{i}" for i in range(15)], client=client, should_stop=should_stop
            )
        )

        assert outcome.report.cancelled is True
        assert outcome.report.hashtags_skipped == 14

    def test_no_keywords_is_an_empty_outcome_rather_than_a_request(self):
        client = FakeTrends({})

        outcome = scout(client, ["", "   "])

        assert outcome.signals == []
        assert client.payloads == []


class TestTheReportNamesTheRightSetting:
    """A breakdown that blames the wrong filter is worse than none.

    The stage keys are shared because the funnel has the same shape whatever is
    scouted. What a stage is called, and which setting caused it, is not --
    telling the owner "under your minimum view count: 198000" about a search
    trend sends them to a filter that had nothing to do with it.
    """

    def make(self, dropped: dict[str, int], controls: ScoutControls):
        from pipeline.trends.report import ScoutReport, payload

        report = ScoutReport(seen=sum(dropped.values()))
        for stage, count in dropped.items():
            report.drop(stage, count)
        return payload(
            report, controls, surfaced=0, drafted=0, inserted=0, hashtags_configured=1,
            source="google_trends",
        )

    def stage(self, doc, key):
        return next(s for s in doc["stages"] if s["key"] == key)

    def test_the_interest_stage_names_min_interest(self):
        doc = self.make({"too_few_plays": 3}, ScoutControls(min_interest=20, min_plays=198_000))
        stage = self.stage(doc, "too_few_plays")

        assert stage["setting"] == "min_interest"
        assert stage["value"] == 20
        assert "search interest" in stage["label"].lower()

    def test_filters_this_source_cannot_apply_name_no_setting(self):
        # Engagement and recency are not reported by Google Trends. Showing the
        # owner's video-era values next to them would imply they are in force.
        doc = self.make({}, ScoutControls(min_engagement_rate=0.05, max_video_age_days=14))

        for key in ("below_engagement", "too_old"):
            stage = self.stage(doc, key)
            assert stage["setting"] is None
            assert stage["value"] is None

    def test_a_video_source_keeps_the_original_wording(self):
        from pipeline.trends.report import ScoutReport, payload

        report = ScoutReport(seen=1)
        report.drop("too_few_plays")
        doc = payload(
            report, ScoutControls(min_plays=50_000), surfaced=0, drafted=0, inserted=0,
            hashtags_configured=1, source="tiktok",
        )
        stage = next(s for s in doc["stages"] if s["key"] == "too_few_plays")

        assert stage["setting"] == "min_plays"
        assert stage["value"] == 50_000

    def test_the_document_records_which_source_ran(self):
        assert self.make({}, ScoutControls())["source"] == "google_trends"
