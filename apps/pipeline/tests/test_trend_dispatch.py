"""The bridge between the button in the app and a Fargate task.

The app cannot call AWS, so a request is a row and this is what acts on it.
Two properties are worth pinning, because both fail silently:

  * a run must never be left claimed. At most one may be in flight, so a row
    stuck at `running` is not one lost run -- it is every future run, until
    somebody opens the console.
  * ECS answers 200 when it places nothing. Trusting the status code would
    leave the row running against a task that does not exist.

Since the daily schedule stopped being an EventBridge cron, this sweep also
decides that a run is due. That adds a third property worth pinning: the
schedule opens a row and the existing claim starts it, so a scheduled run is
the same kind of thing as one the owner asked for -- with the same lock, the
same write-off and the same row to report into.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from botocore.exceptions import ClientError

from pipeline.activities import reconcile


class FakeEcs:
    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response if response is not None else {"tasks": [{"taskArn": "arn:task/1"}]}
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.stopped: list[dict[str, Any]] = []
        self.stop_error: Exception | None = None

    def run_task(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.response

    def stop_task(self, **kw: Any) -> dict[str, Any]:
        self.stopped.append(kw)
        if self.stop_error:
            raise self.stop_error
        return {}


class FakeTable:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self.sink = sink
        self._update: dict[str, Any] = {}

    def update(self, fields: dict[str, Any]) -> FakeTable:
        self._update = fields
        return self

    def eq(self, _col: str, value: Any) -> FakeTable:
        self._update = {**self._update, "_id": value}
        return self

    def execute(self) -> Any:
        self.sink.append(self._update)
        return type("Res", (), {"data": []})()


class FakeRaw:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self.writes)


class FakeSupa:
    def __init__(
        self,
        claim: dict[str, Any] | None = None,
        stale: list[dict[str, Any]] | None = None,
        settings_row: dict[str, Any] | None = None,
        existing_slots: set[str] | None = None,
        needing_stop: list[dict[str, Any]] | None = None,
    ) -> None:
        self._claim = claim
        self._stale = stale or []
        # Paused by default, so that a test about the button is not also a test
        # about what time of day it is being run at.
        self._settings = settings_row if settings_row is not None else {"schedule_enabled": False}
        self._slots = existing_slots or set()
        self._needing_stop = needing_stop or []
        self.finished: list[tuple[str, dict[str, Any]]] = []
        self.opened: list[str] = []
        self.marked_stopped: list[str] = []
        self.raw = FakeRaw()

    def trend_settings(self) -> dict[str, Any] | None:
        return self._settings

    def stale_trend_runs(self, **_kw: Any) -> list[dict[str, Any]]:
        return self._stale

    def claim_trend_run(self) -> dict[str, Any] | None:
        return self._claim

    def finish_trend_run(self, run_id: str, **fields: Any) -> None:
        self.finished.append((run_id, fields))

    def cancelled_runs_needing_stop(self, **_kw: Any) -> list[dict[str, Any]]:
        return self._needing_stop

    def mark_task_stopped(self, run_id: str) -> None:
        self.marked_stopped.append(run_id)

    def scheduled_run_exists(self, slot) -> bool:
        return slot.isoformat() in self._slots

    def open_scheduled_trend_run(self, slot) -> dict[str, Any] | None:
        stamp = slot.isoformat()
        if stamp in self._slots:
            return None                      # what the unique index does
        self._slots.add(stamp)
        self.opened.append(stamp)
        return {"id": "sched-1", "trigger": "schedule", "scheduled_for": stamp}


@pytest.fixture
def configured(monkeypatch):
    """Settings with the task placement filled in, as Terraform provides it."""
    cfg = type(
        "Cfg",
        (),
        {
            "trends_cluster_arn": "arn:cluster/pipeline",
            "trends_task_definition": "arn:taskdef/trends:7",
            "trends_subnet_ids": "subnet-a,subnet-b",
            "trends_security_group_ids": "sg-1",
            "trends_subnet_id_list": ["subnet-a", "subnet-b"],
            "trends_security_group_id_list": ["sg-1"],
        },
    )()
    monkeypatch.setattr(reconcile, "settings", lambda: cfg)
    return cfg


@pytest.fixture
def ecs(monkeypatch):
    client = FakeEcs()
    monkeypatch.setattr(reconcile.boto3, "client", lambda _name: client)
    return client


class TestStartingARun:
    def test_a_requested_run_becomes_a_task(self, configured, ecs):
        supa = FakeSupa(claim={"id": "run-1"})

        result = reconcile.dispatch_trend_runs(supa)

        assert result["started"] == "run-1"
        assert result["task_arn"] == "arn:task/1"
        assert len(ecs.calls) == 1

    def test_the_task_is_told_which_row_to_report_into(self, configured, ecs):
        reconcile.dispatch_trend_runs(FakeSupa(claim={"id": "run-1"}))

        override = ecs.calls[0]["overrides"]["containerOverrides"][0]
        assert override["name"] == "trends"
        assert override["environment"] == [{"name": "TREND_RUN_ID", "value": "run-1"}]

    def test_it_is_placed_where_the_scheduled_run_is(self, configured, ecs):
        reconcile.dispatch_trend_runs(FakeSupa(claim={"id": "run-1"}))

        net = ecs.calls[0]["networkConfiguration"]["awsvpcConfiguration"]
        assert net["subnets"] == ["subnet-a", "subnet-b"]
        assert net["securityGroups"] == ["sg-1"]
        # Egress goes through the NAT gateway, as it does nightly.
        assert net["assignPublicIp"] == "DISABLED"

    def test_the_task_arn_is_recorded_against_the_row(self, configured, ecs):
        supa = FakeSupa(claim={"id": "run-1"})

        reconcile.dispatch_trend_runs(supa)

        assert supa.raw.writes == [{"task_arn": "arn:task/1", "_id": "run-1"}]

    def test_nothing_requested_starts_nothing(self, configured, ecs):
        result = reconcile.dispatch_trend_runs(FakeSupa(claim=None))

        assert result["started"] is None
        assert ecs.calls == []


class TestARunIsNeverLeftClaimed:
    def test_a_refused_run_task_fails_the_row(self, configured, monkeypatch):
        client = FakeEcs(error=ClientError({"Error": {"Code": "AccessDeniedException"}}, "RunTask"))
        monkeypatch.setattr(reconcile.boto3, "client", lambda _name: client)
        supa = FakeSupa(claim={"id": "run-1"})

        result = reconcile.dispatch_trend_runs(supa)

        assert result["started"] is None
        assert supa.finished[0][0] == "run-1"
        assert supa.finished[0][1]["status"] == "failed"
        assert "could not start the task" in supa.finished[0][1]["error"]

    def test_a_200_that_placed_no_task_fails_the_row(self, configured, monkeypatch):
        # ECS answers 200 with the reason in `failures`. Believing the status
        # code would leave the row running against nothing.
        client = FakeEcs(response={"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]})
        monkeypatch.setattr(reconcile.boto3, "client", lambda _name: client)
        supa = FakeSupa(claim={"id": "run-1"})

        result = reconcile.dispatch_trend_runs(supa)

        assert result["started"] is None
        assert supa.finished[0][1]["status"] == "failed"
        assert "RESOURCE:MEMORY" in supa.finished[0][1]["error"]

    def test_missing_configuration_fails_the_row_rather_than_holding_it(self, monkeypatch, ecs):
        cfg = type("Cfg", (), {"trends_cluster_arn": "", "trends_task_definition": "", "trends_subnet_ids": ""})()
        monkeypatch.setattr(reconcile, "settings", lambda: cfg)
        supa = FakeSupa(claim={"id": "run-1"})

        result = reconcile.dispatch_trend_runs(supa)

        assert result["started"] is None
        assert ecs.calls == []
        assert supa.finished[0][1]["status"] == "failed"
        # The message names what is unset, because nothing else will.
        assert "TRENDS_CLUSTER_ARN" in supa.finished[0][1]["error"]


class TestStalledRunsAreRecoverable:
    def test_a_stalled_run_is_written_off(self, configured, ecs):
        supa = FakeSupa(claim=None, stale=[{"id": "old", "status": "running"}])

        result = reconcile.dispatch_trend_runs(supa)

        assert result["expired"] == ["old"]
        assert supa.finished[0][1]["status"] == "failed"

    def test_expiry_happens_before_the_claim(self, configured, ecs):
        # The order is the whole point: at most one run may be in flight, so a
        # request made while a dead run still holds the slot can only start
        # once that row is cleared. Claiming first would postpone every
        # recovery by a full cycle.
        supa = FakeSupa(claim={"id": "new"}, stale=[{"id": "old", "status": "running"}])

        result = reconcile.dispatch_trend_runs(supa)

        assert result["expired"] == ["old"]
        assert result["started"] == "new"
        assert supa.finished[0][0] == "old"

    def test_an_unclaimed_request_is_written_off_differently(self, configured, ecs):
        supa = FakeSupa(claim=None, stale=[{"id": "old", "status": "requested"}])

        reconcile.dispatch_trend_runs(supa)

        assert "without starting" in supa.finished[0][1]["error"]


# ---------------------------------------------------------------------------
# The schedule
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


class TestTheScheduleOpensARun:
    def test_a_due_slot_opens_a_row(self):
        supa = FakeSupa(settings_row=every_day(6))

        run = reconcile.open_due_scheduled_run(supa, now=at(6, 2))

        assert run is not None
        assert run["trigger"] == "schedule"
        assert supa.opened == [at(6).isoformat()]

    def test_the_same_slot_is_only_ever_opened_once(self):
        # The sweep runs every minute and a slot stays due for the length of
        # the catch-up window, so this question is asked about the same instant
        # dozens of times. Each extra yes would be another hour-long browser
        # session.
        supa = FakeSupa(settings_row=every_day(6))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 1)) is not None
        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None
        assert reconcile.open_due_scheduled_run(supa, now=at(6, 30)) is None
        assert supa.opened == [at(6).isoformat()]

    def test_a_paused_schedule_opens_nothing(self):
        supa = FakeSupa(settings_row=every_day(6, schedule_enabled=False))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None
        assert supa.opened == []

    def test_a_day_that_is_not_enabled_opens_nothing(self):
        # 2026-09-08 is a Tuesday, which is 2 in Postgres' Sunday-first count.
        supa = FakeSupa(settings_row=every_day(6, schedule_days=[0, 6]))

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 2)) is None

    def test_before_the_slot_opens_nothing(self):
        supa = FakeSupa(settings_row=every_day(6))

        assert reconcile.open_due_scheduled_run(supa, now=at(5, 59)) is None

    def test_unreadable_settings_still_run_the_old_daily_schedule(self):
        # The cron this replaced was 06:00 UTC daily. A settings table that
        # cannot be read has to behave like the deployment before this change,
        # not silently skip a day of scouting.
        class Unreadable(FakeSupa):
            def trend_settings(self):
                raise RuntimeError("relation does not exist")

        supa = Unreadable(settings_row=None)

        assert reconcile.open_due_scheduled_run(supa, now=at(6, 5)) is not None


@pytest.fixture
def just_after_six(monkeypatch):
    """Pin the sweep's clock.

    `dispatch_trend_runs` reads the wall clock to decide whether a slot is
    due, so a test about the schedule that did not do this would pass or fail
    according to what time of day the suite happened to run at.
    """
    monkeypatch.setattr(reconcile, "_now", lambda: at(6, 2))


class TestTheScheduleUsesTheSamePathAsTheButton:
    def test_a_slot_opened_this_tick_is_started_this_tick(self, configured, ecs, just_after_six):
        # Otherwise the schedule is accurate to the next sweep rather than to
        # the minute, and the row sits at `requested` long enough for the app
        # to start calling it unclaimed.
        supa = FakeSupa(claim={"id": "sched-1", "trigger": "schedule"}, settings_row=every_day(6))

        result = reconcile.dispatch_trend_runs(supa)

        assert supa.opened == [at(6).isoformat()]
        assert result["scheduled"] == at(6).isoformat()
        assert result["started"] == "sched-1"
        assert result["trigger"] == "schedule"
        assert len(ecs.calls) == 1

    def test_a_paused_schedule_leaves_the_sweep_doing_nothing(self, configured, ecs, just_after_six):
        supa = FakeSupa(claim=None, settings_row=every_day(6, schedule_enabled=False))

        result = reconcile.dispatch_trend_runs(supa)

        assert supa.opened == []
        assert result["started"] is None
        assert ecs.calls == []

    def test_a_broken_schedule_does_not_stop_a_requested_run(self, configured, ecs, just_after_six):
        # This sweep is also the only thing that starts a run the owner asked
        # for and the only thing that recovers a stuck one. Neither may depend
        # on the schedule being readable.
        class Broken(FakeSupa):
            def scheduled_run_exists(self, slot):
                raise RuntimeError("PostgREST is having a moment")

        supa = Broken(claim={"id": "run-1"}, settings_row=every_day(6))

        result = reconcile.dispatch_trend_runs(supa)

        assert result["started"] == "run-1"
        assert result["scheduled"] is None
        assert len(ecs.calls) == 1


# ---------------------------------------------------------------------------
# Stopping a cancelled run's task
# ---------------------------------------------------------------------------


class TestStoppingACancelledRun:
    """The AWS half of stopping a run.

    Cancelling is a database write and takes effect on its own -- that is what
    makes the button work when this dispatcher is the thing that is broken. The
    cost is a browser session that outlives the lock it was holding, and this
    is one of the two things that closes that window. The other is the scout
    checking its own row; each covers the case the other cannot.
    """

    def test_a_cancelled_run_s_task_is_stopped(self, configured, ecs):
        supa = FakeSupa(needing_stop=[{"id": "gone", "task_arn": "arn:task/9"}])

        result = reconcile.dispatch_trend_runs(supa)

        assert result["cancelled"] == ["gone"]
        assert ecs.stopped[0]["task"] == "arn:task/9"
        assert ecs.stopped[0]["cluster"] == "arn:cluster/pipeline"

    def test_the_attempt_is_recorded_even_when_it_fails(self, configured, monkeypatch):
        # A task that has already exited is the expected failure, not an
        # exceptional one -- the scout may well have stopped itself first.
        # Recording the attempt anyway is what stops an unstoppable task being
        # retried every minute forever.
        client = FakeEcs()
        client.stop_error = ClientError({"Error": {"Code": "InvalidParameterException"}}, "StopTask")
        monkeypatch.setattr(reconcile.boto3, "client", lambda _name: client)
        supa = FakeSupa(needing_stop=[{"id": "gone", "task_arn": "arn:task/9"}])

        result = reconcile.dispatch_trend_runs(supa)

        assert result["cancelled"] == []
        assert supa.marked_stopped == ["gone"]

    def test_a_failure_here_does_not_stop_the_rest_of_the_sweep(self, configured, ecs):
        # This sweep is also the only thing that recovers a stuck run and the
        # only thing that starts a run the owner asked for.
        class Broken(FakeSupa):
            def cancelled_runs_needing_stop(self, **_kw: Any):
                raise RuntimeError("PostgREST is having a moment")

        supa = Broken(claim={"id": "run-1"})

        result = reconcile.dispatch_trend_runs(supa)

        assert result["cancelled"] == []
        assert result["started"] == "run-1"

    def test_nothing_to_stop_calls_no_aws(self, configured, ecs):
        # The common case by far: a run cancelled before anything claimed it
        # has no task, so there is nothing to stop.
        reconcile.dispatch_trend_runs(FakeSupa(needing_stop=[]))

        assert ecs.stopped == []
