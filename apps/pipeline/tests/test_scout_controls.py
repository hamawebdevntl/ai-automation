"""Reading the settings row without letting it break a run.

Every value here now arrives from a form, which means it arrives from outside.
The CHECK constraints in 20260906150000 are what actually stop a bad one being
stored; this module is the second line, for a row written before those
constraints existed, edited in a console, or read by a build newer than the
database it is pointed at.

The bias throughout is clamp, never refuse. Scouting is the better part of an
hour of paced browser work and it is the only thing that fills the queue, so a
typo must cost a corrected value and a log line, not a day with no ideas.
"""

from __future__ import annotations

import pytest

from pipeline.trends import controls as mod
from pipeline.trends.controls import BlocklistMatcher, ScoutControls, from_row, rotate


class TestDefaultsArePreviousBehaviour:
    """The promise the migration makes: applying it changes nothing.

    Each of these was a constant somewhere in this package or a variable in
    Terraform. If one of them drifts, a database that has not been migrated and
    one that has stop producing the same run, which is the one thing the
    fallback exists to prevent.
    """

    @pytest.mark.parametrize(
        "field, was",
        [
            ("videos_per_hashtag", 30),      # ScoutConfig's default
            ("min_outlier_ratio", 1.5),      # is_worth_surfacing
            ("baseline_sample_size", 12),    # BASELINE_SAMPLE
            ("pacing_min_seconds", 2.0),     # MIN_DELAY_S
            ("pacing_max_seconds", 5.0),     # MAX_DELAY_S
            ("dedup_window_days", 14),       # DEDUP_WINDOW_DAYS
            ("ideas_per_run", 10),           # IDEAS_PER_RUN
            ("idea_expiry_days", 7),         # expire_ideas(older_than_days=7)
            ("min_plays", 0),                # no such filter existed
            ("min_engagement_rate", 0.0),    # nor this one
            ("schedule_hour_utc", 6),        # cron(0 6 * * ? *)
            ("hashtags_per_run", None),      # rotation is new, and off
            ("run_budget_minutes", None),    # so is the budget
        ],
    )
    def test_an_empty_row_reproduces_the_old_constant(self, field, was):
        assert getattr(from_row({}), field) == was

    def test_the_recency_limit_is_the_one_deliberate_exception(self):
        # There was no age filter at all, so any default here is a behaviour
        # change. Thirty days is the chosen one -- see the migration header.
        assert from_row({}).max_video_age_days == 30

    def test_a_missing_row_is_the_same_as_an_empty_one(self):
        assert from_row(None) == from_row({})


class TestValuesOutsideTheBoundsAreCorrectedNotFatal:
    def test_a_wild_video_count_is_clamped_to_the_ceiling(self, caplog):
        with caplog.at_level("WARNING"):
            assert from_row({"videos_per_hashtag": 5000}).videos_per_hashtag == 100
        # Clamping silently would leave the page saying one thing and the run
        # doing another, with nothing anywhere to reconcile them.
        assert "videos_per_hashtag" in caplog.text

    def test_pacing_cannot_be_driven_below_the_floor(self):
        # The floor is the guard that protects the account rather than the run:
        # faster than this is not a quicker scrape, it is a flagged session.
        assert from_row({"pacing_min_seconds": 0}).pacing_min_seconds == 1.0
        assert from_row({"pacing_min_seconds": -30}).pacing_min_seconds == 1.0

    def test_an_inverted_pacing_range_collapses_to_the_minimum(self, caplog):
        # `random.uniform(5, 2)` does not raise; it quietly draws from a range
        # nobody asked for, which would be a pacing change nobody could see.
        with caplog.at_level("WARNING"):
            controls = from_row({"pacing_min_seconds": 5, "pacing_max_seconds": 2})
        assert controls.pacing_min_seconds == 5.0
        assert controls.pacing_max_seconds == 5.0

    def test_a_ratio_below_one_is_raised_to_one(self):
        # Below 1.0 the filter admits videos doing worse than their author's
        # own median, which is not a signal in any sense of the word.
        assert from_row({"min_outlier_ratio": 0.2}).min_outlier_ratio == 1.0

    def test_nonsense_takes_the_default_rather_than_stopping_the_run(self):
        assert from_row({"videos_per_hashtag": "lots"}).videos_per_hashtag == 30
        assert from_row({"min_engagement_rate": None}).min_engagement_rate == 0.0

    def test_numbers_arriving_as_strings_are_read(self):
        # PostgREST returns numeric columns as strings.
        controls = from_row({"min_engagement_rate": "0.0400", "min_outlier_ratio": "3.00"})
        assert controls.min_engagement_rate == 0.04
        assert controls.min_outlier_ratio == 3.0


