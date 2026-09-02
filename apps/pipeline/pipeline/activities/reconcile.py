"""Scheduled sweepers.

These are not defensive extras. Three of our four dependencies lose or hide
state in ways that leave rows stuck forever with nothing raising an alarm:

  * MoneyPrinterTurbo strands an interrupted render at state=4 permanently, and
    loses task state entirely on restart unless Redis is enabled.
  * Postiz has no failure webhook, and can accept a post, return 200, and never
    publish it or record an error.
  * Supabase Database Webhooks are at-most-once with a one-second default
    timeout, so a decision can simply never arrive.

Without these jobs the pipeline silently accumulates dead productions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from pipeline.activities import publish as publish_mod
from pipeline.clients.mpt import MptClient, MptError, MptTaskStateLost
from pipeline.clients.supa import Supa
from pipeline.models import ProductionStatus, TaskState

log = logging.getLogger(__name__)

RENDER_STALE_MINUTES = 2
# A task merely queued behind MPT's concurrency limit is also state=4 with
# progress 0, and is indistinguishable from a running one, so give a job that
# has not started a far longer leash than one that has stalled mid-render.
NOT_STARTED_GRACE_MINUTES = 45
LIVE_STATUSES = ("queued", "running", "awaiting_review", "qc_failed", "publishing")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stale(raw: Any, minutes: int) -> bool:
    ts = _parse_ts(raw)
    return bool(ts and (_now() - ts) > timedelta(minutes=minutes))


# ---------------------------------------------------------------------------
# A -- renders
# ---------------------------------------------------------------------------


def reconcile_renders(supa: Supa | None = None, mpt: MptClient | None = None) -> dict[str, Any]:
    supa = supa or Supa()
    mpt = mpt or MptClient()

    rows = (
        supa.raw.table("productions")
        .select("id,task_id,status,stage,updated_at,style_preset_id")
        .eq("status", ProductionStatus.RUNNING.value)
        .execute()
    ).data or []

    parked, redriven = [], []
    for row in rows:
        if not _stale(row.get("updated_at"), RENDER_STALE_MINUTES):
            continue
        production_id, task_id = row["id"], row.get("task_id")
        if not task_id:
            # Claimed but never submitted, or the claim was released.
            if _stale(row.get("updated_at"), NOT_STARTED_GRACE_MINUTES):
                supa.park(production_id, "running with no task id and no progress")
                parked.append(production_id)
            continue

        try:
            status = mpt.get_task(task_id)
        except MptTaskStateLost:
            # Unambiguous: MPT writes its state row before scheduling, so a 404
            # on an id we hold can only mean the state was lost.
            supa.park(production_id, "render state lost -- MPT restarted without Redis")
            parked.append(production_id)
            continue
        except MptError as exc:
            log.warning("could not reach MPT for %s: %s", production_id, exc)
            continue

        if status.is_failed:
            supa.park(
                production_id,
                f"render failed at {status.failed_stage or 'unknown'}: {status.error or 'no detail'}",
            )
            parked.append(production_id)
        elif status.is_complete:
            # The render finished but the execution died before collecting it.
            log.info("render for %s is complete but the row never advanced", production_id)
            redriven.append(production_id)
        elif status.state == TaskState.PROCESSING:
            grace = NOT_STARTED_GRACE_MINUTES if status.progress == 0 else RENDER_STALE_MINUTES * 30
            if _stale(row.get("updated_at"), grace):
                supa.park(
                    production_id,
                    f"render stranded at {status.progress}% -- MPT never recovers these",
                )
                parked.append(production_id)

    return {"parked": parked, "completed_but_unclaimed": redriven}


# ---------------------------------------------------------------------------
# B -- executions
# ---------------------------------------------------------------------------


def reconcile_executions(supa: Supa | None = None) -> dict[str, Any]:
    """Catch rows whose state machine is no longer running.

    This is the only thing that detects a dead Gate 2 token: the row sits at
    awaiting_review looking perfectly healthy, and the owner's decision would
    resume nothing.
    """
    supa = supa or Supa()
    sfn = boto3.client("stepfunctions")

    rows = (
        supa.raw.table("productions")
        .select("id,status,execution_arn,updated_at")
        .in_("status", list(LIVE_STATUSES))
        .execute()
    ).data or []

    parked = []
    for row in rows:
        arn = row.get("execution_arn")
        if not arn:
            continue
        try:
            described = sfn.describe_execution(executionArn=arn)
        except ClientError as exc:
            log.warning("describe_execution failed for %s: %s", row["id"], exc)
            continue
        state = described.get("status")
        if state != "RUNNING":
            supa.park(row["id"], f"row is {row['status']} but its execution is {state}")
            parked.append(row["id"])
    return {"parked": parked}


# ---------------------------------------------------------------------------
# C -- publishes
# ---------------------------------------------------------------------------


def reconcile_publishes(supa: Supa | None = None) -> dict[str, Any]:
    """The only publish-failure detector that exists."""
    supa = supa or Supa()
    rows = (
        supa.raw.table("productions")
        .select("id")
        .eq("status", ProductionStatus.PUBLISHING.value)
        .execute()
    ).data or []

    resolved = []
    for row in rows:
        try:
            out = publish_mod.poll_publish({"production_id": row["id"]}, supa)
        except Exception as exc:  # noqa: BLE001 - one row must not stop the sweep
            log.warning("publish reconcile failed for %s: %s", row["id"], exc)
            continue
        if out.get("state") != "pending":
            resolved.append({"production_id": row["id"], "state": out.get("state")})
    return {"resolved": resolved}


# ---------------------------------------------------------------------------
# D -- MPT disk
# ---------------------------------------------------------------------------


def reap_mpt_tasks(supa: Supa | None = None, mpt: MptClient | None = None) -> dict[str, Any]:
    """Reclaim disk from finished renders.

    Nothing prunes MPT's task directory, and it has no object storage, so the
    render host fills up over time -- which presents as a mysterious global
    render outage rather than as a disk problem. Because our task ids are the
    production ids, a task is safe to delete exactly when its production has
    reached a terminal state and the video is already in our own storage.
    """
    supa = supa or Supa()
    mpt = mpt or MptClient()

    terminal = (
        supa.raw.table("productions")
        .select("id,status,video_url")
        .in_("status", ["published", "parked", "failed", "rejected"])
        .execute()
    ).data or []

    deleted, skipped = [], []
    for row in terminal:
        if not row.get("video_url"):
            # Never stored, so deleting the source would lose it for good.
            skipped.append(row["id"])
            continue
        try:
            mpt.delete_task(row["id"])
            deleted.append(row["id"])
        except MptError:
            # Includes the 409-while-busy case, which our fork's force flag
            # handles; a failure here is not worth failing the sweep over.
            skipped.append(row["id"])
    return {"deleted": deleted, "skipped": len(skipped)}


# ---------------------------------------------------------------------------
# Ideas
# ---------------------------------------------------------------------------


def expire_ideas(supa: Supa | None = None, older_than_days: int = 7) -> dict[str, Any]:
    """Stop dead trends being approved weeks later.

    The `expired` status exists in the schema and nothing wrote it. Since
    `approve_idea` only acts on a pending idea, this job is the only thing
    preventing a three-week-old idea being produced against a trend that has
    already passed.
    """
    supa = supa or Supa()
    return {"expired": supa.expire_stale_ideas(older_than_days)}
