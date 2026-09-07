"""Supabase access for the pipeline.

The pipeline is the only writer. `ideas`, `productions`, `approvals`,
`publications` and `post_metrics` deliberately have no INSERT or UPDATE policy,
so the browser structurally cannot advance a job; the service-role key bypasses
row-level security and that asymmetry is the security model. Keep this key out
of anything that reaches a browser.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from supabase import Client, create_client

from pipeline.config import settings
from pipeline.models import LIVE_STATUSES, Platform, ProductionStatus, QcReport

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """Coerce an activity payload into something PostgREST will accept.

    Activity results are ordinary dicts, but they carry whatever the providers
    returned -- enums, Paths, the occasional datetime. Anything json does not
    know becomes its string form rather than raising, because this is going into
    a display-only column and a failed event write is a hole in the log.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SupaError(RuntimeError):
    pass


class Supa:
    def __init__(self, client: Client | None = None) -> None:
        cfg = settings()
        self._c = client or create_client(cfg.supabase_url, cfg.supabase_service_role_key)
        self._bucket = cfg.renders_bucket

    @property
    def raw(self) -> Client:
        return self._c

    # -- reads -------------------------------------------------------------

    def production(self, production_id: str) -> dict[str, Any]:
        res = self._c.table("productions").select("*").eq("id", production_id).limit(1).execute()
        if not res.data:
            raise SupaError(f"no production {production_id}")
        return res.data[0]

    def idea(self, idea_id: str) -> dict[str, Any]:
        res = self._c.table("ideas").select("*").eq("id", idea_id).limit(1).execute()
        if not res.data:
            raise SupaError(f"no idea {idea_id}")
        return res.data[0]

    def style_preset(self, preset_id: str) -> dict[str, Any]:
        res = self._c.table("style_presets").select("*").eq("id", preset_id).limit(1).execute()
        if not res.data:
            raise SupaError(f"no style preset {preset_id}")
        return res.data[0]

    def trend_settings(self) -> dict[str, Any] | None:
        """Everything the scout is told to do, as edited in the app.

        The brief, the hashtags, the schedule, the filters and the pacing.
        `select("*")` rather than a column list on purpose: a build that
        predates a column should read the ones it knows and ignore the rest,
        and `pipeline.trends.controls` fills anything absent with the constant
        this code used before that column existed.

        Returns None when the row is missing, which is not an error: the
        migration seeds it, but a pipeline pointed at a database that has not
        been migrated yet should fall back to the environment rather than
        refuse to start.
        """
        res = self._c.table("trend_settings").select("*").limit(1).execute()
        return res.data[0] if res.data else None

    def save_hashtag_cursor(self, cursor: int) -> None:
        """Advance the hashtag rotation. Never raises.

        Bookkeeping, not the run. Losing this leaves the next run scouting the
        same tags again, which is a worse batch of ideas -- but failing the run
        over it throws away the scouting that was about to happen, which is
        worse still.

        Writing only this column is also what keeps `updated_at` honest: the
        touch trigger skips an update that changes the cursor, so the pipeline
        rotating does not present as the owner having edited the settings.
        """
        try:
            self._c.table("trend_settings").update({"hashtag_cursor": cursor}).eq(
                "id", True
            ).execute()
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("could not advance the hashtag cursor to %s: %s", cursor, exc)

    def enabled_platforms(self) -> list[Platform]:
        """Which platforms to publish to, as data rather than code.

        YouTube and TikTok ship disabled: YouTube's default API quota allows six
        uploads a day against a target of ten, and TikTok cannot publish
        automatically until its app audit clears.
        """
        res = (
            self._c.table("platform_targets")
            .select("platform")
            .eq("enabled", True)
            .order("platform")
            .execute()
        )
        return [r["platform"] for r in (res.data or [])]

    def platform_target(self, platform: Platform) -> dict[str, Any] | None:
        res = (
            self._c.table("platform_targets").select("*").eq("platform", platform).limit(1).execute()
        )
        return res.data[0] if res.data else None

    # -- production writes -------------------------------------------------

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        res = self._c.table("productions").update(fields).eq("id", production_id).execute()
        if not res.data:
            raise SupaError(f"update of production {production_id} matched no rows")
        return res.data[0]

    def claim_render_slot(self, production_id: str, task_id: str) -> bool:
        """Reserve the right to submit exactly one render.

        MoneyPrinterTurbo has no idempotency of its own: every POST mints a
        fresh task and re-spends every paid call behind it. This conditional
        update is the guard -- it only succeeds while `task_id` is still null,
        so a duplicate activity invocation loses the race and must not submit.

        Returns True if we won the claim.
        """
        res = (
            self._c.table("productions")
            .update({"task_id": task_id, "status": ProductionStatus.RUNNING, "stage": "render"})
            .eq("id", production_id)
            .is_("task_id", "null")
            .execute()
        )
        return bool(res.data)

    def set_qc(self, production_id: str, report: QcReport) -> dict[str, Any]:
        return self.update_production(production_id, qc=report.model_dump(exclude_none=True))

    def park(self, production_id: str, error: str) -> dict[str, Any]:
        """Stop deliberately and wait for a person.

        The requirement is explicit: a rejection or a quality-check failure
        parks rather than retrying, so no further spend happens without a human
        decision. `parked` is distinct from `failed`, which means the pipeline
        itself broke.

        This used to also write an `approvals` row, which was the only durable
        record a park left. It was the wrong record: `record_system_decision`
        hardcodes `decision = 'rejected'`, so every automated park -- including
        one caused by a network failure on a production that never reached Gate
        2 -- was filed in the audit table as a Gate 2 rejection by nobody. The
        park is now recorded in `production_events`, which is the table for it,
        and `approvals` goes back to meaning what its own comment says: human
        gate decisions.
        """
        row = self.update_production(
            production_id, status=ProductionStatus.PARKED, error=error[:2000]
        )
        self.record_event(
            production_id,
            (row.get("run_state") or {}).get("step") or "unknown",
            "parked",
            detail="Stopped and waiting for a person.",
            error=error[:2000],
        )
        return row

    # -- the step log ------------------------------------------------------

    def record_event(
        self,
        production_id: str,
        step: str,
        outcome: str,
        *,
        detail: str | None = None,
        error: str | None = None,
        attempt: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one row to the production's step log.

        Swallows its own failures. A production must never park because the
        record of it failed to write -- the log exists to explain the run, and
        an explanation that can take the run down with it is worse than a gap.
        The same reasoning as `save_hashtag_cursor` and `finish_trend_run`.
        """
        try:
            self._c.table("production_events").insert(
                {
                    "production_id": production_id,
                    "step": step,
                    "outcome": outcome,
                    "detail": detail,
                    "error": error[:2000] if error else None,
                    "attempt": attempt,
                    "payload": _jsonable(payload or {}),
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001 - never take a production down
            log.warning("could not record %s/%s event on %s: %s", step, outcome, production_id, exc)

    # -- audit -------------------------------------------------------------
    #
    # `record_system_decision` used to live here, and `park()` was its only
    # caller. It hardcoded `decision = 'rejected'`, so every automated park --
    # a HeyGen wallet running dry, a lease expiring five times, a render
    # exceeding its budget -- was filed in `approvals` as a Gate 2 rejection
    # made by nobody, against productions that had in some cases never reached
    # Gate 2. Parks are recorded in `production_events` now, which is the table
    # for them, and `approvals` is left meaning what its own comment says:
    # one immutable row per *human* gate decision.

    # -- the driver's claim and lease --------------------------------------
    #
    # These replaced the Step Functions execution. An execution was the thing
    # that made progress on a production exclusive; a lease is that, expressed
    # as two columns instead of a service.

    def claim_production(self, worker: str, lease_seconds: int) -> dict[str, Any] | None:
        """Take exclusive right to advance one production, or None if none is due.

        An RPC rather than a chained update because PostgREST cannot express
        `for update skip locked`, and that is the entire mechanism -- it is what
        lets two workers each claim a different row without blocking on or
        colliding with each other.

        Note what this deliberately does NOT copy: `claim_trend_run` below
        issues `.update(...).eq("status", "requested")` and takes `rows[0]`,
        which updates *every* matching row and then runs one. That survives
        there because a unique index refuses a second in-flight trend run. Here
        it would hand the same production to every worker at once.
        """
        res = self._c.rpc(
            "claim_production",
            {"p_worker": worker, "p_lease_seconds": lease_seconds},
        ).execute()
        data = res.data
        if isinstance(data, list):
            data = data[0] if data else None
        # A function returning a table row answers with nulls rather than no row
        # when nothing matched, so an id is what "we got one" actually means.
        return data if data and data.get("id") else None

    def save_run_state(
        self, production_id: str, run_state: dict[str, Any], **fields: Any
    ) -> dict[str, Any]:
        """Persist the driver's position and release the lease in one write.

        One statement rather than two, for the same reason `open_gate2` writes
        its marker and its status together: a row whose state advanced but whose
        lease was never released is invisible until the lease expires, and a row
        whose lease was released before its state was written is claimable at
        the step it has already finished.
        """
        return self.update_production(
            production_id,
            run_state=run_state,
            leased_by=None,
            lease_expires_at=None,
            **fields,
        )

    def release_lease(self, production_id: str) -> None:
        """Give the row back without advancing it.

        Used on shutdown and after an infrastructure error, so the next worker
        picks it up at once instead of waiting out the lease.
        """
        self._c.table("productions").update(
            {"leased_by": None, "lease_expires_at": None}
        ).eq("id", production_id).execute()

    def start_approved_productions(self) -> list[dict[str, Any]]:
        """Open a production for every approved idea that has none.

        Replaces the Gate 1 webhook chain -- Supabase Database Webhook, API
        Gateway, SQS, Lambda -- all of which was carrying a single insert. The
        dedup guarantee is unchanged and is now stronger: the `not exists` and
        the `productions_one_live_per_idea` unique index are the same statement
        rather than a read followed by a write.
        """
        res = self._c.rpc("start_approved_productions", {}).execute()
        return res.data or []

    def claim_publish_slot(self, production_id: str) -> bool:
        """Reserve the right to hand this cut to Postiz exactly once.

        The same shape as `claim_render_slot`, and needed for the same reason:
        the lease protects the *step*, but a lease can expire under a worker
        that is wedged rather than dead, and at that moment two workers
        legitimately hold the same row. `claim_render_slot` is what stops the
        second one starting a second billed render; this is what stops it
        posting to a real audience a second time, which Postiz would happily do
        -- it starts its publish workflow with TERMINATE_EXISTING.

        Returns True if we won.
        """
        res = (
            self._c.table("productions")
            .update(
                {
                    "status": ProductionStatus.PUBLISHING.value,
                    "stage": "publishing",
                }
            )
            .eq("id", production_id)
            .eq("status", ProductionStatus.APPROVED.value)
            .execute()
        )
        return bool(res.data)

    def repeatedly_expired_leases(self, threshold: int) -> list[dict[str, Any]]:
        """Rows a worker has died on more than `threshold` times.

        The signature of an out-of-memory kill in `fetch_and_qc`: the lease
        expires, the row is re-claimed, the worker dies again. Re-driving that
        forever is a loop, so it parks instead.
        """
        res = (
            self._c.table("productions")
            .select("id,status,run_state")
            .in_("status", list(LIVE_STATUSES))
            .execute()
        )
        return [
            row
            for row in (res.data or [])
            if int((row.get("run_state") or {}).get("lease_expiries") or 0) >= threshold
        ]

    def unstarted_productions(self) -> list[dict[str, Any]]:
        """Queued rows the driver never picked up.

        `reconcile_renders` cannot see these -- it only looks at rows that are
        already `running`. This is what catches a production opened while the
        driver was down.
        """
        res = (
            self._c.table("productions")
            .select("id,status,run_state,created_at,updated_at,lease_expires_at")
            .eq("status", ProductionStatus.QUEUED.value)
            # A paused row is deliberately not being picked up. Parking it for
            # "the driver never started it" would punish the owner for pausing.
            .is_("paused_at", "null")
            .execute()
        )
        return [
            row
            for row in (res.data or [])
            if not (row.get("run_state") or {}).get("step")
        ]

    def expired_runs(self, days: int) -> list[dict[str, Any]]:
        """Productions still going long after they opened.

        The state machine's top-level 30-day timeout, with one deliberate
        exemption per gate: a row at `await_gate2` or `await_script` is not
        counted. That timeout measured how long a *machine* had been working,
        and the only reason a gate ever fell under it was that a task token
        expires. Nothing expires now, so timing out an owner who took six weeks
        to approve a cut -- or to approve the script for one -- would be
        inventing a limit rather than preserving one.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        res = (
            self._c.table("productions")
            .select("id,status,run_state,created_at")
            .in_("status", list(LIVE_STATUSES))
            .lt("created_at", cutoff)
            .is_("paused_at", "null")
            .execute()
        )
        return [
            row
            for row in (res.data or [])
            if (row.get("run_state") or {}).get("step") not in ("await_gate2", "await_script")
        ]

    def productions_at_step(self, step: str, *, status: str) -> list[dict[str, Any]]:
        """Rows resting at one driver step, for the backlog flush."""
        res = (
            self._c.table("productions")
            .select("id,run_state")
            .eq("status", status)
            .execute()
        )
        return [
            row
            for row in (res.data or [])
            if (row.get("run_state") or {}).get("step") == step
        ]

    # -- publications ------------------------------------------------------

    def upsert_publication(
        self, production_id: str, platform: Platform, **fields: Any
    ) -> dict[str, Any]:
        payload = {"production_id": production_id, "platform": platform, **fields}
        res = (
            self._c.table("publications")
            .upsert(payload, on_conflict="production_id,platform")
            .execute()
        )
        return res.data[0] if res.data else {}

    def publications(self, production_id: str) -> list[dict[str, Any]]:
        res = (
            self._c.table("publications")
            .select("*")
            .eq("production_id", production_id)
            .execute()
        )
        return res.data or []

    # -- storage -----------------------------------------------------------

    def upload_render(self, production_id: str, local: Path, filename: str = "final.mp4") -> str:
        """Put a finished render in object storage and return its path.

        The bucket is private on purpose: Postiz needs a public HTTPS URL, but
        making the bucket itself public would expose every unreviewed cut to
        anyone who guessed a path. A short-lived signed URL is minted at publish
        time instead.

        MoneyPrinterTurbo has no object-storage support at all, so this
        download-then-upload hop is load-bearing rather than an optimisation.
        """
        key = f"{production_id}/{filename}"
        with local.open("rb") as fh:
            self._c.storage.from_(self._bucket).upload(
                path=key,
                file=fh,
                file_options={"content-type": "video/mp4", "upsert": "true"},
            )
        return key

    def signed_render_url(self, key: str, expires_in: int = 3600) -> str:
        res = self._c.storage.from_(self._bucket).create_signed_url(key, expires_in)
        url = res.get("signedURL") or res.get("signedUrl")
        if not url:
            raise SupaError(f"could not sign {key}: {res}")
        return url

    # -- ideas -------------------------------------------------------------

    def insert_ideas(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        res = self._c.table("ideas").insert(rows).execute()
        return res.data or []

    def expire_stale_ideas(self, older_than_days: int = 7) -> int:
        """Stop dead trends being approved weeks later.

        `approve_idea` only acts on a `pending` idea, so expiry is the only
        thing preventing an old idea being produced against a trend that has
        already passed.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        stamp = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        res = (
            self._c.table("ideas")
            .update({"status": "expired"})
            .eq("status", "pending")
            .lt("created_at", stamp)
            .execute()
        )
        return len(res.data or [])

    # -- trend runs ---------------------------------------------------------
    #
    # An on-demand run requested from the app. The row is the request, the lock
    # that stops a second one starting, and the record of what came back; see
    # the 20260906140000 migration for why it is all three.

    def claim_trend_run(self) -> dict[str, Any] | None:
        """Take the oldest requested run, or return None if there is none.

        The `eq("status", "requested")` in the UPDATE is the claim: two
        reconcilers racing here both issue it, and only the first matches a
        row. Reading and then writing would let both through, and each would
        start its own hour-long browser session.
        """
        res = (
            self._c.table("trend_runs")
            .update({"status": "running", "started_at": _now_iso()})
            .eq("status", "requested")
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    def finish_trend_run(self, run_id: str, **fields: Any) -> None:
        """Record how a run ended. Never raises.

        Called from the trend task's own failure path, where the interesting
        error is the one being handled -- losing it to a secondary failure
        writing this row would leave the queue with no explanation at all.

        Only ever writes a row that is still in flight, which is what makes a
        terminal status final. An owner can now stop a run from the app while
        its task is still winding down, and without this filter that task would
        finish a minute later and quietly overwrite `cancelled` with
        `succeeded` -- reviving a run the owner had already been told was
        stopped, and taking the freed lock back with it.
        """
        try:
            (
                self._c.table("trend_runs")
                .update({"finished_at": _now_iso(), **fields})
                .eq("id", run_id)
                .in_("status", ["requested", "running"])
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("could not record the end of trend run %s: %s", run_id, exc)

    def trend_run(self, run_id: str) -> dict[str, Any] | None:
        """The run's own row, for the overrides it carries.

        Returns None rather than raising when it cannot be read. The overrides
        are a refinement of settings that are already loaded and already valid,
        so a failure here should cost the run its requested length, not the
        run itself.
        """
        try:
            res = (
                self._c.table("trend_runs").select("*").eq("id", run_id).limit(1).execute()
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("could not read trend run %s: %s", run_id, exc)
            return None
        rows = res.data or []
        return rows[0] if rows else None

    def trend_run_is_cancelled(self, run_id: str) -> bool:
        """Whether an owner has stopped this run out from under us.

        Read by the scout between hashtags. Cancelling frees the lock in
        Postgres immediately, which is what makes the button work with the
        dispatcher down -- but the browser session it freed the lock from is
        still going, and nothing has told it. This is how it finds out.

        False on any error, deliberately. A transient read failure must not
        abandon a run that is half way through an hour of scouting; the
        dispatcher's `ecs:StopTask` is the other half of this, and the run
        write-off is the backstop behind both.
        """
        try:
            res = (
                self._c.table("trend_runs")
                .select("status")
                .eq("id", run_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("could not check whether run %s was stopped: %s", run_id, exc)
            return False
        rows = res.data or []
        return bool(rows) and rows[0].get("status") == "cancelled"

    def record_cancelled_progress(self, run_id: str, **fields: Any) -> None:
        """Write what a stopped run had found, without reviving it.

        Deliberately not `finish_trend_run`, which refuses to touch a terminal
        row -- that refusal is what stops a task finishing a minute after being
        cancelled and overwriting `cancelled` with `succeeded`. But the
        diagnostic columns are a different matter from the outcome: knowing a
        stopped run had already looked at three hundred videos is the
        difference between "I stopped it too early" and "it was getting
        nowhere anyway".

        So this writes only what the run saw, only onto a row that is already
        `cancelled`, and never touches `status`, `inserted` or `finished_at`.
        Never raises: it is a nicety on a path that has already ended.
        """
        try:
            (
                self._c.table("trend_runs")
                .update(fields)
                .eq("id", run_id)
                .eq("status", "cancelled")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.warning("could not record progress for stopped run %s: %s", run_id, exc)

    def cancelled_runs_needing_stop(self, *, limit: int = 5) -> list[dict[str, Any]]:
        """Cancelled runs whose ECS task has not been stopped yet.

        `task_arn is not null` is what separates a run that was cancelled
        before anything started -- the common case, and the one needing no AWS
        call at all -- from one cancelled mid-session.
        """
        res = (
            self._c.table("trend_runs")
            .select("id,task_arn")
            .eq("status", "cancelled")
            .not_.is_("task_arn", "null")
            .is_("task_stopped_at", "null")
            .limit(limit)
            .execute()
        )
        return res.data or []

    def mark_task_stopped(self, run_id: str) -> None:
        """Record that stopping the task was attempted.

        Written whether or not the call succeeded. It bounds the retry: a task
        that cannot be stopped -- already gone, a cluster since deleted, a
        permission changed -- would otherwise be tried again every minute for
        as long as the row exists.
        """
        try:
            self._c.table("trend_runs").update({"task_stopped_at": _now_iso()}).eq(
                "id", run_id
            ).execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record the task stop for run %s: %s", run_id, exc)

    def stale_trend_runs(
        self, *, running_hours: int, requested_minutes: int | None
    ) -> list[dict[str, Any]]:
        """Runs that will never finish on their own.

        A worker killed mid-scout, a container that will not start, or a
        dispatcher that claimed a row and then died leaves the row in flight
        forever -- and because at most one row may be in flight, that disables
        the button permanently. This is what makes that state recoverable
        without going into the database by hand.

        `requested_minutes=None` means "do not sweep unclaimed requests at
        all", and it is not a convenience -- it is the difference between a
        dispatcher that polls faster than this threshold and one that does not.

        A `requested` row is only stranded if nothing is coming to claim it. On
        a dispatcher that runs every minute, ten minutes of silence really does
        mean that. On one that runs hourly it means nothing at all, and reaping
        the row anyway destroys the request a few lines before the same process
        would have claimed and run it. So the caller states which world it is
        in rather than inheriting an assumption from a comment.
        """
        now = datetime.now(timezone.utc)
        running_cutoff = (now - timedelta(hours=running_hours)).isoformat()
        requested_cutoff = (
            None if requested_minutes is None
            else (now - timedelta(minutes=requested_minutes)).isoformat()
        )
        res = (
            self._c.table("trend_runs")
            .select("id,status,requested_at,started_at")
            .in_("status", ["requested", "running"])
            .execute()
        )
        stale = []
        for row in res.data or []:
            # A claimed row is judged from when it started, an unclaimed one
            # from when it was asked for. `started_at` is null on the second,
            # and on a row claimed by a reconciler that died before setting it.
            if row["status"] == "running":
                cutoff = running_cutoff
            elif requested_cutoff is None:
                continue
            else:
                cutoff = requested_cutoff
            since = row.get("started_at") or row["requested_at"]
            if since < cutoff:
                stale.append(row)
        return stale

    def scheduled_run_exists(self, slot: datetime) -> bool:
        """Whether the run for this schedule slot has already been recorded.

        Asked before inserting, and not the thing that makes the insert safe --
        `trend_runs_one_per_slot` is. This only keeps the ordinary case quiet:
        the dispatcher runs every minute and a slot stays due for the length of
        the catch-up window, so without this the log would carry a unique
        violation a minute for an hour after every scheduled run.
        """
        res = (
            self._c.table("trend_runs")
            .select("id")
            .eq("scheduled_for", slot.isoformat())
            .limit(1)
            .execute()
        )
        return bool(res.data)

    def open_scheduled_trend_run(self, slot: datetime) -> dict[str, Any] | None:
        """Record the run the schedule is asking for, as `requested`.

        Inserted rather than started directly so that a scheduled run is the
        same kind of thing as one the owner asked for: the same single
        in-flight lock, the same claim, the same row to report progress and a
        rejection breakdown into. Before this, a scheduled run had no row at
        all and its result existed only in CloudWatch.

        Returns None when the insert is refused, which is the expected outcome
        of two races rather than an error. Either a run is already in flight --
        `trend_runs_single_in_flight` -- in which case this slot is retried on
        a later tick, or another dispatcher already recorded this slot, and
        `trend_runs_one_per_slot` is what stops the second hour-long browser
        session.
        """
        try:
            res = (
                self._c.table("trend_runs")
                .insert(
                    {
                        "status": "requested",
                        "trigger": "schedule",
                        "scheduled_for": slot.isoformat(),
                    }
                )
                .execute()
            )
        except Exception as exc:  # noqa: BLE001 - both races land here
            log.info("did not open a scheduled run for %s: %s", slot.isoformat(), exc)
            return None
        rows = res.data or []
        return rows[0] if rows else None

    def latest_trend_run(self) -> dict[str, Any] | None:
        res = (
            self._c.table("trend_runs")
            .select("*")
            .order("requested_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None