class TestTheSchedulingFields:
    def test_days_are_de_duplicated_and_ordered(self):
        assert from_row({"schedule_days": [3, 1, 3]}).schedule_days == (1, 3)

    def test_an_empty_day_list_does_not_mean_never(self, caplog):
        # A schedule that is enabled and can never be due is indistinguishable
        # from a dispatcher that is not running -- the exact confusion this
        # whole change exists to remove. Pausing is how "never" is said.
        with caplog.at_level("WARNING"):
            assert from_row({"schedule_days": []}).schedule_days == (0, 1, 2, 3, 4, 5, 6)

    def test_days_outside_the_week_are_dropped(self):
        assert from_row({"schedule_days": [0, 9, 5]}).schedule_days == (0, 5)


class TestTheProviderDefersRatherThanGuesses:
    def test_an_unset_provider_is_empty_so_the_environment_still_decides(self):
        assert from_row({}).idea_provider == ""
        assert from_row({"idea_provider": ""}).idea_provider == ""

    def test_a_named_provider_wins(self):
        assert from_row({"idea_provider": "GEMINI"}).idea_provider == "gemini"

    def test_an_unknown_provider_defers_rather_than_drafting_with_the_other(self, caplog):
        # Quietly falling back to Claude would surface as an unexpected bill
        # rather than as an error.
        with caplog.at_level("WARNING"):
            assert from_row({"idea_provider": "gpt"}).idea_provider == ""


class TestTheBlocklist:
    def test_entries_are_lowercased_and_de_duplicated(self):
        assert mod.normalise_blocklist(["Course", "course", " GIVEAWAY "]) == ("course", "giveaway")

    def test_a_single_character_entry_is_ignored(self, caplog):
        # Matching is whole-word, but "a" is still a word in a great many
        # captions, and the failure mode is a run that rejects everything.
        with caplog.at_level("WARNING"):
            assert mod.normalise_blocklist(["a", "ok"]) == ("ok",)

    def test_whole_words_match_and_fragments_do_not(self):
        matcher = BlocklistMatcher(["course"])
        assert matcher.matched("my free course today") == "course"
        assert matcher.matched("COURSE, today only") == "course"
        assert matcher.matched("we race at the racecourse") is None

    def test_hashtags_are_searched_as_runs_of_text(self):
        # Nobody types "#free course", so a hashtag has to be matched as a
        # single token or a blocklist would never fire on the half of a
        # caption that is hashtags.
        matcher = BlocklistMatcher(["course"])
        assert matcher.matched("link in bio #freecourse") == "course"
        assert matcher.matched("#course") == "course"

    def test_the_longest_match_is_the_one_reported(self):
        matcher = BlocklistMatcher(["free", "free trial"])
        assert matcher.matched("get your free trial") == "free trial"

    def test_an_empty_blocklist_matches_nothing_and_is_falsey(self):
        matcher = BlocklistMatcher([])
        assert not matcher
        assert matcher.matched("anything at all") is None

    def test_regex_characters_are_literal(self):
        # An entry is a word, not a pattern. Compiling it as one would let a
        # settings field raise from inside the scout loop.
        assert BlocklistMatcher(["c++"]).matched("i teach c++ here") == "c++"


