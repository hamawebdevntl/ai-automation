"""Tests for the HeyGen client.

Everything pinned here is something that was verified against the live v3 API
and would otherwise pass review and fail in production:

  * the key goes in `X-Api-Key`, raw -- not `Authorization`, and never `Bearer`;
  * asking for burned-in captions puts the captioned render on a *different*
    URL than `video_url`, so reading the obvious field ships an uncaptioned
    reel and makes our own quality check report captions missing on a video
    that has them;
  * an unrecognised status means "still going", not "broken".
"""

from __future__ import annotations

import pytest

from pipeline.clients.heygen import MAX_SCRIPT_CHARS, HeyGenClient, HeyGenError, HeyGenRejected
from pipeline.models import HeyGenVideo


@pytest.fixture
def client() -> HeyGenClient:
    return HeyGenClient(api_key="test-key")


class TestAuthScheme:
    def test_the_key_is_raw_in_x_api_key(self, client):
        # The trap Postiz sets in `Authorization`, HeyGen sets in its own
        # header: a `Bearer ` prefix 401s, and so does putting the value in
        # `Authorization` instead.
        assert client._headers == {"X-Api-Key": "test-key"}

    def test_a_missing_key_fails_loudly_at_construction(self):
        with pytest.raises(HeyGenError, match="HEYGEN_API_KEY"):
            HeyGenClient(api_key="")


class TestScriptGuards:
    """A presenter video is a script delivered to camera; without one there is
    nothing to render, and HeyGen rejects an over-long one outright."""

    def test_no_script_is_rejected_before_anything_is_billed(self, client):
        with pytest.raises(HeyGenRejected, match="script"):
            client.create_avatar_video(
                avatar_id="a", script="   ", idempotency_key="p1"
            )

    def test_the_limit_is_the_documented_one(self):
        # `POST /v3/videos` caps a script at 5,000 characters and 400s rather
        # than truncating, which is why the client truncates first.
        assert MAX_SCRIPT_CHARS == 5000


class TestCaptionedOutputUrl:
    """The central trap in the response shape."""

    def test_the_captioned_render_is_preferred_over_the_clean_cut(self):
        video = HeyGenVideo(
            id="v1",
            status="completed",
            video_url="https://files.heygen.ai/video/v1.mp4",
            captioned_video_url="https://files.heygen.ai/video/v1_captioned.mp4",
        )
        # Not video_url: that one has no captions on it.
        assert video.output_url() == "https://files.heygen.ai/video/v1_captioned.mp4"

    def test_the_clean_cut_is_used_when_captions_were_not_asked_for(self):
        video = HeyGenVideo(
            id="v1",
            status="completed",
            video_url="https://files.heygen.ai/video/v1.mp4",
            captioned_video_url="https://files.heygen.ai/video/v1_captioned.mp4",
        )
        assert video.output_url(prefer_captioned=False) == "https://files.heygen.ai/video/v1.mp4"

    def test_a_missing_captioned_url_falls_back_rather_than_losing_the_render(self):
        # A video the reviewer can watch beats a discarded one; the quality
        # report is what says the captions are absent.
        video = HeyGenVideo(
            id="v1", status="completed", video_url="https://files.heygen.ai/video/v1.mp4"
        )
        assert video.output_url() == "https://files.heygen.ai/video/v1.mp4"

    def test_only_a_captioned_url_is_still_usable(self):
        video = HeyGenVideo(
            id="v1",
            status="completed",
            captioned_video_url="https://files.heygen.ai/video/v1_captioned.mp4",
        )
        assert video.output_url(prefer_captioned=False) == (
            "https://files.heygen.ai/video/v1_captioned.mp4"
        )

    def test_no_url_at_all_is_none_rather_than_an_empty_string(self):
        assert HeyGenVideo(id="v1", status="completed").output_url() is None


class TestStatusBranching:
    @pytest.mark.parametrize(
        "status,complete,failed,running",
        [
            ("completed", True, False, False),
            ("failed", False, True, False),
            ("pending", False, False, True),
            ("processing", False, False, True),
            # An unrecognised status is likelier to be a new one than a broken
            # one, so it reads as still running rather than as a failure.
            ("something_new", False, False, True),
        ],
    )
    def test_the_three_outcomes(self, status, complete, failed, running):
        video = HeyGenVideo(id="v1", status=status)
        assert video.is_complete is complete
        assert video.is_failed is failed
        assert video.is_running is running

    def test_a_failure_reads_as_one_line_for_the_parked_row(self):
        video = HeyGenVideo(
            id="v1",
            status="failed",
            failure_code="rendering_failed",
            failure_message="Avatar rendering timed out",
        )
        assert video.failure == "rendering_failed: Avatar rendering timed out"

    def test_a_failure_with_no_detail_still_says_something(self):
        assert HeyGenVideo(id="v1", status="failed").failure == "unknown: no detail returned"


class TestResponseTolerance:
    def test_unknown_fields_do_not_break_a_poll(self):
        # The detail response carries plenty we do not drive -- gif_url,
        # video_page_url, timestamps -- and a new field must not fail a render
        # that has already been paid for.
        video = HeyGenVideo.model_validate(
            {
                "id": "v1",
                "status": "completed",
                "video_url": "https://files.heygen.ai/video/v1.mp4",
                "gif_url": "https://files.heygen.ai/gif/v1.gif",
                "video_page_url": "https://app.heygen.com/video/v1",
                "created_at": 1711929600,
                "a_field_that_does_not_exist_yet": True,
            }
        )
        assert video.is_complete
        assert video.duration is None
