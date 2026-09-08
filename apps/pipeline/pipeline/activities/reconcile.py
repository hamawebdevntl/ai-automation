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
# The Step Functions state machine carried `TimeoutSeconds: 2592000` at the top
# level. Kept, but narrowed: `expired_runs` exempts a production sitting at
# Gate 2, because that clock was measuring the machine, not the owner.
RUN_TIMEOUT_DAYS = 30


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
    """Park renders that MoneyPrinterTurbo will never finish.

    Skips rather than raises when MPT is not configured, for the same reason
    `reconcile_publishes` skips when publishing is off: rendering is a
    deployable half of this system, and a sweeper that throws on every tick of
    a deployment that has deliberately left it out is noise standing exactly
    where a real failure would appear.

    Note this is not the same as MPT being *down*. An unreachable MPT still
    raises `MptError` per row below and is logged as the outage it is; this is
    only the case where no render backend was ever configured.
    """
    supa = supa or Supa()
    if mpt is None:
        if not settings().mpt_base_url:
            return {"skipped": "MPT_BASE_URL is not set"}
        mpt = MptClient()

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
# B -- leases
# ---------------------------------------------------------------------------

# How many times a worker may die on the same row before we stop handing it out.
MAX_LEASE_EXPIRIES = 5


def reconcile_leases(supa: Supa | None = None) -> dict[str, Any]:
    """Rows the driver cannot make progress on by itself.

    This replaced `reconcile_executions`, but it is a different job rather than
    a port. That function existed to detect a dead Gate 2 task token -- "the row
    sits at awaiting_review looking perfectly healthy, and the owner's decision
    would resume nothing". There is no token now, so that failure cannot happen
    and the function has nothing to do.

    What is left is narrower. Note first what is NOT here: an expired lease is
    the ordinary recovery path, not an error. A worker that stops mid-step
    leaves a lease that lapses, and the very next claim picks the row up and
    carries on. Sweeping those would be sweeping the mechanism working.

    Two things the claim genuinely cannot resolve:

      * a row that kills whichever worker claims it. The lease lapses, the row
        is re-claimed, the worker dies again -- which is what an out-of-memory
        `fetch_and_qc` looks like from here. Re-driving it forever is a loop.
      * a row the driver never picked up at all. `reconcile_renders` cannot see
        these: it only looks at rows already `running`.

    And the old top-level 30-day execution timeout, narrowed to exempt a
    production waiting on a human -- see `expired_runs` below.
    """
    supa = supa or Supa()

    parked: list[str] = []

    for row in supa.repeatedly_expired_leases(MAX_LEASE_EXPIRIES):
        state = row.get("run_state") or {}
        supa.park(
            row["id"],
            f"the driver died {state.get('lease_expiries')} times on step "
            f"{state.get('step') or 'unknown'} -- it needs a person, not another attempt",
        )
        parked.append(row["id"])

    for row in supa.unstarted_productions():
        # Opened by Gate 1 and never claimed, or uploaded and never checked.
        # Only real when the driver was down; the grace is generous for the
        # same reason it is in `reconcile_renders` -- a queue is not a stall.
        #
        # An upload is measured from `updated_at` rather than `created_at`,
        # because unlike a row that has never been touched it can legitimately
        # be between attempts: `check_upload` retries three times with backoff,
        # and each attempt writes the row. Read from `created_at`, a retrying
        # upload would be parked out from under the retry.
        since = row.get("updated_at") if row.get("source") == "upload" else row.get("created_at")
        if _stale(since, NOT_STARTED_GRACE_MINUTES):
            supa.park(row["id"], "queued but never claimed -- was the driver running?")
            parked.append(row["id"])

    for row in supa.expired_runs(RUN_TIMEOUT_DAYS):
        supa.park(row["id"], f"still running {RUN_TIMEOUT_DAYS} days after it opened")
        parked.append(row["id"])

    if parked:
        log.warning("lease reconciler parked %d production(s): %s", len(parked), parked)
    return {"parked": parked}


# ---------------------------------------------------------------------------
# C -- publishes
# ---------------------------------------------------------------------------


def reconcile_publishes(supa: Supa | None = None) -> dict[str, Any]:
    """The only publish-failure detector that exists."""
    if not settings().publishing_enabled:
        return {"skipped": "publishing is disabled (PUBLISHING_ENABLED=false)"}

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
    if mpt is None:
        # Same reasoning as `reconcile_renders`: nothing to reap when no render
        # backend is configured.
        if not settings().mpt_base_url:
            return {"skipped": "MPT_BASE_URL is not set"}
        mpt = MptClient()

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


# `stop_cancelled_tasks` lived here and is gone with ECS.
#
# It called `ecs:StopTask` on the Fargate task behind a run the owner had
# cancelled -- one of the two halves that closed the window between a row being
# marked `cancelled` and the browser session it was protecting actually
# stopping. The other half is the scout checking its own row between hashtags,
# and that half is the one that never needed AWS. It is now the whole
# mechanism, and it is the better of the two: it works when the dispatcher is
# the broken part, and the dispatcher is now in the same process as the scout
# anyway, so the failure the first half covered no longer has a way to happen.


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
# A request the dispatcher never picked up. This can be short again: the
# dispatcher is a thread in the worker running every minute, so ten of them
# missing a request means it is not running. It was widened to `None` while
# the dispatcher was an hourly GitHub Actions cron, where ten minutes of
# silence was the normal state rather than evidence of anything.
REQUEST_STALE_MINUTES = 10


# `dispatch_trend_runs` lived here and is gone with ECS.
#
# It was a Lambda firing every minute that decided whether to start a trend run
# and then called `ecs:RunTask`, because a Lambda cannot run a browser for an
# hour. That split was an AWS constraint, not a design: a worker on a VPS is a
# process that can simply do both.
#
# `pipeline.trends.worker.main` already collapsed the two halves for the GitHub
# Actions path, and it is now the only implementation -- the driver runs it on
# its own thread. Everything this function did survives there: the write-off of
# stalled runs, `open_due_scheduled_run` below, the single in-flight claim, and
# the run reporting back into its own row.
