"""The two gates.

Most of this file used to be about machinery that no longer exists: a Step
Functions task token, a private table holding it, a webhook bridge redeeming it
and a reconciler covering for the webhook when `pg_net` dropped a delivery.
Those tests went with the code.

What survives is the pair of properties that machinery was protecting, and both
still have teeth:

  * a decision must never be able to reach a row that nothing will pick up;
  * a decision is read from the database, never taken from a caller.

The second used to need saying because the bridge was a public, forgeable HTTP
endpoint. It is now structural -- there is no payload to forge, because there is
no caller -- which is the strongest form of that guarantee and the reason those
tests could be deleted rather than rewritten.
"""

from __future__ import annotations

import pytest

from pipeline.activities import gates
from pipeline.driver import graph as g
from tests.conftest import FakeSupa


class TestGate1:
    def test_an_approved_idea_becomes_a_production(self):
        supa = FakeSupa()
        supa.started = [{"id": "prod-1"}, {"id": "prod-2"}]

        result = gates.start_approved_productions(supa)

        assert result["started"] == ["prod-1", "prod-2"]

    def test_nothing_approved_starts_nothing(self):
        supa = FakeSupa()

        assert gates.start_approved_productions(supa) == {"started": []}

    def test_it_is_one_statement_and_not_a_read_then_a_write(self):
        # The dedup guarantee is `productions_one_live_per_idea`, and it only
        # holds if the check and the insert are the same statement. A read
        # followed by a write here would put a window between them that two
        # workers could both pass through -- and the loser of that race would
        # not fail cleanly, it would raise a unique violation from inside a
        # sweep.
        supa = FakeSupa()

        gates.start_approved_productions(supa)

        assert supa.call_names == ["start_approved_productions"]


class TestGate2Opens:
    """The ordering rule, which outlived the token that made it necessary.

    Under Step Functions the rule was "register the token, then make the row
    decidable". The driver's version is "write the `await_gate2` marker, then
    make the row decidable" -- and it fails the same way if reversed, because
    `claim_production` admits a decided row only when the marker is already
    there. A row that reached `approved` without it would sit at `approved`
    forever with nothing coming for it, which is precisely the invisible hang
    the old rule existed to prevent.

    The difference is that this version can be made structural. Both facts live
    on the same row, so one write covers both and there is no window at all.
    """

    def test_the_marker_and_the_decidable_status_land_in_one_write(self):
        supa = FakeSupa()

        gates.open_gate2({"production_id": "p1", "qc_passed": True}, supa)

        # Exactly one write. Two would be a window, however short.
        assert supa.call_names.count("update_production") == 1

    def test_a_passing_check_opens_the_gate_for_review(self):
        supa = FakeSupa()

        result = gates.open_gate2({"production_id": "p1", "qc_passed": True}, supa)

        assert result["status"] == "awaiting_review"

    def test_a_failing_check_still_opens_the_gate(self):
        # Flagged, not hidden. A quality check that refused to show its failures
        # would be a censor rather than an aid, and the owner is the one who
        # gets to decide whether a warning matters.
        supa = FakeSupa()

        result = gates.open_gate2({"production_id": "p1", "qc_passed": False}, supa)

        assert result["status"] == "qc_failed"

    def test_a_missing_qc_verdict_is_treated_as_passing(self):
        # `generate_copy` can catch forward into the gate without ever having
        # run the check. Defaulting to "failed" would flag every such video for
        # a reason that has nothing to do with the video.
        supa = FakeSupa()

        result = gates.open_gate2({"production_id": "p1"}, supa)

        assert result["status"] == "awaiting_review"


class TestGate2IsClaimableOnlyOnceDecided:
    """The property `claim_production` enforces, asserted against the graph.

    The SQL itself is exercised for real against Supabase; what is checked here
    is that the graph agrees with it -- that `await_gate2` does no work and
    routes on the row's status rather than on anything a step handed it.
    """

    def test_the_waiting_step_runs_nothing(self):
        assert g.GRAPH["await_gate2"].run is None

    def test_an_approved_row_routes_to_publishing(self):
        assert (
            g.GRAPH["await_gate2"].route({"status": "approved", "publishing_enabled": True})
            == "publish"
        )

    def test_an_approved_row_rests_when_publishing_is_off(self):
        assert (
            g.GRAPH["await_gate2"].route({"status": "approved", "publishing_enabled": False})
            == "publishing_disabled"
        )

    def test_a_rejected_row_is_terminal(self):
        assert g.GRAPH["await_gate2"].route({"status": "rejected"}) == "rejected"

    @pytest.mark.parametrize("status", ["awaiting_review", "qc_failed", "queued", "running"])
    def test_an_undecided_row_never_routes_to_publishing(self, status):
        # Belt and braces behind the claim predicate. If a row were ever claimed
        # while still undecided, the expensive direction is publishing something
        # nobody approved -- so the default is a rejection, exactly as the state
        # machine's `Default: Rejected` was.
        assert g.GRAPH["await_gate2"].route({"status": status}) != "publish"


class TestThePublishingBacklog:
    """Approved while publishing was off, and not lost.

    This is what `publishing_disabled` exists for as a distinct terminal. The
    rows keep `status='approved'` with their finished render and their
    per-platform copy, so they are a queryable backlog rather than a batch that
    quietly went nowhere.
    """

    def test_a_rested_production_is_sent_back_to_the_gate(self):
        supa = FakeSupa(status="approved")
        supa.productions_at_step = lambda step, status: [
            {"id": "p1", "run_state": {"step": "publishing_disabled", "ended_at": "then"}}
        ]

        result = gates.flush_publishing_backlog(supa)

        assert result["released"] == ["p1"]
        assert supa.last_saved["step"] == "await_gate2"

    def test_the_end_marker_is_cleared_so_it_is_not_read_as_finished(self):
        supa = FakeSupa(status="approved")
        supa.productions_at_step = lambda step, status: [
            {"id": "p1", "run_state": {"step": "publishing_disabled", "ended_at": "then"}}
        ]

        gates.flush_publishing_backlog(supa)

        assert "ended_at" not in supa.last_saved

    def test_the_render_and_the_copy_are_carried_over_untouched(self):
        # The whole point of resting rather than failing: nothing produced
        # before publishing existed is wasted when it is switched on.
        supa = FakeSupa(status="approved")
        supa.productions_at_step = lambda step, status: [
            {
                "id": "p1",
                "run_state": {
                    "step": "publishing_disabled",
                    "storage_key": "p1/final.mp4",
                    "qc_passed": True,
                },
            }
        ]

        gates.flush_publishing_backlog(supa)

        assert supa.last_saved["storage_key"] == "p1/final.mp4"
        assert supa.last_saved["qc_passed"] is True

    def test_an_empty_backlog_does_nothing(self):
        supa = FakeSupa()
        supa.productions_at_step = lambda step, status: []

        assert gates.flush_publishing_backlog(supa) == {"released": []}
