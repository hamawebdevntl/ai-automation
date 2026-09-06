"""Where the brief, the hashtags and the controls come from.

They moved out of the environment and into `trend_settings`, which the app
edits. The precedence between the two is the whole behaviour: the row wins
when it has a value, the environment answers when it does not, and neither
being set is a refusal rather than a run.

`_inputs` returns a third thing now -- the scout controls -- with the same
precedence and one addition: where the brief and the hashtags fall back to the
environment, the controls fall back to the constants this package used before
any of them were tunable. Most tests here take `[:2]` because they are about
the first two; `TestTheControlsComeAlong` is about the third.
"""

from __future__ import annotations

import pytest

from pipeline.trends.runner import _inputs


class FakeCfg:
    def __init__(
        self, brief: str = "", tags: list[str] | None = None, keywords: list[str] | None = None
    ) -> None:
        self.niche_brief = brief
        self._tags = tags or []
        self._keywords = keywords or []

    @property
    def hashtag_list(self) -> list[str]:
        return self._tags

    @property
    def keyword_list(self) -> list[str]:
        return self._keywords


class FakeSupa:
    def __init__(self, row=None, raises: Exception | None = None) -> None:
        self._row = row
        self._raises = raises

    def trend_settings(self):
        if self._raises:
            raise self._raises
        return self._row


ENV = FakeCfg("env brief", ["envtag"], ["envterm"])

# Every row that is about hashtags has to say so now. The default source is
# Google Trends, which reads `trend_keywords` -- handing it a hashtag list
# produces a run that looks like it worked and finds nothing.
TIKTOK = {"trend_source": "tiktok"}


class TestTheRowWins:
    def test_both_fields_come_from_the_database_when_set(self):
        row = {**TIKTOK, "niche_brief": "db brief", "hashtags": ["dbtag", "other"]}
        assert _inputs(FakeSupa(row), ENV)[:2] == ("db brief", ["dbtag", "other"])

    def test_a_hash_someone_typed_in_the_app_is_stripped(self):
        # The UI normalises on save, but a row written by hand or by an older
        # build should not silently scout a feed called "#saas".
        row = {**TIKTOK, "niche_brief": "db brief", "hashtags": ["#saas", " nocode "]}
        assert _inputs(FakeSupa(row), ENV)[1] == ["saas", "nocode"]


class TestTheEnvironmentIsTheFallback:
    def test_a_missing_row_falls_back_entirely(self):
        # No row means no source either, so this takes the default: Google
        # Trends, and therefore the environment's keywords rather than its tags.
        assert _inputs(FakeSupa(None), ENV)[:2] == ("env brief", ["envterm"])

    def test_each_field_falls_back_on_its_own(self):
        # A brief set in the app with hashtags left to the environment is a
        # coherent state; it must not blank one because the other is set.
        row = {**TIKTOK, "niche_brief": "db brief", "hashtags": []}
        assert _inputs(FakeSupa(row), ENV)[:2] == ("db brief", ["envtag"])

        row = {**TIKTOK, "niche_brief": "   ", "hashtags": ["dbtag"]}
        assert _inputs(FakeSupa(row), ENV)[:2] == ("env brief", ["dbtag"])

    def test_an_unreadable_table_degrades_instead_of_failing(self, caplog):
        # A database missing this migration should behave like the build that
        # came before it, not stop the run.
        supa = FakeSupa(raises=RuntimeError("relation does not exist"))
        with caplog.at_level("WARNING"):
            assert _inputs(supa, ENV)[:2] == ("env brief", ["envterm"])
        assert "falling back to env" in caplog.text


class TestNeitherIsSet:
    @pytest.mark.parametrize("row", [None, {"niche_brief": "", "hashtags": []}])
    def test_nothing_anywhere_yields_nothing(self, row):
        # `run` turns each of these into its own hard stop; the job here is
        # only to not invent a value.
        assert _inputs(FakeSupa(row), FakeCfg())[:2] == ("", [])


