"""The graph is the state machine, so these are the state machine's guarantees.

`infra/statemachine.asl.json` is being deleted. Its `Retry` blocks, its `Catch`
targets and its `Choice` branches were the accumulated result of things going
wrong in production, and every one of them was a comment in a JSON file that
nothing enforced. Here they are assertions.

No fakes and no I/O in this file on purpose: `graph.py` imports nothing that
touches a network, so these run in milliseconds and cannot fail for an unrelated
reason.
"""

from __future__ import annotations

from pipeline.driver import graph as g


class TestPublishIsNeverRetried:
    """The single most expensive mistake this system can make."""

    def test_publish_has_no_retry_policy_at_all(self):
        # Not "few retries" -- none. Postiz starts its publish workflow with
        # TERMINATE_EXISTING, so a retry landing after the provider call
        # succeeded but before the row is marked published posts twice, to a
        # real audience, under our own name.
        assert g.GRAPH["publish"].retry == ()

    def test_publish_catches_to_a_poll_not_a_park(self):
        # On an unknown outcome we go and look rather than guessing. Parking
        # would be safe for the wallet and wrong for the reviewer; retrying
        # would be the opposite.
        assert g.GRAPH["publish"].catch == "poll_publish"


class TestSubmitRenderRetriesOnlyWhatCostsNothing:
    def test_exactly_the_four_free_failures(self):
        # Each of these provably left no orphan render and billed nothing.
        # Widening this set is how you start paying twice for one video.
        policy = g.GRAPH["submit_render"].retry[0]
        assert set(policy.errors) == {
            "MptQueueFull",
            "FalRateLimited",
            "HeyGenRateLimited",
            "HeyGenInProgress",
        }

    def test_it_is_the_only_retry_policy_on_the_step(self):
        assert len(g.GRAPH["submit_render"].retry) == 1

    def test_anything_else_parks(self):
        assert g.GRAPH["submit_render"].catch == "parked"

    def test_an_unlisted_error_does_not_match(self):
        policy = g.GRAPH["submit_render"].retry[0]
        assert not policy.matches(RuntimeError("boom"))

    def test_a_listed_error_matches_by_class_name(self):
        class FalRateLimited(Exception):
            pass

        assert g.GRAPH["submit_render"].retry[0].matches(FalRateLimited())

    def test_a_subclass_matches_through_the_mro(self):
        class MptQueueFull(Exception):
            pass

        class MptQueueFullAndAngry(MptQueueFull):
            pass

        assert g.GRAPH["submit_render"].retry[0].matches(MptQueueFullAndAngry())

    def test_backoff_is_applied_per_attempt(self):
        policy = g.GRAPH["submit_render"].retry[0]
        assert policy.delay_for(1) == 60
        assert policy.delay_for(2) == 90  # 60 * 1.5
        assert policy.delay_for(3) == 135
        assert policy.max_attempts == 10


class TestMissingCopyDoesNotParkAFinishedVideo:
    def test_generate_copy_catches_forward_into_the_gate(self):
        # A rendered, quality-checked video with no captions is still worth a
        # human's time. `publish` skips any platform whose copy is missing.
        assert g.GRAPH["generate_copy"].catch == "open_gate2"

    def test_and_that_is_where_it_goes_on_success_too(self):
        assert g.GRAPH["generate_copy"].next == "open_gate2"


class TestTheWaitStatesBecameDeferrals:
    def test_poll_render_waits_thirty_seconds_as_WaitForRender_did(self):
        assert g.GRAPH["poll_render"].wait_before == 30

    def test_poll_publish_waits_sixty_as_WaitForPublish_did(self):
        assert g.GRAPH["poll_publish"].wait_before == 60

    def test_no_other_step_defers(self):
        deferring = {n for n, s in g.GRAPH.items() if s.wait_before}
        assert deferring == {"poll_render", "poll_publish"}


class TestRenderOutcome:
    def test_complete_goes_to_fetch(self):
        assert g._render_outcome({"state": "complete"}) == "fetch_and_qc"

    def test_failed_parks(self):
        assert g._render_outcome({"state": "failed"}) == "parked"

    def test_running_polls_again(self):
        assert g._render_outcome({"state": "running", "polls": 3}) == "poll_render"

    def test_too_many_polls_parks(self):
        # The poller has its own wall-clock budget, so reaching this means the
        # budget was never applied -- a bug, and one that would otherwise spin
        # forever.
        assert g._render_outcome({"state": "running", "polls": 401}) == "parked"

    def test_the_guard_is_off_by_one_safe(self):
        assert g._render_outcome({"state": "running", "polls": 400}) == "poll_render"

    def test_a_missing_poll_count_is_not_an_error(self):
        assert g._render_outcome({"state": "running"}) == "poll_render"


