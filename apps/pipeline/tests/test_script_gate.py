"""The script gate.

The requirement these cover is a single sentence -- *no render is submitted for
a script a person has not approved* -- and it is enforced in three independent
places on purpose:

  1. `claim_production` will not return a row at `await_script` whose
     `script_approved_at` is null. That is SQL, and it is not tested here.
  2. `_script_outcome` parks rather than routing onward if it is reached with
     the approval missing.
  3. `submit_render` refuses before taking a render claim.

Any one of those holding is enough. Testing all three is the point: the failure
mode is a paid render of words nobody read, and the three checks sit in three
files that are edited by different kinds of change.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.activities import render, script
from pipeline.driver import engine
from pipeline.driver import graph as g
from tests.conftest import FakeSupa

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class ScriptSupa(FakeSupa):
    """A FakeSupa that actually holds a production row.

    The shared fake returns a fixed three-key dict from `production()`, which is
    enough for the gate-ordering assertions it was written for but not for a
    step whose entire job is reading and writing one column.
    """

    def __init__(self, production: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.row: dict[str, Any] = {
            "id": "p1",
            "idea_id": "i1",
            "style_preset_id": "s1",
            "status": "running",
            "script": None,
            "script_approved_at": None,
            "task_id": None,
            **(production or {}),
        }
        self.updates: list[dict[str, Any]] = []
        self.claimed_slots: list[str] = []

    def production(self, production_id: str) -> dict[str, Any]:
        self.calls.append(("production", production_id))
        return dict(self.row)

    def idea(self, idea_id: str) -> dict[str, Any]:
        self.calls.append(("idea", idea_id))
        return {"id": idea_id, "title": "The Property CRM Double-Entry Tax", "hook": "Two systems, one truth."}

    def style_preset(self, preset_id: str) -> dict[str, Any]:
        self.calls.append(("style_preset", preset_id))
        return {"id": preset_id, "slug": "stock-broll", "render_mode": "mpt",
                "video_source": "pexels", "params": {}}

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status")))
        self.updates.append(fields)
        self.row.update(fields)
        return {"id": production_id, **fields}

    def claim_render_slot(self, production_id: str, task_id: str) -> bool:
        self.calls.append(("claim_render_slot", task_id))
        self.claimed_slots.append(task_id)
        return True


class FakeMpt:
    def __init__(self, text: str = "A property CRM makes you type everything twice.") -> None:
        self.text = text
        self.calls: list[tuple[str, int]] = []

    def generate_script(self, subject: str, language: str = "", paragraphs: int = 1) -> str:
        self.calls.append((subject, paragraphs))
        return self.text


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


class TestTheGateIsWhereTheGraphStarts:
    def test_a_production_begins_by_writing_a_script(self):
        assert g.START == "write_script"

    def test_the_gate_sits_between_drafting_and_rendering(self):
        assert g.GRAPH["write_script"].next == "open_script_gate"
        assert g.GRAPH["open_script_gate"].next == "await_script"
        assert g.GRAPH["await_script"].route({"script_approved_at": "2026-09-07T00:00:00Z"}) == "submit_render"

    def test_submit_render_is_reachable_only_through_the_gate(self):
        # The structural version of the requirement. If any other step ever
        # routes straight to `submit_render`, that is a path around the gate and
        # this fails -- which is the only way a reviewer would notice.
        sources = [
            name
            for name, step in g.GRAPH.items()
            if not callable(step.next) and step.next == "submit_render"
        ]
        routed = [
            name
            for name, step in g.GRAPH.items()
            if callable(step.next)
            and "submit_render" in {step.route(s) for s in _plausible_states()}
        ]
        assert sorted(sources + routed) == ["await_script"]

    def test_waiting_for_a_person_does_no_work(self):
        # No activity, so nothing runs and nothing can fail while the row waits.
        assert g.GRAPH["await_script"].run is None

    def test_drafting_retries_before_it_gives_up(self):
        # One LLM call, nothing billed downstream: a blip should not stop for a
        # human on the first try.
        policies = g.GRAPH["write_script"].retry
        assert policies and policies[0].max_attempts >= 2

    def test_a_failed_draft_parks_rather_than_rendering_anyway(self):
        assert g.GRAPH["write_script"].catch == "parked"
        assert g.GRAPH["open_script_gate"].catch == "parked"


def _plausible_states() -> list[dict[str, Any]]:
    """Every routing input the choice functions are asked to handle."""
    return [
        {},
        {"script_approved_at": "2026-09-07T00:00:00Z"},
        {"state": "complete"},
        {"state": "failed"},
        {"state": "running", "polls": 1},
        {"status": "approved", "publishing_enabled": True},
        {"status": "approved", "publishing_enabled": False},
        {"status": "rejected"},
        {"publish_state": {"state": "published"}},
        {"publish_state": {"state": "parked"}},
        {"publish_state": {"state": "running"}},
    ]


class TestTheGateRefusesToOpenItself:
    def test_an_unapproved_script_parks_rather_than_rendering(self):
        # Reaching this function with no approval means the claim predicate and
        # the graph have drifted. Parking spends nothing; routing onward spends
        # a render.
        assert g.GRAPH["await_script"].route({}) == "parked"
        assert g.GRAPH["await_script"].route({"script_approved_at": None}) == "parked"

    def test_it_reads_the_column_and_not_the_payload(self):
        # A payload key that merely *looks* like approval must not open it.
        assert g.GRAPH["await_script"].route({"approved": True, "status": "approved"}) == "parked"


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


class TestWritingTheDraft:
    def test_it_drafts_when_there_is_no_script(self):
        supa = ScriptSupa()
        mpt = FakeMpt()

        result = script.write_script({"production_id": "p1"}, supa, mpt)

        assert result["script_source"] == "drafted"
        assert supa.row["script"] == "A property CRM makes you type everything twice."
        assert mpt.calls, "the drafter was never called"

    def test_the_subject_is_the_idea_title_and_hook(self):
        supa = ScriptSupa()
        mpt = FakeMpt()

        script.write_script({"production_id": "p1"}, supa, mpt)

        subject, _ = mpt.calls[0]
        assert "The Property CRM Double-Entry Tax" in subject
        assert "Two systems, one truth." in subject

    def test_an_existing_script_is_kept(self):
        # The property everything else leans on. This step has a retry policy
        # and can be re-entered by `retry_production`, and the column it writes
        # is where an owner's edits live. Overwriting would silently discard
        # them.
        supa = ScriptSupa({"script": "Words a person wrote and meant."})
        mpt = FakeMpt()

        result = script.write_script({"production_id": "p1"}, supa, mpt)

        assert result["script_source"] == "kept"
        assert supa.row["script"] == "Words a person wrote and meant."
        assert mpt.calls == []

    def test_a_redraft_replaces_the_text(self):
        supa = ScriptSupa({"script": "The first attempt."})
        mpt = FakeMpt("A second, better attempt.")

        result = script.write_script({"production_id": "p1", "redraft": 1}, supa, mpt)

        assert result["script_source"] == "redrafted"
        assert supa.row["script"] == "A second, better attempt."

    def test_the_same_redraft_is_not_served_twice(self):
        # `redraft` is a counter rather than a flag precisely so a re-entry
        # after a transient failure can be told from a fresh request. Without
        # this, a retried step would rewrite the words every time it ran.
        supa = ScriptSupa({"script": "Already redrafted once."})
        mpt = FakeMpt()

        result = script.write_script(
            {"production_id": "p1", "redraft": 1, "drafted_redraft": 1}, supa, mpt
        )

        assert result["script_source"] == "kept"
        assert mpt.calls == []

    def test_an_empty_draft_is_a_failure_rather_than_an_empty_script(self):
        supa = ScriptSupa()

        with pytest.raises(ValueError):
            script.write_script({"production_id": "p1"}, supa, FakeMpt("   "))

    def test_an_over_long_draft_is_cut_to_what_heygen_accepts(self):
        # 5000 is HeyGen's hard limit and the database's check constraint. A
        # draft over it is a bad draft, not a broken pipeline: cut it and let
        # the owner edit, rather than parking the production.
        long = ("This sentence is here to take up room. " * 200).strip()
        assert len(long) > script.MAX_SCRIPT_CHARS
        supa = ScriptSupa()

        result = script.write_script({"production_id": "p1"}, supa, FakeMpt(long))

        assert result["script_truncated"] is True
        assert len(supa.row["script"]) <= script.MAX_SCRIPT_CHARS
        assert supa.row["script"].endswith(".")

    def test_the_presenter_lane_keeps_its_paragraph_setting(self):
        supa = ScriptSupa()
        supa.style_preset = lambda pid: {  # type: ignore[method-assign]
            "id": pid, "slug": "ai-presenter", "render_mode": "heygen",
            "video_source": "heygen", "params": {"heygen": {"paragraphs": 3}},
        }
        mpt = FakeMpt()

        script.write_script({"production_id": "p1"}, supa, mpt)

        assert mpt.calls[0][1] == 3


class TestOpeningTheGate:
    def test_it_puts_the_row_into_awaiting_script(self):
        supa = ScriptSupa()

        result = script.open_script_gate({"production_id": "p1"}, supa)

        assert result["status"] == "awaiting_script"
        assert supa.row["status"] == "awaiting_script"

    def test_it_spends_nothing(self):
        supa = ScriptSupa()

        script.open_script_gate({"production_id": "p1"}, supa)

        assert supa.claimed_slots == []


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(monkeypatch):
    class Cfg:
        publishing_enabled = False
        lease_seconds = 900
        driver_poll_seconds = 5
        production_workers = 2
        worker_id = "test"

    monkeypatch.setattr(engine, "settings", lambda: Cfg())
    return Cfg


class TestTheDriverStopsAtTheGate:
    def test_entering_the_gate_leaves_the_row_unclaimed(self, monkeypatch, settings):
        monkeypatch.setitem(
            engine.ACTIVITIES, "script.open_script_gate", lambda e, s: {"status": "awaiting_script"}
        )
        supa = FakeSupa()

        result = engine.advance(
            {"id": "p1", "status": "running", "run_state": {"step": "open_script_gate"}}, supa
        )

        assert result["outcome"] == "gate_open"
        assert supa.last_saved["step"] == "await_script"

    def test_an_approved_script_sends_the_row_to_the_render(self, settings):
        # The approval is read off the claimed row, not out of `run_state`. A
        # decision is a row change; anything else could be forged by a payload.
        supa = FakeSupa()

        result = engine.advance(
            {
                "id": "p1",
                "status": "running",
                "run_state": {"step": "await_script"},
                "script_approved_at": "2026-09-07T12:00:00Z",
            },
            supa,
        )

        assert result["step"] == "submit_render"

    def test_an_unapproved_row_at_the_gate_parks(self, settings):
        supa = FakeSupa()

        result = engine.advance(
            {"id": "p1", "status": "running", "run_state": {"step": "await_script"}}, supa
        )

        assert result["step"] == "parked"

    def test_the_two_new_steps_are_wired_to_activities(self):
        assert engine.ACTIVITIES["script.write_script"] is script.write_script
        assert engine.ACTIVITIES["script.open_script_gate"] is script.open_script_gate


# ---------------------------------------------------------------------------
# The render refuses independently
# ---------------------------------------------------------------------------


class TestSubmitRenderRefusesAnUnapprovedScript:
    def test_it_parks_without_taking_a_render_claim(self):
        # `claim_render_slot` is the moment `task_id` is set, and `task_id` is
        # this system's "money may be moving" marker. Refusing before it is what
        # makes this check free.
        supa = ScriptSupa({"script": "Perfectly good words, never approved."})

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.parked, "an unapproved script must stop the production"
        assert "without an approved script" in supa.parked[0][1]

    def test_an_approved_but_empty_script_is_also_refused(self):
        supa = ScriptSupa({"script": "   ", "script_approved_at": "2026-09-07T12:00:00Z"})

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.parked


class TestTheApprovedScriptReachesTheBackend:
    def test_moneyprinterturbo_is_given_the_words_rather_than_writing_its_own(self):
        # The two default styles -- Stock b-roll and Generative -- are this
        # lane. Leaving `video_script` empty is what makes MoneyPrinterTurbo
        # generate its own narration, which is exactly what it did before the
        # gate existed. This assertion is the gate meaning anything for them.
        params = render._build_params(
            {"title": "T", "hook": "H"},
            {"slug": "stock-broll", "video_source": "pexels", "params": {}},
            "task-1",
            "The approved words.",
        )

        assert params.video_script == "The approved words."

    def test_a_style_preset_cannot_overwrite_the_approved_script(self):
        # `style_presets.params` is merged over VideoParams so that new upstream
        # fields can be driven from the database. That must not extend to the
        # narration: a configuration row silently replacing approved words would
        # defeat the whole gate.
        params = render._build_params(
            {"title": "T", "hook": "H"},
            {"slug": "x", "video_source": "pexels", "params": {"video_script": "Something else."}},
            "task-1",
            "The approved words.",
        )

        assert params.video_script == "The approved words."

    def test_the_render_is_submitted_with_the_approved_script(self):
        supa = ScriptSupa(
            {"script": "The approved words.", "script_approved_at": "2026-09-07T12:00:00Z"}
        )
        submitted: list[Any] = []

        class Mpt(FakeMpt):
            def submit_render(self, params):
                submitted.append(params)
                return "p1"

        render.submit_render({"production_id": "p1"}, supa, Mpt())

        assert submitted, "no render was submitted"
        assert submitted[0].video_script == "The approved words."
        assert supa.parked == []
