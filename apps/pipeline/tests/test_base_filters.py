"""The filter chain every video source shares.

These used to be private to `tiktok.py`, where they were tested through a
browser-driving scout. Three sources apply them now, and the order they run in
is the funnel all three report into -- so it is worth pinning directly rather
than only through whichever provider happens to exercise it.
"""

from __future__ import annotations

import pytest

from pipeline.trends import base
from pipeline.trends import velocity as vel
from pipeline.trends.controls import BlocklistMatcher, ScoutControls
from pipeline.trends.report import STAGES, VIDEO_STAGES


def stats(plays=1000, likes=50, comments=5, shares=5) -> dict[str, int]:
    return {"plays": plays, "likes": likes, "comments": comments, "shares": shares}


class TestAnAbsentCounterIsNotAZero:
    """`opt_count`, and the one Instagram value that made it necessary.

    `velocity.coerce_count` answers 0 for anything it cannot read, which is
    right for an untyped passthrough and wrong for a hosted API: "nobody
    published this" and "published as zero" are different facts, and only one
    of them is a reason to apply a floor.
    """

    def test_a_missing_value_is_unknown_rather_than_zero(self):
        assert base.opt_count(None) is None

    def test_a_string_count_is_read(self):
        # Both APIs return counters as strings in places.
        assert base.opt_count("1234") == 1234

    def test_a_genuine_zero_survives(self):
        assert base.opt_count(0) == 0
        assert base.opt_count("0") == 0

    def test_instagrams_hidden_like_sentinel_is_unknown(self):
        # Instagram publishes -1 for a hidden like count. Passed through, it
        # makes `engagement_rate` negative, which fails every threshold and
        # gets reported as `below_engagement` -- a filter blamed for a number
        # nobody published.
        assert base.opt_count(-1) is None

    def test_unreadable_junk_is_unknown_rather_than_zero(self):
        assert base.opt_count("not a number") is None
        assert base.opt_count({}) is None


class TestWhichAbsencesAreFatal:
    """`usable_stats`. A view count is required; interactions are conditional."""

    def test_no_view_count_cannot_be_scored(self):
        assert (
            base.usable_stats(
                plays=None, likes=10, comments=1, shares=0, controls=ScoutControls()
            )
            is None
        )

    def test_missing_interactions_are_carried_as_zero_when_no_bar_is_set(self):
        # The default engagement floor is 0.0, so nothing is being decided by
        # the absence and a perfectly good view-count signal is worth keeping.
        controls = ScoutControls(min_engagement_rate=0.0)
        out = base.usable_stats(plays=900, likes=None, comments=None, shares=None, controls=controls)
        assert out == {"plays": 900, "likes": 0, "comments": 0, "shares": 0}

    def test_missing_interactions_are_fatal_once_a_bar_is_set(self):
        # With a floor above zero the absence would decide the outcome, and a
        # bar cannot be applied to a number that was not published.
        controls = ScoutControls(min_engagement_rate=0.02)
        assert (
            base.usable_stats(
                plays=900, likes=None, comments=3, shares=None, controls=controls
            )
            is None
        )

    def test_a_reported_zero_is_not_an_absence(self):
        controls = ScoutControls(min_engagement_rate=0.02)
        out = base.usable_stats(plays=900, likes=0, comments=0, shares=None, controls=controls)
        assert out == {"plays": 900, "likes": 0, "comments": 0, "shares": 0}


