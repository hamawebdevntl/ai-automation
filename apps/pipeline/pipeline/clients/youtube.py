"""YouTube Data API v3.

The only source here that is documented, supported, and not a scrape. That
makes it the most predictable of the three and the most constrained, because
what replaces "will this keep working" is a hard daily quota.

The quota is the whole design, and it is uneven in one specific way: searching
is scarce and everything else is nearly free. A project gets a small daily
allowance of `search.list` calls -- on the order of a hundred -- and 10,000
units a day for everything else, where `videos.list`, `channels.list` and
`playlistItems.list` each cost one unit and batch up to fifty ids per call.

So the rule this client is built around is: **search once per term, and never
search again for anything.** That matters because scoring a video against its
channel's own median needs the channel's recent uploads, and the obvious way to
get them is `search.list` with a `channelId` -- which would spend one of the
day's hundred searches per channel and make a per-channel median unaffordable
after about four keywords.

The affordable route is the uploads playlist every channel has:
`channels.list` gives its id for fifty channels in one unit, `playlistItems`
lists it for one more, and the statistics batch fifty at a time. Roughly one
unit per channel, for the real median rather than a substitute. The rejected
alternative was the median of the search results themselves, which costs
nothing and compares a small channel against MrBeast -- exactly the false
positive `outlier_ratio` exists to prevent.

`quotaExceeded` arrives as a 403 with a reason, not a 429, and does not clear
until midnight US/Pacific -- so it is terminal for the run rather than
something to back off from. A 403 can equally mean a burst limit or a rejected
key, and only the reason in the body tells the three apart.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pipeline.config import settings

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"

# The API's own ceiling on ids or results per call. Asking for more is not an
# error; it silently returns 50, which would look like a channel with a short
# history rather than a paging bug.
MAX_IDS_PER_CALL = 50

# 403 carries two unrelated failures and only the reason tells them apart,
# which is why the body is parsed rather than the status trusted.
#
# Getting this wrong is the most expensive mistake available here. Treating an
# exhausted daily quota as a retryable rate limit spends *tomorrow's*
# allocation on retries; treating a burst limit as terminal throws away a run
# that a few seconds of patience would have completed.
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_BURST_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class YouTubeError(RuntimeError):
    """Anything the YouTube Data API refused or could not do."""


class YouTubeQuotaExceeded(YouTubeError):
    """The daily quota is spent.

    Terminal for the run, not a rate limit: the quota resets at midnight
    Pacific and no amount of backing off inside a run will recover it. A scout
    that hits this should stop and report what it already found.
    """


class YouTubeRateLimited(YouTubeError):
    """A burst limit, not the daily quota. Retryable after a short wait."""


class YouTubeRefused(YouTubeError):
    """Terminal. A bad key, a key without the Data API enabled, or a referrer
    restriction that does not permit server-side use."""


class YouTubeClient:
    """Search for videos, read their statistics, read a channel's uploads."""

    def __init__(self, api_key: str | None = None, timeout: float = 60.0) -> None:
        # `api_key=""` means "explicitly absent" and is honoured without
        # reaching for settings, so a missing key reports itself rather than
        # whatever else happens to be unconfigured.
        key = settings().youtube_api_key if api_key is None else api_key
        if not key:
            raise YouTubeError("YOUTUBE_API_KEY is not configured")
        # The key travels in a header rather than as `?key=`, which is Google's
        # own recommendation and matters concretely here: this client puts the
        # request URL into its error messages, and those messages end up in
        # CloudWatch and in `trend_runs.error`. A key in the query string would
        # be a credential written to the database every time a run failed.
        self._headers = {"X-goog-api-key": key}
        self._timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _get(self, resource: str, params: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(f"{API_BASE}/{resource}", params=params, headers=self._headers)

        if resp.status_code >= 400:
            reason, message = self._error(resp)
            detail = f"YouTube {resource} -> {resp.status_code} {reason}: {message}"
            if reason in _QUOTA_REASONS:
                raise YouTubeQuotaExceeded(
                    f"{detail}. The quota resets at midnight US/Pacific -- note that a "
                    f"06:00 UTC schedule and a manual run land on the same Pacific day, "
                    f"so they share one allocation. Scout fewer search terms per run "
                    f"under Settings, or request more quota."
                )
            if reason in _BURST_REASONS or resp.status_code == 429:
                raise YouTubeRateLimited(detail)
            if resp.status_code in (401, 403):
                raise YouTubeRefused(
                    f"{detail}. Check that the key's project has 'YouTube Data API v3' "
                    f"enabled and that the key has no HTTP-referrer restriction."
                )
            raise YouTubeError(detail)

        try:
            body = resp.json()
        except ValueError as exc:
            raise YouTubeError(f"YouTube {resource} returned a body that is not JSON") from exc
        return body if isinstance(body, dict) else {}

    @staticmethod
    def _error(resp: httpx.Response) -> tuple[str, str]:
        """The reason and message out of an error body, defensively.

        The interesting part is nested three deep in `error.errors[0].reason`,
        and it is the only thing separating an exhausted quota from a rejected
        key. A body that does not have that shape must still produce a readable
        error rather than an IndexError.
        """
        try:
            error = (resp.json() or {}).get("error") or {}
        except ValueError:
            return "", resp.text[:300]
        errors = error.get("errors") or []
        reason = ""
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
        return reason, str(error.get("message") or "")[:300]

    # -- calls -------------------------------------------------------------

    def search_videos(
        self,
        query: str,
        *,
        limit: int,
        published_after: str = "",
        order: str = "viewCount",
    ) -> list[str]:
        """Video ids matching a query. Spends one of the day's few searches.

        Returns ids only. The snippet `search.list` hands back has a title and a
        channel but no statistics at all, so it cannot be scored -- which is why
        every id here goes on to `videos()` regardless.

        Ordered by view count rather than relevance by default: this is looking
        for what performed, and relevance ordering buries a breakout video
        behind older ones that match the words better.
        """
        params: dict[str, Any] = {
            "part": "id",
            "q": query,
            "type": "video",
            "order": order,
            "maxResults": max(1, min(limit, MAX_IDS_PER_CALL)),
        }
        if published_after:
            params["publishedAfter"] = published_after
        body = self._get("search", params)
        return [
            str(item["id"]["videoId"])
            for item in body.get("items") or []
            if isinstance(item, dict)
            and isinstance(item.get("id"), dict)
            and item["id"].get("videoId")
        ]

    def videos(self, video_ids: list[str]) -> list[dict[str, Any]]:
        """Full records for up to 50 ids. Costs 1 unit per call.

        Batched by the caller's list length rather than internally paged,
        because the batch size is also the quota unit and a caller that hands
        this 200 ids should see four calls, not one silent truncation.
        """
        if not video_ids:
            return []
        out: list[dict[str, Any]] = []
        for start in range(0, len(video_ids), MAX_IDS_PER_CALL):
            chunk = video_ids[start : start + MAX_IDS_PER_CALL]
            body = self._get(
                "videos",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            )
            out.extend(item for item in body.get("items") or [] if isinstance(item, dict))
        return out

    def uploads_playlists(self, channel_ids: list[str]) -> dict[str, str]:
        """Each channel's uploads playlist id. Costs 1 unit per 50 channels.

        Asked for rather than derived. There is a widely-repeated trick that
        turns a `UC…` channel id into its uploads playlist by swapping the
        prefix for `UU`, and it does currently work -- but it is not documented
        anywhere Google commits to, and the thing it saves is one unit per fifty
        channels out of a 10,000-unit budget. Reading `relatedPlaylists.uploads`
        is what the API actually promises, and it is batched, so the saving is
        not worth depending on an undocumented string format.
        """
        out: dict[str, str] = {}
        for start in range(0, len(channel_ids), MAX_IDS_PER_CALL):
            chunk = channel_ids[start : start + MAX_IDS_PER_CALL]
            body = self._get(
                "channels", {"part": "contentDetails", "id": ",".join(chunk)}
            )
            for item in body.get("items") or []:
                if not isinstance(item, dict):
                    continue
                related = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
                uploads = related.get("uploads")
                if item.get("id") and uploads:
                    out[str(item["id"])] = str(uploads)
        return out

    def playlist_video_ids(self, playlist_id: str, limit: int) -> list[str]:
        """A playlist's most recent entries, newest first. Costs 1 unit.

        This is the cheap half of the baseline: the uploads playlist gives a
        channel's recent videos for one unit, where `search.list` restricted to
        the channel would spend one of the day's hundred searches per channel
        and make a per-channel median unaffordable.

        A playlist that cannot be read -- a channel with uploads hidden, a
        terminated account -- yields nothing rather than raising. No baseline is
        a recorded outcome upstream, not an error. A quota failure is re-raised,
        because that one is not about this channel.
        """
        try:
            body = self._get(
                "playlistItems",
                {
                    "part": "contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": max(1, min(limit, MAX_IDS_PER_CALL)),
                },
            )
        except (YouTubeQuotaExceeded, YouTubeRateLimited):
            raise
        except YouTubeError as exc:
            log.debug("could not read playlist %s: %s", playlist_id, exc)
            return []
        return [
            str(item["contentDetails"]["videoId"])
            for item in body.get("items") or []
            if isinstance(item, dict)
            and isinstance(item.get("contentDetails"), dict)
            and item["contentDetails"].get("videoId")
        ]
