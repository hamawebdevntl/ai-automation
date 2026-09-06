"""Tests for the approval gates and the callback bridge.

The failure modes these protect against are all silent: a job that hangs
forever with the UI reporting success, a forged request that publishes an
unreviewed video, or a decision that vanishes because a webhook was dropped.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from pipeline.activities import gates
from tests.conftest import FakeSupa


class TestPayloadExtraction:
    """The bridge must cope with every shape a decision can arrive in."""

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"production_id": "p-1"}, "p-1"),
            ({"type": "UPDATE", "table": "productions", "record": {"id": "p-2"}}, "p-2"),
            (
                {"Records": [{"body": json.dumps({"table": "productions", "record": {"id": "p-3"}})}]},
                "p-3",
            ),
            ({"table": "ideas", "record": {"id": "i-1"}}, None),
            ({}, None),
        ],
        ids=["direct", "db-webhook", "sqs-wrapped", "wrong-table", "empty"],
    )
    def test_extracts_a_production_id(self, payload, expected):
        assert gates._extract_id(payload, "productions") == expected


class TestRegisterGate2:
    def test_token_is_stored_before_the_row_becomes_decidable(self, sfn):
        """The ordering rule the whole bridge depends on.

        Reversed, the owner can decide in the window before the token exists,
        the webhook fires with nothing to resume, and the execution hangs until
        timeout while the UI reports success.
        """
        supa = FakeSupa()
        gates.register_gate2(
            {"production_id": "p1", "task_token": "TK", "qc_passed": True}, supa
        )
        names = supa.call_names
        assert names.index("set_gate_token") < names.index("update_production")

    def test_a_qc_failure_still_registers_a_token(self, sfn):
        """`decide_production` accepts qc_failed, so the gate must be enterable.

        Otherwise an owner overriding a QC failure flips the row to approved,
        the bridge finds no token, and nothing publishes while the UI says it
        worked.
        """
        supa = FakeSupa()
        gates.register_gate2(
            {"production_id": "p1", "task_token": "TK", "qc_passed": False}, supa
        )
        assert ("set_gate_token", "p1") in supa.calls
        assert ("update_production", "qc_failed") in supa.calls

    def test_a_decision_that_predates_the_token_resumes_at_once(self, sfn):
        supa = FakeSupa(prior_decision="approved")
        out = gates.register_gate2(
            {"production_id": "p1", "task_token": "TK", "qc_passed": True}, supa
        )
        assert out["resumed_immediately"] == "approved"


class TestBridge:
    def test_the_decision_comes_from_the_database_not_the_payload(self, sfn):
        """The endpoint is forgeable: pg_net cannot sign a request."""
        supa = FakeSupa(token="TK", status="rejected")
        out = gates.gate2_bridge({"production_id": "p1", "decision": "approved"}, supa)
        assert out["resumed"] is True
        assert sfn.sent[0][1]["decision"] == "rejected"

    def test_an_undecided_production_is_ignored(self, sfn):
        supa = FakeSupa(token="TK", status="awaiting_review")
        assert "skipped" in gates.gate2_bridge({"production_id": "p1"}, supa)

    def test_a_missing_token_persists_the_decision_rather_than_losing_it(self, sfn):
        supa = FakeSupa(token=None, status="approved")
        out = gates.gate2_bridge({"production_id": "p1"}, supa)
        assert out["resumed"] is False
        assert ("record_gate_decision", "approved") in supa.calls

    @pytest.mark.parametrize("code", ["TaskDoesNotExist", "TaskTimedOut"])
    def test_an_already_consumed_token_counts_as_success(self, failing_sfn, code):
        """Both mean the execution already moved on. Treating them as failure
        is the classic callback-bridge bug."""
        failing_sfn(code)
        supa = FakeSupa(token="TK", status="approved")
        out = gates._resume("p1", "approved", supa)
        assert out["resumed"] is True and out["already"] == code
        assert ("release_gate_token", "p1") in supa.calls

    def test_a_genuine_error_still_raises(self, failing_sfn):
        failing_sfn("ThrottlingException")
        with pytest.raises(ClientError):
            gates._resume("p1", "approved", FakeSupa(token="TK"))


class TestReconciler:
    def test_it_resumes_what_the_webhook_dropped(self, sfn):
        """Supabase webhooks are at-most-once with a one-second default timeout,
        so this job -- not the webhook -- is the actual guarantee."""
        supa = FakeSupa(token="TK", status="approved")
        assert gates.reconcile_gates(supa)["resumed"] == ["p1"]


class TestBridgeSecret:
    def test_it_fails_closed_on_a_mismatch(self, monkeypatch):
        monkeypatch.setattr(
            gates, "settings", lambda: type("S", (), {"gate_bridge_secret": "s3cret"})()
        )
        assert gates.verify_bridge_secret("s3cret") is True
        assert gates.verify_bridge_secret("wrong") is False
        assert gates.verify_bridge_secret(None) is False
