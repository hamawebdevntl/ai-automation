"""The scheduled sweepers, and the loop that runs them.

These were eight EventBridge Scheduler rules, each pointing at the activities
Lambda with a `{"activity": "..."}` payload. They are a table and a `while` now.

Not system cron, deliberately. Every invocation would pay a Python start and a
`create_client`, and -- the real objection -- it would put the schedule back
outside the application, which is precisely the mistake `trends.schedule` was
written to undo when the trend cron moved from Terraform into a settings table.
Replacing EventBridge with crontab would repeat it in a different file.

Not a second container either. Every sweep here is short, idempotent and
already isolated from the others by its own try/except; the only argument for
splitting them out is that a slow one could starve production stepping, and a
thread answers that for far less.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pipeline.activities import analytics, gates, presenter
from pipeline.activities import reconcile as recon
from pipeline.clients.supa import Supa
from pipeline.config import settings

log = logging.getLogger(__name__)

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


def _publishing() -> bool:
    return settings().publishing_enabled


def _presenter() -> bool:
    """Whether this deployment renders with HeyGen at all."""
    return bool(settings().heygen_api_key)


@dataclass(frozen=True)
class Sweep:
    name: str
    fn: Callable[[Supa], Any]
    seconds: int
    enabled: Callable[[], bool] = lambda: True


SWEEPS: tuple[Sweep, ...] = (
    # Lease recovery wants to be fast, and this is a cheap query. It ran every
    # fifteen minutes as `reconcile_executions`, which was sized for describing
    # a Step Functions execution over the network.
    Sweep("reconcile_leases", recon.reconcile_leases, MINUTE),
    Sweep("reconcile_renders", recon.reconcile_renders, 5 * MINUTE),
    # Only meaningful once Postiz exists. Registered either way so that turning
    # publishing on is an environment change rather than a code change.
    Sweep("reconcile_publishes", recon.reconcile_publishes, 5 * MINUTE, _publishing),
    Sweep("flush_publishing_backlog", gates.flush_publishing_backlog, 5 * MINUTE, _publishing),
    Sweep("collect_analytics", analytics.collect_analytics, 12 * HOUR, _publishing),
    # Every minute, and almost always a no-op: the sweep reads one row and
    # stops unless something asked for a refresh or the cache has gone stale.
    # The period is set by the button in Settings rather than by the six-hourly
    # refill -- an owner who has just created an avatar in HeyGen wants it in
    # the picker now, and a sweep on the refill's own period would make that
    # button a lie.
    Sweep("refresh_presenter_catalogue", presenter.refresh_catalogue, MINUTE, _presenter),
    Sweep("reap_mpt_tasks", recon.reap_mpt_tasks, DAY),
    Sweep("expire_ideas", recon.expire_ideas, DAY),
)


def run_sweeps(stop: threading.Event, supa: Supa | None = None) -> None:
    """Run each sweep on its own period until told to stop."""
    supa = supa or Supa()

    now = time.monotonic()
    # Anything hourly or faster runs immediately: a redeploy is the likeliest
    # moment for a stuck row to exist, and waiting five minutes to find out is
    # five minutes of a queue not moving. The daily sweeps do not -- one of them
    # deletes files off the render host, and neither is urgent enough to pay for
    # on every restart.
    due = {s.name: (now if s.seconds <= HOUR else now + s.seconds) for s in SWEEPS}

    while not stop.is_set():
        now = time.monotonic()
        for sweep in SWEEPS:
            if now < due[sweep.name] or not sweep.enabled():
                continue
            # Rescheduled before the call, not after, so a sweep that runs
            # longer than its own period cannot queue itself up behind itself.
            due[sweep.name] = now + sweep.seconds
            try:
                result = sweep.fn(supa)
            except Exception:
                log.exception("sweep %s failed", sweep.name)
                continue
            if result and any(result.values() if isinstance(result, dict) else [result]):
                log.info("sweep %s: %s", sweep.name, result)
        stop.wait(1.0)

    log.info("sweeps stopped")
