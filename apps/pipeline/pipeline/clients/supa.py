"""Supabase access for the pipeline.

The pipeline is the only writer. `ideas`, `productions`, `approvals`,
`publications` and `post_metrics` deliberately have no INSERT or UPDATE policy,
so the browser structurally cannot advance a job; the service-role key bypasses
row-level security and that asymmetry is the security model. Keep this key out
of anything that reaches a browser.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from supabase import Client, create_client

from pipeline.config import settings
from pipeline.models import Platform, ProductionStatus, QcReport


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