class TestGate2Outcome:
    def test_approved_publishes_when_publishing_is_on(self):
        assert (
            g._gate2_outcome({"status": "approved", "publishing_enabled": True})
            == "publish"
        )

    def test_approved_rests_when_publishing_is_off(self):
        # The ASL templated this at deploy time, so turning publishing on meant
        # re-rendering the state machine. It is a runtime setting now.
        assert (
            g._gate2_outcome({"status": "approved", "publishing_enabled": False})
            == "publishing_disabled"
        )

    def test_rejected_is_terminal(self):
        assert g._gate2_outcome({"status": "rejected"}) == "rejected"

    def test_anything_that_is_not_an_approval_is_a_rejection(self):
        # Matches the ASL's `Default: Rejected`. Failing closed is right here:
        # the expensive direction is publishing something nobody approved.
        assert g._gate2_outcome({"status": "queued"}) == "rejected"
        assert g._gate2_outcome({}) == "rejected"


class TestPublishOutcome:
    def test_published_is_terminal(self):
        assert g._publish_outcome({"publish_state": {"state": "published"}}) == "published"

    def test_parked_is_terminal(self):
        assert g._publish_outcome({"publish_state": {"state": "parked"}}) == "parked"

    def test_pending_polls_again(self):
        assert g._publish_outcome({"publish_state": {"state": "pending"}}) == "poll_publish"

    def test_a_missing_result_polls_rather_than_assuming(self):
        assert g._publish_outcome({}) == "poll_publish"


class TestGate2IsAPauseWithNoWork:
    def test_await_gate2_runs_nothing(self):
        # If it ran anything it would run it on every claim, and a gate can be
        # claimed the instant a decision lands.
        assert g.GRAPH["await_gate2"].run is None

    def test_it_has_no_timeout_field_at_all(self):
        # The ASL's 604800s existed only because a task token expires. Keeping
        # it would have been preserving a workaround for a problem that no
        # longer exists -- and would silently destroy an owner's ability to
        # approve a cut they left for a fortnight.
        assert not hasattr(g.GRAPH["await_gate2"], "timeout_seconds")


class TestTerminals:
    def test_exactly_five(self):
        # `cancelled` is the only one the graph never routes to. It is reached
        # by `cancel_production`, a person deciding to stop; it is listed so
        # that `TERMINALS` is the whole truth and `current_step` cannot fail on
        # a row somebody stopped.
        assert g.TERMINALS == {
            "published",
            "rejected",
            "parked",
            "publishing_disabled",
            "cancelled",
        }

    def test_nothing_in_the_graph_routes_to_cancelled(self):
        for name, step in g.GRAPH.items():
            assert step.catch != "cancelled", f"{name} catches to cancelled"
            if isinstance(step.next, str):
                assert step.next != "cancelled", f"{name} -> cancelled"

    def test_none_of_them_run_anything(self):
        assert all(g.GRAPH[name].run is None for name in g.TERMINALS)

    def test_publishing_disabled_is_the_only_resumable_one(self):
        # It is a backlog, not an outcome: `flush_publishing_backlog` sends
        # these rows back to the gate once Postiz is deployed.
        resumable = {
            n for n, s in g.GRAPH.items() if s.metadata.get("resumable")
        }
        assert resumable == {"publishing_disabled"}


class TestTheGraphIsWellFormed:
    def test_every_static_next_names_a_real_step(self):
        for name, s in g.GRAPH.items():
            if isinstance(s.next, str) and not s.terminal:
                assert s.next in g.GRAPH, f"{name} -> {s.next}"

    def test_every_catch_names_a_real_step(self):
        for name, s in g.GRAPH.items():
            assert s.catch in g.GRAPH, f"{name} catches to {s.catch}"

    def test_every_non_terminal_step_has_an_activity(self):
        # The two waiting steps are the exceptions, and they are exceptions for
        # the same reason: waiting for a person is not work, so there is nothing
        # to run. Everything else must name an activity or the row stops dead.
        for name, s in g.GRAPH.items():
            if s.terminal or name in ("await_gate2", "await_script"):
                continue
            assert s.run, f"{name} has no activity"

    def test_the_start_step_exists(self):
        assert g.START in g.GRAPH

    def test_unknown_steps_raise_rather_than_returning_none(self):
        # A typo in a persisted run_state must be loud. Returning None here
        # would strand the row silently.
        try:
            g.step("submitrender")
        except ValueError as exc:
            assert "unknown step" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestFetchAndQcGetsRoomToRun:
    def test_its_lease_is_an_hour(self):
        # Hundreds of megabytes downloaded, then on the fal_full lane concat,
        # portrait, TTS, transcription and burn-in. A default lease would expire
        # mid-render and hand the row to a second worker.
        assert g.GRAPH["fetch_and_qc"].lease_seconds == 3600

    def test_and_it_is_the_only_step_that_needs_it(self):
        long_leases = {n for n, s in g.GRAPH.items() if s.lease_seconds > 900}
        assert long_leases == {"fetch_and_qc"}
