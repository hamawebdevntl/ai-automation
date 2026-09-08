"""One step of one production.

Everything that decides *what* happens is in `graph.py`; this decides *when*
and writes the result down. The split is deliberate -- the graph is the part
that has to be checked against the state machine it replaced, and it is far
easier to check when it cannot fail for an I/O reason.

The shape of a tick is: claim a row, run its step, merge what came back, route,
persist, release. Nothing sleeps while holding a claim; a step that is not ready
sets `due_at` and gives the row back.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from pipeline.activities import gates, publish, render, script
from pipeline.clients.supa import Supa, SupaError
from pipeline.config import settings
from pipeline.driver import graph as g

log = logging.getLogger(__name__)

# Activities, resolved here rather than imported into `graph.py` so that module
# stays free of anything that opens a socket.
ACTIVITIES = {
    "script.write_script": script.write_script,
    "script.open_script_gate": script.open_script_gate,
    "render.submit_render": render.submit_render,
    "render.poll_render": render.poll_render,
    "render.fetch_and_qc": render.fetch_and_qc,
    "publish.generate_platform_copy": publish.generate_platform_copy,
    "publish.publish": publish.publish,
    "publish.poll_publish": publish.poll_publish,
    "gates.open_gate2": gates.open_gate2,
}

# How long to hold a row back after an infrastructure error. Short, because
# nothing is wrong with the production itself.
INFRA_BACKOFF_SECONDS = 5

# An infrastructure error does not consume an attempt, so a row can bounce on
# one indefinitely. The first bounce is worth a log entry; the next eleven are
# not, and the twelfth -- about a minute later at the backoff above -- is worth
# one again, so the log says "still" rather than saying nothing or saying it
# 720 times an hour.
INFRA_EVENT_EVERY = 12

# Reserved keys. Everything else in `run_state` is activity payload.
_CONTROL_KEYS = (
    "step",
    "due_at",
    "attempts",
    "lease_expiries",
    "opened_at",
    "ended_at",
    # Which step this row was on before it entered the one it is on now. A
    # terminal overwrites `step` with its own name, so without this a parked
    # production would not know where it stopped -- neither the UI's timeline
    # nor `retry_production` could place it.
    "previous_step",
    # Collapses a poll-again arc into one event per meaningful change.
    # `poll_render` can tick 400 times against MAX_RENDER_POLLS; the log should
    # say what changed, not how often we asked.
    "event_fingerprint",
    # Consecutive infrastructure retries on the current step. Cleared the
    # moment the step succeeds. Exists so the log records that a row has been
    # bouncing, without recording every bounce.
    "infra_retries",
)


class InfrastructureError(RuntimeError):
    """Our own plumbing failed, not the step.

    Kept separate because it must not consume an attempt. `publish` has zero
    retries by design, so counting a PostgREST timeout against it would send a
    perfectly good production straight to its catch -- which is exactly the
    failure the Step Functions `Lambda.ServiceException` retry block existed to
    prevent, expressed for the layer we actually have now.
    """


# What entering each step means, in a sentence a person can read. The UI shows
# these; the log reads as prose because of them.
_STEP_OPENING = {
    "write_script": "Writing the narration script for you to review.",
    "open_script_gate": "Opening the script gate for a person to approve the words.",
    "await_script": "Waiting for a person to approve the script. No render is submitted until they do.",
    "submit_render": "Sending the render to the production backend.",
    "poll_render": "Waiting for the render to finish.",
    "fetch_and_qc": "Downloading the finished cut and running the quality check.",
    "generate_copy": "Writing the per-platform copy.",
    "open_gate2": "Opening Gate 2 for a person to sign off the cut.",
    "await_gate2": "Waiting for a person to sign off the cut.",
    "publish": "Publishing to the enabled platforms.",
    "poll_publish": "Confirming the platforms accepted the post.",
}

_TERMINAL_DETAIL = {
    "published": "Published. This production is finished.",
    "rejected": "Rejected at Gate 2. Nothing was published.",
    "parked": "Stopped and waiting for a person.",
    "cancelled": "Cancelled by an owner.",
    "publishing_disabled": (
        "Held: the cut and its copy are ready, but publishing is switched off. "
        "This is a backlog, not a failure."
    ),
}


def _describe(step_name: str, state: dict[str, Any]) -> str | None:
    """One sentence saying what a completed step actually did.

    Reads the keys the activities already return, so nothing in `activities/`
    has to change to be explainable.
    """
    if step_name == "write_script":
        chars = state.get("script_chars")
        size = f" ({chars} characters)" if isinstance(chars, int) else ""
        if state.get("script_source") == "kept":
            return f"Kept the script this production already had{size}. Nothing was rewritten."
        if state.get("script_source") == "redrafted":
            return f"Wrote a fresh draft{size}, replacing the previous one as asked."
        cut = (
            " It came back over the 5000 character limit and was cut."
            if state.get("script_truncated")
            else ""
        )
        return f"Drafted the narration{size}.{cut}"
    if step_name == "open_script_gate":
        return "The script gate is open. Nothing is rendered until a person approves the words."
    if step_name == "submit_render":
        backend = state.get("backend") or "the render backend"
        return f"Render submitted to {backend}."
    if step_name == "poll_render":
        return "The render finished." if state.get("state") == "complete" else "The render stopped."
    if step_name == "fetch_and_qc":
        risk = state.get("slideshow_risk")
        verdict = "passed" if state.get("qc_passed") else "failed"
        tail = f", slideshow risk {risk:.2f}." if isinstance(risk, (int, float)) else "."
        return f"Cut downloaded, stored and checked. Quality check {verdict}{tail}"
    if step_name == "generate_copy":
        if state.get("skipped"):
            return "Per-platform copy was already written, so nothing was regenerated."
        wrote = list(state.get("platforms") or [])
        missing = list(state.get("missing") or [])
        # `missing` has never been surfaced anywhere before: the activity
        # returns it and the publish step silently skips those platforms.
        first = f"Wrote copy for {', '.join(wrote)}." if wrote else "No platform copy was written."
        return first + (
            f" No copy for {', '.join(missing)} — those will be skipped." if missing else ""
        )
    if step_name == "open_gate2":
        return "Gate 2 is open. The cut is waiting for a person."
    if step_name == "publish":
        return "Sent to the publisher."
    if step_name == "poll_publish":
        return "The platforms confirmed the post."
    return None


def _progress_detail(step_name: str, state: dict[str, Any]) -> str | None:
    if step_name != "poll_render":
        return "Still waiting."
    progress = state.get("progress")
    phase = state.get("phase") or state.get("backend")
    where = f" on {phase}" if phase else ""
    if isinstance(progress, (int, float)) and progress:
        return f"Rendering{where}: {int(progress)}%."
    return f"Rendering{where}."


def _fingerprint(state: dict[str, Any]) -> str:
    """What has to change before a poll is worth another log line."""
    progress = state.get("progress")
    bucket = int(progress) // 5 if isinstance(progress, (int, float)) else None
    return f"{state.get('state')}|{state.get('phase')}|{bucket}"


def _event_payload(state: dict[str, Any]) -> dict[str, Any]:
    """The technical half of an event: everything but the driver's own keys."""
    return {k: v for k, v in state.items() if k not in _CONTROL_KEYS and k != "production_id"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _due(seconds: int) -> str:
    return (_now() + timedelta(seconds=seconds)).isoformat()


def _is_due(run_state: dict[str, Any]) -> bool:
    raw = run_state.get("due_at")
    if not raw:
        return True
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")) <= _now()
    except ValueError:
        return True


def current_step(run_state: dict[str, Any] | None) -> str:
    """Where this row is.

    Defaults to the start step, which is correct for exactly one case: a row
    just inserted by `start_approved_productions`, which has no step yet. It is
    also why a terminal writes a terminal marker rather than clearing
    `run_state` -- a cleared row would default back here and re-run the whole
    production, paying for it a second time.
    """
    return (run_state or {}).get("step") or g.START


# ---------------------------------------------------------------------------
# Running one step
# ---------------------------------------------------------------------------


def advance(row: dict[str, Any], supa: Supa) -> dict[str, Any]:
    """Take one claimed production forward by one step.

    Returns a small dict describing what happened, for the log and the tests.
    Never raises for an ordinary step failure -- the graph's `catch` is what
    decides where those go.
    """
    production_id = row["id"]
    run_state: dict[str, Any] = dict(row.get("run_state") or {})
    name = current_step(run_state)
    step = g.step(name)

    if step.terminal:
        # Claimed a finished row. Possible after a `publishing_disabled` flush
        # or a lease that outlived its own terminal write; recording it is
        # cheaper than reasoning about whether it can happen.
        supa.save_run_state(production_id, run_state)
        return {"production_id": production_id, "step": name, "outcome": "already_terminal"}

    run_state.setdefault("opened_at", _now().isoformat())

    if not _is_due(run_state):
        # The claim query filters on this too; reaching here means the row came
        # back early, so give it up rather than running ahead of schedule.
        supa.save_run_state(production_id, run_state)
        return {"production_id": production_id, "step": name, "outcome": "not_due"}

    # A waiting step -- `await_gate2` and `await_script` -- does no work. Its
    # route reads the row it was claimed with, because a decision is a row
    # change rather than anything a previous step returned. Handing the payload
    # these columns instead would be handing the route something a caller could
    # forge.
    if step.run is None:
        decided = {
            **run_state,
            "status": row.get("status"),
            "script_approved_at": row.get("script_approved_at"),
        }
        return _route(production_id, step, decided, run_state, supa)

    event = {k: v for k, v in run_state.items() if k not in _CONTROL_KEYS}
    event["production_id"] = production_id

    if "step" not in run_state:
        # The first step of a fresh row is the one step that never passes
        # through `_enter`, which is what announces every step after it.
        supa.record_event(production_id, name, "started", detail=_STEP_OPENING.get(name))

    try:
        result = _invoke(step, event, supa)
    except InfrastructureError as exc:
        # Deliberately does not touch `attempts`.
        log.warning("infrastructure error on %s/%s: %s", production_id, name, exc)
        # This used to be the one thing that left no trace at all: a row could
        # bounce on a PostgREST or DNS blip indefinitely and look, from the
        # outside, exactly like a row that was making progress. Recorded on the
        # first bounce and then once a minute or so, not on every one.
        repeats = int(run_state.get("infra_retries") or 0) + 1
        run_state["infra_retries"] = repeats
        if repeats == 1 or repeats % INFRA_EVENT_EVERY == 0:
            still = f" This is the {repeats}th time in a row." if repeats > 1 else ""
            supa.record_event(
                production_id,
                name,
                "infra_retry",
                detail=f"Our own plumbing failed, not the step. Trying again in {INFRA_BACKOFF_SECONDS}s.{still}",
                error=str(exc),
                attempt=repeats,
            )
        run_state["due_at"] = _due(INFRA_BACKOFF_SECONDS)
        supa.save_run_state(production_id, run_state)
        return {"production_id": production_id, "step": name, "outcome": "infra_retry"}
    except Exception as exc:  # noqa: BLE001 - routed by the graph, not swallowed
        return _handle_failure(production_id, step, run_state, exc, supa)

    # The step got through, so the bounce count it may have been carrying is
    # over. Cleared here rather than in `_enter`, because a poll-again arc
    # never enters anything and its next infrastructure hiccup is a new run.
    run_state.pop("infra_retries", None)
    merged = {**run_state, **(result or {})}
    return _route(production_id, step, merged, merged, supa)


def _invoke(step: g.Step, event: dict[str, Any], supa: Supa) -> dict[str, Any]:
    """Call the activity, distinguishing our plumbing from its own.

    The distinction is narrow on purpose: only failures raised before the
    activity has reached its own third-party call count as infrastructure. Once
    HeyGen or fal has been contacted, a failure is the step's and must be routed
    by the graph -- treating it as infrastructure would retry a call that may
    already have been billed.
    """
    fn = ACTIVITIES[step.run]
    try:
        return fn(event, supa) or {}
    except (SupaError, httpx.TransportError) as exc:
        raise InfrastructureError(str(exc)) from exc


def _handle_failure(
    production_id: str,
    step: g.Step,
    run_state: dict[str, Any],
    exc: Exception,
    supa: Supa,
) -> dict[str, Any]:
    attempts: dict[str, Any] = dict(run_state.get("attempts") or {})
    used = int(attempts.get(step.name) or 0)

    for policy in step.retry:
        if not policy.matches(exc):
            continue
        if used + 1 >= policy.max_attempts:
            break
        attempts[step.name] = used + 1
        run_state["attempts"] = attempts
        delay = policy.delay_for(used + 1)
        run_state["due_at"] = _due(delay)
        supa.save_run_state(production_id, run_state)
        # The cause used to survive only in the worker log, and only until the
        # attempts ran out -- so a production that succeeded on its third try
        # left no record of the two failures that preceded it.
        supa.record_event(
            production_id,
            step.name,
            "retrying",
            detail=f"Attempt {used + 1} of {policy.max_attempts} failed. Trying again in {delay}s.",
            error=f"{type(exc).__name__}: {exc}",
            attempt=used + 1,
        )
        log.info(
            "retrying %s on %s after %s (attempt %d/%d)",
            step.name,
            production_id,
            type(exc).__name__,
            used + 1,
            policy.max_attempts,
        )
        return {
            "production_id": production_id,
            "step": step.name,
            "outcome": "retry",
            "attempt": used + 1,
        }

    log.warning("%s failed on %s: %s", step.name, production_id, exc)
    supa.record_event(
        production_id,
        step.name,
        "failed",
        detail=f"Gave up on this step and went to {step.catch}.",
        error=f"{type(exc).__name__}: {exc}",
        attempt=used + 1,
    )
    failed = {**run_state, "error": f"{type(exc).__name__}: {exc}"[:2000]}
    return _enter(production_id, step.catch, failed, supa, reason=str(exc))


def _route(
    production_id: str,
    step: g.Step,
    routing_state: dict[str, Any],
    run_state: dict[str, Any],
    supa: Supa,
) -> dict[str, Any]:
    """Decide the next step and write the row there."""
    if step.name == "await_gate2":
        routing_state = {**routing_state, "publishing_enabled": settings().publishing_enabled}

    nxt = step.route(routing_state)

    if nxt == step.name:
        # A poll-again arc. The ASL expressed this as `Default: WaitForRender`;
        # here it is the same step with its own `wait_before` applied.
        #
        # One event per meaningful change rather than one per poll: a render
        # that takes an hour is 120 polls and perhaps a dozen real changes of
        # state, and a log of the other 108 would bury them.
        fingerprint = _fingerprint(routing_state)
        if fingerprint != run_state.get("event_fingerprint"):
            run_state["event_fingerprint"] = fingerprint
            supa.record_event(
                production_id,
                step.name,
                "progress",
                detail=_progress_detail(step.name, routing_state),
                payload=_event_payload(routing_state),
            )
        run_state["due_at"] = _due(step.wait_before or 1)
        supa.save_run_state(production_id, run_state)
        return {"production_id": production_id, "step": step.name, "outcome": "poll_again"}

    # Leaving the step, so it is finished -- which is the only point at which a
    # poll step can honestly be called succeeded.
    if step.run is not None:
        supa.record_event(
            production_id,
            step.name,
            "succeeded",
            detail=_describe(step.name, routing_state),
            payload=_event_payload(routing_state),
        )

    return _enter(production_id, nxt, run_state, supa)


def _enter(
    production_id: str,
    name: str,
    run_state: dict[str, Any],
    supa: Supa,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move the row to `name` and persist."""
    nxt = g.step(name)
    state = {
        **run_state,
        "step": name,
        # Recorded before `step` is overwritten, because a terminal overwrites
        # it with its own name and the step that actually failed would
        # otherwise be lost. `resume_step()` in SQL reads this to know where a
        # retry should go back in.
        "previous_step": run_state.get("step") or g.START,
    }
    state.pop("due_at", None)
    # The new step gets its own poll budget.
    state.pop("event_fingerprint", None)
    if nxt.wait_before:
        state["due_at"] = _due(nxt.wait_before)

    if nxt.terminal:
        return _finish(production_id, nxt, state, supa, reason)

    supa.record_event(production_id, name, "started", detail=_STEP_OPENING.get(name))

    if name in ("await_gate2", "await_script"):
        # The step before each of these already wrote the status; this writes
        # the marker. Nothing more to do but leave the row unclaimed -- the
        # claim predicate is the entire wait, and it costs nothing while it
        # lasts.
        supa.save_run_state(production_id, state)
        return {"production_id": production_id, "step": name, "outcome": "gate_open"}

    supa.save_run_state(production_id, state)
    return {"production_id": production_id, "step": name, "outcome": "advanced"}


def _finish(
    production_id: str,
    step: g.Step,
    run_state: dict[str, Any],
    supa: Supa,
    reason: str | None,
) -> dict[str, Any]:
    """Write a terminal marker.

    Note what is not done here: `run_state` is never cleared. A terminal row
    with an empty `run_state` would read as `step = submit_render` on the next
    claim and render the whole production again.
    """
    state = {**run_state, "step": step.name, "ended_at": _now().isoformat()}
    state.pop("due_at", None)

    if step.name == "parked":
        # `park` writes the status, the error and the event. Doing it here too
        # would duplicate the entry, which is why this branch records nothing.
        supa.park(production_id, reason or state.get("error") or "parked by the driver")
        supa.save_run_state(production_id, state)
    elif step.name == "rejected":
        # `decide_production` already set the status; this only records where
        # the driver stopped.
        supa.save_run_state(production_id, state)
    else:
        # `published` is written by `poll_publish`; `publishing_disabled` keeps
        # status='approved' on purpose, so it stays a queryable backlog.
        supa.save_run_state(production_id, state)

    if step.name != "parked":
        # `park` already recorded its own event, with the error attached.
        supa.record_event(
            production_id,
            step.name,
            "terminal",
            detail=_TERMINAL_DETAIL.get(step.name),
            error=state.get("error"),
        )

    log.info("production %s finished at %s", production_id, step.name)
    return {"production_id": production_id, "step": step.name, "outcome": "terminal"}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def tick(supa: Supa, worker: str) -> dict[str, Any] | None:
    """Claim one production and advance it, or return None if none is due."""
    cfg = settings()
    name_hint = None
    row = supa.claim_production(worker, cfg.lease_seconds)
    if row is None:
        return None

    run_state = dict(row.get("run_state") or {})
    name_hint = current_step(run_state)

    # A row whose lease had already lapsed was abandoned by another worker.
    # Counting that is what lets `reconcile_leases` tell a transient restart
    # from a step that kills whichever worker picks it up.
    if row.get("lease_expires_at"):
        expired = _parse(row["lease_expires_at"])
        if expired and expired < _now():
            run_state["lease_expiries"] = int(run_state.get("lease_expiries") or 0) + 1
            row = {**row, "run_state": run_state}

    try:
        return advance(row, supa)
    except Exception:
        # The row must never be left leased. Anything reaching here is a bug in
        # the driver rather than in a step, and holding the claim would take the
        # production down with it until the lease expired.
        log.exception("driver crashed on %s at %s", row["id"], name_hint)
        supa.release_lease(row["id"])
        raise


def _parse(raw: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def start_due_productions(supa: Supa) -> list[str]:
    """Open a production for every idea approved since the last pass.

    Runs on the dispatcher at the driver's own cadence rather than as a sweep:
    it is one RPC returning nothing on almost every tick, and it is what keeps
    "approve an idea and watch it start" feeling the way the webhook made it
    feel.
    """
    started = [row["id"] for row in supa.start_approved_productions()]
    if started:
        log.info("opened %d production(s): %s", len(started), started)
    return started
