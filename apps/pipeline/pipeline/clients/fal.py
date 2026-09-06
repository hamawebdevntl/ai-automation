"""fal.ai client.

Sits beside mpt.py and postiz.py and, like them, encodes the things that fail
silently rather than leaving them to be rediscovered.

Two of those matter especially:

  * `Authorization: Key <key>` -- not `Bearer`. A generic bearer-token client
    401s here exactly as it does against Postiz.
  * A queued request reaching `COMPLETED` is *not* proof of success. That
    status carries `error` and `error_type` fields when the run failed, so
    "completed" must be checked for an error before its result is read.

fal supports webhooks via a `fal_webhook` query parameter. We poll instead, to
keep one shape across both render backends and to avoid standing up a second
public endpoint for the sake of latency we do not need.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import settings

log = logging.getLogger(__name__)

QUEUE_BASE = "https://queue.fal.run"

# Refusals we must never retry or route around: they are a statement about the
# content, not a transient fault.
_REFUSAL_MARKERS = ("moderation", "safety", "nsfw", "content_policy", "prohibited")


class FalError(RuntimeError):
    """Any unexpected fal failure."""


class FalRateLimited(FalError):
    """HTTP 429. Safe to retry with backoff: nothing was generated or billed."""


class FalRefused(FalError):
    """The model declined the prompt.

    Terminal. A moderation refusal is information about the request, so
    retrying it or quietly re-rendering on another backend would be the wrong
    response -- the production parks and a person decides.
    """


class FalRequestFailed(FalError):
    """Reached COMPLETED carrying an error, or otherwise failed mid-run."""


class FalClient:
    def __init__(
        self, api_key: str | None = None, timeout: float = 60.0
    ) -> None:
        # `api_key=""` means "explicitly absent" and is honoured without
        # reaching for settings. Falling through to settings() here would raise
        # about whatever else is unconfigured -- Supabase, usually -- instead of
        # the key actually in question.
        key = settings().fal_api_key if api_key is None else api_key
        if not key:
            raise FalError("FAL_API_KEY is not configured")
        # Note the scheme: fal expects the literal word "Key", not "Bearer".
        self._headers = {"Authorization": f"Key {key}"}
        self._timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, url, headers=self._headers, **kw)

        if resp.status_code == 429:
            raise FalRateLimited(resp.text[:300])
        if resp.status_code in (401, 403):
            raise FalError("fal rejected the API key (expects 'Authorization: Key <key>')")
        if resp.status_code >= 400:
            raise FalError(f"fal {method} {url} -> {resp.status_code}: {resp.text[:400]}")
        return resp.json() if resp.content else {}

    # -- submit ------------------------------------------------------------

    def submit(self, model: str, payload: dict[str, Any]) -> dict[str, str]:
        """Enqueue a generation and return the handles fal gives us.

        The returned `status_url` and `response_url` are stored and reused
        rather than reconstructed, because they are fal's to define -- building
        them ourselves would couple us to a URL layout we do not own.
        """
        data = self._request("POST", f"{QUEUE_BASE}/{model.strip('/')}", json=payload)
        request_id = data.get("request_id")
        if not request_id:
            raise FalError(f"fal accepted the request but returned no request_id: {data}")
        fallback = f"{QUEUE_BASE}/{model}/requests/{request_id}"
        return {
            "request_id": str(request_id),
            "status_url": data.get("status_url") or f"{fallback}/status",
            "response_url": data.get("response_url") or fallback,
        }

    # -- poll --------------------------------------------------------------

    def status(self, status_url: str) -> dict[str, Any]:
        """Current queue state.

        Returns the raw body. `IN_QUEUE`, `IN_PROGRESS` and `COMPLETED` are the
        documented values; treat anything else as still running rather than as
        a failure, since an unrecognised state is more likely a new one than a
        broken one.
        """
        return self._request("GET", status_url)

    @staticmethod
    def is_terminal(body: dict[str, Any]) -> bool:
        return str(body.get("status", "")).upper() == "COMPLETED"

    @staticmethod
    def raise_for_error(body: dict[str, Any]) -> None:
        """Turn a failed COMPLETED into the right exception.

        This is the trap: `COMPLETED` means the request is no longer queued,
        not that it succeeded. When it failed, `error` and `error_type` are
        populated and the result body carries no media.
        """
        error = body.get("error")
        if not error:
            return
        kind = str(body.get("error_type") or "").lower()
        blob = f"{kind} {error}".lower()
        if any(marker in blob for marker in _REFUSAL_MARKERS):
            raise FalRefused(f"{body.get('error_type')}: {error}")
        raise FalRequestFailed(f"{body.get('error_type') or 'error'}: {error}")

    def result(self, response_url: str) -> dict[str, Any]:
        body = self._request("GET", response_url)
        self.raise_for_error(body)
        return body

    # -- output ------------------------------------------------------------

    @staticmethod
    def video_urls(result: dict[str, Any]) -> list[str]:
        """Pull video URLs out of a model result.

        Shapes differ across fal's catalogue -- some return `video`, some
        `videos`, some nest under `output` -- so accept the known variants
        rather than binding to one model's schema.

        These URLs are publicly accessible and subject to fal's media
        expiration, so they are fetched promptly and never persisted as the
        production's video_url.
        """
        candidates: list[Any] = []
        for key in ("video", "videos", "output"):
            value = result.get(key)
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(value)
        urls = [c.get("url") for c in candidates if isinstance(c, dict) and c.get("url")]
        if not urls and isinstance(result.get("url"), str):
            urls = [result["url"]]
        return [str(u) for u in urls]

    @staticmethod
    def audio_url(result: dict[str, Any]) -> str | None:
        for key in ("audio", "audio_url"):
            value = result.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict) and value.get("url"):
                return str(value["url"])
        return None

    def download(self, url: str, dest: Path) -> Path:
        """Fetch a generated asset.

        No auth header: fal's media URLs are public. Streamed because a clip
        can be tens of megabytes, and fetched without delay because these URLs
        expire.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with (
            httpx.Client(timeout=None, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise FalError(
                    f"fal asset fetch -> {resp.status_code}. These URLs expire, "
                    f"so a 403 here usually means we polled too slowly."
                )
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        return dest

    # -- bounded wait ------------------------------------------------------

    def wait(
        self,
        handles: dict[str, str],
        budget_s: float = 240.0,
        interval_s: float = 3.0,
    ) -> dict[str, Any]:
        """Poll a request to completion inline.

        Only for short calls -- transcription of half a minute of narration
        takes seconds. Video generation is never waited on this way: the state
        machine polls it, so a long render is a durable row rather than a
        function holding a connection open for twenty minutes.
        """
        import time

        deadline = time.monotonic() + budget_s
        while True:
            body = self.status(handles["status_url"])
            if self.is_terminal(body):
                self.raise_for_error(body)
                return self.result(handles["response_url"])
            if time.monotonic() > deadline:
                raise FalRequestFailed(
                    f"fal request {handles['request_id']} did not finish within {budget_s:.0f}s"
                )
            time.sleep(interval_s)

    # -- transcription -----------------------------------------------------

    def transcribe(self, audio_url: str, model: str | None = None) -> dict[str, Any]:
        """Word-level timings for burned-in captions.

        Needed only on the end-to-end path. MoneyPrinterTurbo's captioner is
        selected by global config rather than per request, so it cannot be
        borrowed for a render it did not produce -- and generating an SRT from
        a script without timings would drift out of sync immediately.
        """
        endpoint = model or settings().fal_transcribe_model
        handles = self.submit(endpoint, {"audio_url": audio_url})
        return handles
