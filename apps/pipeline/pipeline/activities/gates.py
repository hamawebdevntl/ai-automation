"""The two approval gates, and the bridge that lets a browser-only app resume a
paused state machine.

The web app has no server tier, so it can never hold AWS credentials and can
never call SendTaskSuccess itself. Instead a decision is a row change, and a
Supabase Database Webhook tells us about it.

That webhook is `pg_net`: at-most-once, no retry, no dead-letter queue, and a
timeout that defaults to one second. It will drop decisions. Since
`decide_production` refuses to act once a status has moved on, a dropped Gate 2
decision would otherwise be unrecoverable -- so `reconcile_gates` below is the
actual guarantee and the webhook is only a latency optimisation.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.models import ProductionStatus

log = logging.getLogger(__name__)

# SendTaskSuccess errors that mean "already resumed", not "failed". Treating
# these as failure is the classic callback-bridge bug: the execution has moved
# on, and retrying forever achieves nothing.
_ALREADY_RESUMED = {"TaskDoesNotExist", "TaskTimedOut"}


def _sfn():
    return boto3.client("stepfunctions")


# ---------------------------------------------------------------------------
# Gate 1 -- a trigger, not a wait
# ---------------------------------------------------------------------------


def start_production(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Begin production for an approved idea.

    Gate 1 needs no task token at all: approving an idea is what *starts* an
    execution rather than something a running one waits on. That is why there
    is only one token type in this system.

    Only the idea id is taken from the payload. Everything else is read from
    Postgres, so a duplicated, replayed or forged webhook cannot cause anything
    but an idempotent re-read.
    """
    supa = supa or Supa()
    idea_id = _extract_id(event, "ideas")
    if not idea_id:
        return {"skipped": "no idea id in payload"}

    idea = supa.idea(idea_id)
    if idea.get("status") != "approved":
        # The webhook may fire on an unrelated update, or arrive out of order.
        return {"skipped": f"idea {idea_id} is {idea.get('status')}, not approved"}

    style_id = idea.get("approved_style_id")
    if not style_id:
        # The schema's own check constraint should make this impossible.
        raise ValueError(f"idea {idea_id} is approved but carries no style preset")

    existing = (
        supa.raw.table("productions")
        .select("id,execution_arn,status")
        .eq("idea_id", idea_id)
        .not_.in_("status", ["rejected", "failed", "parked"])
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        log.info("idea %s already has live production %s", idea_id, row["id"])
        return {"production_id": row["id"], "deduplicated": True}

    preset = supa.style_preset(style_id)
    created = (
        supa.raw.table("productions")
        .insert(
            {
                "idea_id": idea_id,
                "style_preset_id": style_id,
                "status": ProductionStatus.QUEUED.value,
                "stage": "queued",
                # The owner commits money at Gate 1 against the preset's
                # advertised range, so that range is the estimate of record.
                "cost_estimate_usd": preset.get("est_cost_max_usd"),
            }
        )
        .execute()
    )
    production_id = created.data[0]["id"]

    arn = settings().state_machine_arn
    if not arn:
        raise RuntimeError("STATE_MACHINE_ARN is not configured")

    # Naming the execution after the production id makes Step Functions itself
    # reject a duplicate start for 90 days, and means the ARN is always
    # reconstructible -- so an untracked execution is nearly impossible.
    try:
        started = _sfn().start_execution(
            stateMachineArn=arn,
            name=f"prod-{production_id}",
            input=f'{{"production_id":"{production_id}"}}',
        )
        supa.update_production(production_id, execution_arn=started["executionArn"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ExecutionAlreadyExists":
            log.info("execution for %s already exists", production_id)
            return {"production_id": production_id, "deduplicated": True}
        supa.update_production(
            production_id, status=ProductionStatus.FAILED.value, error=f"start_execution: {exc}"
        )
        raise

    return {"production_id": production_id}


# ---------------------------------------------------------------------------
# Gate 2 -- a genuine pause
# ---------------------------------------------------------------------------


def register_gate2(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Store the callback token, then open the gate. Order matters.

    The token must be registered *before* the row becomes decidable. Reversed,
    the owner could decide in the window before the token exists, the webhook
    would fire with nothing to resume, and the execution would hang until its
    timeout with no visible cause -- while the UI reported success.
    """
    supa = supa or Supa()
    production_id = event["production_id"]
    token = event["task_token"]
    qc_passed = bool(event.get("qc_passed", True))

    # 1. token first.
    supa.set_gate_token(production_id, token)

    # 2. only now is the row decidable.
    status = ProductionStatus.AWAITING_REVIEW if qc_passed else ProductionStatus.QC_FAILED
    supa.update_production(production_id, status=status.value, stage="review")

    # 3. Safety net for a re-drive: if this production was already decided in a
    #    previous life of the execution, resume immediately rather than waiting
    #    for a decision that has already happened.
    prior = supa.peek_gate_decision(production_id)
    if prior:
        log.info("production %s was already decided (%s); resuming at once", production_id, prior)
        _resume(production_id, prior, supa)
        return {"registered": True, "resumed_immediately": prior}

    return {"registered": True, "status": status.value}


def gate2_bridge(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Resume a paused execution after the owner decides.

    The payload is untrusted. `pg_net` cannot sign a request, so this endpoint
    is reachable and forgeable; only the production id is taken from the body
    and the decision itself is read from Postgres. A forged, replayed or
    duplicated delivery therefore causes an idempotent re-read and nothing
    more.
    """
    supa = supa or Supa()
    production_id = _extract_id(event, "productions")
    if not production_id:
        return {"skipped": "no production id in payload"}

    row = supa.production(production_id)
    decision = row.get("status")
    if decision not in ("approved", "rejected"):
        return {"skipped": f"production {production_id} is {decision}, not a decision"}

    return _resume(production_id, decision, supa)


def _resume(production_id: str, decision: str, supa: Supa) -> dict[str, Any]:
    # Lease rather than delete-then-send, so a failed send can be retried while
    # two concurrent deliveries cannot both resume the execution.
    token = supa.take_gate_token(production_id)
    if not token:
        # Either no token was ever registered, or another caller holds the
        # lease. Record the decision so `register_gate2` can resume itself if
        # it has not run yet; a concurrent holder will finish the job.
        supa.record_gate_decision(production_id, decision)
        log.warning("no claimable token for %s; decision recorded for replay", production_id)
        return {"production_id": production_id, "resumed": False, "reason": "no claimable token"}

    payload = f'{{"decision":"{decision}","production_id":"{production_id}"}}'
    try:
        _sfn().send_task_success(taskToken=token, output=payload)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _ALREADY_RESUMED:
            log.info("token for %s was already consumed (%s); treating as done", production_id, code)
            supa.release_gate_token(production_id)
            return {"production_id": production_id, "resumed": True, "already": code}
        raise

    supa.release_gate_token(production_id)
    return {"production_id": production_id, "resumed": True, "decision": decision}


# ---------------------------------------------------------------------------
# Reconciler -- the actual guarantee
# ---------------------------------------------------------------------------


def reconcile_gates(supa: Supa | None = None) -> dict[str, Any]:
    """Resume any decided production whose webhook never arrived.

    This is what makes the bridge safe to depend on. Run it on a short schedule
    -- it neutralises webhook non-delivery, the one-second timeout, a `pg_net`
    worker restart, API Gateway errors and Lambda throttling in one query.
    """
    supa = supa or Supa()
    resumed: list[str] = []
    for row in supa.pending_gate_resumes():
        production_id = row["production_id"]
        result = _resume(production_id, row["status"], supa)
        if result.get("resumed"):
            resumed.append(production_id)
    if resumed:
        log.info("gate reconciler resumed %d execution(s): %s", len(resumed), resumed)
    return {"resumed": resumed}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def verify_bridge_secret(provided: str | None) -> bool:
    """Constant-time check of the shared secret.

    `pg_net` can only send static headers, so this is all the authentication
    available. It is deliberately not the only defence: the handlers above read
    their decision from Postgres rather than the payload.
    """
    expected = settings().gate_bridge_secret
    if not expected:
        return True  # not configured; the payload is untrusted regardless
    return bool(provided) and hmac.compare_digest(provided, expected)


def _extract_id(event: dict[str, Any], table: str) -> str | None:
    """Pull a row id out of whatever shape reached us.

    A Supabase Database Webhook sends `{type, table, record, old_record}`. Going
    through SQS wraps that in `{Records: [{body: "<json>"}]}`. A direct
    invocation may just pass `{production_id: ...}`.
    """
    import json

    if "Records" in event:
        bodies = [json.loads(r["body"]) for r in event["Records"] if r.get("body")]
        for body in bodies:
            found = _extract_id(body, table)
            if found:
                return found
        return None

    for key in ("production_id", "idea_id"):
        if event.get(key):
            return str(event[key])

    record = event.get("record") or event.get("new") or {}
    if record.get("id") and (event.get("table") in (table, None)):
        return str(record["id"])
    return None
