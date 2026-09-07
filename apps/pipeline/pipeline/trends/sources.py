"""Which scout runs, what it reads, and what it needs before it starts.

One table, because the alternative is the same `if` written in four places.
Adding `apify` and `youtube` alongside `google_trends` turned the runner's
two-branch conditional into a three-way one repeated at every site that cares
about the source -- which vocabulary it reads, what to say when that list is
empty, which credential it needs, and how to call it. Three of those were
already conditionals; the fourth was about to be.

This lives in its own module rather than in `controls.py` because it imports
the provider modules and they import `controls` -- putting the registry there
would be a cycle. `controls.SOURCES` stays the validation authority, because
`_source()` needs it to coerce a row before anything here is consulted, and a
test asserts the two agree.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from pipeline.config import Settings
from pipeline.trends import apify, gtrends, youtube
from pipeline.trends.base import ScoutOutcome
from pipeline.trends.controls import DEFAULT_TREND_SOURCE, ScoutControls

log = logging.getLogger(__name__)

# The two lists in `trend_settings` a source can read. They are not
# interchangeable: `#exceltips` is how a video is filed, "bookkeeping software"
# is what somebody types when they have had enough of doing it by hand. Handing
# either to the wrong source produces a run that reports success and finds
# nothing -- the failure that looks most like a quiet week.
KEYWORDS = "keywords"
HASHTAGS = "hashtags"

Scout = Callable[[list[str], ScoutControls, Settings, "Callable[[], bool] | None"], ScoutOutcome]
Preflight = Callable[[Settings, ScoutControls], None]


@dataclass(frozen=True)
class Spec:
    """Everything the runner needs to know about one source."""

    name: str
    vocabulary: str
    # The runner's refusal when that list is empty, in this source's own words.
    nothing_to_scout: str
    preflight: Preflight
    scout: Scout


# ---------------------------------------------------------------------------
# Credential checks.
#
# Called before scouting rather than at the point of use, for the reason
# `ideas.resolve_provider` gives: the scout is the expensive part, and a missing
# key for the selected source is knowable at the start. On Apify it is more than
# expensive -- a run bills per result, so discovering the problem afterwards is
# the one failure that costs money and produces nothing.
#
# Each message names the variable, where the value comes from, and the way out.
# ---------------------------------------------------------------------------


def _no_credentials_needed(cfg: Settings, controls: ScoutControls) -> None:
    """Google Trends has no key. There is no official API to have one for."""


def _apify_ready(cfg: Settings, controls: ScoutControls) -> None:
    if not cfg.apify_token.strip():
        raise RuntimeError(
            "trend_source=apify but APIFY_TOKEN is not set. A token comes from "
            "https://console.apify.com/settings/integrations and belongs in the "
            "pipeline secrets bundle; or choose a different source under Settings "
            "in the app. Refusing to start rather than scouting: an Apify run "
            "bills per result, and finding this out after the scrape is the one "
            "failure that costs money and produces nothing."
        )
    if not controls.apify_platforms:
        raise RuntimeError(
            "trend_source=apify but no platforms are selected. Choose TikTok, "
            "Instagram or both under Settings in the app."
        )


def _youtube_ready(cfg: Settings, controls: ScoutControls) -> None:
    if not cfg.youtube_api_key.strip():
        raise RuntimeError(
            "trend_source=youtube but YOUTUBE_API_KEY is not set. Create a key in a "
            "Google Cloud project with 'YouTube Data API v3' enabled "
            "(https://console.cloud.google.com/apis/library/youtube.googleapis.com) "
            "and add it to the pipeline secrets bundle, or choose another source "
            "under Settings in the app."
        )


# ---------------------------------------------------------------------------
# Calling the scouts. Each adapter's only job is to build that source's config
# out of the controls, so the runner never mentions a provider by name.
# ---------------------------------------------------------------------------


def _scout_google_trends(
    terms: list[str],
    controls: ScoutControls,
    cfg: Settings,
    should_stop: Callable[[], bool] | None,
) -> ScoutOutcome:
    return gtrends.scout(
        gtrends.GTrendsConfig(
            keywords=terms,
            controls=controls,
            geo=controls.trend_geo,
            should_stop=should_stop,
        )
    )


def _scout_apify(
    terms: list[str],
    controls: ScoutControls,
    cfg: Settings,
    should_stop: Callable[[], bool] | None,
) -> ScoutOutcome:
    return apify.scout(
        apify.ApifyConfig(
            hashtags=terms,
            platforms=controls.apify_platforms,
            controls=controls,
            should_stop=should_stop,
        )
    )


def _scout_youtube(
    terms: list[str],
    controls: ScoutControls,
    cfg: Settings,
    should_stop: Callable[[], bool] | None,
) -> ScoutOutcome:
    return youtube.scout(
        youtube.YouTubeConfig(
            keywords=terms,
            controls=controls,
            should_stop=should_stop,
        )
    )


REGISTRY: dict[str, Spec] = {
    "apify": Spec(
        name="apify",
        vocabulary=HASHTAGS,
        nothing_to_scout=(
            "No hashtags are set; there is nothing to scout. Add some under "
            "Settings in the app, or set TREND_HASHTAGS."
        ),
        preflight=_apify_ready,
        scout=_scout_apify,
    ),
    "google_trends": Spec(
        name="google_trends",
        vocabulary=KEYWORDS,
        nothing_to_scout=(
            "No search terms are set; there is nothing to scout. Add some under "
            "Settings in the app."
        ),
        preflight=_no_credentials_needed,
        scout=_scout_google_trends,
    ),
    "youtube": Spec(
        name="youtube",
        vocabulary=KEYWORDS,
        nothing_to_scout=(
            "No search terms are set; there is nothing to scout. Add some under "
            "Settings in the app, or set TREND_KEYWORDS."
        ),
        preflight=_youtube_ready,
        scout=_scout_youtube,
    ),
}


def spec(name: str) -> Spec:
    """The chosen source, falling back rather than raising.

    `controls._source` has already coerced an unknown row value to the default,
    so a miss here means `SOURCES` and `REGISTRY` disagree -- a mistake made
    while adding a source, not one a database can cause. Even so it falls back
    rather than raising, for the same reason `_source` does: a configuration
    slip should degrade to scouting something, not to a run that cannot start.
    The test that keeps the two in step is the real guard.
    """
    chosen = REGISTRY.get(name)
    if chosen is None:
        log.warning("no scout is registered for trend_source=%r; using %s", name, DEFAULT_TREND_SOURCE)
        return REGISTRY[DEFAULT_TREND_SOURCE]
    return chosen
