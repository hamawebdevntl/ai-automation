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
from pipeline.config import settings
from pipeline.models import ProductionStatus, TaskState
from pipeline.trends import controls as controls_mod
from pipeline.trends import schedule
from pipeline.trends.controls import ScoutControls

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
    if not settings().publishing_enabled:
        return {"skipped": "publishing is disabled (postiz_enabled=false)"}

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


def expire_ideas(supa: Supa | None = None, older_than_days: int | None = None) -> dict[str, Any]:
    """Stop dead trends being approved weeks later.

    The `expired` status exists in the schema and nothing wrote it. Since
    `approve_idea` only acts on a pending idea, this job is the only thing
    preventing an idea being produced against a trend that has already passed.

    `older_than_days` now comes from `trend_settings.idea_expiry_days`, because
    the right value depends on how often the owner actually reviews the queue
    -- which is not something a constant in a sweeper can know. An explicit
    argument still wins, for a one-off sweep and for the tests.
    """
    supa = supa or Supa()
    window = older_than_days if older_than_days is not None else _scout_controls(supa).idea_expiry_days
    return {"expired": supa.expire_stale_ideas(window), "older_than_days": window}


# ---------------------------------------------------------------------------
# Trend runs
# ---------------------------------------------------------------------------


def _scout_controls(supa: Supa) -> ScoutControls:
    """The trend settings, or the constants this code used before they existed.

    Never raises. Both callers below are scheduled sweepers, and a settings
    table that is missing or briefly unreadable must leave them behaving like
    the build before this change -- which, for the schedule, means the daily
    06:00 UTC run the Terraform cron used to perform. Defaulting to "no
    schedule" instead would turn a transient read error into a silently
    skipped day of scouting.
    """
    try:
        return controls_mod.from_row(supa.trend_settings())
    except Exception as exc:  # noqa: BLE001 - see docstring
        log.warning("could not read trend_settings, using defaults: %s", exc)
        return ScoutControls()


def stop_cancelled_tasks(supa: Supa) -> list[str]:
    """Kill the Fargate task behind a run the owner stopped.

    Cancelling is deliberately a pure database operation -- it has to work when
    this dispatcher is not running, since a dispatcher that is not running is
    the most common reason a run gets stuck in the first place. The cost of
    that choice is that the row can be `cancelled` while the browser session it
    was protecting is still going.

    Two things close that window, and neither is sufficient alone. This is the
    first: an `ecs:StopTask` within the minute, which works even against a task
    that has stopped responding. The second is the scout checking its own row
    between hashtags, which works even when this Lambda is the thing that is
    broken. Together they cover each other's failure, and the three-hour
    write-off is the backstop behind both.

    Never raises. A run that cannot be stopped is already out of the way of
    every future run -- the lock was freed when it was cancelled -- so failing
    here is untidy rather than harmful, and must not take the rest of the sweep
    down with it.
    """
    stopped: list[str] = []
    cfg = settings()
    if not cfg.trends_cluster_arn:
        return stopped

    for row in supa.cancelled_runs_needing_stop():
        try:
            boto3.client("ecs").stop_task(
                cluster=cfg.trends_cluster_arn,
                task=row["task_arn"],
                reason="stopped from the app",
            )
            stopped.append(row["id"])
            log.info("stopped the task for cancelled run %s", row["id"])
        except ClientError as exc:
            # A task that has already exited is the expected failure here, not
            # an exceptional one: the scout may well have noticed the
            # cancellation and stopped itself first, which is the system
            # working. Logged at info for that reason.
            log.info("could not stop the task for run %s: %s", row["id"], exc)
        finally:
            # Recorded either way. See `Supa.mark_task_stopped` -- this is what
            # stops an unstoppable task being retried every minute forever.
            supa.mark_task_stopped(row["id"])
    return stopped


def open_due_scheduled_run(supa: Supa, now: datetime | None = None) -> dict[str, Any] | None:
    """Record a run if the owner's schedule says one is due.

    This is what replaced the EventBridge cron. The schedule used to be a
    `cron(0 6 * * ? *)` in Terraform pointing straight at ecs:RunTask, which
    made the time of day a deployment and gave a scheduled run no row to
    report into. It is now a handful of columns and this function.

    Deliberately the same mechanism as the button rather than a second one: it
    opens a `trend_runs` row and lets the claim below start it. That buys the
    single in-flight lock, the stale-run write-off, the progress the app polls
    and the rejection breakdown -- none of which a scheduled run had before --
    without any of it needing a second implementation.

    Returns the row it opened, or None if nothing was due or the insert lost a
    race. See `schedule.due_slot` for what "due" means, and `pause` for the
    reason a paused schedule is a setting rather than a deleted row.
    """
    controls = _scout_controls(supa)
    slot = schedule.due_slot(now or _now(), controls)
    if slot is None:
        return None

    # Cheap check first; `trend_runs_one_per_slot` is what actually guarantees
    # this. See `Supa.scheduled_run_exists`.
    if supa.scheduled_run_exists(slot):
        return None

    run = supa.open_scheduled_trend_run(slot)
    if run:
        log.info("opened the scheduled trend run for %s", slot.isoformat())
    return run


# How long a claimed run may stay in flight before it is written off. Generous:
# fifteen hashtags is around an hour of deliberate pacing, and a run killed
# early produces nothing at all, so the cost of waiting too long is a late
# button and the cost of giving up too early is a wasted hour of scouting.
RUN_STALE_HOURS = 3
# A request the dispatcher never picked up. This one can be short -- the
# dispatcher runs every minute, so ten of them missing it means it is not
# running or ecs:RunTask is failing in a way that never reached the row.
REQUEST_STALE_MINUTES = 10


