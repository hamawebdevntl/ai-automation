"""One step of one production.

These cover what the driver has to earn that Step Functions simply provided:
that a restart resumes rather than re-runs, that a retry defers rather than
spins, that our own plumbing failing is not the step failing, and that a
finished row stays finished.

The last one is the sharpest. `current_step` defaults a row with no step to
`submit_render`, because that is right for a row Gate 1 has only just inserted.
The cost of that default is that any code path which clears `run_state` turns a
finished production into a brand-new one and pays for the render again.
"""

from __future__ import annotations

import httpx
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


class TestAStepAdvancesTheRow:
    def test_a_successful_step_moves_to_the_next_one(self, monkeypatch):
        stub(monkeypatch, "render.submit_render", lambda e, s: {"task_id": "t1", "polls": 0})
        supa = FakeSupa()

        result = engine.advance(row("submit_render"), supa)

        assert result["step"] == "poll_render"
        assert supa.last_saved["step"] == "poll_render"

    def test_the_result_merges_flat_into_run_state(self, monkeypatch):
        # Flat, not nested under a ResultPath. The state machine nested things
        # only because ASL cannot merge two objects, and every activity reads
        # its inputs at the top level -- `publish` wants `storage_key`, not
        # `media.storage_key`. Nesting here would break all 17 of them.
        stub(
            monkeypatch,
            "render.submit_render",
            lambda e, s: {"fal_request_id": "req-9", "fal_status_url": "https://x/status"},
        )
        supa = FakeSupa()

        engine.advance(row("submit_render"), supa)

        assert supa.last_saved["fal_request_id"] == "req-9"
        assert supa.last_saved["fal_status_url"] == "https://x/status"

    def test_earlier_payload_survives_a_later_step(self, monkeypatch):
        # The handles on a paid generation must not be dropped by a step that
        # happens not to mention them.
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "running", "polls": 2})
        supa = FakeSupa()

        engine.advance(
            row("poll_render", run_state={"fal_request_id": "req-9", "backend": "fal_visuals"}),
            supa,
        )

        assert supa.last_saved["fal_request_id"] == "req-9"

    def test_the_activity_is_given_the_production_id(self, monkeypatch):
        seen = {}
        stub(monkeypatch, "render.submit_render", lambda e, s: seen.update(e) or {})
        engine.advance(row("submit_render"), FakeSupa())

        assert seen["production_id"] == "p1"

    def test_control_keys_are_not_passed_to_the_activity(self, monkeypatch):
        # `step`, `due_at` and `attempts` are the driver's bookkeeping. An
        # activity receiving them would be receiving something it has no
        # business acting on.
        seen = {}
        stub(monkeypatch, "render.submit_render", lambda e, s: seen.update(e) or {})

        engine.advance(
            row("submit_render", run_state={"attempts": {"submit_render": 1}, "due_at": "x"}),
            FakeSupa(),
        )

        assert "attempts" not in seen
        assert "due_at" not in seen
        assert "step" not in seen


class TestPollingDefersRatherThanSpinning:
    def test_a_running_render_polls_again_later(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "running", "polls": 1})
        supa = FakeSupa()

        result = engine.advance(row("poll_render"), supa)

        assert result["outcome"] == "poll_again"
        assert supa.last_saved["step"] == "poll_render"
        # A worker thread must never sleep holding a claim: the row is given
        # back with a time on it instead.
        assert supa.last_saved["due_at"]

    def test_a_row_that_is_not_yet_due_is_handed_straight_back(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: pytest.fail("ran too early"))
        supa = FakeSupa()

        result = engine.advance(
            row("poll_render", run_state={"due_at": "2099-01-01T00:00:00+00:00"}), supa
        )

        assert result["outcome"] == "not_due"

    def test_a_completed_render_moves_on(self, monkeypatch):
        stub(
            monkeypatch,
            "render.poll_render",
            lambda e, s: {"state": "complete", "video_ref": "v1"},
        )
        supa = FakeSupa()

        assert engine.advance(row("poll_render"), supa)["step"] == "fetch_and_qc"


class TestRetries:
    def test_a_listed_error_defers_instead_of_failing(self, monkeypatch):
        class MptQueueFull(Exception):
            pass

        def boom(e, s):
            raise MptQueueFull("queue is full")

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        result = engine.advance(row("submit_render"), supa)

        assert result["outcome"] == "retry"
        assert supa.last_saved["step"] == "submit_render"
        assert supa.last_saved["attempts"]["submit_render"] == 1

    def test_an_unlisted_error_parks_at_once(self, monkeypatch):
        def boom(e, s):
            raise RuntimeError("the avatar does not exist")

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        result = engine.advance(row("submit_render"), supa)

        assert result["step"] == "parked"
        assert supa.parked

    def test_attempts_are_exhausted_and_then_it_parks(self, monkeypatch):
        class FalRateLimited(Exception):
            pass

        def boom(e, s):
            raise FalRateLimited("429")

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        result = engine.advance(
            row("submit_render", run_state={"attempts": {"submit_render": 9}}), supa
        )

        assert result["step"] == "parked"

    def test_publish_is_not_retried_it_goes_to_the_poll(self, monkeypatch):
        # The graph says `retry=()`; this is that decision reaching the engine.
        # A retry here posts to a real audience twice.
        def boom(e, s):
            raise RuntimeError("connection reset mid-create")

        stub(monkeypatch, "publish.publish", boom)
        supa = FakeSupa()

        result = engine.advance(row("publish"), supa)

        assert result["step"] == "poll_publish"
        assert not supa.parked


