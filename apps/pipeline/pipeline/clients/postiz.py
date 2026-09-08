"""Postiz client.

Postiz is deployed as-is and only ever called over its public REST API, which is
what keeps its AGPL licence at arm's length. Everything here was verified
against its source, because several of its requirements are surprising enough
to fail silently.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx

from pipeline.config import settings
from pipeline.models import Platform, PostizCreatedPost, PostizIntegration, PostizMedia


class PostizError(RuntimeError):
    pass


class PostizThrottled(PostizError):
    """HTTP 429. Only `POST /posts` is throttled: 90 creations/hour/org by default.

    GET polling is explicitly exempt, so the reconciler can poll freely. The
    only realistic way to burn this limit is a retry storm -- which is another
    reason publish is never retried automatically.
    """


def deterministic_post_id(production_id: str, platform: str) -> str:
    """A stable id for one production's post on one platform.

    Postiz upserts on the caller-supplied `posts[].value[].id`, so reusing this
    id makes a duplicate create collapse onto the same row instead of producing
    a second post. Note this protects the *row*, not the platform call: see
    `create_post`.

    `group` cannot serve this purpose -- the public controller creates posts
    with `keepGroup=false`, so the group is rotated to a fresh uuid every call.
    """
    digest = hashlib.sha256(f"{production_id}:{platform}".encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


class PostizClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        cfg = settings()
        self._base = (base_url or cfg.postiz_base_url).rstrip("/")
        # Nothing should construct this client when publishing is switched off,
        # so say so here rather than letting an empty base URL turn into a
        # connection error against "http:///public/v1/posts".
        if not self._base:
            raise PostizError(
                "POSTIZ_BASE_URL is empty: no publishing service is deployed "
                "(PUBLISHING_ENABLED=false). Publishing and analytics are "
                "switched off, and nothing should be reaching for this client."
            )
        # The raw key, with no scheme. Postiz compares the Authorization header
        # verbatim against organization.apiKey, so prefixing "Bearer " -- which
        # any generic bearer-token HTTP client will do -- silently 401s.
        # (A `pos_`-prefixed value is routed to OAuth lookup instead.)
        self._headers = {"Authorization": api_key or cfg.postiz_api_key}
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self._base}/public/v1/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, self._url(path), headers=self._headers, **kw)
        if resp.status_code == 429:
            raise PostizThrottled(resp.text[:200])
        if resp.status_code == 401:
            raise PostizError(
                "Postiz rejected the API key. It must be the raw key in Authorization "
                "with no 'Bearer ' prefix."
            )
        if resp.status_code >= 400:
            raise PostizError(f"Postiz {method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    # -- channels ----------------------------------------------------------

    def integrations(self) -> list[PostizIntegration]:
        """Connected channels.

        `identifier` is the provider key and decides which settings shape
        applies. Filter out `disabled` ones; note the list also includes
        non-social integrations, for which analytics always returns [].
        """
        raw = self._request("GET", "integrations") or []
        return [PostizIntegration.model_validate(i) for i in raw]

    def integration_settings(self, integration_id: str) -> dict[str, Any]:
        """Self-describing per-channel constraints.

        Returns `{output: {rules, maxLength, settings, tools}}` where `settings`
        is the JSON schema of that provider's settings DTO. Worth asserting
        against at startup so a Postiz upgrade cannot silently invalidate the
        settings we build below.
        """
        return self._request("GET", f"integration-settings/{integration_id}") or {}

    # -- media -------------------------------------------------------------

    def upload_from_url(self, url: str) -> PostizMedia:
        """Have Postiz pull the video itself.

        The URL must be public HTTPS. Postiz fetches through an SSRF-safe
        dispatcher that rejects private and internal addresses, so it can never
        reach MoneyPrinterTurbo directly -- the render has to be in object
        storage on a signed URL first. Postiz also buffers the whole file into
        memory and sniffs the real MIME type, and `video/mp4` is the only video
        type it accepts.
        """
        raw = self._request("POST", "upload-from-url", json={"url": url})
        return PostizMedia.model_validate(raw)

    # -- per-platform settings --------------------------------------------
    #
    # `settings.__type` is never sent. Postiz overwrites it server-side from
    # the channel's providerIdentifier, and sending our own is at best ignored.

    @staticmethod
    def instagram_settings() -> dict[str, Any]:
        # `post_type` is the only required field. 'post' becomes media_type
        # REELS for a video; 'story' becomes STORIES.
        return {"post_type": "post"}

    @staticmethod
    def tiktok_settings(title: str, made_with_ai: bool = True) -> dict[str, Any]:
        # Every field here except video_made_with_ai is required by the DTO.
        #
        # content_posting_method is the one that matters most: DIRECT_POST
        # actually publishes, while UPLOAD only drops the media into the
        # account's TikTok inbox for someone to finish by hand within 24 hours,
        # and silently discards every other setting.
        #
        # video_made_with_ai maps to TikTok's `is_aigc` disclosure flag, and is
        # applied only for video posts under DIRECT_POST.
        return {
            "title": title[:90],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "duet": False,
            "stitch": False,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": made_with_ai,
            "content_posting_method": "DIRECT_POST",
        }

    @staticmethod
    def youtube_settings(
        title: str,
        privacy: Literal["public", "private", "unlisted"] = "public",
        tags: list[str] | None = None,
        made_for_kids: bool = False,
    ) -> dict[str, Any]:
        # The video *description* is not a settings field -- Postiz sends the
        # post content as the description. Only the title lives here, and it
        # must be 2-100 characters. There is no category field at all.
        clean = title.strip()[:100]
        if len(clean) < 2:
            raise PostizError("YouTube requires a title of at least 2 characters")
        out: dict[str, Any] = {
            "title": clean,
            "type": privacy,
            "selfDeclaredMadeForKids": "yes" if made_for_kids else "no",
        }
        if tags:
            # Combined tag length is capped at 500 characters upstream.
            out["tags"] = [{"value": t, "label": t} for t in tags]
        return out

    @staticmethod
    def linkedin_settings() -> dict[str, Any]:
        # Both LinkedIn settings fields are optional, so an empty object is
        # valid. Connect a LinkedIn *Page*: the personal provider reports no
        # analytics whatsoever.
        return {}

    def settings_for(
        self,
        platform: Platform,
        *,
        title: str,
        tags: list[str] | None = None,
        made_with_ai: bool = True,
    ) -> dict[str, Any]:
        """Per-platform settings for one post.

        `made_with_ai` defaults to true because it is true of everything this
        pipeline renders, and a disclosure that defaults to "no" is the wrong
        way round: the failure mode of over-disclosing is a label nobody minds,
        and the failure mode of under-disclosing is a platform sanction. It is
        a parameter at all because an uploaded cut may genuinely not be
        AI-generated, and only its uploader knows.
        """
        if platform == "instagram":
            return self.instagram_settings()
        if platform == "tiktok":
            return self.tiktok_settings(title, made_with_ai=made_with_ai)
        if platform == "youtube":
            return self.youtube_settings(title, tags=tags)
        if platform == "linkedin":
            return self.linkedin_settings()
        raise PostizError(f"no settings builder for platform {platform!r}")

    # -- posting -----------------------------------------------------------

    def create_post(
        self,
        *,
        integration_id: str,
        content: str,
        media: PostizMedia,
        settings_obj: dict[str, Any],
        post_id: str,
        publish_now: bool = True,
        when: datetime | None = None,
    ) -> list[PostizCreatedPost]:
        """Create one post on one channel.

        NEVER retry this call. Postiz starts its publish workflow with
        `workflowIdConflictPolicy: 'TERMINATE_EXISTING'`, so a retry that lands
        after the provider call succeeded but before the row is marked
        PUBLISHED will re-queue and publish to the real platform a second time.
        The deterministic `post_id` dedups the database row, not the platform
        call. On an unknown outcome, poll -- never resubmit.

        A 200 here means *queued*, never published: `createPost` fires its
        workflow with a swallowed `.catch()`, so an unreachable Temporal leaves
        the row at QUEUE forever with no error and no webhook.
        """
        # `date` is required even for immediate publishing, and is ignored in
        # that case. `shortLink` must be present. `tags` must be an array.
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        body = {
            "type": "now" if publish_now else "schedule",
            "shortLink": False,
            "date": stamp,
            "tags": [],
            "posts": [
                {
                    "integration": {"id": integration_id},
                    "value": [
                        {
                            "id": post_id,
                            "content": content,
                            "image": [{"id": media.id, "path": media.path}],
                        }
                    ],
                    "settings": settings_obj,
                }
            ],
        }
        raw = self._request("POST", "posts", json=body) or []
        return [PostizCreatedPost.model_validate(p) for p in raw]

    def list_posts(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """The only reliable way to learn a post's real outcome.

        Postiz's webhooks fire on success only, carry no signature, are never
        retried and cannot be managed through the public API -- so failure is
        detectable by polling and nothing else.

        Both bounds are mandatory; this is a calendar-window query, not a lookup
        by id, so use a generous window and match on our deterministic ids.
        Be aware the response does *not* include the `error` column, so an
        ERROR state arrives without a reason.
        """
        raw = self._request(
            "GET",
            "posts",
            params={"startDate": since.isoformat(), "endDate": until.isoformat()},
        )
        if isinstance(raw, dict):
            return raw.get("posts") or []
        return raw or []

    def analytics(self, integration_id: str, days: int = 7) -> list[dict[str, Any]]:
        """Channel analytics. `date` is a number of days back, not a timestamp.

        Returns `[{label, data: [{total, date}], percentageChange}]` where the
        totals are strings and `percentageChange` is often a hardcoded
        placeholder -- store the label/value pairs and ignore the rest.
        """
        return self._request("GET", f"analytics/{integration_id}", params={"date": days}) or []

    def post_analytics(self, post_id: str, days: int = 7) -> list[dict[str, Any]]:
        """Per-post analytics.

        Returns [] while the post has no releaseId (not yet published) and
        `{missing: true}` when the release id is literally 'missing'. TikTok
        needs a second lookup to resolve its real video id, so a fresh TikTok
        post can legitimately return [] for a while.
        """
        raw = self._request("GET", f"analytics/post/{post_id}", params={"date": days})
        return raw if isinstance(raw, list) else []

    def find_slot(self, integration_id: str) -> str | None:
        raw = self._request("GET", f"find-slot/{integration_id}") or {}
        return raw.get("date")

    @staticmethod
    def poll_window(created_at: datetime) -> tuple[datetime, datetime]:
        """A window wide enough to catch our post.

        `publishDate` for an immediate post is floored to the minute, so pad
        both ends rather than matching exactly.
        """
        return created_at - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(hours=1)
