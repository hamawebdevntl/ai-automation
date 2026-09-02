"""Tests for the fal client.

Two of fal's behaviours are the kind that pass review and then fail in
production, so both are pinned here: the authorization scheme is `Key` rather
than `Bearer`, and reaching `COMPLETED` says only that a request left the
queue, not that it succeeded.
"""

from __future__ import annotations

import pytest

from pipeline.clients.fal import (
    FalClient,
    FalRefused,
    FalRequestFailed,
)


@pytest.fixture
def client() -> FalClient:
    return FalClient(api_key="test-key")


class TestAuthScheme:
    def test_the_scheme_is_key_not_bearer(self, client):
        # A generic bearer-token HTTP client 401s against fal, exactly as it
        # does against Postiz. This is the assertion that catches that.
        assert client._headers["Authorization"] == "Key test-key"

    def test_a_missing_key_fails_loudly_at_construction(self):
        with pytest.raises(Exception, match="FAL_API_KEY"):
            FalClient(api_key="")


class TestCompletedIsNotSuccess:
    """`COMPLETED` means "no longer queued", not "worked"."""

    def test_a_clean_completion_raises_nothing(self):
        FalClient.raise_for_error({"status": "COMPLETED"})

    def test_an_error_carried_alongside_completed_raises(self):
        with pytest.raises(FalRequestFailed, match="out of capacity"):
            FalClient.raise_for_error(
                {"status": "COMPLETED", "error": "out of capacity", "error_type": "internal"}
            )

    @pytest.mark.parametrize(
        "error_type,message",
        [
            ("moderation_failure", "prompt rejected"),
            ("content_policy", "not allowed"),
            ("internal", "flagged by safety classifier"),
            ("validation", "nsfw content detected"),
        ],
    )
    def test_a_refusal_is_distinguished_from_a_fault(self, error_type, message):
        # A refusal is information about the content, so it must never be
        # retried or quietly routed to another backend -- it parks.
        with pytest.raises(FalRefused):
            FalClient.raise_for_error(
                {"status": "COMPLETED", "error": message, "error_type": error_type}
            )

    def test_is_terminal_only_for_completed(self):
        assert FalClient.is_terminal({"status": "COMPLETED"})
        assert not FalClient.is_terminal({"status": "IN_QUEUE"})
        assert not FalClient.is_terminal({"status": "IN_PROGRESS"})
        # An unrecognised status is likelier to be a new one than a broken one,
        # so it reads as "still running" rather than as a failure.
        assert not FalClient.is_terminal({"status": "SOMETHING_NEW"})
        assert not FalClient.is_terminal({})


class TestOutputShapes:
    """fal's catalogue does not agree on where the video goes."""

    @pytest.mark.parametrize(
        "result,expected",
        [
            ({"video": {"url": "https://a/1.mp4"}}, ["https://a/1.mp4"]),
            ({"videos": [{"url": "https://a/1.mp4"}, {"url": "https://a/2.mp4"}]},
             ["https://a/1.mp4", "https://a/2.mp4"]),
            ({"output": {"url": "https://a/3.mp4"}}, ["https://a/3.mp4"]),
            ({"url": "https://a/4.mp4"}, ["https://a/4.mp4"]),
            ({}, []),
            ({"video": {}}, []),
        ],
        ids=["video", "videos-list", "output", "bare-url", "empty", "no-url"],
    )
    def test_video_urls_accepts_the_known_variants(self, result, expected):
        assert FalClient.video_urls(result) == expected

    @pytest.mark.parametrize(
        "result,expected",
        [
            ({"audio": {"url": "https://a/a.mp3"}}, "https://a/a.mp3"),
            ({"audio_url": "https://a/b.mp3"}, "https://a/b.mp3"),
            ({}, None),
        ],
    )
    def test_audio_url_accepts_both_shapes(self, result, expected):
        assert FalClient.audio_url(result) == expected