def dispatch_trend_runs(supa: Supa | None = None) -> dict[str, Any]:
    """Start the Fargate task for a trend run, requested or scheduled.

    The app has no way to call AWS -- there is no server tier -- so a request
    is a row, and this is what acts on it. Same shape as every other write in
    this system: Postgres records the intention, a scheduled job carries it out.

    Since the schedule became a setting rather than a cron, this is also what
    decides that a run is due. That is why it does three things in a fixed
    order, and the order is the interesting part:

      0. Stop the task behind any run an owner cancelled. First because it is
         the only step that is racing something -- a live browser session that
         no longer holds the lock protecting it.
      1. Write off runs that will never finish. At most one run may be in
         flight, which means a single crashed task disables both the button
         and the schedule until something clears the row. If that clearing
         only happened after a successful claim, the one state that needs
         recovering would be the one state that never recovers.
      2. Open a run if the owner's schedule says one is due -- which can only
         succeed once step 1 has freed the lock.
      3. Claim whatever is waiting and start it. A run opened in step 2 is
         started by step 3 on the same tick, so the schedule is accurate to
         the minute rather than to the next sweep.
    """
    supa = supa or Supa()

    # Isolated for the same reason the schedule step below is: this sweep is
    # also the only thing that recovers a stuck run and the only thing that
    # starts a run the owner asked for. Neither may stop happening because an
    # ecs:StopTask misbehaved.
    try:
        cancelled = stop_cancelled_tasks(supa)
    except Exception as exc:  # noqa: BLE001 - see above
        log.warning("could not stop cancelled trend tasks: %s", exc)
        cancelled = []

    expired = []
    for row in supa.stale_trend_runs(
        running_hours=RUN_STALE_HOURS, requested_minutes=REQUEST_STALE_MINUTES
    ):
        was = row["status"]
        supa.finish_trend_run(
            row["id"],
            status="failed",
            error=(
                f"gave up on a run left {was} for more than "
                + (
                    f"{RUN_STALE_HOURS}h"
                    if was == "running"
                    else f"{REQUEST_STALE_MINUTES} minutes without starting"
                )
            ),
        )
        expired.append(row["id"])
    if expired:
        log.warning("wrote off %d stalled trend run(s): %s", len(expired), expired)

    # Between the write-off and the claim: a slot that was due while a dead run
    # held the lock is startable now, and one opened here is claimed below.
    #
    # Isolated from the rest of the sweep on purpose. This function is also the
    # only thing that writes off a stuck run and the only thing that starts a
    # run the owner asked for, and neither should stop happening because the
    # schedule could not be read or a slot insert misbehaved.
    try:
        scheduled = open_due_scheduled_run(supa)
    except Exception as exc:  # noqa: BLE001 - see above
        log.warning("could not open a scheduled trend run: %s", exc)
        scheduled = None

    run = supa.claim_trend_run()
    if run is None:
        return {"cancelled": cancelled, "expired": expired, "scheduled": None, "started": None}

    cfg = settings()
    missing = [
        name
        for name, value in (
            ("TRENDS_CLUSTER_ARN", cfg.trends_cluster_arn),
            ("TRENDS_TASK_DEFINITION", cfg.trends_task_definition),
            ("TRENDS_SUBNET_IDS", cfg.trends_subnet_ids),
        )
        if not value
    ]
    if missing:
        # Configuration, not weather. Fail the row rather than leaving it
        # claimed, so the owner sees why instead of watching a spinner.
        reason = f"the trend task is not configured for on-demand runs: {', '.join(missing)} unset"
        supa.finish_trend_run(run["id"], status="failed", error=reason)
        log.error("%s", reason)
        return {
            "cancelled": cancelled,
            "expired": expired,
            "scheduled": _slot_of(scheduled),
            "started": None,
            "error": reason,
        }

    try:
        started = boto3.client("ecs").run_task(
            cluster=cfg.trends_cluster_arn,
            taskDefinition=cfg.trends_task_definition,
            launchType="FARGATE",
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": cfg.trends_subnet_id_list,
                    "securityGroups": cfg.trends_security_group_id_list,
                    # Egress goes out through the NAT gateway, exactly as the
                    # scheduled run does.
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "trends",
                        # How the task knows which row to report back into.
                        "environment": [{"name": "TREND_RUN_ID", "value": run["id"]}],
                    }
                ]
            },
        )
    except ClientError as exc:
        supa.finish_trend_run(run["id"], status="failed", error=f"could not start the task: {exc}")
        log.exception("ecs:RunTask failed for trend run %s", run["id"])
        return {
            "cancelled": cancelled,
            "expired": expired,
            "scheduled": _slot_of(scheduled),
            "started": None,
            "error": str(exc),
        }

    # RunTask answers 200 even when it started nothing: a task that cannot be
    # placed comes back in `failures`, with `tasks` empty.
    tasks = started.get("tasks") or []
    if not tasks:
        reason = f"ECS accepted the request but placed no task: {started.get('failures')}"
        supa.finish_trend_run(run["id"], status="failed", error=reason)
        log.error("%s", reason)
        return {
            "cancelled": cancelled,
            "expired": expired,
            "scheduled": _slot_of(scheduled),
            "started": None,
            "error": reason,
        }

    arn = tasks[0].get("taskArn")
    supa.raw.table("trend_runs").update({"task_arn": arn}).eq("id", run["id"]).execute()
    log.info("started trend run %s as %s", run["id"], arn)
    return {
        "cancelled": cancelled,
        "expired": expired,
        "scheduled": _slot_of(scheduled),
        "started": run["id"],
        "trigger": run.get("trigger", "manual"),
        "task_arn": arn,
    }


def _slot_of(run: dict[str, Any] | None) -> str | None:
    """The slot a scheduled run was opened for, for the sweep's own log line."""
    return (run or {}).get("scheduled_for")
