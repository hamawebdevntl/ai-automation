"""The trend-research run.

Scouts, scores, drafts ideas, and writes them to the Gate 1 queue.

Which source it scouts is a setting, and this module deliberately names none of
them: `sources.REGISTRY` says which vocabulary a source reads, which credential
it needs and how to call it, so adding a source does not mean editing the run.

Every decision this makes is read from `trend_settings` rather than compiled
in. The precedence is the same one the brief and the hashtags have always had:
the row wins when it has a value, and the constants this code used before the
row existed answer when it does not. That is what lets the task run against a
database that has not been migrated, and what keeps the tests from needing one.
"""

from __future__ import annotations

import logging
import re

from pipeline.clients.supa import Supa
from pipeline.config import Settings, settings
from pipeline.trends import controls as controls_mod
from pipeline.trends import ideas as ideas_mod
from pipeline.trends import report as report_mod
from pipeline.trends import sources as sources_mod
from pipeline.trends.controls import ScoutControls

log = logging.getLogger(__name__)


def _normalise(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _existing_titles(supa: Supa, window_days: int) -> set[str]:
    """Titles recently added, for suppressing near-duplicates.

    The window is a setting because it decides which of two sentences the app
    gets to say about an empty run. Too short and the same idea is re-drafted
    every few days; too long and a format still worth using is suppressed
    because it was tried a month ago and never approved.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = (
        supa.raw.table("ideas")
        .select("title")
        .gte("created_at", cutoff)
        .execute()
    ).data or []
    return {_normalise(r["title"]) for r in rows if r.get("title")}


def _inputs(supa: Supa, cfg: Settings) -> tuple[str, list[str], ScoutControls]:
    """The brief, the hashtags and the controls, database first.

    All of these used to come only from the environment or from constants,
    which made them deploy-time decisions. They are now edited in the app, so
    `trend_settings` wins -- but the environment and the old constants still
    answer when the row is absent or a field is blank. That fallback is what
    lets a headless run work against a database that has not been migrated,
    and what keeps the tests from needing one at all.

    Each field falls back independently: a brief set in the app and hashtags
    left to the environment is a coherent state, not a half-configured one.
    """
    row: dict = {}
    try:
        row = supa.trend_settings() or {}
    except Exception as exc:  # noqa: BLE001 - the environment is a real answer
        # Never fatal. A settings table that is missing or unreadable should
        # degrade to the previous behaviour, not stop the run outright.
        log.warning("could not read trend_settings, falling back to env: %s", exc)

    brief = (row.get("niche_brief") or "").strip() or cfg.niche_brief
    controls = controls_mod.from_row(row)

    # Each source has its own vocabulary, and they are not interchangeable.
    # `#exceltips` is how a video is filed; "bookkeeping software" is what
    # somebody types when they have had enough of doing it by hand. Handing
    # either list to the other source produces a run that looks like it worked
    # and finds nothing. Which list a source reads is declared in
    # `sources.REGISTRY` rather than decided here.
    if sources_mod.spec(controls.trend_source).vocabulary == sources_mod.KEYWORDS:
        terms = [k.strip() for k in (row.get("trend_keywords") or []) if k and k.strip()]
        return brief, (terms or cfg.keyword_list), controls

    tags = [t.strip().lstrip("#") for t in (row.get("hashtags") or []) if t and t.strip()]
    return brief, (tags or cfg.hashtag_list), controls


def run(supa: Supa | None = None, run_id: str = "") -> dict:
    """Scout, score, draft, insert.

    `run_id` is the row this run reports into, when there is one. It is passed
    down rather than only used for bookkeeping because it is also how the scout
    learns it has been stopped: cancelling frees the in-flight lock in Postgres
    immediately, and this task is the thing that lock was protecting.
    """
    supa = supa or Supa()
    cfg = settings()

    niche_brief, terms, controls = _inputs(supa, cfg)
    source = sources_mod.spec(controls.trend_source)

    # A run started from the button may carry its own length. Merged here
    # rather than inside `_inputs` because it belongs to this run, not to the
    # configuration -- and because a scheduled run has no row-level overrides
    # to merge, so the two paths stay visibly different.
    if run_id:
        controls = controls_mod.with_run_overrides(controls, supa.trend_run(run_id))

    if not niche_brief.strip():
        # Deliberately a hard stop. A trend run without a brief fills the
        # owner's queue with plausible, irrelevant ideas -- which is worse than
        # an empty queue, because each one costs a review.
        raise RuntimeError(
            "No niche brief is set. Set one under Settings in the app, or via "
            "NICHE_BRIEF. Trend research cannot judge relevance without it; "
            "refusing to fill the approval queue with generic ideas."
        )

    if not terms:
        raise RuntimeError(source.nothing_to_scout)

    # Rotation. Scouting a slice of the list keeps a run short without changing
    # what qualifies as a signal, and the cursor is advanced before scouting
    # rather than after so that a run which dies mid-session does not make the
    # next one repeat the same tags.
    selected, next_cursor = controls_mod.rotate(terms, controls.hashtag_cursor, controls.hashtags_per_run)
    if len(selected) < len(terms):
        log.info(
            "rotating: scouting %d of %d terms this run (%s)",
            len(selected),
            len(terms),
            ", ".join(selected),
        )
        supa.save_hashtag_cursor(next_cursor)

    # Both credential checks happen here, before the scout, and not where the
    # credential is used: scouting takes minutes and, on Apify, bills per
    # result. A missing key would otherwise throw all of that away at the last
    # step -- or on Apify, spend money and then throw it away.
    #
    # The source goes first because it is the thing about to run.
    source.preflight(cfg, controls)

    provider = ideas_mod.resolve_provider(
        controls.idea_provider or cfg.idea_provider, gemini_api_key=cfg.gemini_api_key
    )
    log.info("scouting %s, drafting ideas with %s", source.name, provider)

    # Only when there is a row to be stopped. A container started by hand has
    # nothing watching it and nothing to ask.
    should_stop = (lambda: supa.trend_run_is_cancelled(run_id)) if run_id else None

    # Every source returns the same `ScoutOutcome`, which is what lets
    # everything below this line -- spreading, drafting, duplicate suppression,
    # the rejection report -- stay ignorant of where signals came from. It is
    # also why this is one call rather than a branch per source.
    outcome = source.scout(selected, controls, cfg, should_stop)
    signals, report = outcome.signals, outcome.report
    log.info(
        "scouted %d signals worth surfacing from %d videos across %d hashtags",
        len(signals),
        report.seen,
        len(report.hashtags_scouted),
    )

    drafted: list = []
    inserted: list = []
    suppressed = 0

    # A stopped run drafts nothing and inserts nothing, even when it had
    # already found something worth drafting. Stop has to mean stop: filling
    # the queue a minute after the owner was told the run was cancelled is a
    # surprise, and it spends an LLM call on a batch nobody asked to finish.
    if signals and not report.cancelled:
        drafted = ideas_mod.generate_ideas(
            signals, niche_brief, count=controls.ideas_per_run, provider=provider
        )
        platforms = supa.enabled_platforms()
        rows = ideas_mod.to_rows(drafted, signals, platforms)

        # EventBridge schedules are at-least-once, so a retried run must not
        # double the queue. This is application-level rather than a unique
        # constraint, because two near-identical titles are not exactly equal.
        seen_titles = _existing_titles(supa, controls.dedup_window_days)
        fresh = [r for r in rows if _normalise(r["title"]) not in seen_titles]
        suppressed = len(rows) - len(fresh)
        report.drop("duplicate", suppressed)

        inserted = supa.insert_ideas(fresh)
        log.info("inserted %d ideas (%d suppressed as duplicates)", len(inserted), suppressed)

    # Built on every path, including the empty one. The empty path is the one
    # that needed it: "no signal cleared its baseline" was the whole
    # explanation a run had for an untouched queue, and it named neither which
    # bar nor how many videos had been looked at.
    rejections = report_mod.payload(
        report,
        controls,
        surfaced=len(signals),
        drafted=len(drafted),
        inserted=len(inserted),
        hashtags_configured=len(terms),
        source=controls.trend_source,
    )
    log.info("trend run funnel: %s", report_mod.summarise(rejections))

    return {
        "cancelled": report.cancelled,
        "signals": len(signals),
        "drafted": len(drafted),
        "inserted": len(inserted),
        "suppressed": suppressed,
        "scouted": report.seen,
        "hashtags_scouted": report.hashtags_scouted,
        "rejections": rejections,
        "provider": provider,
        "hashtags": selected,
    }


def main() -> None:
    """Entry point for the container, scheduled or on-demand.

    The bookkeeping lives here rather than in `run` so that `run` stays a
    function that scouts and returns counts, callable from a test without a
    row to report into.

    `TREND_RUN_ID` is set as a container override by the dispatcher, which now
    starts the scheduled run as well as the on-demand one -- so unlike before,
    every run has a row to report into. Its absence means someone started this
    container by hand.

    The failure path matters as much as the success one. At most one run may be
    in flight, so a task that dies without writing back holds the button shut
    until a sweeper writes the row off minutes later. Recording the failure
    here turns that into an error the owner can read immediately.
    """
    logging.basicConfig(level=logging.INFO)
    run_id = settings().trend_run_id.strip()
    supa = Supa()

    try:
        result = run(supa, run_id=run_id)
    except Exception as exc:
        if run_id:
            supa.finish_trend_run(run_id, status="failed", error=str(exc)[:2000])
        raise

    if result.get("cancelled"):
        # The row is already `cancelled`, written by the owner, and its status
        # is theirs -- reporting an outcome into it would be reporting on a run
        # they were told had stopped, and `finish_trend_run` refuses it anyway.
        #
        # What it had already seen is a different matter, and worth keeping:
        # it is what separates "I stopped it too early" from "it was getting
        # nowhere". Diagnostics only; nothing that changes how the run ended.
        if run_id:
            supa.record_cancelled_progress(
                run_id,
                scouted=result.get("scouted"),
                hashtags_scouted=result.get("hashtags_scouted"),
                rejections=result.get("rejections"),
            )
        log.info(
            "trend run %s was stopped from the app after %s videos",
            run_id or "(headless)",
            result.get("scouted"),
        )
        return

    if run_id:
        supa.finish_trend_run(
            run_id,
            status="succeeded",
            signals=result.get("signals"),
            drafted=result.get("drafted"),
            inserted=result.get("inserted"),
            suppressed=result.get("suppressed"),
            scouted=result.get("scouted"),
            hashtags_scouted=result.get("hashtags_scouted"),
            rejections=result.get("rejections"),
        )
    log.info("trend run complete: %s", {k: v for k, v in result.items() if k != "rejections"})


if __name__ == "__main__":
    main()
