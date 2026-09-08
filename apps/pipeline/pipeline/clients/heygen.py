"""HeyGen client -- the presenter lane.

Sits beside mpt.py, fal.py and postiz.py and, like them, encodes the things
that fail silently rather than leaving them to be rediscovered. Everything
here was checked against the live v3 API rather than taken from documentation.

Four points matter more than the rest:

  * **v3, not v2.** The v1/v2 endpoints still answer, but they return a
    deprecation warning and are removed on 2026-10-31. `POST /v2/video/generate`
    is the call most examples on the internet still show; it is the wrong one.
  * **`X-Api-Key`, with the raw key.** Not `Authorization`, and no `Bearer`
    prefix -- the same trap as Postiz, in a different header.
  * **A burned-in caption track lands on a *different* URL.** When `caption.style`
    is set, `video_url` is the clean cut and `captioned_video_url` is the one
    with captions. Reading `video_url` would ship an uncaptioned video *and*
    have our own quality check report missing captions on a render that was
    made with them.
  * **`POST /v3/videos` takes an `Idempotency-Key`.** Passing the production id
    makes a resubmit replay the original response for 24 hours instead of
    billing a second render. This is why an ambiguous failure on this lane is
    safe to retry, where the same failure on fal is not.

HeyGen supports webhooks via `callback_url`. We poll instead, to keep one shape
across all three render backends and to avoid standing up a second public
endpoint for latency we do not need.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import HeyGenVideo

log = logging.getLogger(__name__)

API_BASE = "https://api.heygen.com"

# `POST /v3/videos` rejects a longer script outright rather than truncating it.
MAX_SCRIPT_CHARS = 5000

# `GET /v3/avatars/looks` caps `limit` at 50 and 400s with `invalid_parameter`
# above it rather than clamping, so the clamp happens here.
MAX_LOOKS_PAGE = 50

# Terminal, and about our configuration rather than a transient fault: retrying
# these spends nothing but time, so they park instead.
_CONFIG_ERROR_CODES = (
    "avatar_not_found",
    "voice_not_found",
    "invalid_parameter",
    "unauthorized",
    "forbidden",
)


class HeyGenError(RuntimeError):
    """Any unexpected HeyGen failure."""


class HeyGenRateLimited(HeyGenError):
    """HTTP 429.

    Two different limits arrive this way: requests per minute, and concurrent
    render jobs -- ten on Pay-As-You-Go, counting every avatar render, Video
    Agent session and translation in flight. At ten reels a day we are nowhere
    near the second one, but a backlog flush could be, so `retry_after` is
    carried through rather than discarded.

    Safe to retry: nothing was generated or billed.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HeyGenInProgress(HeyGenError):
    """HTTP 409 `request_in_progress`.

    A submit carrying an Idempotency-Key that is still in flight. The original
    request is doing exactly what we wanted, but its video id is not knowable
    from here, so the only correct response is to back off and submit again --
    the replay then returns the id.
    """


class HeyGenUnreachable(HeyGenError):
    """The request never got an answer: DNS, connection refused, reset, timeout.

    Raised as a HeyGen error on purpose rather than letting `httpx`'s own
    exception escape. The engine treats a bare `httpx.TransportError` as *our*
    plumbing failing -- a Supabase blip -- and retries it every five seconds
    without ever consuming an attempt. A provider outage wearing that label
    spins forever and logs on every tick. Wearing this one, the graph decides:
    `submit_render` retries it (the idempotency key makes a resubmit safe),
    `poll_render` retries it a few times, and then it parks, visibly.
    """


class HeyGenRejected(HeyGenError):
    """A terminal configuration error: unknown avatar, unknown voice, bad field.

    Never retried. A preset naming an avatar this account cannot use will fail
    identically every time, so it parks for a human instead of burning the
    state machine's attempts.
    """


