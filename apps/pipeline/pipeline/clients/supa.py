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
from pathlib import Path
from typing import Any

from supabase import Client, create_client

from pipeline.config import settings
from pipeline.models import Platform, ProductionStatus, QcReport

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        """
        row = self.update_production(
            production_id, status=ProductionStatus.PARKED, error=error[:2000]
        )
        self.record_system_decision("production", production_id, gate=2, note=f"parked: {error}"[:500])
        return row

    # -- audit -------------------------------------------------------------

    def record_system_decision(
        self, subject_type: str, subject_id: str, *, gate: int, note: str
    ) -> None:
        """Write an audit row for an automated transition.

        `approvals.actor_id` was NOT NULL against `profiles`, which made it
        impossible for the system to record anything. It is now nullable with a
        `source` discriminator, and a check constraint still requires an actor
        whenever `source = 'human'`.
        """
        self._c.table("approvals").insert(
            {
                "gate": gate,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "decision": "rejected",
                "note": note,
                "source": "system",
                "actor_id": None,
            }
        ).execute()

    # -- gate tokens (private schema, via security-definer RPCs) -----------
    #
    # These wrap functions in `public` that reach into the `private` schema.
    # `private` is not in the exposed schema list, so neither the browser nor
    # even a service-role PostgREST call can touch those tables directly, and
    # EXECUTE on the wrappers is granted to `service_role` alone.

    def set_gate_token(self, production_id: str, token: str) -> None:
        self._c.rpc(
            "set_gate_token", {"p_production_id": production_id, "p_token": token}
        ).execute()

    def take_gate_token(self, production_id: str) -> str | None:
        """Lease the token for a resume attempt.

        Returns None when there is no token, or when another caller leased it
        within the last minute. The lease means a failed send can be retried
        without two concurrent deliveries both resuming the execution.
        """
        res = self._c.rpc("take_gate_token", {"p_production_id": production_id}).execute()
        return res.data or None

    def release_gate_token(self, production_id: str) -> None:
        """Call only once the resume is confirmed.

        `TaskDoesNotExist` and `TaskTimedOut` both count as confirmed: they mean
        the execution has already moved on.
        """
        self._c.rpc("release_gate_token", {"p_production_id": production_id}).execute()

    def record_gate_decision(self, production_id: str, decision: str) -> None:
        """Park a decision that arrived before the token was registered.

        The callback state checks for this the moment it stores its token, which
        is what closes the race in the other direction.
        """
        self._c.rpc(
            "record_gate_decision",
            {"p_production_id": production_id, "p_decision": decision},
        ).execute()

    def peek_gate_decision(self, production_id: str) -> str | None:
        res = self._c.rpc("peek_gate_decision", {"p_production_id": production_id}).execute()
        return res.data or None

    def pending_gate_resumes(self) -> list[dict[str, Any]]:
        """Rows already decided but not yet resumed.

        This is what makes the database webhook an optimisation rather than a
        correctness dependency. Supabase webhooks are pg_net: at-most-once, no
        retry, no dead-letter queue, and a default timeout of one second.
        """
        res = self._c.rpc("list_pending_gate_resumes", {}).execute()
        return res.data or []

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

    def stale_trend_runs(self, *, running_hours: int, requested_minutes: int) -> list[dict[str, Any]]:
        """Runs that will never finish on their own.

        A task killed by Fargate, an image that will not start, or a reconciler
        that claimed a row and then died leaves the row in flight forever -- and
        because at most one row may be in flight, that disables the button
        permanently. This is what makes that state recoverable without a
        console.
        """
        now = datetime.now(timezone.utc)
        running_cutoff = (now - timedelta(hours=running_hours)).isoformat()
        requested_cutoff = (now - timedelta(minutes=requested_minutes)).isoformat()
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
            cutoff = running_cutoff if row["status"] == "running" else requested_cutoff
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
