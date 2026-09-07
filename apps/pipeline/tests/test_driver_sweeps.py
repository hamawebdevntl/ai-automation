"""The scheduler that replaced eight EventBridge rules.

EventBridge gave three things for free that a loop has to be written to
provide: one rule failing did not affect the others, a slow invocation did not
stack up behind itself, and the schedule was declarative enough to read. The
first two are tested here; the third is the `SWEEPS` table itself.
"""

from __future__ import annotations

import threading

import pytest

from pipeline.driver import sweeps


class Boom(RuntimeError):
    pass


@pytest.fixture
def publishing_off(monkeypatch):
    class Cfg:
        publishing_enabled = False

    monkeypatch.setattr(sweeps, "settings", lambda: Cfg())


class TestTheTable:
    def test_every_sweep_from_the_eventbridge_schedule_is_present(self):
        # Two of the eight are deliberately absent: `reconcile_gates` (there is
        # no token to reconcile) and `dispatch_trend_runs` (it is a thread of
        # its own now, because a scout run is an hour long and would block every
        # other sweep).
        assert {s.name for s in sweeps.SWEEPS} == {
            "reconcile_leases",
            "reconcile_renders",
            "reconcile_publishes",
            "flush_publishing_backlog",
            "collect_analytics",
            "reap_mpt_tasks",
            "expire_ideas",
        }

    def test_the_publishing_sweeps_are_gated_rather_than_omitted(self, publishing_off):
        # Registered either way, so turning publishing on is an environment
        # change and not a code change.
        gated = {s.name for s in sweeps.SWEEPS if not s.enabled()}
        assert gated == {
            "reconcile_publishes",
            "flush_publishing_backlog",
            "collect_analytics",
        }

    def test_lease_recovery_is_the_most_frequent(self):
        # It ran every fifteen minutes as `reconcile_executions`, sized for
        # describing a Step Functions execution over the network. It is a cheap
        # local query now, and it is the thing you want fast.
        by_name = {s.name: s.seconds for s in sweeps.SWEEPS}
        assert by_name["reconcile_leases"] == 60
        assert by_name["reconcile_leases"] == min(by_name.values())

    def test_the_destructive_sweeps_are_daily(self):
        by_name = {s.name: s.seconds for s in sweeps.SWEEPS}
        assert by_name["reap_mpt_tasks"] == sweeps.DAY
        assert by_name["expire_ideas"] == sweeps.DAY


class TestTheLoop:
    def _run_once(self, monkeypatch, table):
        """Run the loop with a stop event that fires after the first pass."""
        monkeypatch.setattr(sweeps, "SWEEPS", table)
        stop = threading.Event()
        real_wait = stop.wait

        def wait_then_stop(_timeout):
            stop.set()
            return real_wait(0)

        monkeypatch.setattr(stop, "wait", wait_then_stop)
        sweeps.run_sweeps(stop, supa=object())
        return stop

    def test_one_sweeper_raising_does_not_stop_the_rest(self, monkeypatch):
        ran = []

        table = (
            sweeps.Sweep("first", lambda s: ran.append("first"), 60),
            sweeps.Sweep("explodes", lambda s: (_ for _ in ()).throw(Boom("nope")), 60),
            sweeps.Sweep("last", lambda s: ran.append("last"), 60),
        )
        self._run_once(monkeypatch, table)

        # Without the per-sweep try/except, "last" never runs -- and the sweep
        # that never runs is whichever one happens to be after the broken one,
        # which is not a failure mode anyone would predict from the symptom.
        assert ran == ["first", "last"]

    def test_a_disabled_sweep_is_skipped(self, monkeypatch):
        ran = []
        table = (
            sweeps.Sweep("off", lambda s: ran.append("off"), 60, lambda: False),
            sweeps.Sweep("on", lambda s: ran.append("on"), 60),
        )
        self._run_once(monkeypatch, table)

        assert ran == ["on"]

    def test_hourly_and_faster_sweeps_run_at_boot(self, monkeypatch):
        # A redeploy is the likeliest moment for a stuck row to exist, so
        # waiting five minutes to find out is five minutes of a queue not
        # moving.
        ran = []
        table = (sweeps.Sweep("fast", lambda s: ran.append("fast"), 5 * sweeps.MINUTE),)
        self._run_once(monkeypatch, table)

        assert ran == ["fast"]

    def test_daily_sweeps_do_not_run_at_boot(self, monkeypatch):
        # One of them deletes render files off the MPT host. Doing that on every
        # restart would make a deploy destructive.
        ran = []
        table = (sweeps.Sweep("daily", lambda s: ran.append("daily"), sweeps.DAY),)
        self._run_once(monkeypatch, table)

        assert ran == []

    def test_a_sweep_is_rescheduled_before_it_runs(self, monkeypatch):
        # Rescheduling after the call would let a sweep that takes longer than
        # its own period become immediately due again the moment it finished,
        # and it would then run back to back forever.
        calls = []

        def slow(_supa):
            calls.append(1)

        table = (sweeps.Sweep("slow", slow, 60),)
        monkeypatch.setattr(sweeps, "SWEEPS", table)

        stop = threading.Event()
        passes = {"n": 0}

        def wait(_timeout):
            passes["n"] += 1
            if passes["n"] >= 3:
                stop.set()
            return False

        monkeypatch.setattr(stop, "wait", wait)
        sweeps.run_sweeps(stop, supa=object())

        # Three passes of the loop, one run: the other two found it not yet due.
        assert len(calls) == 1


class TestSweepsSkipWhatIsNotDeployed:
    """A sweeper for an absent half must be quiet, not noisy.

    Found by running the driver against a real database with no render backend
    configured: `reconcile_renders` constructed an `MptClient` unconditionally,
    which raises without `MPT_BASE_URL`, so every tick logged a traceback. That
    is not merely untidy -- it puts a stack trace exactly where a real render
    failure would appear, on a schedule, so the first genuine one is invisible.

    Rendering and publishing are both deployable halves of this system. Neither
    being present is a configuration, not a fault.
    """

    def test_reconcile_renders_skips_when_no_backend_is_configured(self, monkeypatch):
        from pipeline.activities import reconcile

        class Cfg:
            mpt_base_url = ""
            publishing_enabled = False

        monkeypatch.setattr(reconcile, "settings", lambda: Cfg())

        assert reconcile.reconcile_renders(supa=object()) == {"skipped": "MPT_BASE_URL is not set"}

    def test_reap_mpt_tasks_skips_too(self, monkeypatch):
        from pipeline.activities import reconcile

        class Cfg:
            mpt_base_url = ""
            publishing_enabled = False

        monkeypatch.setattr(reconcile, "settings", lambda: Cfg())

        assert reconcile.reap_mpt_tasks(supa=object()) == {"skipped": "MPT_BASE_URL is not set"}

    def test_an_injected_client_still_wins(self, monkeypatch):
        # The skip is about configuration, not about the tests being able to
        # drive these with a fake.
        from pipeline.activities import reconcile

        class FakeSupa:
            @property
            def raw(self):
                raise AssertionError("should have got past the skip and queried")

        class Cfg:
            mpt_base_url = ""

        monkeypatch.setattr(reconcile, "settings", lambda: Cfg())

        try:
            reconcile.reconcile_renders(supa=FakeSupa(), mpt=object())
        except AssertionError as exc:
            assert "past the skip" in str(exc)
        else:
            raise AssertionError("the injected client was ignored")
