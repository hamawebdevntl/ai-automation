"""The trend scout as a scheduled job, with no AWS behind it.

On AWS this is two pieces: `dispatch_trend_runs`, a Lambda that fires every
minute and decides whether to start anything, and a Fargate task that does the
scouting. That split exists because a Lambda cannot run a browser for an hour.

A CI runner has no such constraint -- it is a machine that boots, does one
thing and exits -- so both halves collapse into this: decide whether there is
work, and if there is, do it in the same process.

What it deliberately keeps from the AWS path:

  * the same `trend_runs` row, the same single in-flight lock, the same claim.
    A run started here is indistinguishable in the app from one started there,
    which is what lets the button, the banner and the rejection breakdown work
    unchanged.
  * the same schedule logic. `schedule.due_slot` reads the owner's settings, so
    moving the runner does not move the schedule into a cron expression nobody
    can edit -- which was the whole point of making it a setting.

What it changes: the granularity. A workflow cron cannot fire every minute
without spending the free tier on doing nothing, so this runs hourly and a
scheduled slot starts at the top of the hour following it. A run the owner
asks for waits at most that long too, unless something triggers the workflow
sooner.

Exits 0 with nothing done when there is no work. That is the common case --
most invocations of this find an empty queue and stop within seconds.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from pipeline.activities.reconcile import (
    REQUEST_STALE_MINUTES,
    RUN_STALE_HOURS,
    open_due_scheduled_run,
)
from pipeline.clients.supa import Supa
from pipeline.trends import runner

log = logging.getLogger(__name__)


def _write_off_stale(supa: Supa) -> list[str]:
    """Clear runs that will never finish, so they stop holding the lock.

    The same job the AWS sweeper does, and needed for the same reason: at most
    one run may be in flight, so a job cancelled mid-scout -- which on a CI
    runner means the whole machine vanished without warning -- would otherwise
    block every future run rather than just its own.

    A CI runner makes this more likely than Fargate did, not less: workflows
    get cancelled, time out, and lose their machine to spot reclamation.
    """
    expired: list[str] = []
    for row in supa.stale_trend_runs(
        running_hours=RUN_STALE_HOURS, requested_minutes=REQUEST_STALE_MINUTES
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


def main() -> int:
    """Claim a run if there is one, and carry it out. Returns an exit code."""
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
    log.info("claimed run %s (%s)", run_id, run.get("trigger", "manual"))

    # `runner.main` reads this to know which row to report into, and the scout
    # reads it to notice being stopped from the app.
    os.environ["TREND_RUN_ID"] = run_id

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
