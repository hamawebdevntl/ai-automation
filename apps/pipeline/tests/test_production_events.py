"""The step log: what a production leaves behind after it runs.

Before this existed, `run_state` was a rolling snapshot -- it said where a row
was and never where it had been -- so a production that failed twice and then
succeeded was indistinguishable from one that succeeded first time, and a
production that parked did not record which step it parked on.

These tests are mostly about the things that used to leave no trace at all:
an infrastructure blip, the cause of a retry that was later recovered from, and
the platforms `generate_copy` quietly failed to write copy for.
"""

from __future__ import annotations

import pytest

from pipeline.clients.supa import SupaError
from pipeline.driver import engine
from tests.conftest import FakeSupa


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    class Cfg:
        publishing_enabled = False
        lease_seconds = 900
        driver_poll_seconds = 5
        production_workers = 2
        worker_id = "test"

    monkeypatch.setattr(engine, "settings", lambda: Cfg())
    return Cfg


def row(step: str = "submit_render", **kw):
    state = {"step": step, **kw.pop("run_state", {})}
    return {"id": "p1", "status": kw.pop("status", "running"), "run_state": state, **kw}


def stub(monkeypatch, name: str, fn):
    monkeypatch.setitem(engine.ACTIVITIES, name, fn)


class TestAStepAnnouncesItself:
    def test_a_completed_step_is_recorded_as_succeeded(self, monkeypatch):
        stub(monkeypatch, "render.submit_render", lambda e, s: {"task_id": "t1", "backend": "mpt"})
        supa = FakeSupa()

        engine.advance(row("submit_render"), supa)

        assert ("submit_render", "succeeded") in supa.event_trail
        # ... and the step it moved into announces its own start, so the log
        # reads as a sequence rather than a set of endings.
        assert ("poll_render", "started") in supa.event_trail

    def test_the_first_step_of_a_fresh_row_still_announces_itself(self, monkeypatch):
        # A row Gate 1 has just inserted has no `step` at all, and so never
        # passes through `_enter` -- the thing that announces every other step.
        stub(monkeypatch, "script.write_script", lambda e, s: {"script_chars": 220})
        supa = FakeSupa()

        engine.advance({"id": "p1", "status": "queued", "run_state": {}}, supa)

        assert supa.event_trail[0] == ("write_script", "started")

    def test_the_detail_says_what_the_step_did(self, monkeypatch):
        stub(
            monkeypatch, "render.submit_render", lambda e, s: {"task_id": "t1", "backend": "heygen"}
        )
        supa = FakeSupa()

        engine.advance(row("submit_render"), supa)

        succeeded = supa.events_of("succeeded")[0]
        assert "heygen" in succeeded["detail"]

    def test_the_payload_carries_the_technical_record_but_not_driver_bookkeeping(self, monkeypatch):
        stub(
            monkeypatch,
            "render.submit_render",
            lambda e, s: {"task_id": "t1", "heygen_video_id": "v9"},
        )
        supa = FakeSupa()

        engine.advance(row("submit_render", run_state={"attempts": {"submit_render": 1}}), supa)

        payload = supa.events_of("succeeded")[0]["payload"]
        assert payload["heygen_video_id"] == "v9"
        assert "attempts" not in payload
        assert "step" not in payload


class TestWhereAProductionStopped:
    def test_entering_a_step_records_the_one_it_came_from(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "complete"})
        supa = FakeSupa()

        engine.advance(row("poll_render"), supa)

        assert supa.last_saved["step"] == "fetch_and_qc"
        assert supa.last_saved["previous_step"] == "poll_render"

    def test_a_park_remembers_the_step_that_failed(self, monkeypatch):
        # The whole reason `previous_step` exists: a terminal overwrites `step`
        # with its own name, so without it a parked production could not say
        # what it was doing, and `retry_production` would have nowhere to go
        # back in -- it would default to submit_render and pay again.
        def boom(event, supa):
            raise RuntimeError("mpt fell over")

        stub(monkeypatch, "render.fetch_and_qc", boom)
        supa = FakeSupa()

        engine.advance(row("fetch_and_qc", run_state={"attempts": {"fetch_and_qc": 99}}), supa)

        assert supa.last_saved["step"] == "parked"
        assert supa.last_saved["previous_step"] == "fetch_and_qc"

    def test_a_park_on_the_very_first_step_falls_back_to_the_start(self, monkeypatch):
        # A row with no `step` key at all -- which is every row
        # `start_approved_productions` inserts. `current_step` defaults it, and
        # this asserts the park records that default rather than nothing.
        #
        # The attempts are pre-spent because the first step is now
        # `write_script`, whose retry matches any error: a fresh row can no
        # longer park on its first tick, which is the point of giving a
        # transient LLM failure three goes before it stops for a person.
        def boom(event, supa):
            raise RuntimeError("no wallet")

        stub(monkeypatch, "script.write_script", boom)
        supa = FakeSupa()

        engine.advance(
            {"id": "p1", "status": "queued", "run_state": {"attempts": {"write_script": 99}}},
            supa,
        )

        assert supa.last_saved["step"] == "parked"
        assert supa.last_saved["previous_step"] == "write_script"

    def test_a_transient_failure_on_the_first_step_retries_before_it_parks(self, monkeypatch):
        # The other half of the change above, stated directly: drafting a script
        # costs one LLM call and no video generation, so a blip must not send a
        # brand-new production straight to a human.
        def boom(event, supa):
            raise RuntimeError("the drafting service just restarted")

        stub(monkeypatch, "script.write_script", boom)
        supa = FakeSupa()

        result = engine.advance({"id": "p1", "status": "queued", "run_state": {}}, supa)

        assert result["outcome"] == "retry"
        # Deferred in place, not moved: a retry sets `due_at` and leaves the
        # step where it was -- which for a fresh row means no `step` key yet.
        assert supa.last_saved.get("step") != "parked"
        assert supa.last_saved["due_at"]


