"""Shared fakes.

The gates are the correctness-critical part of the pipeline, so they are tested
against fakes rather than mocks: a fake that records call *order* is what lets
us assert the ordering rules the design depends on.

That mattered more under Step Functions, where Gate 2 had a genuine ordering
hazard -- a task token had to be registered before the row became decidable, or
a decision made in the window between them resumed nothing and the execution
hung until its timeout while the interface reported success. The token is gone
and so is the window, but the recording fake stays: the driver has its own
ordering rules, and they fail just as quietly when broken.
"""

from __future__ import annotations

from typing import Any

# `None` is a meaningful value for `rate`: it is how a test says "nothing
# prices this model", which is one of the refusals worth pinning. So the
# default cannot be expressed as None.
_UNSET = object()


class FakeSupa:
    """Records the order of every call, which is the point."""

    def __init__(
        self,
        status: str = "approved",
        run_state: dict[str, Any] | None = None,
        claim: dict[str, Any] | None = None,
        publish_slot: bool = True,
        block_reason: str | None = None,
        spend_key: dict[str, Any] | None = None,
        rate: dict[str, Any] | None | object = _UNSET,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._status = status
        self._run_state = run_state or {}
        self._claim = claim
        self._publish_slot = publish_slot
        self.saved: list[dict[str, Any]] = []
        self.parked: list[tuple[str, str]] = []
        self.released: list[str] = []
        self.started: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        # The spend gate. Clear and priced by default, so that a test about
        # something else is not a test about a cap -- the caps that matter are
        # asserted explicitly in test_spend.py.
        self._block_reason = block_reason
        self._spend_key = spend_key or {"provider": "mpt", "model": "pexels"}
        self._rate: dict[str, Any] | None = (
            {"provider": "mpt", "model": "pexels", "unit": "render", "rate_usd": 0.0}
            if rate is _UNSET
            else rate  # type: ignore[assignment]
        )
        self.recorded_spend: list[dict[str, Any]] = []

    @property
    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # -- reads -------------------------------------------------------------

    def production(self, production_id: str) -> dict[str, Any]:
        self.calls.append(("production", production_id))
        return {"id": production_id, "status": self._status, "run_state": self._run_state}

    # -- the driver's claim and lease --------------------------------------

    def claim_production(self, worker: str, lease_seconds: int) -> dict[str, Any] | None:
        self.calls.append(("claim_production", worker))
        return self._claim

    def save_run_state(
        self, production_id: str, run_state: dict[str, Any], **fields: Any
    ) -> dict[str, Any]:
        self.calls.append(("save_run_state", run_state.get("step")))
        self.saved.append({"id": production_id, **run_state, **fields})
        self._run_state = run_state
        return {"id": production_id}

    def release_lease(self, production_id: str) -> None:
        self.calls.append(("release_lease", production_id))
        self.released.append(production_id)

    def start_approved_productions(self) -> list[dict[str, Any]]:
        self.calls.append(("start_approved_productions", None))
        return self.started

    def claim_publish_slot(self, production_id: str) -> bool:
        self.calls.append(("claim_publish_slot", production_id))
        return self._publish_slot

    # -- writes ------------------------------------------------------------

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status")))
        return {"id": production_id, **fields}

    def park(self, production_id: str, error: str) -> dict[str, Any]:
        self.calls.append(("park", error))
        self.parked.append((production_id, error))
        # The real one records the park itself, so the fake must too -- a test
        # that asserts on the event log would otherwise miss every park.
        self.record_event(production_id, self._run_state.get("step") or "unknown", "parked", error=error)
        return {"id": production_id, "status": "parked", "error": error}

    # -- spend -------------------------------------------------------------

    def style_preset_spend(self, preset_id: str) -> dict[str, Any]:
        self.calls.append(("style_preset_spend", preset_id))
        return {"style_preset_id": preset_id, **self._spend_key}

    def spend_block_reason(self, provider: str, model: str = "") -> str | None:
        self.calls.append(("spend_block_reason", (provider, model)))
        return self._block_reason

    def spend_rate(self, provider: str, model: str = "") -> dict[str, Any] | None:
        self.calls.append(("spend_rate", (provider, model)))
        return dict(self._rate) if self._rate else None

    def record_spend(
        self,
        production_id: str,
        *,
        provider: str,
        model: str,
        kind: str,
        amount_usd: float,
        source: str,
        external_ref: str = "",
        detail: dict[str, Any] | None = None,
        replace: bool = False,
    ) -> None:
        self.calls.append(("record_spend", amount_usd))
        # Idempotent on the same key as the real unique constraint, so a test
        # that retries a step sees the same thing production would: one row --
        # and, on the one lane where a resubmit bills again, two.
        key = (production_id, provider, model, kind, external_ref or "")
        for row in self.recorded_spend:
            if (
                row["production_id"],
                row["provider"],
                row["model"],
                row["kind"],
                row["external_ref"],
            ) == key:
                if replace:
                    row.update(amount_usd=amount_usd, source=source, detail=detail or {})
                return
        self.recorded_spend.append(
            {
                "production_id": production_id,
                "provider": provider,
                "model": model,
                "kind": kind,
                "amount_usd": amount_usd,
                "source": source,
                "external_ref": external_ref or "",
                "detail": detail or {},
            }
        )

    def render_charges(
        self, production_id: str, provider: str, model: str, kind: str
    ) -> list[dict[str, Any]]:
        self.calls.append(("render_charges", (provider, model, kind)))
        return [
            row
            for row in self.recorded_spend
            if row["production_id"] == production_id
            and row["provider"] == provider
            and row["model"] == (model or "")
            and row["kind"] == kind
        ]

    def spend_of(self, kind: str = "render") -> list[dict[str, Any]]:
        return [row for row in self.recorded_spend if row["kind"] == kind]

    # -- the step log ------------------------------------------------------

    def record_event(
        self,
        production_id: str,
        step: str,
        outcome: str,
        *,
        detail: str | None = None,
        error: str | None = None,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        # Deliberately not appended to `self.calls`: those assertions are about
        # the order of the writes that move a production, and threading a log
        # entry between each of them would rewrite every existing expectation.
        self.events.append(
            {
                "production_id": production_id,
                "step": step,
                "outcome": outcome,
                "detail": detail,
                "error": error,
                "attempt": attempt,
                "payload": payload or {},
            }
        )

    def events_of(self, outcome: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["outcome"] == outcome]

    @property
    def event_trail(self) -> list[tuple[str, str]]:
        """(step, outcome) in order -- what the UI's timeline is built from."""
        return [(e["step"], e["outcome"]) for e in self.events]

    @property
    def last_saved(self) -> dict[str, Any]:
        return self.saved[-1]
