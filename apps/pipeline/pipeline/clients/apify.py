"""Apify: hosted scrapers, rented rather than maintained.

The scout used to drive TikTok itself, through a library that signed each
request inside a real browser. That stopped working, and the reason it stopped
is not fixable from here: TikTok changes its defences continuously and the
library had one author. Apify is the other side of that trade -- somebody else
runs the arms race, we pay per run, and the failure mode becomes a bill rather
than a wedged Playwright session.

Two things about the API shape everything below.

An actor run is a job, not a request. Starting one returns immediately with a
run id; the scrape happens on Apify's machines for anywhere between seconds and
many minutes, and the results land in a dataset you fetch afterwards. There is
a synchronous endpoint that does all three in one call, and this client
deliberately does not use it: it caps at five minutes, gives no way to abort
what it started, and turns a slow hashtag into a dead HTTP connection holding a
run we are still being charged for. Start, poll, fetch -- and abort, which is
the only way an owner pressing Stop actually stops the spending.

And a run can fail in a way that still costs money. `SUCCEEDED` is one of
several terminal states; `FAILED`, `ABORTED` and `TIMED-OUT` are the others,
and all four end the polling loop. Treating anything non-terminal as "not
finished yet" is what keeps a broken actor from being waited on for the whole
run budget.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from pipeline.config import settings

log = logging.getLogger(__name__)

API_BASE = "https://api.apify.com/v2"

# The run states that mean "stop waiting". Apify documents these as the
# terminal set; anything else (READY, RUNNING) means keep polling.
TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMING-OUT"})


class ApifyError(RuntimeError):
    """Anything Apify refused or could not do."""


class ApifyRateLimited(ApifyError):
    """HTTP 429. Safe to retry with backoff: nothing was scraped or billed."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ApifyRefused(ApifyError):
    """Terminal. A bad token, a revoked one, or an actor that does not exist.

    Separate from `ApifyError` because retrying it cannot help and a run should
    say so immediately rather than spending its budget discovering it.
    """


class ApifyRunFailed(ApifyError):
    """The actor ran and ended in a non-successful terminal state."""

    def __init__(self, message: str, status: str = "") -> None:
        super().__init__(message)
        self.status = status


class ApifyClient:
    """Start actor runs, wait for them, read their datasets, abort them."""

    def __init__(self, api_key: str | None = None, timeout: float = 60.0) -> None:
        # `api_key=""` means "explicitly absent" and is honoured without
        # reaching for settings, so a missing token reports itself rather than
        # whatever else happens to be unconfigured.
        key = settings().apify_token if api_key is None else api_key
        if not key:
            raise ApifyError("APIFY_TOKEN is not configured")
        self._headers = {"Authorization": f"Bearer {key}"}
        self._timeout = timeout

    # -- plumbing ----------------------------------------------------------

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.request(method, f"{API_BASE}{path}", headers=self._headers, **kw)

        if resp.status_code == 429:
            after = resp.headers.get("Retry-After")
            raise ApifyRateLimited(
                f"Apify rate limited {method} {path}",
                retry_after=int(after) if (after or "").isdigit() else None,
            )
        if resp.status_code >= 400:
            detail = f"Apify {method} {path} -> {resp.status_code}: {resp.text[:300]}"
            if resp.status_code in (401, 403):
                raise ApifyRefused(
                    f"{detail}. The token goes in Authorization as 'Bearer <token>'; "
                    f"get one from https://console.apify.com/settings/integrations."
                )
            if resp.status_code == 404:
                raise ApifyRefused(
                    f"{detail}. Check the actor id -- in a URL path it is "
                    f"'username~actor-name', not 'username/actor-name'."
                )
            raise ApifyError(detail)

        if not resp.content:
            return {}
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApifyError(f"Apify {method} {path} returned a body that is not JSON") from exc
        # Single resources arrive wrapped in `data`; dataset items arrive as a
        # bare list. Unwrap the former and pass the latter through.
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body

    # -- runs --------------------------------------------------------------

    def start_run(self, actor_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Queue an actor run. Returns the run record, including its id.

        The actor id is path-encoded with a tilde, which is Apify's own
        convention and the single easiest thing to get wrong: `clockworks/…`
        404s where `clockworks~…` works.
        """
        run = self._request(
            "POST", f"/acts/{actor_id.replace('/', '~')}/runs", json=payload
        )
        if not isinstance(run, dict) or not run.get("id"):
            raise ApifyError(f"Apify accepted a run of {actor_id} but named no run id")
        return run

    def run_status(self, run_id: str) -> dict[str, Any]:
        """The run's current state, including its dataset id once there is one."""
        run = self._request("GET", f"/actor-runs/{run_id}")
        return run if isinstance(run, dict) else {}

    def abort_run(self, run_id: str) -> None:
        """Stop a run that is still going.

        Never raises. This is called when an owner has already stopped the
        trend run, or when the budget has expired -- both paths are on their way
        out, and failing to abort costs money but must not also cost the
        diagnostics the run was about to write. Logged loudly for that reason:
        it is the one error here with a bill attached.
        """
        try:
            self._request("POST", f"/actor-runs/{run_id}/abort")
        except Exception as exc:  # noqa: BLE001 - the caller is already leaving
            log.warning("could not abort Apify run %s, it may keep billing: %s", run_id, exc)

    def dataset_items(self, dataset_id: str, limit: int = 0) -> list[dict[str, Any]]:
        """The run's results.

        `limit=0` means "whatever the actor produced". We ask for a cap anyway
        wherever the caller has one, because the actor's own results limit is a
        request rather than a guarantee and a runaway scrape should not become
        a runaway parse.
        """
        params: dict[str, Any] = {"clean": "true", "format": "json"}
        if limit > 0:
            params["limit"] = limit
        items = self._request("GET", f"/datasets/{dataset_id}/items", params=params)
        if not isinstance(items, list):
            raise ApifyError(f"Apify dataset {dataset_id} returned {type(items).__name__}, not a list")
        return [item for item in items if isinstance(item, dict)]