class TestFailuresAreRecordedWhileTheyHappen:
    def test_a_retry_records_its_cause_at_the_time(self, monkeypatch):
        # Previously the cause survived only in the worker log, and only until
        # the attempts ran out -- so a step that failed twice and then worked
        # left no evidence it had ever struggled.
        def flaky(event, supa):
            raise RuntimeError("mpt returned nonsense")

        stub(monkeypatch, "render.poll_render", flaky)
        supa = FakeSupa()

        engine.advance(row("poll_render"), supa)

        retrying = supa.events_of("retrying")
        assert len(retrying) == 1
        assert retrying[0]["attempt"] == 1
        assert "RuntimeError" in retrying[0]["error"]
        assert "mpt returned nonsense" in retrying[0]["error"]

    def test_an_exhausted_step_records_the_failure_and_then_the_park(self, monkeypatch):
        def boom(event, supa):
            raise RuntimeError("terminal-ish")

        stub(monkeypatch, "render.poll_render", boom)
        supa = FakeSupa()

        engine.advance(row("poll_render", run_state={"attempts": {"poll_render": 99}}), supa)

        trail = supa.event_trail
        assert ("poll_render", "failed") in trail
        assert any(outcome == "parked" for _, outcome in trail)

    def test_a_park_is_recorded_once_not_twice(self, monkeypatch):
        # `park()` records the park, so `_finish` must not record a `terminal`
        # for it as well.
        def boom(event, supa):
            raise RuntimeError("nope")

        stub(monkeypatch, "render.poll_render", boom)
        supa = FakeSupa()

        engine.advance(row("poll_render", run_state={"attempts": {"poll_render": 99}}), supa)

        assert len(supa.events_of("parked")) == 1
        assert supa.events_of("terminal") == []

    def test_an_infrastructure_blip_is_recorded_rather_than_vanishing(self, monkeypatch):
        # This is the one that used to persist nothing whatsoever: `due_at` was
        # bumped five seconds and the row bounced, looking identical from the
        # outside to a row that was making progress.
        def unreachable(event, supa):
            raise SupaError("connection reset")

        stub(monkeypatch, "render.poll_render", unreachable)
        supa = FakeSupa()

        result = engine.advance(row("poll_render"), supa)

        assert result["outcome"] == "infra_retry"
        blips = supa.events_of("infra_retry")
        assert len(blips) == 1
        assert "connection reset" in blips[0]["error"]
        # And it still must not have consumed one of the step's attempts.
        assert supa.events_of("retrying") == []
        assert "attempts" not in supa.last_saved or not supa.last_saved["attempts"]


class TestPollingDoesNotDrownTheLog:
    def test_an_unchanged_poll_is_recorded_once(self, monkeypatch):
        # `poll_render` can tick 400 times against MAX_RENDER_POLLS. A log with
        # 400 "still rendering" lines in it hides the two that matter.
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "running", "progress": 40})
        supa = FakeSupa()

        state = {"step": "poll_render"}
        for _ in range(5):
            engine.advance({"id": "p1", "status": "running", "run_state": state}, supa)
            # A poll defers itself 30s. Clearing that is what makes this five
            # polls rather than one poll and four "not due yet".
            state = {k: v for k, v in supa.last_saved.items() if k != "due_at"}

        assert len(supa.events_of("progress")) == 1

    def test_real_movement_is_recorded(self, monkeypatch):
        supa = FakeSupa()
        state = {"step": "poll_render"}

        for pct in (10, 40, 80):
            stub(
                monkeypatch,
                "render.poll_render",
                lambda e, s, p=pct: {"state": "running", "progress": p},
            )
            engine.advance({"id": "p1", "status": "running", "run_state": state}, supa)
            state = {k: v for k, v in supa.last_saved.items() if k != "due_at"}

        assert len(supa.events_of("progress")) == 3

    def test_a_poll_step_is_only_succeeded_when_it_actually_finishes(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "running", "progress": 10})
        supa = FakeSupa()

        engine.advance(row("poll_render"), supa)

        assert supa.events_of("succeeded") == []