class TestTheControlsComeAlong:
    """The third member: everything else the scout was told, or the old constants."""

    def test_the_row_decides_when_it_has_values(self):
        row = {
            **TIKTOK,
            "niche_brief": "db brief",
            "hashtags": ["dbtag"],
            "min_plays": 50000,
            "videos_per_hashtag": 12,
            "schedule_hour_utc": 21,
        }
        controls = _inputs(FakeSupa(row), ENV)[2]
        assert (controls.min_plays, controls.videos_per_hashtag, controls.schedule_hour_utc) == (
            50000,
            12,
            21,
        )

    def test_a_missing_row_yields_the_constants_this_code_used_before(self):
        # The point of the whole fallback: a database without the migration
        # must produce the behaviour of the build before it, not a refusal and
        # not a different set of numbers.
        controls = _inputs(FakeSupa(None), ENV)[2]
        assert controls.videos_per_hashtag == 30          # ScoutConfig's old default
        assert controls.min_outlier_ratio == 1.5          # is_worth_surfacing's old bar
        assert controls.dedup_window_days == 14           # DEDUP_WINDOW_DAYS
        assert controls.baseline_sample_size == 12        # BASELINE_SAMPLE
        assert (controls.pacing_min_seconds, controls.pacing_max_seconds) == (2.0, 5.0)
        assert controls.ideas_per_run == 10               # IDEAS_PER_RUN
        assert controls.schedule_hour_utc == 6            # cron(0 6 * * ? *)
        assert controls.hashtags_per_run is None          # rotation off
        assert controls.run_budget_minutes is None        # no ceiling

    def test_an_unreadable_table_still_yields_usable_controls(self):
        controls = _inputs(FakeSupa(raises=RuntimeError("nope")), ENV)[2]
        assert controls.videos_per_hashtag == 30
        assert controls.schedule_enabled is True

    def test_the_provider_defers_to_the_environment_when_unset(self):
        # Empty rather than "claude", so that IDEA_LLM_PROVIDER still decides
        # on a database whose column has never been written.
        assert _inputs(FakeSupa(None), ENV)[2].idea_provider == ""


class TestEachSourceReadsItsOwnVocabulary:
    """A hashtag is not a search term.

    `#exceltips` is how a video is filed. "bookkeeping software" is what
    somebody types when they have had enough of doing it by hand. Handing
    either list to the other source produces a run that completes, reports
    success, and finds nothing -- which is the worst kind of wrong here,
    because it looks exactly like a quiet week.
    """

    @staticmethod
    def row(**extra: object) -> dict:
        return {
            "niche_brief": "db brief",
            "hashtags": ["exceltips", "crm"],
            "trend_keywords": ["bookkeeping software", "invoice software"],
            **extra,
        }

    def test_google_trends_reads_the_keywords(self):
        row = self.row(trend_source="google_trends")
        assert _inputs(FakeSupa(row), ENV)[1] == ["bookkeeping software", "invoice software"]

    def test_tiktok_reads_the_hashtags(self):
        row = self.row(trend_source="tiktok")
        assert _inputs(FakeSupa(row), ENV)[1] == ["exceltips", "crm"]

    def test_the_default_source_is_google_trends(self):
        # TikTok-Api is pinned at its own latest release and refuses every
        # feed, so it is kept as a setting rather than a default.
        row = self.row()
        assert _inputs(FakeSupa(row), ENV)[2].trend_source == "google_trends"
        assert _inputs(FakeSupa(row), ENV)[1] == ["bookkeeping software", "invoice software"]

    def test_an_empty_keyword_list_falls_back_to_the_environment(self):
        row = {"niche_brief": "db brief", "trend_source": "google_trends", "trend_keywords": []}
        assert _inputs(FakeSupa(row), ENV)[1] == ["envterm"]

    def test_the_region_comes_through_uppercased(self):
        row = self.row(trend_geo="gb")
        assert _inputs(FakeSupa(row), ENV)[2].trend_geo == "GB"