class TestInfrastructureErrorsDoNotCountAgainstTheStep:
    """Our plumbing failing is not the step failing.

    This is the honest translation of the state machine's
    `Lambda.ServiceException` retry block, which covered faults in the
    invocation layer rather than in the work. Without it a PostgREST timeout
    would consume an attempt -- and `publish` has none to consume, so a Supabase
    blip would send a perfectly good production straight to its catch arc.
    """

    def test_a_supabase_error_defers_without_consuming_an_attempt(self, monkeypatch):
        def boom(e, s):
            raise SupaError("update matched no rows")

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        result = engine.advance(row("submit_render"), supa)

        assert result["outcome"] == "infra_retry"
        assert "attempts" not in supa.last_saved
        assert supa.last_saved["step"] == "submit_render"

    def test_a_transport_error_is_treated_the_same(self, monkeypatch):
        def boom(e, s):
            raise httpx.ConnectError("name resolution failed")

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        assert engine.advance(row("submit_render"), supa)["outcome"] == "infra_retry"

    def test_it_does_not_swallow_a_real_step_failure(self, monkeypatch):
        # An HTTPStatusError from HeyGen is the step's problem, not ours -- the
        # call was made and may have been billed.
        def boom(e, s):
            raise httpx.HTTPStatusError(
                "402", request=httpx.Request("POST", "https://api.heygen.com"),
                response=httpx.Response(402),
            )

        stub(monkeypatch, "render.submit_render", boom)
        supa = FakeSupa()

        assert engine.advance(row("submit_render"), supa)["step"] == "parked"


class TestTerminalsStayTerminal:
    def test_a_terminal_never_clears_run_state(self, monkeypatch):
        # The landmine. `current_step` defaults a row with no step to
        # `submit_render`, so a terminal that wrote `run_state = {}` would make
        # the next claim re-render the whole production and pay for it again.
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "failed"})
        supa = FakeSupa()

        engine.advance(row("poll_render"), supa)

        assert supa.last_saved["step"] == "parked"
        assert engine.current_step(supa.last_saved) == "parked"

    def test_a_terminal_records_when_it_ended(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "failed"})
        supa = FakeSupa()

        engine.advance(row("poll_render"), supa)

        assert supa.last_saved["ended_at"]

    def test_claiming_an_already_finished_row_does_nothing(self):
        supa = FakeSupa()

        result = engine.advance(row("published"), supa)

        assert result["outcome"] == "already_terminal"
        assert not supa.parked

    def test_a_terminal_carries_no_due_at(self, monkeypatch):
        stub(monkeypatch, "render.poll_render", lambda e, s: {"state": "failed"})
        supa = FakeSupa()

        engine.advance(row("poll_render", run_state={"due_at": "2020-01-01T00:00:00+00:00"}), supa)

        assert "due_at" not in supa.last_saved


class TestResumingAfterARestart:
    """The property that justifies persisting run_state at all."""

    def test_a_row_mid_render_resumes_at_its_own_step(self):
        # A worker restart during a twenty-minute fal generation. The handles
        # are on the row, so the new worker polls the same generation instead of
        # submitting -- and paying for -- a second one.
        state = {
            "step": "poll_render",
            "backend": "fal_visuals",
            "fal_request_id": "req-9",
            "fal_status_url": "https://queue.fal.run/x/status",
        }
        assert engine.current_step(state) == "poll_render"

    def test_a_brand_new_row_starts_at_the_beginning(self):
        # The one case the default is for: Gate 1 inserts with run_state '{}'.
        #
        # The start step is `write_script`, not `submit_render`, and that is the
        # whole of the script gate's guarantee about fresh rows: a production
        # nobody has touched begins by drafting words for a person to read, not
        # by spending money. `resume_step()` in the script-gate migration
        # carries the same default, and the two must not drift.
        assert engine.current_step({}) == "write_script"
        assert engine.current_step(None) == "write_script"


class TestTheTick:
    def test_no_claim_means_no_work(self):
        supa = FakeSupa(claim=None)

        assert engine.tick(supa, "w1") is None

    def test_an_expired_lease_is_counted_when_reclaimed(self, monkeypatch):
        # What tells `reconcile_leases` the difference between a worker that
        # restarted once and a step that kills whichever worker takes it.
        stub(monkeypatch, "render.submit_render", lambda e, s: {})
        supa = FakeSupa(
            claim={
                "id": "p1",
                "status": "running",
                "run_state": {"step": "submit_render"},
                "lease_expires_at": "2020-01-01T00:00:00+00:00",
            }
        )

        engine.tick(supa, "w1")

        assert supa.last_saved["lease_expiries"] == 1

    def test_a_driver_bug_releases_the_lease_rather_than_holding_it(self, monkeypatch):
        # Anything reaching here is our fault, not the step's. Keeping the claim
        # would take the production down with the bug until the lease expired.
        def explode(row, supa):
            raise RuntimeError("driver bug")

        monkeypatch.setattr(engine, "advance", explode)
        supa = FakeSupa(claim={"id": "p1", "status": "running", "run_state": {}})

        with pytest.raises(RuntimeError):
            engine.tick(supa, "w1")

        assert supa.released == ["p1"]


class TestOpeningProductions:
    def test_it_reports_what_it_started(self):
        supa = FakeSupa()
        supa.started = [{"id": "p1"}]

        assert engine.start_due_productions(supa) == ["p1"]

    def test_nothing_approved_is_the_quiet_common_case(self):
        assert engine.start_due_productions(FakeSupa()) == []
