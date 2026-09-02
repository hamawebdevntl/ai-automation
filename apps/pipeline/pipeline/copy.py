"""Per-platform copy for a finished production.

Four platforms need four different pieces of writing for the same video, and
nothing in the stack generated them before this. MoneyPrinterTurbo's
`social-metadata` endpoint does the generation; this module owns the platform
translation, the length limits, and the shape the review UI expects.
"""

from __future__ import annotations

import logging

from pipeline.clients.mpt import MptClient, MptError
from pipeline.models import MPT_SOCIAL_PLATFORM, Platform, PlatformCopy

log = logging.getLogger(__name__)

# Hard caps enforced by each provider. Exceeding them is rejected at publish
# time, which would strand a production that has already been paid for and
# approved, so trim here instead.
CAPTION_LIMIT: dict[str, int] = {
    "instagram": 2200,
    "tiktok": 2000,
    "youtube": 5000,   # description
    "linkedin": 3000,
}
YOUTUBE_TITLE_MIN = 2
YOUTUBE_TITLE_MAX = 100


def _hashtags(raw: list[str] | str | None) -> list[str]:
    if not raw:
        return []
    items = raw.split() if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for tag in items:
        tag = tag.strip()
        if not tag:
            continue
        out.append(tag if tag.startswith("#") else f"#{tag}")
    return out


def _compose_caption(caption: str, tags: list[str], limit: int) -> str:
    """Fit the caption and as many hashtags as will fit.

    Trimming the caption to make room for hashtags would be the wrong trade, so
    hashtags are dropped from the end instead.
    """
    body = (caption or "").strip()
    if len(body) >= limit:
        return body[:limit].rstrip()
    out = body
    for tag in tags:
        candidate = f"{out} {tag}" if out else tag
        if len(candidate) > limit:
            break
        out = candidate
    return out


def copy_for_platform(
    client: MptClient,
    platform: Platform,
    *,
    video_subject: str,
    video_script: str,
    language: str = "",
) -> PlatformCopy:
    """Generate copy for one platform.

    Raises rather than falling back. A silent fallback is exactly the failure
    mode we removed from MoneyPrinterTurbo: copy that looks valid but is shaped
    for the wrong platform is worse than no copy, because a reviewer will
    approve it.
    """
    if platform not in MPT_SOCIAL_PLATFORM:
        raise MptError(f"no copy generator available for {platform!r}")

    meta = client.social_metadata(
        platform=platform,
        video_subject=video_subject,
        video_script=video_script,
        language=language,
    )
    tags = _hashtags(meta.hashtags)
    limit = CAPTION_LIMIT[platform]
    caption = _compose_caption(meta.caption or video_subject, tags, limit)

    if platform == "youtube":
        # Title and description are separate fields, and only the title is a
        # settings value -- Postiz sends the post content as the description.
        title = (meta.title or video_subject).strip()[:YOUTUBE_TITLE_MAX]
        if len(title) < YOUTUBE_TITLE_MIN:
            title = (video_subject.strip() + " ")[:YOUTUBE_TITLE_MAX].strip() or "Untitled"
        return PlatformCopy(title=title, description=caption)

    return PlatformCopy(caption=caption)


def copy_for_platforms(
    client: MptClient,
    platforms: list[Platform],
    *,
    video_subject: str,
    video_script: str,
    language: str = "",
) -> dict[str, dict[str, str]]:
    """Build `productions.platform_copy`.

    One platform failing does not sink the others: the production can still be
    published where copy succeeded, and the review screen shows what is
    missing. A platform with no copy is simply absent from the map.
    """
    out: dict[str, dict[str, str]] = {}
    for platform in platforms:
        try:
            piece = copy_for_platform(
                client,
                platform,
                video_subject=video_subject,
                video_script=video_script,
                language=language,
            )
        except Exception as exc:  # noqa: BLE001 - one platform must not sink the rest
            log.warning("copy generation failed for %s: %s", platform, exc)
            continue
        out[platform] = {k: v for k, v in piece.model_dump().items() if v}
    return out
