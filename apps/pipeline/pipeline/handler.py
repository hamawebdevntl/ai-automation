"""Lambda entrypoint.

One image, one dispatch table. Step Functions passes `activity` in the payload
and the state machine's states map one-to-one onto the keys below.

Two functions are deployed from this image rather than one, because
`fetch_and_qc` is the only activity that handles the actual video: it needs
more memory, a longer timeout and ffmpeg, and sizing everything else for that
would be wasteful. The webhook bridges are invoked from SQS instead of Step
Functions, and are dispatched here too so there is a single deployment artifact.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from pipeline.activities import analytics, gates, publish, render
from pipeline.activities import reconcile as recon

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    # Step Functions states
    "submit_render": render.submit_render,
    "poll_render": render.poll_render,
    "fetch_and_qc": render.fetch_and_qc,
    "generate_copy": publish.generate_platform_copy,
    "register_gate2": gates.register_gate2,
    "publish": publish.publish,
    "poll_publish": publish.poll_publish,
    # Webhook bridges, invoked from SQS
    "start_production": gates.start_production,
    "gate2_bridge": gates.gate2_bridge,
    # Scheduled reconcilers
    "reconcile_gates": lambda event: gates.reconcile_gates(),
    "reconcile_renders": lambda event: recon.reconcile_renders(),
    "reconcile_executions": lambda event: recon.reconcile_executions(),
    "reconcile_publishes": lambda event: recon.reconcile_publishes(),
    "reap_mpt_tasks": lambda event: recon.reap_mpt_tasks(),
    "expire_ideas": lambda event: recon.expire_ideas(),
    "collect_analytics": lambda event: analytics.collect_analytics(),
}


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    # A state may pass its entire input through as `payload` rather than
    # enumerating fields. That matters once a render can run on more than one
    # backend: the state carries different keys depending on which one is in
    # play, and Step Functions fails on a missing JSONPath rather than treating
    # it as null, so listing them would break the moment a backend differed.
    payload = event.get("payload")
    if isinstance(payload, dict):
        event = {**payload, **{k: v for k, v in event.items() if k != "payload"}}

    activity = event.get("activity") or _activity_from_sqs(event)
    if not activity:
        raise ValueError(f"no activity in payload: {sorted(event)[:8]}")

    fn = DISPATCH.get(activity)
    if fn is None:
        raise ValueError(f"unknown activity {activity!r}; known: {sorted(DISPATCH)}")

    log.info("activity=%s production=%s", activity, event.get("production_id"))
    result = fn(event)
    log.info("activity=%s done: %s", activity, json.dumps(result, default=str)[:500])
    return result


def _activity_from_sqs(event: dict[str, Any]) -> str | None:
    """An SQS batch carries the activity in the message attributes.

    The queue is fed by API Gateway from a Supabase Database Webhook, and the
    route decides which bridge should handle it.
    """
    records = event.get("Records") or []
    for record in records:
        attrs = record.get("messageAttributes") or {}
        value = (attrs.get("activity") or {}).get("stringValue")
        if value:
            return value
    return None