class TestTheStepsThatUsedToBeSilent:
    def test_generate_copy_says_which_platforms_it_could_not_write(self, monkeypatch):
        # The activity has always returned `missing`; nothing has ever shown it,
        # and `publish` silently skips those platforms.
        stub(
            monkeypatch,
            "publish.generate_platform_copy",
            lambda e, s: {"platforms": ["instagram"], "missing": ["linkedin"]},
        )
        supa = FakeSupa()

        engine.advance(row("generate_copy"), supa)

        detail = supa.events_of("succeeded")[0]["detail"]
        assert "instagram" in detail
        assert "linkedin" in detail

    def test_the_quality_check_reports_its_verdict(self, monkeypatch):
        stub(
            monkeypatch,
            "render.fetch_and_qc",
            lambda e, s: {"qc_passed": False, "slideshow_risk": 0.4, "storage_key": "p1/final.mp4"},
        )
        supa = FakeSupa()

        engine.advance(row("fetch_and_qc"), supa)

        detail = supa.events_of("succeeded")[0]["detail"]
        assert "failed" in detail
        assert "0.40" in detail


class TestTerminalsExplainThemselves:
    def test_publishing_disabled_reads_as_a_backlog_not_a_failure(self, monkeypatch):
        supa = FakeSupa()

        engine.advance(row("await_gate2", status="approved"), supa)

        terminal = supa.events_of("terminal")[0]
        assert terminal["step"] == "publishing_disabled"
        assert "not a failure" in terminal["detail"]

    def test_a_gate2_rejection_is_recorded_as_a_terminal(self, monkeypatch):
        supa = FakeSupa()

        engine.advance(row("await_gate2", status="rejected"), supa)

        assert ("rejected", "terminal") in supa.event_trail


class TestInfrastructureBouncesDoNotFloodTheLog:
    def _bounce(self, monkeypatch, supa, times: int):
        def unreachable(event, supa):
            raise SupaError("connection reset")

        stub(monkeypatch, "render.poll_render", unreachable)
        state = {"step": "poll_render"}
        for _ in range(times):
            engine.advance({"id": "p1", "status": "running", "run_state": state}, supa)
            state = {k: v for k, v in supa.last_saved.items() if k != "due_at"}
        return state

    def test_the_first_bounce_is_recorded(self, monkeypatch):
        supa = FakeSupa()
        self._bounce(monkeypatch, supa, 1)
        assert len(supa.events_of("infra_retry")) == 1

    def test_eleven_more_are_not(self, monkeypatch):
        # A row bouncing every five seconds would otherwise write 720 entries an
        # hour, burying the one event that explains what it is waiting on.
        supa = FakeSupa()
        self._bounce(monkeypatch, supa, engine.INFRA_EVENT_EVERY - 1)
        assert len(supa.events_of("infra_retry")) == 1

    def test_the_twelfth_says_it_is_still_happening(self, monkeypatch):
        supa = FakeSupa()
        state = self._bounce(monkeypatch, supa, engine.INFRA_EVENT_EVERY)

        events = supa.events_of("infra_retry")
        assert len(events) == 2
        assert events[1]["attempt"] == engine.INFRA_EVENT_EVERY
        assert "in a row" in events[1]["detail"]
        assert state["infra_retries"] == engine.INFRA_EVENT_EVERY

    def test_a_success_clears_the_count(self, monkeypatch):
        supa = FakeSupa()
        state = self._bounce(monkeypatch, supa, 3)
        assert state["infra_retries"] == 3

        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "running", "progress": 10})
        engine.advance({"id": "p1", "status": "running", "run_state": state}, supa)

        assert "infra_retries" not in supa.last_saved

    def test_the_count_never_reaches_an_activity(self, monkeypatch):
        seen = {}

        def capture(event, supa):
            seen.update(event)
            return {"state": "running"}

        stub(monkeypatch, "render.poll_render", capture)
        supa = FakeSupa()
        engine.advance(
            {
                "id": "p1",
                "status": "running",
                "run_state": {"step": "poll_render", "infra_retries": 4},
            },
            supa,
        )
        assert "infra_retries" not in seen
