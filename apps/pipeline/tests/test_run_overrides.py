"""A run's requested length, from the row to the scout.

`with_run_overrides` is unit-tested next to the other controls. This is the
wiring around it, which is where a per-run setting is most likely to be quietly
lost: the runner has to read its own row, merge the overrides, and then use the
merged values -- both for the scout config it builds and for the rotation it
performs before scouting starts.

Getting that wrong is invisible from the outside. The run still works, still
reports, and still fills the queue; it just ignores the length that was asked
for, which is exactly the complaint that is impossible to distinguish from a
slow platform.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.trends import apify, gtrends, runner, youtube
from pipeline.trends.base import ScoutOutcome
from pipeline.trends.report import ScoutReport


class FakeCfg:
    niche_brief = "we automate operations for small businesses"
    tiktok_ms_token = None
    idea_provider = "claude"
    gemini_api_key = ""
    # The credential preflight runs before the scout, so a source under test
    # has to look configured. Set here rather than per test because which
    # source is chosen is the subject and the keys are not.
    apify_token = "test-token"
    youtube_api_key = "test-key"
    ideas_per_run = 10

    @property
    def hashtag_list(self) -> list[str]:
        return []


# Search terms rather than hashtags: Google Trends is the default source, and
# each source reads its own list.
SIXTEEN = [f"term {i}" for i in range(16)]


class FakeSupa:
    def __init__(self, run_row: dict[str, Any] | None, settings_row: dict[str, Any] | None = None) -> None:
        self._run = run_row
        self._settings = settings_row if settings_row is not None else {
            "niche_brief": "we automate operations for small businesses",
            "trend_keywords": SIXTEEN,
        }
        self.cursor_saves: list[int] = []

    def trend_settings(self) -> dict[str, Any] | None:
        return self._settings

    def trend_run(self, _run_id: str) -> dict[str, Any] | None:
        return self._run

    def save_hashtag_cursor(self, cursor: int) -> None:
        self.cursor_saves.append(cursor)

    def trend_run_is_cancelled(self, _run_id: str) -> bool:
        return False

    def enabled_platforms(self) -> list[str]:
        return ["instagram"]


@pytest.fixture
def captured(monkeypatch):
    """Run the runner with the scout replaced by a recorder.

    An empty outcome on purpose: this is about what the scout was *asked* to
    do, and returning no signals keeps drafting and duplicate suppression out
    of a test that is not about either.
    """
    seen: dict[str, Any] = {}

    def fake_scout(config):
        seen["config"] = config
        return ScoutOutcome(signals=[], report=ScoutReport())

    # Every one of them, so the fixture does not quietly decide which source is
    # under test. `seen["config"]` is whichever one the runner actually chose,
    # and its type is how a test can tell.
    #
    # Patched on the provider modules rather than through `runner`, which no
    # longer imports them: the registry in `sources.py` is what names a scout
    # now. These are the same module objects the registry closed over, so the
    # substitution still takes effect.
    for module in (apify, gtrends, youtube):
        monkeypatch.setattr(module, "scout", fake_scout)
    monkeypatch.setattr(runner, "settings", lambda: FakeCfg())
    monkeypatch.setattr(runner.ideas_mod, "resolve_provider", lambda *_a, **_kw: "claude")
    return seen


def override(budget: int | None = None, hashtags: int | None = None) -> dict[str, Any]:
    return {"override_run_budget_minutes": budget, "override_hashtags_per_run": hashtags}


class TestTheRequestedLengthReachesTheScout:
    def test_the_budget_from_the_row_is_what_the_scout_is_given(self, captured):
        supa = FakeSupa(override(budget=14, hashtags=4))

        runner.run(supa, run_id="run-1")

        controls = captured["config"].controls
        assert controls.run_budget_minutes == 14
        assert controls.run_budget_seconds == 840.0

    def test_the_hashtag_count_actually_shortens_the_scout(self, captured):
        # The lever that decides the cost. Merging it into the controls but
        # rotating on the saved value would leave a "Quick" run scouting all
        # sixteen hashtags while reporting that it scouted four.
        supa = FakeSupa(override(hashtags=4))

        runner.run(supa, run_id="run-1")

        assert captured["config"].keywords == SIXTEEN[:4]

    def test_the_rotation_advances_by_what_was_actually_scouted(self, captured):
        supa = FakeSupa(override(hashtags=4))

        runner.run(supa, run_id="run-1")

        # Next run picks up at tag4, not wherever the saved setting would have left it.
        assert supa.cursor_saves == [4]

    def test_a_run_with_no_override_uses_the_saved_settings(self, captured):
        supa = FakeSupa(override())

        runner.run(supa, run_id="run-1")

        assert captured["config"].controls.run_budget_minutes is None
        assert len(captured["config"].keywords) == 16

    def test_a_scheduled_run_is_unaffected(self, captured):
        # The dispatcher opens rows with both overrides null, and `run_id` is
        # still set. Nothing should change for it.
        supa = FakeSupa(override())

        runner.run(supa, run_id="scheduled-1")

        assert len(captured["config"].keywords) == 16

    def test_a_headless_run_never_asks_for_a_row(self, captured):
        # No run id means nothing is watching and there is no row to read.
        class Exploding(FakeSupa):
            def trend_run(self, _run_id):
                raise AssertionError("should not read a run row without an id")

        runner.run(Exploding(override(hashtags=4)), run_id="")

        assert len(captured["config"].keywords) == 16

    def test_an_unreadable_run_row_costs_the_length_not_the_run(self, captured):
        # The overrides refine settings that are already loaded and valid, so a
        # failed read should lose the requested length, not the hour of
        # scouting behind it.
        supa = FakeSupa(None)

        result = runner.run(supa, run_id="run-1")

        assert result["signals"] == 0
        assert len(captured["config"].keywords) == 16

    def test_the_override_cannot_loosen_a_filter(self, captured):
        # Only the two costs are overridable. A shorter run must still judge
        # what it finds by the same bars, or two runs stop being comparable.
        supa = FakeSupa(
            {**override(budget=14, hashtags=4), "override_min_outlier_ratio": 1.0},
            settings_row={
                "niche_brief": "we automate operations for small businesses",
                "trend_keywords": SIXTEEN,
                "min_outlier_ratio": 3.0,
            },
        )

        runner.run(supa, run_id="run-1")

        assert captured["config"].controls.min_outlier_ratio == 3.0


class TestTheSourceDecidesWhichScoutRuns:
    """One outcome type, three sources, and the registry picks.

    Everything after the scout -- spreading signals across keywords, drafting,
    duplicate suppression, the rejection report -- is written against
    `ScoutOutcome` and never asks where it came from. That is what made adding
    the third and fourth sources a module each rather than another migration.
    """

    def test_google_trends_gets_the_keywords_and_the_region(self, captured):
        supa = FakeSupa(
            override(),
            settings_row={
                "niche_brief": "b",
                "trend_source": "google_trends",
                "trend_keywords": ["invoice software"],
                "trend_geo": "GB",
            },
        )

        runner.run(supa, run_id="run-1")

        config = captured["config"]
        assert config.keywords == ["invoice software"]
        assert config.geo == "GB"

    def test_apify_gets_the_hashtags_and_the_platforms(self, captured):
        supa = FakeSupa(
            override(),
            settings_row={
                "niche_brief": "b",
                "trend_source": "apify",
                "hashtags": ["crm"],
                "apify_platforms": ["instagram"],
            },
        )

        runner.run(supa, run_id="run-1")

        config = captured["config"]
        assert config.hashtags == ["crm"]
        assert config.platforms == ("instagram",)

    def test_youtube_gets_the_keywords(self, captured):
        supa = FakeSupa(
            override(),
            settings_row={
                "niche_brief": "b",
                "trend_source": "youtube",
                "trend_keywords": ["invoice software"],
                # Deliberately also present, and deliberately not what YouTube
                # is handed. A search API takes search terms.
                "hashtags": ["crm"],
            },
        )

        runner.run(supa, run_id="run-1")

        assert captured["config"].keywords == ["invoice software"]

    def test_the_length_override_applies_whichever_source_is_chosen(self, captured):
        supa = FakeSupa(
            override(budget=9, hashtags=2),
            settings_row={
                "niche_brief": "b",
                "trend_source": "google_trends",
                "trend_keywords": SIXTEEN,
            },
        )

        runner.run(supa, run_id="run-1")

        assert captured["config"].controls.run_budget_minutes == 9
        assert len(captured["config"].keywords) == 2