class TestRotation:
    TAGS = ("a", "b", "c", "d", "e")

    def test_rotation_off_scouts_everything_every_run(self):
        assert rotate(list(self.TAGS), 0, None) == (list(self.TAGS), 0)

    def test_a_slice_is_taken_from_the_cursor(self):
        assert rotate(list(self.TAGS), 0, 2) == (["a", "b"], 2)
        assert rotate(list(self.TAGS), 2, 2) == (["c", "d"], 4)

    def test_the_slice_wraps_around_the_end(self):
        assert rotate(list(self.TAGS), 4, 2) == (["e", "a"], 1)

    def test_consecutive_runs_cover_the_whole_list(self):
        # The claim rotation makes: a shorter run today does not mean a corner
        # of the audience is never scouted.
        seen: list[str] = []
        cursor = 0
        for _ in range(3):
            selected, cursor = rotate(list(self.TAGS), cursor, 2)
            seen.extend(selected)
        assert set(self.TAGS) <= set(seen)

    def test_asking_for_more_than_exists_scouts_each_tag_once(self):
        # Wrapping past the whole list would pay twice for the same feed.
        assert rotate(list(self.TAGS), 0, 99) == (list(self.TAGS), 0)

    def test_a_cursor_past_the_end_survives_the_list_being_shortened(self):
        # The list is edited freely in the app, so the cursor is taken modulo
        # its current length rather than reconciled on every save.
        assert rotate(["a", "b"], 97, 1) == (["b"], 0)

    def test_an_empty_list_rotates_to_nothing(self):
        assert rotate([], 3, 2) == ([], 0)


class TestTheRunBudget:
    def test_minutes_become_seconds_for_the_scout(self):
        assert ScoutControls(run_budget_minutes=30).run_budget_seconds == 1800.0

    def test_no_budget_stays_none_rather_than_becoming_zero(self):
        # Zero would read as "already exhausted" and end every run instantly.
        assert ScoutControls(run_budget_minutes=None).run_budget_seconds is None


class TestOneRunSOwnLength:
    """A length chosen for one run, over settings meant for every run.

    The button and the schedule want different things. Someone pressing
    "Generate more ideas" has a reason the schedule knows nothing about, and it
    must not become tomorrow morning's behaviour.
    """

    def test_no_row_leaves_the_settings_alone(self):
        controls = ScoutControls(hashtags_per_run=8, run_budget_minutes=60)
        assert mod.with_run_overrides(controls, None) is controls

    def test_a_scheduled_run_overrides_nothing(self):
        # The dispatcher inserts neither override, so both arrive null.
        controls = ScoutControls(hashtags_per_run=8, run_budget_minutes=60)
        row = {"override_run_budget_minutes": None, "override_hashtags_per_run": None}
        assert mod.with_run_overrides(controls, row) == controls

    def test_a_requested_length_wins_for_this_run(self):
        controls = ScoutControls(hashtags_per_run=None, run_budget_minutes=None)
        row = {"override_run_budget_minutes": 12, "override_hashtags_per_run": 4}
        merged = mod.with_run_overrides(controls, row)
        assert (merged.run_budget_minutes, merged.hashtags_per_run) == (12, 4)

    def test_each_override_is_independent(self):
        controls = ScoutControls(hashtags_per_run=8, run_budget_minutes=60)
        row = {"override_run_budget_minutes": None, "override_hashtags_per_run": 3}
        merged = mod.with_run_overrides(controls, row)
        assert merged.hashtags_per_run == 3
        assert merged.run_budget_minutes == 60      # untouched

    def test_it_changes_cost_and_never_the_bars(self):
        # The reason only these two are overridable: a shorter run must still
        # judge what it finds by the same standards, or two runs stop being
        # comparable and the queue means different things on different days.
        controls = ScoutControls(min_outlier_ratio=3.0, min_plays=50_000, max_video_age_days=14)
        row = {"override_run_budget_minutes": 10, "override_hashtags_per_run": 2}
        merged = mod.with_run_overrides(controls, row)
        assert merged.min_outlier_ratio == 3.0
        assert merged.min_plays == 50_000
        assert merged.max_video_age_days == 14

    def test_an_out_of_range_override_is_clamped_not_obeyed(self):
        # Reachable from a stale tab. The bounds are about what the pipeline
        # survives, not about which form the number was typed into.
        row = {"override_run_budget_minutes": 9000, "override_hashtags_per_run": 0}
        merged = mod.with_run_overrides(ScoutControls(), row)
        assert merged.run_budget_minutes == 240
        assert merged.hashtags_per_run == 1


