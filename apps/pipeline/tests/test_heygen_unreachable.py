"""A HeyGen outage must be HeyGen's failure, not ours.

The engine reads a bare `httpx.TransportError` as *our* infrastructure blipping
-- the Supabase case -- and retries it every five seconds without consuming an
attempt. A provider that cannot be reached wearing that label spins forever.
These pin the wrap that gives it its own name, and the graph's answer to it.
"""

from __future__ import annotations

import httpx
import pytest

from pipeline.clients import heygen
from pipeline.driver import graph as g


class _RefusingClient:
    """`httpx.Client` whose every request fails before an answer arrives."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def request(self, *a, **k):
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("reset"),
    ],
)
def test_a_transport_failure_is_a_heygen_error(monkeypatch, exc):
    monkeypatch.setattr(heygen.httpx, "Client", _RefusingClient(exc))
    client = heygen.HeyGenClient(api_key="k")

    with pytest.raises(heygen.HeyGenUnreachable) as raised:
        client._request("GET", "/v3/users/me")

    assert isinstance(raised.value, heygen.HeyGenError)
    assert type(exc).__name__ in str(raised.value)
    # The cause is kept, so a log line still shows the underlying httpx error.
    assert raised.value.__cause__ is exc


def test_it_is_not_an_httpx_error_any_more(monkeypatch):
    # The whole point: the engine's `except httpx.TransportError` must not see it.
    monkeypatch.setattr(heygen.httpx, "Client", _RefusingClient(httpx.ConnectError("no route")))
    with pytest.raises(heygen.HeyGenUnreachable) as raised:
        heygen.HeyGenClient(api_key="k")._request("GET", "/x")
    assert not isinstance(raised.value, httpx.TransportError)


def test_submit_render_retries_it_and_poll_render_gives_up_on_it():
    # Retrying a submit is safe because it carries an Idempotency-Key. A poll
    # is covered by ANY_ERROR and parks after its attempts, which is the
    # visible outcome an outage lasting longer than that deserves.
    exc = heygen.HeyGenUnreachable("unreachable")
    assert g.GRAPH["submit_render"].retry[0].matches(exc)
    assert g.GRAPH["poll_render"].retry[0].matches(exc)
    assert g.GRAPH["submit_render"].retry[0].max_attempts == 10
