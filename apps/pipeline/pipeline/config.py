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
    mpt_base_url: str = Field(alias="MPT_BASE_URL")
    mpt_api_key: str = Field(alias="MPT_API_KEY")
    # MPT renders in a background thread and offers no webhook, so every wait
    # is a poll. A render that has not moved within this budget is stranded:
    # an interrupted MPT render stays at state=4 forever and is never recovered.
    mpt_render_budget_seconds: int = Field(default=3600, alias="MPT_RENDER_BUDGET_SECONDS")

    # --- Postiz -------------------------------------------------------------
    postiz_base_url: str = Field(alias="POSTIZ_BASE_URL")
    postiz_api_key: str = Field(alias="POSTIZ_API_KEY")

    # --- Trend research -----------------------------------------------------
    # The niche brief is what makes idea generation relevant rather than
    # generically topical. There is no sensible default: without it every idea
    # costs the owner a review and possibly a render for nothing.
    niche_brief: str = Field(default="", alias="NICHE_BRIEF")
    trend_hashtags: str = Field(default="", alias="TREND_HASHTAGS")
    trend_keywords: str = Field(default="", alias="TREND_KEYWORDS")
    tiktok_ms_token: str | None = Field(default=None, alias="TIKTOK_MS_TOKEN")
    ideas_per_run: int = Field(default=10, alias="IDEAS_PER_RUN")

    @property
    def hashtag_list(self) -> list[str]:
        return [h.strip().lstrip("#") for h in self.trend_hashtags.split(",") if h.strip()]

    @property
    def keyword_list(self) -> list[str]:
        return [k.strip() for k in self.trend_keywords.split(",") if k.strip()]

    # --- AWS ----------------------------------------------------------------
    state_machine_arn: str | None = Field(default=None, alias="STATE_MACHINE_ARN")
    gate_bridge_secret: str | None = Field(default=None, alias="GATE_BRIDGE_SECRET")


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
