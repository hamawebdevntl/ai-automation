"""The scout as a CI job.

On AWS this is a Lambda deciding and a Fargate task doing. On a runner it is
one process that does both, and the risk of that collapse is that the pieces
which made the AWS version safe get dropped on the way -- the claim, the
in-flight lock, the write-off, the failure path.

The most important behaviour here is the boring one: doing nothing, quickly.
Most invocations find no work, and a job that scouted on every wake-up would
burn a private repo's free minutes in a week.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.trends import worker


class FakeSupa:
    def __init__(
        self,
        claim: dict[str, Any] | None = None,
        stale: list[dict[str, Any]] | None = None,
        settings_row: dict[str, Any] | None = None,
    ) -> None:
        self._claim = claim
        self._stale = stale or []
        # Paused, so a test about the button is not also a test about what time
        # of day the suite is being run at.
        self._settings = settings_row if settings_row is not None else {"schedule_enabled": False}
        self.finished: list[tuple[str, dict[str, Any]]] = []
        self.cancelled_progress: list[str] = []

    def trend_settings(self):
        return self._settings

    def stale_trend_runs(self, **_kw: Any) -> list[dict[str, Any]]:
        return self._stale

    def claim_trend_run(self):
        return self._claim

    def scheduled_run_exists(self, _slot) -> bool:
        return True

    def open_scheduled_trend_run(self, _slot):
        return None

    def finish_trend_run(self, run_id: str, **fields: Any) -> None:
        self.finished.append((run_id, fields))

    def record_cancelled_progress(self, run_id: str, **_fields: Any) -> None:
        self.cancelled_progress.append(run_id)


@pytest.fixture
def no_scouting(monkeypatch):
    """Replace the run itself; these tests are about the job around it."""
    calls: dict[str, Any] = {}

    def fake_run(_supa, run_id: str = ""):
        calls["run_id"] = run_id
        return {
            "cancelled": False,
            "signals": 3,
            "drafted": 2,
            "inserted": 2,
            "suppressed": 0,
            "scouted": 120,
            "hashtags_scouted": ["crm"],
            "rejections": {"seen": 120},
        }

    monkeypatch.setattr(worker.runner, "run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def no_run_id_leak(monkeypatch):
    # The worker exports TREND_RUN_ID for the scout to read; it must not leak
    # between tests.
    monkeypatch.delenv("TREND_RUN_ID", raising=False)


class TestDoingNothingCheaply:
    def test_no_work_exits_zero_without_scouting(self, monkeypatch, no_scouting):
        monkeypatch.setattr(worker, "Supa", lambda: FakeSupa(claim=None))

        assert worker.main() == 0
        assert "run_id" not in no_scouting

    def test_a_paused_schedule_with_no_request_does_nothing(self, monkeypatch, no_scouting):
        monkeypatch.setattr(worker, "Supa", lambda: FakeSupa(claim=None))
        assert worker.main() == 0


class TestCarryingOutARun:
    def test_a_claimed_run_is_scouted_and_reported(self, monkeypatch, no_scouting):
        supa = FakeSupa(claim={"id": "run-1", "trigger": "manual"})
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0
        assert no_scouting["run_id"] == "run-1"

        run_id, fields = supa.finished[0]
        assert run_id == "run-1"
        assert fields["status"] == "succeeded"
        assert fields["inserted"] == 2
        assert fields["scouted"] == 120

    def test_the_scout_is_told_which_row_it_is(self, monkeypatch, no_scouting):
        # How the scout notices being stopped from the app mid-run.
        import os

        monkeypatch.setattr(worker, "Supa", lambda: FakeSupa(claim={"id": "run-7"}))
        worker.main()
        assert os.environ["TREND_RUN_ID"] == "run-7"

    def test_a_stopped_run_keeps_its_status(self, monkeypatch):
        # The owner cancelled it; the job must record what it found without
        # reviving the row as succeeded.
        def cancelled_run(_supa, run_id: str = ""):
            return {"cancelled": True, "scouted": 40, "hashtags_scouted": [], "rejections": {}}

        monkeypatch.setattr(worker.runner, "run", cancelled_run)
        supa = FakeSupa(claim={"id": "run-1"})
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0
        assert supa.cancelled_progress == ["run-1"]
        assert supa.finished == []


class TestARunIsNeverLeftClaimed:
    def test_a_crash_fails_the_row_rather_than_holding_the_lock(self, monkeypatch):
        # A runner can vanish mid-job. If the row stayed claimed it would block
        # not this run but every future one.
        def explode(_supa, run_id: str = ""):
            raise RuntimeError("playwright would not start")

        monkeypatch.setattr(worker.runner, "run", explode)
        supa = FakeSupa(claim={"id": "run-1"})
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 1
        assert supa.finished[0][1]["status"] == "failed"
        assert "playwright" in supa.finished[0][1]["error"]

    def test_a_stalled_run_from_a_lost_machine_is_written_off(self, monkeypatch, no_scouting):
        supa = FakeSupa(claim=None, stale=[{"id": "old", "status": "running"}])
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        worker.main()

        assert supa.finished[0][0] == "old"
        assert supa.finished[0][1]["status"] == "failed"

    def test_the_write_off_happens_before_the_claim(self, monkeypatch, no_scouting):
        # Same ordering the AWS sweep uses, and for the same reason: a request
        # made while a dead run holds the slot can only start once it is freed.
        supa = FakeSupa(claim={"id": "new"}, stale=[{"id": "old", "status": "running"}])
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        worker.main()

        assert next(r for r, _ in supa.finished) == "old"

    def test_an_unreadable_schedule_does_not_stop_a_requested_run(self, monkeypatch, no_scouting):
        class Broken(FakeSupa):
            def trend_settings(self):
                raise RuntimeError("PostgREST is having a moment")

            def scheduled_run_exists(self, _slot):
                raise RuntimeError("nor this")

        supa = Broken(claim={"id": "run-1"})
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0
        assert no_scouting["run_id"] == "run-1"
