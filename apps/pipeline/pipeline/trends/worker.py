"""The trend scout's dispatcher and the scout, in one process.

On AWS this was two pieces: `dispatch_trend_runs`, a Lambda that fired every
minute and decided whether to start anything, and a Fargate task that did the
scouting. That split existed because a Lambda cannot run for an hour.

Neither a CI runner nor a worker on a VPS has that constraint, so both halves
collapse into this: decide whether there is work, and if there is, do it in the
same process. It was written for GitHub Actions and is now the only
implementation -- `pipeline.driver.worker` calls it on a timer, and the AWS
dispatcher it replaced has been deleted rather than ported.

What it deliberately keeps from the AWS path:

  * the same `trend_runs` row, the same single in-flight lock, the same claim.
    A run started here is indistinguishable in the app from one started there,
    which is what lets the button, the banner and the rejection breakdown work
    unchanged.
  * the same schedule logic. `schedule.due_slot` reads the owner's settings, so
    moving the runner does not move the schedule into a cron expression nobody
    can edit -- which was the whole point of making it a setting.

Granularity came back with the move off GitHub Actions. A workflow cron cannot
fire every minute without spending the free tier on doing nothing, so the CI
version ran hourly and a scheduled slot started at the top of the hour after it.
A thread has no such cost, so this runs every minute again -- which is what the
owner's schedule in `trend_settings` was designed for, and what the EventBridge
dispatcher used to give it.

Returns having done nothing when there is no work, which is the common case:
most calls find an empty queue and return within milliseconds.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from pipeline.activities.reconcile import (
    RUN_STALE_HOURS,
    open_due_scheduled_run,
)
from pipeline.clients.supa import Supa
from pipeline.trends import runner

log = logging.getLogger(__name__)


def _write_off_stale(supa: Supa) -> list[str]:
    """Clear *claimed* runs that will never finish, so they stop holding the lock.

    At most one run may be in flight, so a run whose process vanished mid-scout
    would otherwise block every future run rather than just its own.

    Unclaimed requests are deliberately left alone, and that is the whole
    difference between this and the AWS sweeper it replaced. That one gave up on
    any request older than ten minutes, on the reasoning that its dispatcher ran
    every minute so ten of them missing a request meant it was not running.

    That reasoning does not transfer, and the cadence is not why. There, the
    dispatcher and the scout were separate -- a Lambda deciding, a Fargate task
    doing -- so a request could genuinely sit unclaimed. Here they are the same
    process: the next thing this function's caller does is claim the request,
    whatever its age. Reaping it first would mean writing off a request a few
    lines before claiming it.

    That is not hypothetical; it is the incident this comment exists for. While
    this ran as an hourly cron the ten-minute rule meant a button pressed more
    than ten minutes before the run fired was written off by the process about
    to serve it -- so the button worked for ten minutes in every sixty and spent
    the other fifty reporting "gave up on a run left requested with nothing
    running it", a sweeper describing a stall it had caused itself. Restoring
    the rule now that the dispatcher runs every minute again would shrink that
    window rather than close it, and it would reopen the moment anything
    delayed the thread.

    A request only strands if this process has stopped running -- and then no
    sweeper of ours is running either, which is what Stop in the app is for.
    """
    expired: list[str] = []
    for row in supa.stale_trend_runs(
        running_hours=RUN_STALE_HOURS, requested_minutes=None
    ):
        supa.finish_trend_run(
            row["id"],
            status="failed",
            error=f"gave up on a run left {row['status']} with nothing running it",
        )
        expired.append(row["id"])
    if expired:
        log.warning("wrote off %d stalled run(s): %s", len(expired), expired)
    return expired


def main(supa: Supa | None = None) -> int:
    """Claim a run if there is one, and carry it out. Returns an exit code."""
    if supa is None:
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
        supa = Supa()

    _write_off_stale(supa)

    # A slot the owner's schedule says is due becomes a row, exactly as the
    # Lambda would have made it. `due_slot` enforces its own catch-up window,
    # so an hourly runner cannot fire a 06:00 slot at nine in the evening.
    try:
        open_due_scheduled_run(supa)
    except Exception as exc:  # noqa: BLE001 - a failed schedule read must not
        # stop a run the owner asked for by hand.
        log.warning("could not open a scheduled run: %s", exc)

    run = supa.claim_trend_run()
    if run is None:
        log.info("nothing to do: no run requested and none due")
        return 0

    run_id = str(run["id"])
    log.info(
        "claimed run %s (%s)",
        run_id,
        "described search" if run.get("prompt") else run.get("trigger", "manual"),
    )

    # The run id is passed as an argument and never through the environment.
    # It used to be set here as `os.environ["TREND_RUN_ID"]`, which was wrong in
    # two ways that cancelled each other out: it is process-global, so it would
    # race between this thread and anything else in the worker -- and it never
    # took effect anyway, because `settings()` is `lru_cache`d and would not
    # have re-read it. `runner.run` takes the id directly, and the cancellation
    # check is built from that parameter.

    try:
        result: dict[str, Any] = runner.run(supa, run_id=run_id)
    except Exception as exc:
        # The row must never be left claimed. Without this the single in-flight
        # lock would be held by a run that has already crashed, and the button
        # would stay shut until the write-off above ran an hour later.
        supa.finish_trend_run(run_id, status="failed", error=str(exc)[:2000])
        log.exception("run %s failed", run_id)
        return 1

    if result.get("cancelled"):
        supa.record_cancelled_progress(
            run_id,
            scouted=result.get("scouted"),
            hashtags_scouted=result.get("hashtags_scouted"),
            rejections=result.get("rejections"),
        )
        log.info("run %s was stopped from the app", run_id)
        return 0

    supa.finish_trend_run(
        run_id,
        status="succeeded",
        signals=result.get("signals"),
        drafted=result.get("drafted"),
        inserted=result.get("inserted"),
        suppressed=result.get("suppressed"),
        scouted=result.get("scouted"),
        hashtags_scouted=result.get("hashtags_scouted"),
        rejections=result.get("rejections"),
    )
    log.info(
        "run %s finished: %d ideas from %d videos",
        run_id,
        result.get("inserted") or 0,
        result.get("scouted") or 0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
