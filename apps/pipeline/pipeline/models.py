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
    # "linkedin" is deliberately absent: upstream has no LinkedIn spec at all
    # (`grep -cin linkedin app/services/llm.py` -> 0). Until our fork adds one,
    # asking MPT for LinkedIn copy yields TikTok copy. See copy.py.
}


class ProductionStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    QC_FAILED = "qc_failed"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    PARKED = "parked"


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


class ProductionContext(BaseModel):
    """What a state machine activity is handed.

    Deliberately tiny. Step Functions caps execution input/output and the
    SendTaskSuccess payload at 256 KB, and a script plus four platforms of copy
    plus a QC report would approach that. Activities receive an id and read the
    rest from Postgres.
    """

    production_id: str
    idea_id: str | None = None
    style_preset_id: str | None = None
    attempt: int = 0
    extra: dict[str, Any] = Field(default_factory=dict)
