"""Shared fakes.

The gate handlers are the correctness-critical part of the pipeline, so they are
tested against fakes rather than mocks: a fake that records call *order* is what
lets us assert the one ordering rule the design depends on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest


class FakeSfn:
    """Stands in for the Step Functions client."""

    def __init__(self, fail_code: str | None = None) -> None:
        self.fail_code = fail_code
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.started: list[dict[str, Any]] = []

    def send_task_success(self, taskToken: str, output: str) -> None:  # noqa: N803
        if self.fail_code:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": self.fail_code}}, "SendTaskSuccess")
        self.sent.append((taskToken, json.loads(output)))

    def start_execution(self, **kw: Any) -> dict[str, str]:
        self.started.append(kw)
        return {"executionArn": f"arn:aws:states:::execution/{kw['name']}"}


class FakeSupa:
    """Records the order of every call, which is the point."""

    def __init__(
        self,
        token: str | None = None,
        status: str = "approved",
        prior_decision: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._token = token
        self._status = status
        self._prior = prior_decision

    @property
    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def set_gate_token(self, production_id: str, token: str) -> None:
        self.calls.append(("set_gate_token", production_id))
        self._token = token

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status")))
        return {"id": production_id, **fields}

    def take_gate_token(self, production_id: str) -> str | None:
        self.calls.append(("take_gate_token", production_id))
        token, self._token = self._token, None
        return token

    def release_gate_token(self, production_id: str) -> None:
        self.calls.append(("release_gate_token", production_id))

    def record_gate_decision(self, production_id: str, decision: str) -> None:
        self.calls.append(("record_gate_decision", decision))

    def peek_gate_decision(self, production_id: str) -> str | None:
        return self._prior

    def production(self, production_id: str) -> dict[str, Any]:
        return {"id": production_id, "status": self._status}

    def pending_gate_resumes(self) -> list[dict[str, Any]]:
        return [{"production_id": "p1", "status": self._status}]


@pytest.fixture
def sfn(monkeypatch):
    """Patch the module's Step Functions accessor and hand back the fake."""
    import pipeline.activities.gates as gates

    fake = FakeSfn()
    monkeypatch.setattr(gates, "_sfn", lambda: fake)
    return fake


@pytest.fixture
def failing_sfn(monkeypatch):
    import pipeline.activities.gates as gates

    def _make(code: str) -> FakeSfn:
        fake = FakeSfn(fail_code=code)
        monkeypatch.setattr(gates, "_sfn", lambda: fake)
        return fake

    return _make
