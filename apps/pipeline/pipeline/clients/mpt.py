"""MoneyPrinterTurbo client.

Every non-obvious behaviour below was verified against the service source, and
each one maps to a distinct exception so the state machine can attach the right
retry policy. Getting that wrong costs real money: a retried submit is a second
full render, meaning a second LLM script, a second TTS pass and a second set of
paid stock or generative calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import MPT_SOCIAL_PLATFORM, SocialMetadata, TaskStatus, VideoParams


class MptError(RuntimeError):
    """Any unexpected MPT failure."""


class MptQueueFull(MptError):
    """HTTP 429 -- `max_concurrent_tasks` plus `max_queued_tasks` are saturated.

    Safe to retry with backoff, and this is the one place declarative retry
    genuinely earns its keep. MPT writes its state row *before* scheduling and
    deletes it again when the queue rejects the task, so a 429 provably leaves
    no orphan render behind.
    """


class MptTaskStateLost(MptError):
    """HTTP 404 on a task we hold an id for.

    Terminal, never transient. Because the state row is written before the work
    is scheduled, a task id we were given can only 404 if the state was lost --
    which happens on restart whenever `enable_redis` is false. Retrying cannot
    help; the render is gone.
    """


class MptClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        cfg = settings()
        self._base = (base_url or cfg.mpt_base_url).rstrip("/")
        self._key = api_key or cfg.mpt_api_key
        # Loud here rather than at import, which is what lets a deployment that
        # never renders -- the trend scout on its own, for instance -- run
        # without inventing MoneyPrinterTurbo credentials it will never use.
        # The failure still cannot be silent: nothing reaches this constructor
        # unless a render is actually about to be submitted.
        if not self._base:
            raise MptError(
                "MPT_BASE_URL is not set, so no render can be submitted. Set it, "
                "or use a style preset whose render_mode is not 'mpt'."
            )
        # MPT reads `x-api-key` with `headers.getlist()` and rejects the request
        # when there is more than one value, because proxies disagree on
        # ordering. Never let a caller add a second one.
        self._headers = {"x-api-key": self._key}
        self._timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v1/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, self._url(path), headers=self._headers, **kw)

        if resp.status_code == 429:
            raise MptQueueFull(resp.text)
        if resp.status_code == 401:
            raise MptError("MPT rejected the API key (check app.api_key and the x-api-key header)")
        if resp.status_code >= 400:
            # MPT remaps Pydantic validation failures to 400, not 422, and its
            # message is prefixed with the request id.
            raise MptError(f"MPT {method} {path} -> {resp.status_code}: {resp.text[:400]}")

        body = resp.json()
        return body.get("data") or {}

    # -- render ------------------------------------------------------------

    def submit_render(self, params: VideoParams) -> str:
        """Start a render and return the task id.

        We always send `task_id`. Upstream ignores it and mints a fresh uuid4,
        so this is only idempotent once our fork's caller-supplied-id change is
        deployed -- until then the caller must still guard against resubmitting.
        Compare the returned id with what we sent to know which is in play.
        """
        payload = params.model_dump(exclude_none=True)
        data = self._request("POST", "videos", json=payload)
        task_id = data.get("task_id")
        if not task_id:
            raise MptError(f"MPT accepted the render but returned no task_id: {data}")
        return str(task_id)

    def get_task(self, task_id: str) -> TaskStatus:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(self._url(f"tasks/{task_id}"), headers=self._headers)
        if resp.status_code == 404:
            raise MptTaskStateLost(task_id)
        if resp.status_code == 429:
            raise MptQueueFull(resp.text)
        if resp.status_code >= 400:
            raise MptError(f"MPT task lookup -> {resp.status_code}: {resp.text[:400]}")
        return TaskStatus.model_validate(resp.json().get("data") or {})

    def delete_task(self, task_id: str) -> None:
        """Free a task's disk.

        Returns 409 while the task is "busy", which means `state == 4` -- so a
        render stranded at state=4 forever is undeletable through the API and
        its files are never reclaimed. Our fork adds a force flag; until then a
        409 here is expected and non-fatal.
        """
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.delete(self._url(f"tasks/{task_id}"), headers=self._headers)
        if resp.status_code == 409:
            raise MptError(f"task {task_id} is busy; needs the force flag from our fork")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise MptError(f"MPT delete -> {resp.status_code}: {resp.text[:200]}")

    # -- artifacts ---------------------------------------------------------

    def resolve_artifact_url(self, ref: str) -> str:
        """Turn a `videos[]` entry into something fetchable.

        MPT rewrites these through its own `app.endpoint`. When that is unset it
        returns a *relative* path like `/tasks/<id>/final-1.mp4`, so we have to
        resolve it ourselves.
        """
        if ref.startswith(("http://", "https://")):
            return ref
        return f"{self._base}/{ref.lstrip('/')}"

    def download_artifact(self, ref: str, dest: Path) -> Path:
        """Stream an artifact to disk.

        Streamed rather than buffered because a 9:16 render can be hundreds of
        megabytes. The `/tasks` static mount is behind the same `x-api-key`
        middleware as the API, which is also why Postiz can never be pointed at
        one of these URLs -- quite apart from it rejecting private addresses.
        """
        url = self.resolve_artifact_url(ref)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=None, follow_redirects=True) as client:
            with client.stream("GET", url, headers=self._headers) as resp:
                if resp.status_code >= 400:
                    raise MptError(f"artifact fetch {url} -> {resp.status_code}")
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 20):
                        fh.write(chunk)
        return dest

    def generate_script(self, subject: str, language: str = "", paragraphs: int = 1) -> str:
        """Write a narration script.

        A synchronous LLM call. Reused by the end-to-end fal path, which has no
        narration text of its own -- fal generates pictures and speech, not a
        script, and standing up a second script generator when this one is
        already deployed and already tuned for short-form would be waste.
        """
        data = self._request(
            "POST",
            "scripts",
            json={
                "video_subject": subject,
                "video_language": language,
                "paragraph_number": paragraphs,
            },
        )
        script = data.get("video_script") or data.get("script") or ""
        if not script:
            raise MptError(f"script generation returned nothing usable: {data}")
        return str(script)

    # -- supplied materials ------------------------------------------------

    def upload_material(self, local: Path) -> str:
        """Upload a clip for MoneyPrinterTurbo to assemble, and return its name.

        This is what makes a second visual backend possible without a second
        assembly path. `video_source="local"` resolves each material through
        `resolve_path_within_directory` against `storage/local_videos` and
        rejects anything outside it, so a remote URL cannot be handed over --
        the bytes have to be uploaded first.
        """
        with local.open("rb") as fh:
            with httpx.Client(timeout=None) as client:
                resp = client.post(
                    self._url("video_materials"),
                    headers=self._headers,
                    files={"file": (local.name, fh, "video/mp4")},
                )
        if resp.status_code >= 400:
            raise MptError(f"material upload -> {resp.status_code}: {resp.text[:300]}")
        data = resp.json().get("data") or {}
        name = data.get("file") or data.get("name") or data.get("path")
        if not name:
            raise MptError(f"material upload returned no filename: {data}")
        # MPT resolves materials relative to its own storage/local_videos, so
        # hand back only the basename.
        return str(name).rsplit("/", 1)[-1]

    # -- copy --------------------------------------------------------------

    def social_metadata(
        self,
        platform: str,
        video_subject: str,
        video_script: str,
        language: str = "",
    ) -> SocialMetadata:
        """Per-platform copy.

        Translates our platform name to MPT's key and refuses anything it has no
        spec for. Upstream resolves an unknown platform to "tiktok" *silently
        with a 200*, so a bare pass-through of "linkedin", "youtube" or
        "instagram" returns TikTok-shaped copy that looks perfectly valid.
        """
        key = MPT_SOCIAL_PLATFORM.get(platform)
        if key is None:
            raise MptError(
                f"MoneyPrinterTurbo has no copy spec for {platform!r}; asking anyway would "
                f"silently return TikTok copy. Supported: {sorted(MPT_SOCIAL_PLATFORM)}"
            )
        data = self._request(
            "POST",
            "social-metadata",
            json={
                "video_subject": video_subject,
                "video_script": video_script,
                "language": language,
                "platform": key,
            },
        )
        return SocialMetadata.model_validate(data)

    def ping(self) -> bool:
        with httpx.Client(timeout=10.0) as client:
            return client.get(f"{self._base}/ping").status_code == 200
