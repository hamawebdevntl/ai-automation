"""The worker process: `python -m pipeline.driver.worker`.

This is what a Step Functions state machine, two Lambda functions, an SQS pair,
an API Gateway, an ECS cluster and eight EventBridge schedules turn into once
none of them are available: one process with three kinds of thread.

  * N production threads, each claiming a row and advancing it one step.
  * one sweep thread, running the reconcilers on their periods.
  * one trends thread, which is `pipeline.trends.worker` -- itself already the
    collapse of a Lambda dispatcher and a Fargate task into one process, and the
    pattern the rest of this is modelled on.

Nothing here sleeps holding a claim, so a thread is only ever busy with work
that is actually running.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import uuid

from pipeline.activities import clips as clip_activities
from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.driver import engine
from pipeline.driver.sweeps import run_sweeps
from pipeline.trends import worker as trends_worker

log = logging.getLogger(__name__)

# How often the trends dispatcher looks for work. It returns in milliseconds
# when there is none, which is almost every time.
TRENDS_INTERVAL_SECONDS = 60

# How often the clipping lane looks for an uploaded recording to work on. It
# returns in milliseconds when there is none, which is almost every time -- one
# indexed query against `clip_sources`.
CLIPS_INTERVAL_SECONDS = 20


def _worker_id(index: int) -> str:
    base = settings().worker_id or f"{os.uname().nodename}-{uuid.uuid4().hex[:6]}"
    return f"{base}#{index}"


def run_productions(stop: threading.Event, index: int) -> None:
    """Claim and advance productions until told to stop.

    One `Supa` per thread on purpose -- the client is not shared.
    """
    supa = Supa()
    worker = _worker_id(index)
    idle = settings().driver_poll_seconds
    log.info("production worker %s started", worker)

    # Only one thread opens productions. Every thread could call it safely --
    # the insert is guarded by `productions_one_live_per_idea` -- but there is
    # no reason for four threads to ask the same question every five seconds.
    dispatcher = index == 0

    while not stop.is_set():
        try:
            if dispatcher:
                engine.start_due_productions(supa)
            result = engine.tick(supa, worker)
        except Exception:
            log.exception("production worker %s recovered from an error", worker)
            stop.wait(idle)
            continue

        if result is None:
            stop.wait(idle)
        # Otherwise loop straight round: there may be more work, and a step that
        # is not yet due sets its own `due_at` rather than blocking here.

    log.info("production worker %s stopped", worker)


def run_trends(stop: threading.Event) -> None:
    """The trend scout's dispatcher, as a thread.

    `trends.worker.main` was written for GitHub Actions -- "a machine that
    boots, does one thing and exits" -- and already contains everything the AWS
    dispatcher did: the write-off of stalled runs, the due-slot check, the
    single in-flight claim, and the run reporting into its own row. Calling it
    on a timer is the whole port.

    Its own thread rather than a sweep slot because a scout run is an hour of
    deliberately paced scraping, and it must not hold up a reconciler.
    """
    supa = Supa()
    while not stop.is_set():
        try:
            trends_worker.main(supa)
        except Exception:
            log.exception("trend dispatcher recovered from an error")
        stop.wait(TRENDS_INTERVAL_SECONDS)
    log.info("trend dispatcher stopped")


def run_clips(stop: threading.Event) -> None:
    """The clipping lane's pre-gate phases, as a thread.

    Its own thread rather than a sweep slot, for the reason `driver/sweeps.py`
    gives for keeping slow work out of that loop: "a slow one could starve
    production stepping". Transcribing an hour of audio is minutes of waiting on
    fal, and a sweep holding its slot for that long would stall lease recovery
    for every production in the system.

    The trend scout is in its own thread on exactly this argument and is the
    pattern here. What is different is that this loop keeps going while there is
    work: each call advances one recording by one phase, so a batch of three
    uploads would otherwise take a minute to get started rather than seconds.
    """
    supa = Supa()
    worker = _worker_id(0).replace("#0", "#clips")
    log.info("clip worker %s started", worker)

    while not stop.is_set():
        try:
            result = clip_activities.advance_clip_sources(supa, worker)
        except Exception:
            # `advance_clip_sources` already marks a failing source `failed` with
            # the reason on the row. Reaching here means the claim itself failed
            # -- a Supabase blip -- so there is nothing to record against and the
            # right response is to wait and ask again.
            log.exception("clip worker recovered from an error")
            stop.wait(CLIPS_INTERVAL_SECONDS)
            continue

        if not result:
            stop.wait(CLIPS_INTERVAL_SECONDS)
        else:
            log.info("clip worker: %s", result)

    log.info("clip worker stopped")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = settings()
    stop = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        # A redeploy is a SIGTERM. Without this the rows held at that moment
        # stay leased until they expire, and the new process cannot touch them
        # -- turning a two-second restart into minutes of a stalled queue.
        log.info("signal %s: finishing the current step, then stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threads = [
        threading.Thread(target=run_sweeps, args=(stop,), name="sweeps", daemon=True),
        threading.Thread(target=run_trends, args=(stop,), name="trends", daemon=True),
        threading.Thread(target=run_clips, args=(stop,), name="clips", daemon=True),
    ]
    threads += [
        threading.Thread(target=run_productions, args=(stop, i), name=f"prod-{i}", daemon=True)
        for i in range(cfg.production_workers)
    ]

    for thread in threads:
        thread.start()
    log.info(
        "worker up: %d production thread(s), publishing=%s",
        cfg.production_workers,
        cfg.publishing_enabled,
    )

    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()

    for thread in threads:
        thread.join(timeout=30)
    log.info("worker down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