class HeyGenClient:
    def __init__(self, api_key: str | None = None, timeout: float = 60.0) -> None:
        # `api_key=""` means "explicitly absent" and is honoured without
        # reaching for settings, so a missing key reports itself rather than
        # whatever else happens to be unconfigured.
        key = settings().heygen_api_key if api_key is None else api_key
        if not key:
            raise HeyGenError("HEYGEN_API_KEY is not configured")
        # The raw key in X-Api-Key. A `Bearer` prefix, or the same value in
        # `Authorization`, 401s.
        self._headers = {"X-Api-Key": key}
        self._timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _request(
        self, method: str, path: str, *, idempotency_key: str | None = None, **kw: Any
    ) -> dict[str, Any]:
        """`data` as a mapping -- the shape every endpoint the pipeline drives returns."""
        data = self._request_data(method, path, idempotency_key=idempotency_key, **kw)
        return data if isinstance(data, dict) else {}

    def _request_data(
        self, method: str, path: str, *, idempotency_key: str | None = None, **kw: Any
    ) -> Any:
        """`data` exactly as it arrived.

        Split out from `_request` because not every v3 endpoint wraps a mapping:
        `GET /v3/avatars/looks` puts a *list* there, and coercing that to `{}`
        reports an account full of avatars as having none.
        """
        headers = dict(self._headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        with httpx.Client(timeout=self._timeout) as client:
            try:
                resp = client.request(method, f"{API_BASE}{path}", headers=headers, **kw)
            except httpx.TransportError as exc:
                raise HeyGenUnreachable(
                    f"HeyGen {method} {path} unreachable: {type(exc).__name__}: {exc}"
                ) from exc

        if resp.status_code == 429:
            after = resp.headers.get("Retry-After")
            raise HeyGenRateLimited(
                resp.text[:300], retry_after=int(after) if (after or "").isdigit() else None
            )
        if resp.status_code == 409:
            raise HeyGenInProgress(resp.text[:300])
        if resp.status_code >= 400:
            code, message = self._error(resp)
            detail = f"HeyGen {method} {path} -> {resp.status_code} {code}: {message}"
            if resp.status_code in (401, 403):
                raise HeyGenRejected(
                    f"{detail}. The key must be the raw value in X-Api-Key, with no "
                    f"'Bearer ' prefix and not in Authorization."
                )
            if code in _CONFIG_ERROR_CODES:
                raise HeyGenRejected(detail)
            raise HeyGenError(detail)

        body = resp.json() if resp.content else {}
        # Every v3 response wraps its payload in `data`. An `error` key that is
        # populated alongside a 200 is treated as a failure rather than trusted,
        # because the legacy endpoints do exactly that.
        error = body.get("error") if isinstance(body, dict) else None
        if error:
            # Defensive about the shape as well as the presence: a 200 body is
            # not a contract we control, and `error` arriving as a bare string
            # must not turn into an AttributeError three frames away.
            if isinstance(error, dict):
                detail = f"{error.get('code', '')}: {error.get('message', '')}"
            else:
                detail = str(error)[:300]
            raise HeyGenError(f"HeyGen {method} {path} returned 200 carrying {detail}")
        return body.get("data") if isinstance(body, dict) else None

    @staticmethod
    def _error(resp: httpx.Response) -> tuple[str, str]:
        """Pull `code` and `message` out of a v3 error body.

        The shape is `{"error": {"code", "message", "doc_url"}}`. It is parsed
        rather than logged raw because `code` is what decides retry from park.
        """
        try:
            error = (resp.json() or {}).get("error") or {}
        except Exception:  # noqa: BLE001 - a proxy can return HTML
            return "", resp.text[:300]
        return str(error.get("code") or ""), str(error.get("message") or "")

    # -- preflight ---------------------------------------------------------

    def account(self) -> dict[str, Any]:
        """Verify the key and read the balance.

        `GET /v3/users/me` is the documented way to check a key, and the wallet
        balance it returns is the only advance warning of the failure mode that
        matters operationally: an approved presenter reel that cannot render
        because the account is empty.
        """
        return self._request("GET", "/v3/users/me")

    def looks(
        self, *, ownership: str = "private", limit: int = MAX_LOOKS_PAGE
    ) -> list[dict[str, Any]]:
        """The avatar looks this account can actually use.

        The check DEPLOY.md section 5a asks an operator to curl before the
        first presenter approval: a preset naming a look this account does not
        own fails with `avatar_not_found`, which is terminal, so the production
        parks after Gate 1 has already been passed.

        `limit` is clamped rather than passed through: the endpoint 400s with
        `invalid_parameter` above 50 instead of reducing it, and a caller
        asking for 100 wants "all of them", not an error.
        """
        data = self._request_data(
            "GET",
            "/v3/avatars/looks",
            params={"limit": min(max(int(limit), 1), MAX_LOOKS_PAGE), "ownership": ownership},
        )
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def voice(self, voice_id: str) -> dict[str, Any]:
        """One voice by id.

        `GET /v3/voices/{id}` resolves a voice the paginated listing does not
        contain: a cloned or private voice is absent from `GET /v3/voices`
        (3,089 entries over 62 pages on this account, none of them the one the
        preset names) but answers here directly. So this is the only cheap way
        to tell "the preset names a voice this account cannot use" -- which
        fails terminally as `voice_not_found` -- from a working configuration.

        The `preview_audio_url` it returns is HeyGen's own reference recording
        of the voice, which is what makes it possible to check that a finished
        render actually used it.
        """
        return self._request("GET", f"/v3/voices/{voice_id}")

    # -- submit ------------------------------------------------------------

    def create_avatar_video(
        self,
        *,
        avatar_id: str,
        script: str,
        idempotency_key: str,
        voice_id: str | None = None,
        aspect_ratio: str = "9:16",
        resolution: str = "1080p",
        burn_captions: bool = True,
        fit: str | None = None,
        title: str | None = None,
        voice_settings: dict[str, Any] | None = None,
        engine: str | None = None,
        background: dict[str, Any] | None = None,
    ) -> HeyGenVideo:
        """Start one avatar render.

        `idempotency_key` is not optional here even though the API treats it as
        such: it is the whole reason this lane can be retried. HeyGen replays
        the original response for 24 hours, so a duplicate submit returns the
        first video id instead of billing a second render. Keys must match
        `[A-Za-z0-9_:.-]{1,255}`, which a production uuid satisfies.

        Note the 24-hour window. It is far longer than any retry the state
        machine will make, but it is not forever: re-running a production a day
        later is a new render and a new charge, which is correct but worth
        knowing before anyone reruns a backlog.
        """
        text = (script or "").strip()
        if not text:
            raise HeyGenRejected("HeyGen needs a script to speak; none was generated")
        if len(text) > MAX_SCRIPT_CHARS:
            # Truncated at a sentence boundary rather than mid-word, because
            # the alternative is a 400 that throws away the whole production.
            log.warning(
                "script is %d chars, over HeyGen's %d limit; truncating",
                len(text),
                MAX_SCRIPT_CHARS,
            )
            text = text[:MAX_SCRIPT_CHARS].rsplit(".", 1)[0] + "."

        payload: dict[str, Any] = {
            "type": "avatar",
            "avatar_id": avatar_id,
            "script": text,
            "aspect_ratio": aspect_ratio,
            "resolution": resolution,
        }
        # Sent explicitly whenever the preset names one. The API will fall back
        # to the avatar's own default voice, but relying on that makes the
        # narrator a property of HeyGen's catalogue rather than of our preset.
        if voice_id:
            payload["voice_id"] = voice_id
        if fit:
            payload["fit"] = fit
        if title:
            payload["title"] = title[:120]
        if voice_settings:
            payload["voice_settings"] = voice_settings
        if background:
            payload["background"] = background
        # Omitted, the server picks Avatar IV. That matters: studio avatars in
        # the public catalogue advertise `avatar_iii` only, so a preset naming
        # one must set this or the render fails on an engine it never asked for.
        if engine:
            payload["engine"] = engine
        if burn_captions:
            # `style` is what burns them in. Without it a sidecar SRT is still
            # produced and the rendered video carries no captions at all.
            payload["caption"] = {"file_format": "srt", "style": "default"}

        data = self._request("POST", "/v3/videos", idempotency_key=idempotency_key, json=payload)
        video_id = data.get("video_id")
        if not video_id:
            raise HeyGenError(f"HeyGen accepted the request but returned no video_id: {data}")
        return HeyGenVideo(id=str(video_id), status=str(data.get("status") or "pending"))

    # -- poll --------------------------------------------------------------

    def video(self, video_id: str) -> HeyGenVideo:
        """Current state of one render.

        `status` is one of waiting / pending / processing / completed / failed.
        Anything else reads as still running: an unrecognised status is likelier
        to be a new one than a broken one -- which is not hypothetical, since
        `waiting` is what a real submit returns and it appears in none of the
        documented lists.
        """
        data = self._request("GET", f"/v3/videos/{video_id}")
        return HeyGenVideo.model_validate({**data, "id": data.get("id") or video_id})

    # -- output ------------------------------------------------------------

    def download(self, url: str, dest: Path) -> Path:
        """Fetch a finished render.

        No auth header: these are presigned URLs. They also expire, which is
        why the pipeline downloads as soon as a render completes rather than
        storing the URL and coming back to it.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with (
            httpx.Client(timeout=None, follow_redirects=True) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code >= 400:
                raise HeyGenError(
                    f"HeyGen asset fetch -> {resp.status_code}. These URLs are presigned "
                    f"and expire, so a 403 here usually means we polled too slowly."
                )
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    fh.write(chunk)
        return dest
