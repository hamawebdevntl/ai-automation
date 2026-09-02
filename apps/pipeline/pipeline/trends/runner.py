"""The scheduled trend-research run.

Scouts, scores, drafts ideas, and writes them to the Gate 1 queue. Runs as a
Fargate task rather than a Lambda because TikTok-Api drives a real Playwright
browser per session, which is far too heavy for the activities image.
"""

from __future__ import annotations

import logging
import re

from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.trends import ideas as ideas_mod
from pipeline.trends import tiktok
from pipeline.trends.velocity import Signal

log = logging.getLogger(__name__)

# How far back to look when suppressing near-duplicate ideas.
DEDUP_WINDOW_DAYS = 14


def _normalise(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _existing_titles(supa: Supa) -> set[str]:
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)).isoformat()
    rows = (
        supa.raw.table("ideas")
        .select("title")
        .gte("created_at", cutoff)
        .execute()
    ).data or []
    return {_normalise(r["title"]) for r in rows if r.get("title")}


def run(supa: Supa | None = None) -> dict:
    """Scout, score, draft, insert."""
    supa = supa or Supa()
    cfg = settings()

    if not cfg.niche_brief.strip():
        # Deliberately a hard stop. A trend run without a brief fills the
        # owner's queue with plausible, irrelevant ideas -- which is worse than
        # an empty queue, because each one costs a review.
        raise RuntimeError(
            "NICHE_BRIEF is not set. Trend research cannot judge relevance without it; "
            "refusing to fill the approval queue with generic ideas."
        )

    hashtags = cfg.hashtag_list
    if not hashtags:
        raise RuntimeError("TREND_HASHTAGS is empty; there is nothing to scout.")

    signals: list[Signal] = tiktok.scout(
        tiktok.ScoutConfig(
            hashtags=hashtags,
            ms_token=cfg.tiktok_ms_token,
            videos_per_hashtag=30,
        )
    )
    log.info("scouted %d signals worth surfacing across %d hashtags", len(signals), len(hashtags))
    if not signals:
        return {"signals": 0, "inserted": 0, "reason": "no signal cleared its source's baseline"}

    drafted = ideas_mod.generate_ideas(
        signals, cfg.niche_brief, count=cfg.ideas_per_run
    )
    platforms = supa.enabled_platforms()
    rows = ideas_mod.to_rows(drafted, signals, platforms)

    # EventBridge schedules are at-least-once, so a retried run must not double
    # the queue. This is application-level rather than a unique constraint,
    # because two near-identical titles are not exactly equal.
    seen = _existing_titles(supa)
    fresh = [r for r in rows if _normalise(r["title"]) not in seen]
    suppressed = len(rows) - len(fresh)

    inserted = supa.insert_ideas(fresh)
    log.info("inserted %d ideas (%d suppressed as duplicates)", len(inserted), suppressed)
    return {
        "signals": len(signals),
        "drafted": len(drafted),
        "inserted": len(inserted),
        "suppressed": suppressed,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    result = run()
    log.info("trend run complete: %s", result)


if __name__ == "__main__":
    main()
