"""Runtime configuration, all from the environment.

Nothing here has a default that would work by accident in production: a missing
service-role key should fail loudly at import, not silently write nowhere.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # --- Supabase -----------------------------------------------------------
    # The service role bypasses row-level security, which is the only reason
    # the pipeline can write `ideas` and `productions` at all: those tables
    # deliberately have no INSERT/UPDATE policy.
    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_service_role_key: str = Field(alias="SUPABASE_SERVICE_ROLE_KEY")
    renders_bucket: str = Field(default="renders", alias="RENDERS_BUCKET")

    # --- MoneyPrinterTurbo --------------------------------------------------
    # Empty by default, for the same reason Postiz is: rendering is a
    # deployable half of this system rather than a given. The trend scout uses
    # neither of these, so requiring them made a trends-only deployment --
    # GitHub Actions, a cron on a laptop -- invent two credentials for a
    # service it never contacts, purely to get past import.
    #
    # The loudness moves rather than disappearing: `MptClient` refuses to be
    # constructed without a base URL, and nothing constructs one unless a
    # render is actually being submitted.
    mpt_base_url: str = Field(default="", alias="MPT_BASE_URL")
    mpt_api_key: str = Field(default="", alias="MPT_API_KEY")
    # MPT renders in a background thread and offers no webhook, so every wait
    # is a poll. A render that has not moved within this budget is stranded:
    # an interrupted MPT render stays at state=4 forever and is never recovered.
    mpt_render_budget_seconds: int = Field(default=3600, alias="MPT_RENDER_BUDGET_SECONDS")

    # --- Postiz -------------------------------------------------------------
    # Publishing is a deployable half of the system, not a given: Postiz is only
    # worth its t3.large once the platform apps are approved and channels are
    # connected, so `PUBLISHING_ENABLED=false` leaves it out entirely -- and
    # Postiz is not in `docker-compose.yml` at all until it is wanted, since it
    # brings its own Postgres, Redis, Temporal and Elasticsearch.
    #
    # These two therefore default to empty rather than failing at import, which
    # is the one exception to the rule at the top of this file. The loudness is
    # not lost, only moved: PostizClient refuses to be constructed without a
    # base URL, and every activity that would need one checks the flag below
    # first. `publishing_enabled` defaults to True on purpose -- the dangerous
    # mistake is silently never posting, so an unset flag means "publish".
    publishing_enabled: bool = Field(default=True, alias="PUBLISHING_ENABLED")
    postiz_base_url: str = Field(default="", alias="POSTIZ_BASE_URL")
    postiz_api_key: str = Field(default="", alias="POSTIZ_API_KEY")

    # --- fal.ai -------------------------------------------------------------
    # The second render backend. Selected per style preset via
    # style_presets.render_mode, so nothing here decides which one a video uses.
    fal_api_key: str = Field(default="", alias="FAL_API_KEY")
    fal_transcribe_model: str = Field(
        default="fal-ai/whisper", alias="FAL_TRANSCRIBE_MODEL"
    )
    # A generated clip's URL is public and expires, so the end-to-end path must
    # fetch promptly rather than storing the URL and coming back to it.
    fal_poll_budget_seconds: int = Field(default=1800, alias="FAL_POLL_BUDGET_SECONDS")

    # --- The source-footage lane --------------------------------------------
    # How long the signed URL to the owner's upload stays valid.
    #
    # This has to outlive the whole render rather than the submit. The provider
    # fetches the file when the job leaves the queue, not when it is accepted,
    # and it may re-fetch mid-run -- so a URL sized for the submit expires under
    # a job that is queued behind someone else's, and the render fails after
    # being billed for. Six hours against a thirty-minute poll budget is
    # deliberately generous: the URL grants read access to one object the owner
    # uploaded themselves, so a long lifetime costs far less than a short one.
    #
    # `_submit_fal_video` refuses to submit if this is not longer than
    # `fal_poll_budget_seconds`, so the two cannot be tuned out of step.
    source_video_url_ttl_seconds: int = Field(
        default=6 * 3600, alias="SOURCE_VIDEO_URL_TTL_SECONDS"
    )

    # --- Intelligent clipping -----------------------------------------------
    # The lane that cuts an uploaded recording down rather than generating
    # anything. Everything here is about the two calls that happen *before* the
    # gate: transcription and one structured LLM call. Neither renders, which is
    # what makes discarding a candidate free.

    # How long to wait for a transcript.
    #
    # Far longer than `FalClient.wait`'s 240s default, which was sized for
    # transcribing forty seconds of narration on the fal_full lane. Here the
    # input is the whole recording -- an hour of webinar is a plausible upload --
    # and the job also queues behind everything else in the fal account. Past
    # this the source is marked failed, which costs nothing and is retryable.
    clip_transcribe_budget_seconds: int = Field(
        default=2700, alias="CLIP_TRANSCRIBE_BUDGET_SECONDS"
    )

    # How long a worker's claim on a `clip_sources` row survives unrenewed.
    #
    # Sized for the transcribe phase, which is the long one, and deliberately
    # longer than it: a lease that expires mid-transcription would hand the row
    # to a second worker and pay fal twice for one file. Nothing sleeps holding
    # it -- the phase runs, the row is written, the lease is released.
    clip_lease_seconds: int = Field(default=3600, alias="CLIP_LEASE_SECONDS")

    # The longest recording that will be transcribed.
    #
    # A ceiling on the one cost here that scales with the owner's file rather
    # than with our settings: fal bills transcription per minute of audio, and
    # the upload length is chosen by whoever drags the file in. Four hours is
    # generous for the intended input (a talk, a webinar, a podcast) and refuses
    # the case that is almost certainly a mistake -- a whole day of recording
    # dropped in by accident.
    #
    # Refused before transcription rather than after, so the bill is never
    # incurred; the source fails with the measured duration in the message.
    clip_max_source_seconds: int = Field(default=4 * 3600, alias="CLIP_MAX_SOURCE_SECONDS")

    # --- HeyGen -------------------------------------------------------------
    # The presenter lane. Selected per style preset via
    # style_presets.render_mode = 'heygen', so nothing here decides which
    # backend a video uses -- only whether the lane can run at all.
    #
    # Empty by default, like fal's: the key is only needed when a preset that
    # names it is approved, and the client fails loudly at construction rather
    # than at import.
    heygen_api_key: str = Field(default="", alias="HEYGEN_API_KEY")
    # A talking-head render is minutes, not the twenty a stock render can take,
    # but the queue is shared with everything else in the account and a job can
    # sit behind the concurrency limit, so the budget is generous. Past it the
    # render is treated as stranded and the row parks.
    heygen_poll_budget_seconds: int = Field(default=1800, alias="HEYGEN_POLL_BUDGET_SECONDS")

    # --- Idea generation ----------------------------------------------------
    # Which model drafts the Gate 1 queue. Claude is the default; Gemini is here
    # because its free tier makes a demo possible without a billing account.
    #
    # The prompt, the output schema and the row mapping are shared between the
    # two, so this switches which model answers and nothing else -- which is the
    # only reason the ideas either one produces are comparable at all.
    idea_provider: str = Field(default="claude", alias="IDEA_LLM_PROVIDER")
    # Anthropic's SDK reads ANTHROPIC_API_KEY from the environment itself, so
    # there is deliberately no field for it: a second source of truth for one
    # key is how a key ends up set in the place that is not read. Gemini's
    # client takes its key as an argument, so that one is ours to carry.
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    # Flash rather than Pro on purpose: Pro has no free tier, and the point of
    # this lane is that it costs nothing to try.
    #
    # 3.5 rather than the newer 3.8 because 3.8 answered a one-word prompt but
    # returned 503 "high demand" on every schema-constrained request tried --
    # free-tier traffic is shed first, and the newest model is where the queue
    # is. Worth retrying later; it is a one-word change here.
    gemini_model: str = Field(default="gemini-3.5-flash", alias="GEMINI_MODEL")

    # --- Trend research -----------------------------------------------------
    # The niche brief is what makes idea generation relevant rather than
    # generically topical. There is no sensible default: without it every idea
    # costs the owner a review and possibly a render for nothing.
    niche_brief: str = Field(default="", alias="NICHE_BRIEF")
    trend_hashtags: str = Field(default="", alias="TREND_HASHTAGS")
    trend_keywords: str = Field(default="", alias="TREND_KEYWORDS")
    tiktok_ms_token: str | None = Field(default=None, alias="TIKTOK_MS_TOKEN")
    ideas_per_run: int = Field(default=10, alias="IDEAS_PER_RUN")

    # Credentials for the two sources that replaced the browser-driven scout.
    #
    # Empty rather than required, like every other key here: a deployment that
    # only ever selects Google Trends should not have to hold credentials for
    # sources it does not use. The refusal happens when the source is selected
    # -- see `trends.credentials`, which checks before scouting starts rather
    # than at the point of use.
    #
    # Apify bills per compute unit, so this token spends money when a run uses
    # it. The YouTube key is quota'd rather than billed: 10,000 units a day,
    # and a search costs 100 of them.
    apify_token: str = Field(default="", alias="APIFY_TOKEN")
    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")

    # The trend run currently in flight, when the scout is driven directly
    # rather than through the dispatcher. Left for the tests and for a one-off
    # `python -m pipeline.trends.runner`; the worker passes the id as an
    # argument instead, because a process-global would race across its threads.
    trend_run_id: str = Field(default="", alias="TREND_RUN_ID")

    @property
    def hashtag_list(self) -> list[str]:
        return [h.strip().lstrip("#") for h in self.trend_hashtags.split(",") if h.strip()]

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.trend_keywords.split(",") if k.strip()]

    # --- The driver ---------------------------------------------------------
    # What used to be Step Functions' concern. A production is advanced by
    # whichever worker holds its lease, so these are the three numbers that
    # decide how quickly work is picked up and how long a dead worker holds on.
    #
    # An identity for the lease. Only ever read back in a log line or the
    # `leased_by` column -- `lease_expires_at` is what actually excludes -- so a
    # duplicate across two hosts is untidy rather than unsafe. Defaults to the
    # hostname plus a random suffix.
    worker_id: str = Field(default="", alias="WORKER_ID")

    # Two, because the failure to avoid is a multi-minute `fetch_and_qc` --
    # hundreds of megabytes and four ffmpeg passes -- stalling every other
    # production's thirty-second poll. One spare thread fixes that. Past about
    # four you are only queueing on MoneyPrinterTurbo's own concurrency limit,
    # which answers with the 429 that `submit_render` already retries, and on a
    # CPU that MPT is saturating anyway.
    production_workers: int = Field(default=2, alias="PRODUCTION_WORKERS")

    # How long an idle worker waits before asking again. This is the Gate 2
    # latency: it is what stands between an owner clicking approve and the
    # production moving. The webhook it replaced managed about a second but was
    # at-most-once, and the sweeper that made it trustworthy ran every sixty.
    driver_poll_seconds: int = Field(default=5, alias="DRIVER_POLL_SECONDS")

    # How long a claim survives without being renewed. Long enough that an
    # ordinary step never loses its row mid-flight, short enough that a worker
    # killed by the OOM reaper does not hold a production hostage. `fetch_and_qc`
    # overrides this per step -- see `driver/graph.py`.
    lease_seconds: int = Field(default=900, alias="LEASE_SECONDS")


# `_load_secrets_into_env` lived here and is gone with AWS.
#
# Lambda cannot inject a Secrets Manager value into an environment variable, and
# a service-role key sitting in a plain Lambda env var is readable to anyone
# with GetFunctionConfiguration -- so only the ARN was passed in and the bundle
# was fetched once at cold start. Neither constraint exists on a machine we
# control: an `.env` file read by Compose is the same secret with one fewer
# service in front of it.


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