class TestTheOrderIsTheDiagnostic:
    """The cheap filters, in the order `STAGES` lists them.

    The order is load-bearing: the report's promise is that the first stage
    with a large count is the one to loosen, which only holds if a stage can
    reject only what the stages above it passed.
    """

    def test_age_is_checked_before_the_view_floor(self):
        controls = ScoutControls(max_video_age_days=30, min_plays=1_000_000)
        # Fails both bars. Attributed to the earlier one.
        assert (
            base.filter_cheaply(
                age=90.0,
                stats=stats(plays=1),
                caption="anything",
                controls=controls,
                blocklist=BlocklistMatcher(()),
            )
            == "too_old"
        )

    def test_the_view_floor_is_checked_before_the_blocklist(self):
        controls = ScoutControls(min_plays=5000)
        assert (
            base.filter_cheaply(
                age=1.0,
                stats=stats(plays=10),
                caption="my free course",
                controls=controls,
                blocklist=BlocklistMatcher(("course",)),
            )
            == "too_few_plays"
        )

    def test_a_blocked_word_is_matched_whole_and_case_insensitively(self):
        controls = ScoutControls()
        assert (
            base.filter_cheaply(
                age=1.0,
                stats=stats(),
                caption="My Free COURSE here",
                controls=controls,
                blocklist=BlocklistMatcher(("course",)),
            )
            == "blocked_caption"
        )

    def test_nothing_is_rejected_when_it_clears_everything(self):
        assert (
            base.filter_cheaply(
                age=1.0,
                stats=stats(),
                caption="a normal caption",
                controls=ScoutControls(),
                blocklist=BlocklistMatcher(()),
            )
            is None
        )

    def test_an_unreadable_timestamp_is_not_evidence_of_age(self):
        # `age_days(None)` answers YOUNG_DAYS, which passes any limit of a week
        # or more. Rejecting on a missing field would quietly discard whole
        # feeds the day a payload shape changed.
        age = vel.age_days(None)
        assert (
            base.filter_cheaply(
                age=age,
                stats=stats(),
                caption="",
                controls=ScoutControls(max_video_age_days=30),
                blocklist=BlocklistMatcher(()),
            )
            is None
        )

    def test_the_stages_are_listed_in_the_order_they_are_applied(self):
        # The whole reading of a breakdown depends on this: the first stage
        # with a large count is the one to loosen, which only holds if a stage
        # rejects nothing the stages above it already rejected.
        #
        # `no_metrics` leads because a provider checks it before anything else
        # -- an item with no published numbers cannot be judged by any bar --
        # then the three free filters in `filter_cheaply`, then the baseline
        # and the two scored bars.
        assert VIDEO_STAGES == (
            "no_metrics",
            "too_old",
            "too_few_plays",
            "blocked_caption",
            "no_baseline",
            "below_ratio",
            "below_engagement",
        )

    def test_every_stage_the_shared_code_names_exists(self):
        # `report.drop` raises for an unknown stage, so a typo here would only
        # surface on the run that first hit that filter.
        known = {key for key, _, _, _ in STAGES}
        assert {"too_old", "too_few_plays", "blocked_caption", "no_baseline", "no_metrics"} <= known


class TestWhichBarASignalFailed:
    """`attribute_failure`. Ratio first, so the counts still sum."""

    def signal(self, ratio: float, engagement: float) -> vel.Signal:
        return vel.Signal(
            source="test",
            source_url="https://example.com/1",
            title="t",
            keyword="k",
            plays=1000,
            ratio=ratio,
            engagement=engagement,
            age_days=1.0,
        )

    def test_a_signal_clearing_both_bars_is_not_rejected(self):
        controls = ScoutControls(min_outlier_ratio=1.5, min_engagement_rate=0.02)
        assert base.attribute_failure(self.signal(2.0, 0.05), controls) is None

    def test_failing_the_ratio_is_reported_against_the_ratio(self):
        controls = ScoutControls(min_outlier_ratio=1.5, min_engagement_rate=0.02)
        assert base.attribute_failure(self.signal(1.1, 0.05), controls) == "below_ratio"

    def test_failing_only_engagement_is_reported_against_engagement(self):
        controls = ScoutControls(min_outlier_ratio=1.5, min_engagement_rate=0.02)
        assert base.attribute_failure(self.signal(2.0, 0.001), controls) == "below_engagement"

    def test_failing_both_is_counted_once_against_the_first(self):
        # Counted twice, the stage totals would exceed `seen` and the funnel
        # would stop adding up -- which is the only thing that makes it
        # readable.
        controls = ScoutControls(min_outlier_ratio=1.5, min_engagement_rate=0.02)
        assert base.attribute_failure(self.signal(1.1, 0.001), controls) == "below_ratio"


class TestTheBudget:
    def test_no_budget_never_expires(self):
        assert base.Budget(None).exhausted is False

    def test_a_budget_expires_once_the_clock_passes_it(self, monkeypatch):
        clock = iter([100.0, 100.0, 105.0])
        monkeypatch.setattr(base.time, "monotonic", lambda: next(clock))
        budget = base.Budget(1.0)
        assert budget.exhausted is False
        assert budget.exhausted is True


@pytest.mark.parametrize("source", ["apify", "youtube", "google_trends"])
def test_every_source_can_still_name_the_outcome_type(source):
    # `ScoutOutcome` moved out of `tiktok.py` so that a source did not have to
    # import the TikTok module in order to name what it returns.
    from pipeline.trends import tiktok

    assert tiktok.ScoutOutcome is base.ScoutOutcome