class TestWhichPlatformsApifyScrapes:
    """`apify_platforms`, coerced the way `schedule_days` is.

    Same shape of problem: a list of names from a database column, where an
    unrecognised entry should be ignored and an empty result should mean the
    default rather than "nothing".
    """

    def test_both_platforms_by_default(self):
        # They answer the same question of different audiences, and an owner
        # who wants one can say so in a click.
        assert mod.from_row({}).apify_platforms == ("tiktok", "instagram")

    def test_one_platform_is_honoured(self):
        assert mod.from_row({"apify_platforms": ["instagram"]}).apify_platforms == ("instagram",)

    def test_the_order_is_fixed_rather_than_however_the_row_stored_it(self):
        # Two installs with the same platforms selected should scout them in
        # the same sequence, because a run budget can expire partway through
        # and "we ran out of time" should not mean a different platform each
        # time.
        assert mod.from_row({"apify_platforms": ["instagram", "tiktok"]}).apify_platforms == (
            "tiktok",
            "instagram",
        )

    def test_an_unscrapeable_platform_is_ignored_rather_than_fatal(self):
        # A row written by a build newer than this one should degrade to
        # scouting what it understands, not refuse to run.
        assert mod.from_row({"apify_platforms": ["tiktok", "threads"]}).apify_platforms == (
            "tiktok",
        )

    def test_an_empty_list_takes_the_default_rather_than_meaning_none(self):
        # A source that is selected and can never scout is indistinguishable
        # from a runner that has stopped working. "Neither" is said by choosing
        # a different source.
        assert mod.from_row({"apify_platforms": []}).apify_platforms == ("tiktok", "instagram")

    def test_a_list_of_nothing_recognisable_takes_the_default(self):
        assert mod.from_row({"apify_platforms": ["threads", "bluesky"]}).apify_platforms == (
            "tiktok",
            "instagram",
        )

    def test_a_value_that_is_not_a_list_takes_the_default(self):
        assert mod.from_row({"apify_platforms": "tiktok"}).apify_platforms == (
            "tiktok",
            "instagram",
        )

    def test_duplicates_collapse(self):
        assert mod.from_row({"apify_platforms": ["tiktok", "tiktok"]}).apify_platforms == (
            "tiktok",
        )

    def test_case_and_padding_are_forgiven(self):
        assert mod.from_row({"apify_platforms": [" TikTok "]}).apify_platforms == ("tiktok",)


class TestTheRetiredSource:
    def test_a_row_still_naming_tiktok_falls_back(self):
        # TikTok-Api refused every feed, so the source was retired. An install
        # that has not run the migration must degrade to scouting something.
        assert mod.from_row({"trend_source": "tiktok"}).trend_source == "google_trends"

    def test_the_three_live_sources_are_accepted(self):
        for name in ("apify", "google_trends", "youtube"):
            assert mod.from_row({"trend_source": name}).trend_source == name

    def test_only_the_video_sources_count_as_video_sources(self):
        assert mod.is_video_source("apify") is True
        assert mod.is_video_source("youtube") is True
        assert mod.is_video_source("google_trends") is False
