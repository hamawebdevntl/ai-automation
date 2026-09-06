"""Runtime configuration, all from the environment.

Nothing here has a default that would work by accident in production: a missing
service-role key should fail loudly at import, not silently write nowhere.
"""

from __future__ import annotations

import json
import os
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
    # connected, so `postiz_enabled` in Terraform can leave it out entirely.
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

    # Where the on-demand trend run is launched. Only `dispatch_trend_runs`
    # reads these; the daily run is launched by EventBridge, which is told the
    # same things by Terraform and needs nothing here.
    #
    # The run id the task should report into. Set per-task by the dispatcher as
    # a container override, so it is empty on the scheduled run -- which is how
    # the runner tells an on-demand run from a scheduled one.
    trend_run_id: str = Field(default="", alias="TREND_RUN_ID")
    trends_cluster_arn: str = Field(default="", alias="TRENDS_CLUSTER_ARN")
    trends_task_definition: str = Field(default="", alias="TRENDS_TASK_DEFINITION")
    trends_subnet_ids: str = Field(default="", alias="TRENDS_SUBNET_IDS")
    trends_security_group_ids: str = Field(default="", alias="TRENDS_SECURITY_GROUP_IDS")

    @property
    def trends_subnet_id_list(self) -> list[str]:
        return [s.strip() for s in self.trends_subnet_ids.split(",") if s.strip()]

    @property
    def trends_security_group_id_list(self) -> list[str]:
        return [s.strip() for s in self.trends_security_group_ids.split(",") if s.strip()]

    @property
    def hashtag_list(self) -> list[str]:
        return [h.strip().lstrip("#") for h in self.trend_hashtags.split(",") if h.strip()]

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.trend_keywords.split(",") if k.strip()]

    # --- AWS ----------------------------------------------------------------
    state_machine_arn: str | None = Field(default=None, alias="STATE_MACHINE_ARN")
    gate_bridge_secret: str | None = Field(default=None, alias="GATE_BRIDGE_SECRET")


def _load_secrets_into_env() -> None:
    """Resolve a Secrets Manager bundle into the environment.

    Lambda cannot inject Secrets Manager values into environment variables, and
    putting a service-role key or an API key in a plain Lambda env var leaves it
    readable to anyone with GetFunctionConfiguration. So the ARN is the only
    thing passed in, and the bundle is fetched once at cold start.

    Existing environment variables win, which keeps local overrides and tests
    working without touching AWS.
    """
    arn = os.environ.get("PIPELINE_SECRETS_ARN")
    if not arn:
        return
    try:
        import boto3

        payload = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
        for key, value in json.loads(payload).items():
            os.environ.setdefault(key, str(value))
    except Exception as exc:
        raise RuntimeError(
            f"could not resolve PIPELINE_SECRETS_ARN: {type(exc).__name__}"
        ) from exc


@lru_cache(maxsize=1)
def settings() -> Settings:
    _load_secrets_into_env()
    return Settings()  # type: ignore[call-arg]
