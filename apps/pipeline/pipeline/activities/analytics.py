"""Pull published-post metrics back into the database.

This closes the loop the whole system exists for: what the owner approved, and
how it actually performed. Without it, idea scoring has no way to learn from
our own results and stays purely a function of other people's trends.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.clients.postiz import PostizClient, PostizError
from pipeline.clients.supa import Supa

log = logging.getLogger(__name__)

# Sampled for this long after publishing. Short-form performance is mostly
# settled within a week, and sampling forever would grow the table without
# telling us anything new.
SAMPLE_WINDOW_DAYS = 8


def collect_analytics(
    supa: Supa | None = None, postiz: PostizClient | None = None
) -> dict[str, Any]:
    """Sample metrics for recently published posts."""
    supa = supa or Supa()
    postiz = postiz or PostizClient()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=SAMPLE_WINDOW_DAYS)).isoformat()
    productions = (
        supa.raw.table("productions")
        .select("id,completed_at")
        .eq("status", "published")
        .gte("completed_at", cutoff)
        .execute()
    ).data or []

    written, skipped = 0, 0
    for production in productions:
        for pub in supa.publications(production["id"]):
            post_id = pub.get("postiz_post_id")
            if pub.get("state") != "published" or not post_id:
                continue
            try:
                series = postiz.post_analytics(post_id, days=SAMPLE_WINDOW_DAYS)
            except PostizError as exc:
                log.warning("analytics failed for %s/%s: %s", production["id"], pub["platform"], exc)
                continue

            if not series:
                # Legitimately empty while a release id is still resolving --
                # a fresh TikTok post needs a second lookup before its real
                # video id exists, so this is expected rather than an error.
                skipped += 1
                continue

            rows = list(_flatten(pub["id"], series))
            if rows:
                # Unique on (publication_id, captured_at, label), so a repeated
                # run inside the same instant is a no-op rather than a
                # duplicate sample.
                supa.raw.table("post_metrics").upsert(
                    rows, on_conflict="publication_id,captured_at,label"
                ).execute()
                written += len(rows)

    log.info("wrote %d metric rows across %d productions", written, len(productions))
    return {"productions": len(productions), "metrics_written": written, "not_ready": skipped}


def _flatten(publication_id: str, series: list[dict[str, Any]]):
    """Turn Postiz's AnalyticsData[] into label/value rows.

    Postiz returns `[{label, data: [{total, date}], percentageChange}]` with the
    totals as *strings*, and `percentageChange` is frequently a hardcoded
    placeholder -- so the raw string is kept alongside a best-effort numeric and
    the change field is ignored entirely.
    """
    captured = datetime.now(timezone.utc).isoformat()
    for entry in series:
        label = entry.get("label")
        if not label:
            continue
        points = entry.get("data") or []
        raw = str(points[-1].get("total")) if points else None
        yield {
            "publication_id": publication_id,
            "captured_at": captured,
            "label": str(label),
            "value": _numeric(raw),
            "raw_value": raw,
        }


def _numeric(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
