"""The registry that decides which scout runs.

The point of the table is that adding a source is data rather than another
branch in the runner. These tests pin the two things that would make it lie:
the registry disagreeing with what a row is allowed to say, and a credential
being checked after the expensive part instead of before it.
"""

from __future__ import annotations

import pytest

from pipeline.trends import sources
from pipeline.trends.controls import SOURCES, ScoutControls


class FakeCfg:
    apify_token = ""
    youtube_api_key = ""


class TestTheRegistryAndTheColumnAgree:
    def test_every_selectable_source_has_a_scout(self):
        # `controls.SOURCES` is what the row is validated against and what the
        # Settings dropdown mirrors. A source in one and not the other is a
        # dropdown entry that cannot run, or a scout nobody can select.
        assert set(SOURCES) == set(sources.REGISTRY)

    def test_each_spec_knows_its_own_name(self):
        for name, spec in sources.REGISTRY.items():
            assert spec.name == name

    def test_every_source_reads_one_of_the_two_vocabularies(self):
        for spec in sources.REGISTRY.values():
            assert spec.vocabulary in (sources.KEYWORDS, sources.HASHTAGS)

    def test_the_vocabularies_are_the_ones_the_apis_accept(self):
        # A search API takes what somebody types; a scraper takes how a video
        # is filed. Handing either the other list produces a run that reports
        # success and finds nothing.
        assert sources.REGISTRY["google_trends"].vocabulary == sources.KEYWORDS
        assert sources.REGISTRY["youtube"].vocabulary == sources.KEYWORDS
        assert sources.REGISTRY["apify"].vocabulary == sources.HASHTAGS

    def test_every_source_says_what_an_empty_list_means_in_its_own_words(self):
        for spec in sources.REGISTRY.values():
            assert "nothing to scout" in spec.nothing_to_scout


class TestARetiredSourceDegradesRatherThanCrashing:
    def test_tiktok_is_no_longer_selectable(self):
        assert "tiktok" not in SOURCES
        assert "tiktok" not in sources.REGISTRY

    def test_the_module_is_dormant_rather_than_deleted(self):
        # Kept because the code is sound and the arms race is not settled
        # forever. Restoring it is adding a string to SOURCES and a Spec here.
        from pipeline.trends import tiktok

        assert callable(tiktok.scout)

    def test_an_unknown_source_falls_back_to_the_default(self, caplog):
        # `controls._source` already coerces a row, so reaching this means the
        # registry and SOURCES disagree -- a mistake made while adding a
        # source. It still degrades, because a configuration slip should not
        # stop a run from happening at all.
        spec = sources.spec("something_nobody_wrote")
        assert spec.name == "google_trends"


class TestCredentialsAreCheckedBeforeTheExpensivePart:
    """Mirrors `ideas.resolve_provider`, and for a sharper reason.

    A missing LLM key wastes a scrape. A missing Apify token would be found
    after one -- and an Apify scrape is billed per result, so the cost of
    checking late is money rather than minutes.
    """

    def test_google_trends_needs_no_credential(self):
        # There is no official Google Trends API, so there is no key to have.
        sources.REGISTRY["google_trends"].preflight(FakeCfg(), ScoutControls())

    def test_apify_refuses_without_a_token(self):
        with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
            sources.REGISTRY["apify"].preflight(FakeCfg(), ScoutControls())

    def test_the_apify_refusal_says_where_a_token_comes_from(self):
        with pytest.raises(RuntimeError, match="console.apify.com"):
            sources.REGISTRY["apify"].preflight(FakeCfg(), ScoutControls())

    def test_apify_refuses_with_no_platforms_selected(self):
        cfg = FakeCfg()
        cfg.apify_token = "tok"
        controls = ScoutControls(apify_platforms=())
        with pytest.raises(RuntimeError, match="no platforms are selected"):
            sources.REGISTRY["apify"].preflight(cfg, controls)

    def test_apify_is_satisfied_by_a_token_and_a_platform(self):
        cfg = FakeCfg()
        cfg.apify_token = "tok"
        sources.REGISTRY["apify"].preflight(cfg, ScoutControls(apify_platforms=("tiktok",)))

    def test_youtube_refuses_without_a_key(self):
        with pytest.raises(RuntimeError, match="YOUTUBE_API_KEY"):
            sources.REGISTRY["youtube"].preflight(FakeCfg(), ScoutControls())

    def test_the_youtube_refusal_names_the_api_to_enable(self):
        with pytest.raises(RuntimeError, match="YouTube Data API v3"):
            sources.REGISTRY["youtube"].preflight(FakeCfg(), ScoutControls())

    def test_every_refusal_offers_a_way_out(self):
        # A run that stops has to say what to do about it. Both of these are
        # reachable by an owner who chose a source before adding its key.
        for name in ("apify", "youtube"):
            with pytest.raises(RuntimeError, match="Settings"):
                sources.REGISTRY[name].preflight(FakeCfg(), ScoutControls())


def test_the_preflight_runs_before_the_scout(monkeypatch):
    """The ordering is the whole point, so it is asserted directly.

    If the scout ran first, the message an owner saw would be whatever the
    scrape failed with -- after it had been paid for.
    """
    from pipeline.trends import apify, runner

    def exploding_scout(_config):
        raise AssertionError("the scout must not be reached without a token")

    monkeypatch.setattr(apify, "scout", exploding_scout)

    class Cfg(FakeCfg):
        niche_brief = "b"
        idea_provider = "claude"
        gemini_api_key = ""
        @property
        def hashtag_list(self) -> list[str]:
            return ["crm"]

        @property
        def keyword_list(self) -> list[str]:
            return []

    class Supa:
        raw = None

        def trend_settings(self):
            return {"niche_brief": "b", "trend_source": "apify", "hashtags": ["crm"]}

        def save_hashtag_cursor(self, _cursor):
            pass

    monkeypatch.setattr(runner, "settings", lambda: Cfg())

    with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
        runner.run(Supa())
