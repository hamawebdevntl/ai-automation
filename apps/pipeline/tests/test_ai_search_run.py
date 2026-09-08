"""A run started from a description, from the row to the queue.

`interpret` is unit-tested on its own. This is the wiring around it in the
runner, which is where a described run is most likely to quietly become an
ordinary one: which terms it scouts, the cursor it must not touch, the moment
the interpretation is written, and what its ideas carry when they land.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline import llm
from pipeline.trends import apify, gtrends, runner, sources, youtube
from pipeline.trends import ideas as ideas_mod
from pipeline.trends import interpret as interpret_mod
from pipeline.trends.base import ScoutOutcome
from pipeline.trends.controls import ScoutControls
from pipeline.trends.report import ScoutReport, payload
from pipeline.trends.velocity import Signal

DESCRIPTION = "I want to start a small home fitness brand for busy parents"
SAVED = [f"saved term {i}" for i in range(16)]
READ = ["home workout for parents", "quick workout at home", "fitness for busy mums"]


class FakeCfg:
    niche_brief = "we automate operations for small businesses"
    idea_provider = "gemini"
    gemini_api_key = "k"
    gemini_model = "gemini-test"
    apify_token = "test-token"
    youtube_api_key = "test-key"

    @property
    def hashtag_list(self) -> list[str]:
        return []

    @property
    def keyword_list(self) -> list[str]:
        return []


class FakeIdeasTable:
    def select(self, *_a: Any, **_kw: Any) -> FakeIdeasTable:
        return self

    def gte(self, *_a: Any, **_kw: Any) -> FakeIdeasTable:
        return self

    def execute(self) -> Any:
        return type("Res", (), {"data": []})()


class FakeRaw:
    def table(self, name: str) -> Any:
        assert name == "ideas"
        return FakeIdeasTable()


class FakeSupa:
    def __init__(self, run_row: dict[str, Any] | None, settings_row: dict[str, Any] | None = None):
        self._run = run_row
        self._settings = (
            settings_row
            if settings_row is not None
            else {"niche_brief": FakeCfg.niche_brief, "trend_keywords": SAVED}
        )
        self.raw = FakeRaw()
        self.cursor_saves: list[int] = []
        self.interpretations: list[dict[str, Any]] = []
        self.inserted: list[dict[str, Any]] = []

    def trend_settings(self):
        return self._settings

    def trend_run(self, _run_id: str):
        return self._run

    def save_hashtag_cursor(self, cursor: int) -> None:
        self.cursor_saves.append(cursor)

    def trend_run_is_cancelled(self, _run_id: str) -> bool:
        return False

    def enabled_platforms(self) -> list[str]:
        return ["instagram"]

    def record_interpretation(self, run_id: str, interpretation: dict[str, Any]) -> None:
        self.interpretations.append({"run_id": run_id, **interpretation})

    def insert_ideas(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.inserted.extend(rows)
        return rows


def described(**over: Any) -> dict[str, Any]:
    """A migrated row carrying a description, unless told otherwise."""
    row: dict[str, Any] = {
        "id": "run-1",
        "prompt": DESCRIPTION,
        "override_run_budget_minutes": None,
        "override_hashtags_per_run": None,
    }
    row.update(over)
    return row


def signal(keyword: str, ratio: float = 3.0) -> Signal:
    return Signal(
        source="google_trends",
        source_url="https://trends.google.com/",
        title=keyword,
        keyword=keyword,
        plays=0,
        ratio=ratio,
        engagement=0.0,
        age_days=0.0,
    )


@pytest.fixture
def scout(monkeypatch):
    """Every scout replaced by a recorder that answers with one signal per term."""
    seen: dict[str, Any] = {}

    def fake_scout(config):
        seen["config"] = config
        terms = list(getattr(config, "keywords", None) or getattr(config, "hashtags", None) or [])
        report = ScoutReport(seen=len(terms), hashtags_scouted=list(terms))
        return ScoutOutcome(signals=[signal(t) for t in terms], report=report)

    for module in (apify, gtrends, youtube):
        monkeypatch.setattr(module, "scout", fake_scout)
    monkeypatch.setattr(runner, "settings", lambda: FakeCfg())
    monkeypatch.setattr(runner.ideas_mod, "resolve_provider", lambda *_a, **_kw: "gemini")
    return seen


@pytest.fixture
def reading(monkeypatch):
    """`interpret` replaced by a recorder. Returns (calls, answer); edit `answer`."""
    calls: list[dict[str, Any]] = []
    answer: dict[str, Any] = {"terms": list(READ), "nudge": None}

    def fake_interpret(description, brief, source, *, cap, provider, **_kw):
        calls.append(
            {"description": description, "brief": brief, "source": source, "cap": cap, "provider": provider}
        )
        return interpret_mod.Interpretation(
            restatement="A home fitness brand for busy parents.",
            terms=list(answer["terms"]),
            vague=answer["nudge"] is not None,
            nudge=answer["nudge"],
        )

    monkeypatch.setattr(runner.interpret_mod, "interpret", fake_interpret)
    return calls, answer


@pytest.fixture
def drafting(monkeypatch):
    """`generate_ideas` replaced by a recorder that scores ideas as told."""
    state: dict[str, Any] = {"scores": [90, 70], "kwargs": None}

    def fake_generate(signals, brief, *, count, provider, description=None):
        state["kwargs"] = {"description": description, "count": count, "provider": provider}
        if description is None:
            return [
                ideas_mod.ReelIdea(title=f"Idea {i}", hook="h", angle="a", rationale=f"about {s.keyword}")
                for i, s in enumerate(signals[:count])
            ]
        return [
            ideas_mod.RelevantReelIdea(
                title=f"Idea {i}",
                hook="h",
                angle="a",
                rationale=f"about {s.keyword}",
                relevance=score,
                connection="It is for the parents you described.",
            )
            for i, (s, score) in enumerate(zip(signals, state["scores"]))
        ]

    monkeypatch.setattr(runner.ideas_mod, "generate_ideas", fake_generate)
    return state


class TestADescribedRunScoutsWhatWasRead:
    def test_the_interpreted_terms_are_scouted_not_the_saved_list(self, scout, reading, drafting):
        runner.run(FakeSupa(described()), run_id="run-1")
        assert scout["config"].keywords == READ

    def test_the_saved_list_is_never_rotated(self, scout, reading, drafting):
        # Even with a length that would rotate an ordinary run. The saved list
        # is an audience choice; a one-off question must not nudge it along.
        supa = FakeSupa(described(override_hashtags_per_run=4))
        runner.run(supa, run_id="run-1")
        assert supa.cursor_saves == []

    def test_the_run_length_becomes_the_term_cap(self, scout, reading, drafting):
        calls, _ = reading
        runner.run(FakeSupa(described(override_hashtags_per_run=3)), run_id="run-1")
        assert calls[0]["cap"] == 3

    def test_the_default_cap_applies_when_no_length_was_chosen(self, scout, reading, drafting):
        calls, _ = reading
        runner.run(FakeSupa(described()), run_id="run-1")
        assert calls[0]["cap"] == interpret_mod.AI_TERMS_DEFAULT

    def test_the_reader_is_told_the_active_sources_vocabulary(self, scout, reading, drafting):
        calls, _ = reading
        supa = FakeSupa(
            described(),
            settings_row={"niche_brief": "b", "trend_source": "apify", "hashtags": ["crm"]},
        )
        runner.run(supa, run_id="run-1")
        assert calls[0]["source"].vocabulary == sources.HASHTAGS
        assert scout["config"].hashtags == READ

    def test_the_reader_gets_the_description_the_brief_and_the_provider(self, scout, reading, drafting):
        calls, _ = reading
        runner.run(FakeSupa(described()), run_id="run-1")
        assert calls[0]["description"] == DESCRIPTION
        assert calls[0]["brief"] == FakeCfg.niche_brief
        assert calls[0]["provider"] == "gemini"

    def test_the_interpretation_is_recorded_before_the_scout_starts(
        self, monkeypatch, scout, reading, drafting
    ):
        # The app shows "understood as ..." while the scout is still going,
        # which is the only point at which a misreading is cheap to correct.
        supa = FakeSupa(described())
        recorded_by_then: dict[str, int] = {}
        fake = gtrends.scout

        def checking(config):
            recorded_by_then["count"] = len(supa.interpretations)
            return fake(config)

        monkeypatch.setattr(gtrends, "scout", checking)
        runner.run(supa, run_id="run-1")
        assert recorded_by_then["count"] == 1

    def test_the_document_names_the_source_vocabulary_and_model(self, scout, reading, drafting):
        supa = FakeSupa(described())
        runner.run(supa, run_id="run-1")
        doc = supa.interpretations[0]
        assert doc["run_id"] == "run-1"
        assert doc["source"] == "google_trends"
        assert doc["vocabulary"] == sources.KEYWORDS
        assert doc["provider"] == "gemini"
        assert doc["model"] == "gemini-test"
        assert doc["terms"] == READ

    def test_nothing_to_search_for_fails_the_run_with_the_nudge(self, scout, reading, drafting):
        _, answer = reading
        answer["terms"] = []
        answer["nudge"] = "Say who it is for."
        supa = FakeSupa(described())

        with pytest.raises(RuntimeError, match="Say who it is for"):
            runner.run(supa, run_id="run-1")

        # Recorded even so: the nudge is what the app shows against the failure.
        assert len(supa.interpretations) == 1
        assert "config" not in scout

    def test_a_model_that_returns_nothing_usable_fails_the_run(self, monkeypatch, scout, drafting):
        def exploding(*_a, **_kw):
            raise llm.LlmError("gemini returned no content")

        monkeypatch.setattr(runner.interpret_mod, "interpret", exploding)
        with pytest.raises(llm.LlmError):
            runner.run(FakeSupa(described()), run_id="run-1")
        # Never fell back to the saved list.
        assert "config" not in scout

    def test_a_refused_preflight_costs_nothing(self, monkeypatch, scout, reading, drafting):
        # The credential check comes before the paid read, not after it.
        calls, _ = reading
        monkeypatch.setattr(FakeCfg, "apify_token", "")
        supa = FakeSupa(
            described(),
            settings_row={"niche_brief": "b", "trend_source": "apify", "hashtags": ["crm"]},
        )
        with pytest.raises(RuntimeError, match="APIFY_TOKEN"):
            runner.run(supa, run_id="run-1")
        assert calls == []
        assert supa.cursor_saves == []


class TestWhatADescribedRunsIdeasCarry:
    def test_drafting_is_told_the_description(self, scout, reading, drafting):
        runner.run(FakeSupa(described()), run_id="run-1")
        assert drafting["kwargs"]["description"] == DESCRIPTION

    def test_ideas_below_the_floor_are_dropped_and_counted(self, scout, reading, drafting):
        drafting["scores"] = [90, 10]
        supa = FakeSupa(described())

        result = runner.run(supa, run_id="run-1")

        assert [r["relevance"] for r in supa.inserted] == [90]
        assert result["irrelevant"] == 1
        assert result["drafted"] == 2
        assert result["inserted"] == 1
        stage = next(s for s in result["rejections"]["stages"] if s["key"] == "irrelevant")
        assert stage["dropped"] == 1
        assert stage["level"] == "idea"

    def test_rows_carry_the_run_and_the_connection(self, scout, reading, drafting):
        supa = FakeSupa(described())
        runner.run(supa, run_id="run-1")
        assert {r["trend_run_id"] for r in supa.inserted} == {"run-1"}
        assert all(r["connection"] == "It is for the parents you described." for r in supa.inserted)

    def test_the_configured_count_is_the_terms_that_were_read(self, scout, reading, drafting):
        result = runner.run(FakeSupa(described()), run_id="run-1")
        assert result["rejections"]["hashtags_configured"] == len(READ)
        assert result["prompted"] is True


class TestAnOrdinaryRunIsUntouched:
    def test_no_description_means_no_reading(self, scout, reading, drafting):
        calls, _ = reading
        supa = FakeSupa(described(prompt=None))
        runner.run(supa, run_id="run-1")
        assert calls == []
        assert scout["config"].keywords == SAVED
        assert supa.interpretations == []

    def test_its_rows_carry_the_run_but_no_score(self, scout, reading, drafting):
        supa = FakeSupa(described(prompt=None))
        result = runner.run(supa, run_id="run-1")
        assert supa.inserted
        assert all(r["trend_run_id"] == "run-1" for r in supa.inserted)
        assert all(r["relevance"] is None and r["connection"] is None for r in supa.inserted)
        assert not any(s["key"] == "irrelevant" for s in result["rejections"]["stages"])
        assert result["prompted"] is False

    def test_a_database_without_the_columns_gets_the_old_row_shape(self, scout, reading, drafting):
        # The row shape says whether the migration is applied. No `prompt` key
        # means an older database, and the rows must not name columns it lacks
        # -- or every run fails after an hour of scouting.
        supa = FakeSupa(
            {"id": "run-1", "override_run_budget_minutes": None, "override_hashtags_per_run": None}
        )
        runner.run(supa, run_id="run-1")
        assert supa.inserted
        assert all("trend_run_id" not in r and "relevance" not in r for r in supa.inserted)

    def test_a_headless_run_has_no_row_and_no_tags(self, scout, reading, drafting):
        supa = FakeSupa(None)
        runner.run(supa, run_id="")
        assert supa.inserted
        assert all("trend_run_id" not in r for r in supa.inserted)


class TestTheIrrelevantStage:
    def test_is_counted_in_ideas_not_videos(self):
        report = ScoutReport(seen=5)
        report.drop("irrelevant", 2)
        assert report.total_dropped == 0

    def test_appears_only_on_a_described_run(self):
        def keys(prompted: bool) -> list[str]:
            doc = payload(
                ScoutReport(),
                ScoutControls(),
                surfaced=0,
                drafted=0,
                inserted=0,
                hashtags_configured=1,
                prompted=prompted,
            )
            return [s["key"] for s in doc["stages"]]

        assert "irrelevant" in keys(True)
        assert "irrelevant" not in keys(False)
        assert "duplicate" in keys(False)
