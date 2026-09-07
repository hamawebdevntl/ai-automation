"""The two approval gates.

Both used to need machinery. Gate 1 was a Supabase Database Webhook into API
Gateway, onto SQS, into a Lambda, which read the idea and called
`StartExecution`. Gate 2 was a Step Functions task token, stored in a private
table behind six security-definer functions, redeemed by a second webhook on the
same path, with a sweeper behind it because `pg_net` is at-most-once and would
sometimes simply drop a decision.

None of that survives, and what replaced it is smaller than the thing it
replaced was working around: a decision is a row change, and the driver claims
rows. `claim_production` admits a row at `await_gate2` only once its status is
`approved` or `rejected`, so waiting costs nothing and cannot be dropped.
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.clients.supa import Supa
from pipeline.models import ProductionStatus

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gate 1 -- a trigger, not a wait
# ---------------------------------------------------------------------------


def start_approved_productions(supa: Supa | None = None) -> dict[str, Any]:
    """Open a production for every approved idea that does not have one.

    Gate 1 never needed a token: approving an idea is what *starts* work rather
    than something running work waits on. That was true under Step Functions
    too, which is why there was only ever one token type in this system.

    Everything is read from Postgres inside a single statement, so there is no
    payload to forge and no window between the check and the insert. The dedup
    guarantee is the one that was always there --
    `productions_one_live_per_idea`, which excludes rejected, failed and parked
    so a re-run is still possible -- but it is now enforced in the same
    statement as the read rather than across a read and a write.
    """
    supa = supa or Supa()
    rows = supa.start_approved_productions()
    if rows:
        log.info("Gate 1 opened %d production(s)", len(rows))
    return {"started": [row["id"] for row in rows]}


# ---------------------------------------------------------------------------
# Gate 2 -- a genuine pause
# ---------------------------------------------------------------------------


def open_gate2(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Make the production decidable, and stop.

    The ordering rule this function inherits is worth restating, because the
    hazard survived the rewrite even though the token did not. Under Step
    Functions the rule was "register the token before the row becomes
    decidable" -- reversed, an owner deciding in that window would find no token
    to redeem, and the execution hung until timeout while the interface reported
    success. The driver has the same failure in different clothes: if the status
    became `approved` while `run_state.step` still said `generate_copy`, the
    claim predicate would never match and the row would sit at `approved`
    forever with nothing coming for it.

    The difference is that here the ordering can be removed rather than merely
    got right. Both facts are columns on the same row, so one PATCH writes them
    together and there is no window at all.
    """
    supa = supa or Supa()
    production_id = event["production_id"]
    qc_passed = bool(event.get("qc_passed", True))

    status = ProductionStatus.AWAITING_REVIEW if qc_passed else ProductionStatus.QC_FAILED

    # A failed quality check still opens the gate -- it arrives flagged, so an
    # owner can look at it and override. Refusing to show it would make the
    # check a censor rather than an aid.
    supa.update_production(production_id, status=status.value, stage="review")

    log.info("production %s is at Gate 2 (%s)", production_id, status.value)
    return {"status": status.value, "qc_passed": qc_passed}


def flush_publishing_backlog(supa: Supa | None = None) -> dict[str, Any]:
    """Send approved-but-unpublished cuts back to the gate.

    Everything approved while publishing was switched off rests at
    `status='approved'` with `run_state.step = 'publishing_disabled'`, holding
    its finished render and its per-platform copy. This is what releases them
    once Postiz exists, and it is the whole reason `publishing_disabled` is a
    distinct terminal rather than just `published` with an asterisk.

    Deliberately not automatic in spirit even though it is automatic in
    mechanism: a month of backlog released at once is a month of posts released
    at once, and `platform_targets.daily_cap` is not what will stop it. The
    sweep is registered only when publishing is enabled, so turning it on is the
    deliberate act.
    """
    supa = supa or Supa()
    rows = supa.productions_at_step(
        "publishing_disabled", status=ProductionStatus.APPROVED.value
    )
    released = []
    for row in rows:
        state = {**(row.get("run_state") or {}), "step": "await_gate2"}
        state.pop("ended_at", None)
        supa.save_run_state(row["id"], state)
        released.append(row["id"])
    if released:
        log.info("released %d production(s) from the publishing backlog", len(released))
    return {"released": released}
