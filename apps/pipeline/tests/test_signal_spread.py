"""Which signals reach the model when there are more than fit.

The hashtag list is deliberately spread across industries, and this is the last
place that spread can be silently lost: a plain top-N by ratio can hand the
model forty signals from one hashtag, because ratio is per-author and some
rooms produce high ratios cheaply.
"""

from __future__ import annotations

from pipeline.trends.ideas import MAX_SIGNALS, _spread
from pipeline.trends.velocity import Signal


def sig(keyword: str, ratio: float) -> Signal:
    return Signal(
        source="tiktok",
        source_url=f"https://tiktok.com/@a/video/{keyword}{ratio}",
        title=f"{keyword} at {ratio}x",
        keyword=keyword,
        plays=1000,
        ratio=ratio,
        engagement=0.1,
        age_days=2.0,
    )


def ranked(keyword: str, n: int, start: float = 9.0) -> list[Signal]:
    """`n` signals for one keyword, strongest first, as the scout sorts them."""
    return [sig(keyword, start - i * 0.1) for i in range(n)]


class TestOneHashtagCannotTakeTheWholeBatch:
    def test_a_dominant_keyword_does_not_crowd_the_others_out(self):
        # Every signal from `crm` outranks every signal from the other two, so
        # a top-N slice would be entirely `crm`.
        signals = ranked("crm", 50, start=9.0) + ranked("lawfirm", 10, start=3.0) + ranked("medspa", 10, start=2.0)
        signals.sort(key=lambda s: s.ratio, reverse=True)

        chosen = _spread(signals, MAX_SIGNALS)

        assert len(chosen) == MAX_SIGNALS
        assert {s.keyword for s in chosen} == {"crm", "lawfirm", "medspa"}
        # Each weaker room keeps everything it had rather than being truncated.
        assert sum(s.keyword == "lawfirm" for s in chosen) == 10
        assert sum(s.keyword == "medspa" for s in chosen) == 10

    def test_the_strongest_signal_overall_is_still_first(self):
        signals = ranked("crm", 5, start=9.0) + ranked("lawfirm", 5, start=3.0)
        signals.sort(key=lambda s: s.ratio, reverse=True)

        assert _spread(signals, MAX_SIGNALS)[0].ratio == 9.0

    def test_ratio_order_is_kept_within_a_keyword(self):
        signals = ranked("crm", 6, start=9.0) + ranked("lawfirm", 6, start=8.0)
        signals.sort(key=lambda s: s.ratio, reverse=True)

        crm = [s.ratio for s in _spread(signals, MAX_SIGNALS) if s.keyword == "crm"]
        assert crm == sorted(crm, reverse=True)


class TestItStillBehavesLikeASlice:
    def test_it_never_returns_more_than_the_limit(self):
        signals = ranked("crm", 30) + ranked("lawfirm", 30)
        assert len(_spread(signals, MAX_SIGNALS)) == MAX_SIGNALS

    def test_everything_is_kept_when_it_all_fits(self):
        signals = ranked("crm", 3) + ranked("lawfirm", 4)
        assert len(_spread(signals, MAX_SIGNALS)) == 7

    def test_no_signal_is_duplicated_or_invented(self):
        signals = ranked("crm", 25) + ranked("lawfirm", 25)
        chosen = _spread(signals, MAX_SIGNALS)

        assert len({s.source_url for s in chosen}) == len(chosen)
        assert all(s in signals for s in chosen)

    def test_a_single_keyword_degrades_to_a_plain_slice(self):
        signals = ranked("crm", 50)
        assert _spread(signals, MAX_SIGNALS) == signals[:MAX_SIGNALS]

    def test_no_signals_is_not_an_error(self):
        assert _spread([], MAX_SIGNALS) == []

    def test_keywords_are_grouped_case_insensitively(self):
        # `_inputs` lowercases what it reads, but a row written by hand may not
        # have, and "CRM" and "crm" are one room.
        signals = [sig("CRM", 9.0), sig("crm", 8.0), sig("lawfirm", 7.0)]
        chosen = _spread(signals, 2)

        assert [s.keyword for s in chosen] == ["CRM", "lawfirm"]
