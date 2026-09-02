"""Tests for per-platform copy assembly.

The generation itself is an LLM call; what is testable -- and what will
actually break in production -- is the fitting of captions and hashtags inside
each provider's hard limit. Exceeding one is rejected at publish time, which
would strand a production that has already been paid for and approved.
"""

from __future__ import annotations

import pytest

from pipeline.copy import CAPTION_LIMIT, _compose_caption, _hashtags


class TestHashtagNormalisation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (["ai", "#tech"], ["#ai", "#tech"]),
            ("ai tech", ["#ai", "#tech"]),
            (None, []),
            ([], []),
            (["  ", "x"], ["#x"]),
        ],
        ids=["mixed-prefix", "space-separated-string", "none", "empty", "blank-entry"],
    )
    def test_it_normalises_whatever_the_model_returned(self, raw, expected):
        assert _hashtags(raw) == expected


class TestCaptionComposition:
    def test_everything_fits_when_there_is_room(self):
        assert _compose_caption("Hello world", ["#a", "#b"], 100) == "Hello world #a #b"

    def test_hashtags_are_dropped_rather_than_the_caption_trimmed(self):
        # Losing words to make room for a hashtag is the wrong trade.
        assert _compose_caption("Hello", ["#aaaaaaaaaa", "#b"], 12) == "Hello"

    def test_it_stops_at_the_first_hashtag_that_does_not_fit(self):
        assert _compose_caption("Hi", ["#a", "#bbbbbbbbbbbbbbbbbb", "#c"], 8) == "Hi #a"

    def test_an_over_long_caption_is_trimmed_to_the_limit(self):
        out = _compose_caption("x" * 50, ["#tag"], 20)
        assert out == "x" * 20

    def test_hashtags_alone_are_valid_when_there_is_no_caption(self):
        assert _compose_caption("", ["#a", "#b"], 100) == "#a #b"


class TestLimits:
    def test_every_target_platform_has_a_limit(self):
        assert set(CAPTION_LIMIT) == {"instagram", "tiktok", "youtube", "linkedin"}

    def test_the_limits_match_what_the_providers_actually_enforce(self):
        assert CAPTION_LIMIT["instagram"] == 2200
        assert CAPTION_LIMIT["tiktok"] == 2000
        assert CAPTION_LIMIT["youtube"] == 5000   # description
        assert CAPTION_LIMIT["linkedin"] == 3000
