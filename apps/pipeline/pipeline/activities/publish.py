"""Copy generation and publishing.

Publishing is the one irreversible step in the pipeline, so the rules here are
deliberately conservative: never retry a create, dedup on a deterministic id,
and treat a 200 as "queued" rather than "published".
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline.clients.mpt import MptClient
from pipeline.clients.postiz import PostizClient, PostizError, deterministic_post_id
from pipeline.clients.supa import Supa
from pipeline.copy import copy_for_platforms
from pipeline.models import Platform, PostizIntegration, PostizMedia, ProductionStatus

log = logging.getLogger(__name__)

# Which Postiz provider identifiers satisfy each of our platform names.
# Ordered by preference: a company LinkedIn Page is strongly preferred over a
# personal profile, because the personal provider reports no analytics at all.
ACCEPTABLE_IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram", "instagram-standalone"),
    "tiktok": ("tiktok", "tiktok-business"),
    "youtube": ("youtube",),
    "linkedin": ("linkedin-page", "linkedin"),
}

# Postiz budgets roughly 30 minutes of pending checks in its own publish
# workflow, so past this point it has either given up or never started.
STALE_QUEUE_MINUTES = 45


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def generate_platform_copy(
    event: dict[str, Any], supa: Supa | None = None, mpt: MptClient | None = None
) -> dict[str, Any]:
    """Write `productions.platform_copy` for every enabled platform.

    Written conditionally: if copy already exists we keep it. Regenerating
    would spend four more LLM calls and produce *different* text, so a reviewer
    could approve one version and publish another.
    """
    supa = supa or Supa()
    mpt = mpt or MptClient()
    production_id = event["production_id"]

    production = supa.production(production_id)
    if production.get("platform_copy"):
        return {"production_id": production_id, "skipped": "copy already present"}

    idea = supa.idea(production["idea_id"])
    platforms = supa.enabled_platforms()
    script = event.get("script") or production.get("script") or ""
    subject = idea.get("title") or "Untitled"

    copy_map = copy_for_platforms(
        mpt, platforms, video_subject=subject, video_script=script, language=""
    )
    missing = [p for p in platforms if p not in copy_map]
    if missing:
        log.warning("no copy generated for %s on production %s", missing, production_id)

    supa.update_production(production_id, platform_copy=copy_map, script=script or None)
    return {"production_id": production_id, "platforms": list(copy_map), "missing": missing}


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def _resolve_integrations(
    postiz: PostizClient, supa: Supa, platforms: list[Platform]
) -> dict[str, PostizIntegration]:
    """Map our platform names onto connected Postiz channels.

    A configured `platform_targets.integration_id` always wins, so an account
    with two TikTok channels connected is never a coin flip.
    """
    available = [i for i in postiz.integrations() if not i.disabled]
    by_id = {i.id: i for i in available}
    resolved: dict[str, PostizIntegration] = {}

    for platform in platforms:
        target = supa.platform_target(platform) or {}
        pinned = target.get("integration_id")
        if pinned and pinned in by_id:
            resolved[platform] = by_id[pinned]
            continue
        for identifier in ACCEPTABLE_IDENTIFIERS.get(platform, ()):
            match = next((i for i in available if i.identifier == identifier), None)
            if match:
                resolved[platform] = match
                if platform == "linkedin" and identifier == "linkedin":
                    log.warning(
                        "using a personal LinkedIn channel for %s; it reports no analytics. "
                        "Connect a LinkedIn Page instead.",
                        platform,
                    )
                break
        else:
            log.error("no connected Postiz channel for %s", platform)
    return resolved


def publish(
    event: dict[str, Any], supa: Supa | None = None, postiz: PostizClient | None = None
) -> dict[str, Any]:
    """Hand the finished cut to Postiz, once per platform.

    Never retry this activity. Postiz starts its publish workflow with
    TERMINATE_EXISTING, so a retry landing after the provider call succeeded but
    before the row is marked published will post to the real platform a second
    time. The deterministic post id dedups the database row, not the platform
    call.
    """
    supa = supa or Supa()
    postiz = postiz or PostizClient()
    production_id = event["production_id"]

    production = supa.production(production_id)
    copy_map = production.get("platform_copy") or {}
    platforms = supa.enabled_platforms()
    if not platforms:
        return _park(supa, production_id, "no platform is enabled in platform_targets")

    supa.update_production(production_id, status=ProductionStatus.PUBLISHING.value, stage="publishing")

    # Postiz pulls the file itself, and requires a public HTTPS URL: our bucket
    # is private, so mint a short-lived signed URL now rather than storing one.
    storage_key = event.get("storage_key") or f"{production_id}/final.mp4"
    signed = supa.signed_render_url(storage_key, expires_in=3600)

    # Upload once and reuse. upload-from-url is not idempotent and re-downloads
    # the entire file into Postiz's memory on every call.
    media_id = production.get("postiz_media_id")
    media_path = production.get("postiz_media_path")
    if media_id and media_path:
        media = PostizMedia(id=media_id, path=media_path)
    else:
        media = postiz.upload_from_url(signed)
        supa.update_production(
            production_id, postiz_media_id=media.id, postiz_media_path=media.path
        )

    integrations = _resolve_integrations(postiz, supa, platforms)
    results: list[dict[str, Any]] = []

    for platform in platforms:
        integration = integrations.get(platform)
        if integration is None:
            supa.upsert_publication(
                production_id, platform, state="skipped", error="no connected channel"
            )
            continue

        piece = copy_map.get(platform) or {}
        content = piece.get("description") or piece.get("caption") or ""
        title = piece.get("title") or (production.get("script") or "")[:90] or "Untitled"
        if not content:
            supa.upsert_publication(
                production_id, platform, state="skipped", error="no copy generated"
            )
            continue

        post_id = deterministic_post_id(production_id, platform)
        try:
            created = postiz.create_post(
                integration_id=integration.id,
                content=content,
                media=media,
                settings_obj=postiz.settings_for(platform, title=title),
                post_id=post_id,
            )
            supa.upsert_publication(
                production_id, platform,
                integration_id=integration.id,
                postiz_post_id=(created[0].post_id if created else post_id),
                state="queued", error=None,
            )
            results.append({"platform": platform, "post_id": post_id})
        except PostizError as exc:
            # One platform failing must not stop the others. YouTube in
            # particular will fail on quota every day until the extension
            # lands, and that must not block Instagram and LinkedIn.
            log.warning("publish to %s failed for %s: %s", platform, production_id, exc)
            supa.upsert_publication(
                production_id, platform, integration_id=integration.id,
                state="error", error=str(exc)[:1000],
            )

    return {"production_id": production_id, "queued": results, "media_id": media.id}


def poll_publish(
    event: dict[str, Any], supa: Supa | None = None, postiz: PostizClient | None = None
) -> dict[str, Any]:
    """Determine what actually happened.

    This is the only failure detector that exists. Postiz's webhooks fire on
    success only, carry no signature, are never retried, and cannot be managed
    through the public API -- and `createPost` swallows a workflow-start
    failure, so a post can sit at QUEUE forever with no error and no
    notification.
    """
    supa = supa or Supa()
    postiz = postiz or PostizClient()
    production_id = event["production_id"]

    production = supa.production(production_id)
    created_at = _parse_ts(production.get("created_at")) or datetime.now(timezone.utc)
    since, until = postiz.poll_window(created_at)

    remote = {p.get("id"): p for p in postiz.list_posts(since, until) if p.get("id")}
    rows = supa.publications(production_id)
    if not rows:
        return {"state": "pending", "reason": "no publication rows yet"}

    terminal, published = 0, 0
    for row in rows:
        if row["state"] in ("published", "error", "skipped"):
            terminal += 1
            published += 1 if row["state"] == "published" else 0
            continue

        post = remote.get(row.get("postiz_post_id"))
        if post is None:
            # The create never landed. Safe to let the state machine try again
            # only because nothing was published.
            age = datetime.now(timezone.utc) - created_at
            if age > timedelta(minutes=STALE_QUEUE_MINUTES):
                supa.upsert_publication(
                    production_id, row["platform"], state="error",
                    error="Postiz never recorded this post",
                )
                terminal += 1
            continue

        state = (post.get("state") or "").upper()
        if state == "PUBLISHED":
            supa.upsert_publication(
                production_id, row["platform"], state="published",
                release_url=post.get("releaseURL"), error=None,
            )
            terminal += 1
            published += 1
        elif state == "ERROR":
            # `GET /posts` does not select the error column, so no reason is
            # available here -- only that it failed.
            supa.upsert_publication(
                production_id, row["platform"], state="error",
                error="Postiz reported ERROR (no detail exposed by the API)",
            )
            terminal += 1
        else:
            age = datetime.now(timezone.utc) - created_at
            if age > timedelta(minutes=STALE_QUEUE_MINUTES):
                supa.upsert_publication(
                    production_id, row["platform"], state="error",
                    error=f"still {state or 'QUEUE'} after {STALE_QUEUE_MINUTES} minutes",
                )
                terminal += 1

    if terminal < len(rows):
        return {"state": "pending", "terminal": terminal, "total": len(rows)}

    if published:
        supa.update_production(
            production_id,
            status=ProductionStatus.PUBLISHED.value,
            stage="published",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"state": "published", "published": published, "total": len(rows)}

    return _park(supa, production_id, "every platform failed to publish")


def _park(supa: Supa, production_id: str, message: str) -> dict[str, Any]:
    supa.park(production_id, message)
    return {"state": "parked", "error": message}


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
