"""The scout's dispatcher and the scout, in one process.

On AWS this was a Lambda deciding and a Fargate task doing. Here it is one
process that does both, and the risk of that collapse is that the pieces which
made the AWS version safe get dropped on the way -- the claim, the in-flight
lock, the write-off, the failure path.

It is now called on a timer by a thread in `pipeline.driver.worker` rather than
by a GitHub Actions cron, which is why the schedule tests moved here from
`test_trend_dispatch.py` when `dispatch_trend_runs` was deleted: this is the
only dispatcher there is.

The most important behaviour is still the boring one -- doing nothing, quickly.
Most calls find no work, and a dispatcher that scouted on every wake-up would
run an hour-long browser session every minute.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from pipeline.activities import reconcile
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
    # Nothing should be writing this any more; the fixture stays so that a
    # reintroduction of the environment-variable handoff fails loudly here.
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
        #
        # Passed as an argument, not through the environment. It used to be set
        # as os.environ["TREND_RUN_ID"], which was wrong in two ways that
        # happened to cancel out: process-global, so it would race between this
        # and anything else in the worker process -- and never read anyway,
        # because `settings()` is lru_cached.
        monkeypatch.setattr(worker, "Supa", lambda: FakeSupa(claim={"id": "run-7"}))

        worker.main()

        assert no_scouting["run_id"] == "run-7"

    def test_the_run_id_does_not_go_through_the_environment(self, monkeypatch, no_scouting):
        import os

        monkeypatch.setattr(worker, "Supa", lambda: FakeSupa(claim={"id": "run-7"}))

        worker.main()

        # A second thread scouting a different run must not be able to see this
        # one's id.
        assert "TREND_RUN_ID" not in os.environ

    def test_an_injected_client_is_used_rather_than_a_new_one(self, monkeypatch, no_scouting):
        # The driver owns one Supa per thread and hands it in; constructing a
        # second one here would open a client per minute forever.
        def explode():
            raise AssertionError("main() built its own client instead of using the injected one")

        monkeypatch.setattr(worker, "Supa", explode)
        supa = FakeSupa(claim={"id": "run-3"})

        assert worker.main(supa) == 0
        assert no_scouting["run_id"] == "run-3"

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


# ---------------------------------------------------------------------------
# The schedule
#
# These moved here from `test_trend_dispatch.py` when `dispatch_trend_runs` was
# deleted with ECS. `open_due_scheduled_run` survived that deletion unchanged --
# it was always pure database work -- and it is what makes the owner's schedule
# a setting rather than a deployment, so it keeps its tests.
# ---------------------------------------------------------------------------


def at(hour: int, minute: int = 0, *, day: int = 8) -> datetime:
    """A UTC instant on a known weekday. 2026-09-08 is a Tuesday."""
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def every_day(hour: int = 6, minute: int = 0, **extra: Any) -> dict[str, Any]:
    """A settings row whose schedule fires daily at `hour`:`minute` UTC."""
    return {
        "schedule_enabled": True,
        "schedule_hour_utc": hour,
        "schedule_minute_utc": minute,
        "schedule_days": [0, 1, 2, 3, 4, 5, 6],
        **extra,
    }


class ScheduleSupa(FakeSupa):
    """A FakeSupa that actually opens slots, rather than reporting them taken."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.opened: list[str] = []

    def scheduled_run_exists(self, slot) -> bool:
        return slot.isoformat() in self.opened

    def open_scheduled_trend_run(self, slot):
        self.opened.append(slot.isoformat())
        return {"id": "sched-1", "trigger": "schedule", "scheduled_for": slot.isoformat()}


class TestTheScheduleOpensARun:
    def test_a_due_slot_opens_a_row(self):
        supa = ScheduleSupa(settings_row=every_day(6))

        run = reconcile.open_due_scheduled_run(supa, now=at(6, 2))

        assert run is not None
        assert run["trigger"] == "schedule"
        assert supa.opened == [at(6).isoformat()]

    def test_the_same_slot_is_only_ever_opened_once(self):
        # The dispatcher runs every minute and a slot stays due for the length
        # of the catch-up window, so this question is asked about the same
        # instant dozens of times. Each extra yes would be another hour-long
        # browser session.
        supa = ScheduleSupa(settings_row=every_day(6))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 1)) is not None
        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None
        assert reconcile.open_due_scheduled_run(supa, now=at(6, 30)) is None
        assert supa.opened == [at(6).isoformat()]

    def test_a_paused_schedule_opens_nothing(self):
        supa = ScheduleSupa(settings_row=every_day(6, schedule_enabled=False))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None
        assert supa.opened == []

    def test_a_day_that_is_not_enabled_opens_nothing(self):
        # 2026-09-08 is a Tuesday, which is 2 in Postgres' Sunday-first count.
        supa = ScheduleSupa(settings_row=every_day(6, schedule_days=[0, 6]))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None

    def test_before_the_slot_opens_nothing(self):
        supa = ScheduleSupa(settings_row=every_day(6))

        assert reconcile.open_due_scheduled_run(supa, now=at(5, 59)) is None

    def test_unreadable_settings_still_run_the_old_daily_schedule(self):
        # The cron this replaced was 06:00 UTC daily. A settings table that
        # cannot be read has to behave like the deployment before this change,
        # not silently skip a day of scouting.
        class Unreadable(ScheduleSupa):
            def trend_settings(self):
                raise RuntimeError("relation does not exist")

        supa = Unreadable(settings_row=None)

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 5)) is not None


class TestTheScheduleUsesTheSamePathAsTheButton:
    """A scheduled run is a `trend_runs` row like any other.

    This was the point of moving the schedule out of an EventBridge cron: under
    the cron a scheduled run had no row, so its outcome, its progress and its
    rejection breakdown existed only in CloudWatch. Opening a row and letting
    the ordinary claim start it is what gave a scheduled run somewhere to
    report.
    """

    def test_a_slot_opened_this_tick_is_started_this_tick(self, monkeypatch, no_scouting):
        # Otherwise the schedule is accurate to the next tick rather than to the
        # minute, and the row sits at `requested` long enough for the app to
        # start calling it unclaimed.
        monkeypatch.setattr(worker, "_now", lambda: at(6, 2), raising=False)

        supa = ScheduleSupa(
            claim={"id": "sched-1", "trigger": "schedule"},
            settings_row=every_day(6),
        )
        monkeypatch.setattr(reconcile, "_now", lambda: at(6, 2))
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0

        assert supa.opened == [at(6).isoformat()]
        assert no_scouting["run_id"] == "sched-1"

    def test_a_paused_schedule_leaves_the_dispatcher_doing_nothing(self, monkeypatch, no_scouting):
        supa = ScheduleSupa(claim=None, settings_row=every_day(6, schedule_enabled=False))
        monkeypatch.setattr(reconcile, "_now", lambda: at(6, 2))
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0

        assert supa.opened == []
        assert "run_id" not in no_scouting

    def test_a_broken_schedule_does_not_stop_a_requested_run(self, monkeypatch, no_scouting):
        # This is also the only thing that starts a run the owner asked for and
        # the only thing that recovers a stuck one. Neither may depend on the
        # schedule being readable.
        class Broken(ScheduleSupa):
            def scheduled_run_exists(self, slot):
                raise RuntimeError("PostgREST is having a moment")

        supa = Broken(claim={"id": "run-1"}, settings_row=every_day(6))
        monkeypatch.setattr(reconcile, "_now", lambda: at(6, 2))
        monkeypatch.setattr(worker, "Supa", lambda: supa)

        assert worker.main() == 0
        assert no_scouting["run_id"] == "run-1"
