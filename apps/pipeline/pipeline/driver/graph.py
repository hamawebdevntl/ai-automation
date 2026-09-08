"""The production graph, transcribed from AWS Step Functions.

This file replaces `infra/statemachine.asl.json`. It is deliberately pure --
no I/O, no Supabase, no imports from `engine` -- so that it can be read side by
side with the ASL it came from and checked line for line. Everything that
executes lives in `engine.py`.

The central translation: the state machine had three ways of not doing
something yet -- `Wait` states, `Retry.IntervalSeconds`, and the `Choice`
default arcs that looped back on themselves. All three collapse into one field,
`run_state.due_at`, and no worker thread ever sleeps holding a production. That
is why `WaitForRender` and `WaitForPublish` are not steps here: they are
`wait_before` on the polls that followed them.

What is NOT carried over is the second retry block on `SubmitRender`
(`Lambda.ServiceException`, `Lambda.TooManyRequestsException`, `Lambda.Unknown`).
Those were faults in the invocation layer, which no longer exists. Their true
analogue is a PostgREST or DNS blip reaching Supabase, and that must not consume
a step's attempts -- `publish` has none to consume. `engine.py` handles it
separately, and that is deliberate rather than an omission.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# Stands in for ASL's `States.ALL`.
ANY_ERROR = ("*",)


@dataclass(frozen=True)
class Retry:
    """One ASL `Retry` entry.

    `errors` holds exception *class names*, matched against the raised
    exception's MRO, so the strings here are the same literals the ASL used and
    a rename in `clients/` shows up as a test failure rather than a silent loss
    of retrying.
    """

    errors: tuple[str, ...]
    interval_seconds: int
    max_attempts: int
    backoff_rate: float = 1.0

    def matches(self, exc: BaseException) -> bool:
        if self.errors == ANY_ERROR:
            return True
        names = {cls.__name__ for cls in type(exc).__mro__}
        return bool(names & set(self.errors))

    def delay_for(self, attempt: int) -> int:
        """Seconds to wait before attempt number `attempt` (1-based)."""
        return int(self.interval_seconds * (self.backoff_rate ** max(0, attempt - 1)))


@dataclass(frozen=True)
class Step:
    """One state.

    `next` is either a step name or a function of the merged run_state, which
    is how ASL's `Choice` states are expressed -- there is no separate Choice
    step, because a Choice never did any work.
    """

    name: str
    run: str | None = None
    """Dotted `module.function` in `pipeline.activities`, or None for a step
    that waits rather than acts. Resolved by `engine.py` rather than imported
    here, so this module stays free of I/O and importable in isolation."""

    retry: tuple[Retry, ...] = ()
    catch: str = "parked"
    next: str | Callable[[dict[str, Any]], str] = "parked"
    wait_before: int = 0
    """Seconds to defer before running, replacing the ASL `Wait` state that
    preceded this one."""

    lease_seconds: int = 900
    terminal: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def route(self, state: dict[str, Any]) -> str:
        return self.next(state) if callable(self.next) else self.next


# ---------------------------------------------------------------------------
# Choice states
# ---------------------------------------------------------------------------

# The ASL guard on `RenderOutcome`: "A guard against looping forever. The poller
# has its own wall-clock budget, so reaching this means the budget was never
# applied."
MAX_RENDER_POLLS = 400


def _render_outcome(state: dict[str, Any]) -> str:
    """ASL `RenderOutcome`."""
    result = state.get("state")
    if result == "complete":
        return "fetch_and_qc"
    if result == "failed":
        return "parked"
    if int(state.get("polls") or 0) > MAX_RENDER_POLLS:
        return "parked"
    return "poll_render"


def _script_outcome(state: dict[str, Any]) -> str:
    """The script gate's only arc.

    There is no rejecting branch, and that is not an omission. Rejecting a
    script is editing it -- `save_script` and `approve_script` both write the
    text -- and rejecting the *idea* behind it is cancelling the production,
    which is a control rather than a route. So the one thing that can be waited
    for here is approval.

    Reads `productions.script_approved_at` rather than the payload, for the same
    reason `_gate2_outcome` reads `productions.status`: the decision is a row
    change made by a security-definer function, and taking it from the payload
    would be taking it from something a caller could forge. `claim_production`
    will not return this row at all until that column is set, so reaching here
    with it null means the claim predicate and this function have drifted apart
    -- park rather than render, because the alternative is spending money on an
    unapproved script.
    """
    if state.get("script_approved_at"):
        return "submit_render"
    return "parked"


def _gate2_outcome(state: dict[str, Any]) -> str:
    """ASL `Gate2Outcome`.

    The ASL templated this branch at deploy time -- `on_approved` was `Publish`
    or `PublishingDisabled` depending on a Terraform variable -- which meant
    turning publishing on required re-rendering the state machine. It is
    `PUBLISHING_ENABLED` at runtime now, so the same production can be re-driven
    into publishing later without anything being redeployed.

    Reads `productions.status`, not the payload: the decision is a row change
    made by `decide_production`, and taking it from anywhere else would be
    taking it from something a caller could forge.
    """
    if state.get("status") == "approved":
        return "publish" if state.get("publishing_enabled") else "publishing_disabled"
    return "rejected"


def _publish_outcome(state: dict[str, Any]) -> str:
    """ASL `PublishOutcome`."""
    result = (state.get("publish_state") or {}).get("state")
    if result == "published":
        return "published"
    if result == "parked":
        return "parked"
    return "poll_publish"


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------

START = "write_script"

GRAPH: dict[str, Step] = {
    "write_script": Step(
        name="write_script",
        run="script.write_script",
        retry=(
            # One synchronous LLM call, and nothing downstream of it has been
            # billed, so a couple of attempts is the right budget: enough to
            # ride out a restart of the drafting service, few enough that a
            # genuinely broken one parks quickly and visibly.
            Retry(ANY_ERROR, interval_seconds=20, max_attempts=3, backoff_rate=2),
        ),
        # Parking here costs nothing -- no render has been submitted -- and it
        # is not a dead end: `approve_script` accepts a production parked at
        # this step, so an owner can write the script by hand and carry on with
        # the drafting service still down.
        catch="parked",
        next="open_script_gate",
    ),
    "open_script_gate": Step(
        name="open_script_gate",
        run="script.open_script_gate",
        catch="parked",
        next="await_script",
    ),
    "await_script": Step(
        name="await_script",
        # No `run`, exactly like `await_gate2`. The whole of the gate is that
        # `claim_production` does not return a row whose status is
        # `awaiting_script`, and does return it once `approve_script` has moved
        # the status to `running` and set `script_approved_at`.
        #
        # This is the step that makes the brief's central requirement true:
        # `submit_render` is not reachable from anywhere else, so no render can
        # be submitted for a production whose script a person has not approved.
        run=None,
        next=_script_outcome,
    ),
    "submit_render": Step(
        name="submit_render",
        run="render.submit_render",
        retry=(
            # Verbatim from the ASL, and the reasoning is worth keeping: MPT
            # returns 429 when its queue is saturated and writes its state row
            # before scheduling, deleting it again on rejection, so a 429
            # provably leaves no orphan render. HeyGen's 429 is the same story,
            # and its 409 request_in_progress means an earlier submit under our
            # idempotency key is still in flight -- backing off and resubmitting
            # replays it and returns the video id. None of these has billed
            # anything, which is the only reason they are safe to retry.
            Retry(
                errors=(
                    "MptQueueFull",
                    "FalRateLimited",
                    "HeyGenRateLimited",
                    "HeyGenInProgress",
                    # A submit that never got an answer. Safe for the same
                    # reason as the 409 above it: the request carries an
                    # `Idempotency-Key`, so if it did reach HeyGen the retry
                    # replays it and returns the same video id, and if it did
                    # not, nothing was billed. Without this line a transport
                    # error is not a HeyGen failure at all but an engine-level
                    # "infrastructure" retry -- every five seconds, forever.
                    "HeyGenUnreachable",
                ),
                interval_seconds=60,
                max_attempts=10,
                backoff_rate=1.5,
            ),
        ),
        catch="parked",
        next="poll_render",
    ),
    "poll_render": Step(
        name="poll_render",
        run="render.poll_render",
        # Was the `WaitForRender` state.
        wait_before=30,
        retry=(Retry(ANY_ERROR, interval_seconds=30, max_attempts=5, backoff_rate=2),),
        catch="parked",
        next=_render_outcome,
    ),
    "fetch_and_qc": Step(
        name="fetch_and_qc",
        run="render.fetch_and_qc",
        retry=(Retry(ANY_ERROR, interval_seconds=20, max_attempts=3, backoff_rate=2),),
        catch="parked",
        next="generate_copy",
        # The one step that legitimately runs for many minutes: it downloads
        # hundreds of megabytes, and on the fal_full lane `_assemble_fal_full`
        # then does concat, portrait, TTS, transcription and burn-in. A lease
        # sized for the others would expire mid-render and hand the row to a
        # second worker while the first was still working. Sizing the lease per
        # step is what removes the need for a heartbeat.
        lease_seconds=3600,
    ),
    "generate_copy": Step(
        name="generate_copy",
        run="publish.generate_platform_copy",
        retry=(
            # Four synchronous LLM calls. The activity writes conditionally and
            # skips when copy already exists, so a retry cannot produce a
            # second, different version of the text.
            Retry(ANY_ERROR, interval_seconds=15, max_attempts=2, backoff_rate=2),
        ),
        # Not `parked`. Missing copy is not worth parking a finished video for:
        # let the reviewer see it, and the publish step skips any platform with
        # no copy.
        catch="open_gate2",
        next="open_gate2",
    ),
    "open_gate2": Step(
        name="open_gate2",
        run="gates.open_gate2",
        catch="parked",
        next="await_gate2",
    ),
    "await_gate2": Step(
        name="await_gate2",
        # No `run`. This is the whole of the Gate 2 pause: the row is not
        # claimable while its status is awaiting_review or qc_failed, and
        # becomes claimable the moment `decide_production` moves it. See
        # `claim_production` in the migration.
        #
        # The ASL carried `TimeoutSeconds: 604800` here. That existed solely
        # because a Step Functions task token expires after seven days -- it
        # was never a statement about how long an owner may take. There is no
        # token now, so it is gone: a decision on day forty resumes exactly as
        # well as one on day one.
        run=None,
        next=_gate2_outcome,
    ),
    "publish": Step(
        name="publish",
        run="publish.publish",
        # EMPTY ON PURPOSE, and asserted by name in the tests. Postiz starts its
        # publish workflow with TERMINATE_EXISTING, so a retry landing after the
        # provider call succeeded but before the row is marked published posts
        # to the real platform twice.
        retry=(),
        # And for the same reason the catch is a poll rather than a park: on any
        # unknown outcome we go and look, instead of guessing.
        catch="poll_publish",
        next="poll_publish",
    ),
    "poll_publish": Step(
        name="poll_publish",
        run="publish.poll_publish",
        # Was the `WaitForPublish` state.
        wait_before=60,
        retry=(Retry(ANY_ERROR, interval_seconds=30, max_attempts=5, backoff_rate=2),),
        catch="parked",
        next=_publish_outcome,
    ),
    # --- terminals ---------------------------------------------------------
    #
    # All four are ordinary ends. There is no "failed run" in this driver at
    # all, which is the same decision the ASL made by modelling Rejected and
    # Parked as `Succeed`: if a rejection registered as a failure it would
    # pollute the one signal that should mean something is genuinely broken.
    # What replaced the CloudWatch alarm is
    # `select count(*) from productions where status = 'parked'`.
    "published": Step(name="published", terminal=True),
    "rejected": Step(name="rejected", terminal=True),
    "parked": Step(name="parked", terminal=True),
    # Reached only from outside the driver, by `cancel_production`. The graph
    # never routes here -- a cancelled row is not claimable, so `advance` never
    # sees one -- but it is listed so `TERMINALS` is the whole truth and
    # `engine.current_step` cannot fail on a row a person stopped.
    "cancelled": Step(name="cancelled", terminal=True),
    "publishing_disabled": Step(
        name="publishing_disabled",
        terminal=True,
        # Approved, rendered, captioned and deliberately not posted, because no
        # publishing service is deployed. The row keeps status='approved', so
        # these are a queryable backlog rather than a lost batch --
        # `flush_publishing_backlog` is what releases them once Postiz exists.
        metadata={"resumable": True},
    ),
}

TERMINALS = frozenset(name for name, step in GRAPH.items() if step.terminal)


def step(name: str) -> Step:
    try:
        return GRAPH[name]
    except KeyError:
        raise ValueError(f"unknown step {name!r}; known: {sorted(GRAPH)}") from None
