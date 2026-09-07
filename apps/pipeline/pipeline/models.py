"""Domain models mirroring the Supabase schema, plus MoneyPrinterTurbo's wire types.

The database is the contract between this package and the web app. The web app
keeps its own hand-written TypeScript in `apps/web/src/lib/database.types.ts`;
these are the Python half. Where a JSON blob is shared with the UI the shape is
fixed by what the UI already parses, and that is called out on the model.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------

Platform = Literal["instagram", "tiktok", "youtube", "linkedin"]
PLATFORMS: tuple[Platform, ...] = ("instagram", "tiktok", "youtube", "linkedin")

# MoneyPrinterTurbo's own platform keys, which are NOT our platform names.
#
# `app/services/llm.py` defines SOCIAL_PLATFORMS as
# {tiktok, youtube_shorts, instagram_reels, facebook_reels} and resolves any
# unknown value to DEFAULT_SOCIAL_PLATFORM = "tiktok" *silently, with a 200*.
# So passing "youtube" or "instagram" -- let alone "linkedin" -- returns
# TikTok-shaped copy and no warning. Always translate, never pass through.
MPT_SOCIAL_PLATFORM: dict[str, str] = {
    "tiktok": "tiktok",
    "youtube": "youtube_shorts",
    "instagram": "instagram_reels",
    # LinkedIn works only because our fork added a spec for it. Upstream has
    # none (`grep -cin linkedin app/services/llm.py` -> 0 before the change)
    # and silently returned TikTok copy instead. If this package is ever
    # pointed at an unforked MoneyPrinterTurbo, this entry must come out --
    # the fork also makes an unknown platform raise, so a mismatch fails loudly
    # rather than producing plausible-looking wrong copy.
    "linkedin": "linkedin",
}


class ProductionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    # Stopped at the script gate. The same kind of state as AWAITING_REVIEW --
    # deliberately halted, waiting for a person -- and deliberately not PARKED,
    # which means the machine stopped because something is wrong. Nothing is
    # wrong: the pipeline is doing exactly what it should, which is refusing to
    # spend money on words nobody has read.
    AWAITING_SCRIPT = "awaiting_script"
    QC_FAILED = "qc_failed"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    PARKED = "parked"
    # Set only by `cancel_production`. Distinct from PARKED, which means the
    # machine stopped and wants a person, and from REJECTED, which means a
    # person said no to a finished cut: this one means a person stopped the
    # production before it got that far.
    CANCELLED = "cancelled"


# Statuses a production can hold while it is still the pipeline's problem.
#
# Lives here rather than in `reconcile.py` because both the sweepers and the
# data layer need it, and `reconcile` imports `supa` -- putting it there and
# importing it back would be a cycle.
LIVE_STATUSES: tuple[str, ...] = (
    ProductionStatus.QUEUED.value,
    ProductionStatus.RUNNING.value,
    ProductionStatus.AWAITING_SCRIPT.value,
    ProductionStatus.AWAITING_REVIEW.value,
    ProductionStatus.QC_FAILED.value,
    ProductionStatus.PUBLISHING.value,
)


class VelocityLabel(str, Enum):
    BREAKOUT = "breakout"
    RISING = "rising"
    STEADY = "steady"
    DECLINING = "declining"


def velocity_label(ratio: float) -> VelocityLabel:
    """Thresholds are fixed by a comment on `ideas.velocity_ratio`."""
    if ratio >= 3.0:
        return VelocityLabel.BREAKOUT
    if ratio >= 1.5:
        return VelocityLabel.RISING
    if ratio >= 0.8:
        return VelocityLabel.STEADY
    return VelocityLabel.DECLINING


# ---------------------------------------------------------------------------
# Shapes the web UI already parses. Changing these breaks the review screen.
# ---------------------------------------------------------------------------


class QcCheck(BaseModel):
    """One row in `QcReportCard`."""

    key: str
    label: str
    status: Literal["pass", "warn", "fail"]
    detail: str | None = None


class QcReport(BaseModel):
    """`productions.qc`. Consumed by `apps/web/src/features/queue/components/qc-report-card.tsx`."""

    passed: bool
    slideshow_risk: float | None = None
    checks: list[QcCheck] = Field(default_factory=list)


class PlatformCopy(BaseModel):
    """One entry in `productions.platform_copy`, rendered by `PlatformCopyPanel`."""

    caption: str | None = None
    title: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# MoneyPrinterTurbo wire types
# ---------------------------------------------------------------------------


class TaskState(IntEnum):
    FAILED = -1
    COMPLETE = 1
    PROCESSING = 4


class VideoParams(BaseModel):
    """Body of `POST /api/v1/videos`.

    Only the fields we actually drive are declared; `style_presets.params`
    supplies overrides as raw JSON and is merged over the top, so an unknown
    upstream field can be used from the database without a code change.

    `task_id` is *our fork's* addition. Upstream mints a fresh uuid4 on every
    POST with no idempotency key, so a retried request means a second full
    render and a second set of paid API calls. Sending the production id makes
    that impossible.
    """

    model_config = {"extra": "allow"}

    video_subject: str
    task_id: str | None = None
    video_script: str = ""
    video_terms: str | list[str] | None = None
    video_aspect: str = "9:16"
    video_source: str = "pexels"
    # Required whenever video_source is "local", which is how clips generated
    # elsewhere are handed to MoneyPrinterTurbo for assembly. Each entry's
    # `url` is a filename inside its storage/local_videos, not a link.
    video_materials: list[dict[str, Any]] | None = None
    video_count: int = 1
    video_clip_duration: int = 5
    video_concat_mode: str = "random"
    video_transition_mode: str | None = None
    voice_name: str = ""
    voice_rate: float = 1.0
    voice_volume: float = 1.0
    bgm_type: str = "random"
    bgm_volume: float = 0.2
    subtitle_enabled: bool = True
    subtitle_position: str = "bottom"
    font_name: str = "STHeitiMedium.ttc"
    font_size: int = 60
    text_fore_color: str = "#FFFFFF"
    stroke_color: str = "#000000"
    stroke_width: float = 1.5
    paragraph_number: int = 1
    n_threads: int = 2


class TaskStatus(BaseModel):
    """`data` of `GET /api/v1/tasks/{task_id}`.

    `extra="allow"` because MPT's own model allows extras and passes pipeline
    detail through: `script`, `terms`, `audio_file`, `subtitle_path`,
    `materials`, `warnings`, and the cross-post fields.
    """

    model_config = {"extra": "allow"}

    task_id: str
    state: int
    progress: int = 0
    videos: list[str] | None = None
    combined_videos: list[str] | None = None
    failed_stage: str | None = None
    error: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.state == TaskState.COMPLETE

    @property
    def is_failed(self) -> bool:
        return self.state == TaskState.FAILED

    @property
    def is_processing(self) -> bool:
        # Note: a task merely *queued* behind MPT's max_concurrent_tasks limit
        # is also state=4 with progress=0, because `update_task` runs before
        # `add_task`. Queued and running are indistinguishable from here, which
        # is why the render reconciler treats progress==0 more leniently.
        return self.state == TaskState.PROCESSING


# ---------------------------------------------------------------------------
# HeyGen wire types -- the presenter lane
# ---------------------------------------------------------------------------


class HeyGenStatus(str, Enum):
    """`status` on `GET /v3/videos/{id}`.

    `WAITING` is what `POST /v3/videos` actually returns on acceptance, and it
    is absent from every documented list of these values. It is here because a
    real submit produced it; `is_running` would have covered it regardless,
    which is the reason that property is written as "not terminal" rather than
    as a membership test.
    """

    WAITING = "waiting"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class HeyGenVideo(BaseModel):
    """`data` of `POST /v3/videos` and `GET /v3/videos/{id}`.

    `extra="allow"` because the detail response carries a good deal we do not
    drive -- gif_url, folder_id, video_page_url, timestamps -- and a new field
    should not break a poll.
    """

    model_config = {"extra": "allow"}

    id: str
    status: str
    video_url: str | None = None
    captioned_video_url: str | None = None
    subtitle_url: str | None = None
    thumbnail_url: str | None = None
    duration: float | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == HeyGenStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        return self.status == HeyGenStatus.FAILED

    @property
    def is_running(self) -> bool:
        # Deliberately not `not is_complete and not is_failed` on a closed set:
        # an unrecognised status is likelier to be a new one than a broken one,
        # so anything non-terminal reads as still running.
        return not self.is_complete and not self.is_failed

    def output_url(self, prefer_captioned: bool = True) -> str | None:
        """The file we actually want.

        This is the trap in HeyGen's response. Asking for burned-in captions
        does not change `video_url` -- that stays the clean cut -- and puts the
        captioned render on `captioned_video_url` instead. Taking `video_url`
        would publish a reel with no captions and, worse, make our own quality
        check report them missing on a render that has them.

        Falls back to the clean cut, because a video with no captions is still
        a video a reviewer can judge, and the quality report will say what is
        missing.
        """
        if prefer_captioned and self.captioned_video_url:
            return self.captioned_video_url
        return self.video_url or self.captioned_video_url

    @property
    def failure(self) -> str:
        """One readable line for the parked row."""
        code = self.failure_code or "unknown"
        return f"{code}: {self.failure_message or 'no detail returned'}"


class SocialMetadata(BaseModel):
    """`data` of `POST /api/v1/social-metadata`."""

    model_config = {"extra": "allow"}

    title: str | None = None
    caption: str | None = None
    hashtags: list[str] | str | None = None


# ---------------------------------------------------------------------------
# Postiz wire types
# ---------------------------------------------------------------------------


class PostizIntegration(BaseModel):
    """One entry from `GET /public/v1/integrations`."""

    model_config = {"extra": "allow"}

    id: str
    name: str
    identifier: str
    disabled: bool = False
    profile: str | None = None


class PostizMedia(BaseModel):
    """Response of `/upload` and `/upload-from-url`.

    Both `id` and `path` are required when referencing it from a post, so this
    is passed through to `posts[].value[].image` nearly verbatim.
    """

    model_config = {"extra": "allow"}

    id: str
    path: str


class PostizCreatedPost(BaseModel):
    post_id: str = Field(alias="postId")
    integration: str

    model_config = {"populate_by_name": True, "extra": "allow"}


# Postiz's own post states, from its Prisma schema.
PostizState = Literal["QUEUE", "PUBLISHED", "ERROR", "DRAFT"]
