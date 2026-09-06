"""When a scheduled run is due.

This replaced `cron(0 6 * * ? *)` in Terraform, and it has to be at least as
trustworthy as a cron was -- with the difference that it now answers to
settings the owner changes and to a dispatcher that fires every minute.

Three things here are easy to get wrong and silent when they are:

  * the weekday convention. Postgres counts from Sunday, Python from Monday.
    Mixing them puts every run on the wrong day, which looks like a working
    schedule six days in seven.
  * the slot before midnight. At 00:10 with a 23:30 schedule, the run that is
    due is on yesterday's date.
  * how late is too late. A slot with no expiry fires whenever an obstruction
    clears, which is an hour of scraping at a time nobody chose.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pipeline.trends.controls import ScoutControls
from pipeline.trends.schedule import CATCHUP_MINUTES, due_slot, next_slot, postgres_dow

# 2026-09-06 is a Sunday, so this week runs Sunday 6th to Saturday 12th and
# every weekday index below can be read off the date.
SUNDAY = datetime(2026, 9, 6, tzinfo=timezone.utc)


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def daily(hour: int = 6, minute: int = 0, **extra) -> ScoutControls:
    return ScoutControls(schedule_hour_utc=hour, schedule_minute_utc=minute, **extra)


class TestTheWeekdayConvention:
    @pytest.mark.parametrize(
        "day, expected",
        [(6, 0), (7, 1), (8, 2), (9, 3), (10, 4), (11, 5), (12, 6)],
    )
    def test_the_week_starts_on_sunday_as_postgres_counts_it(self, day, expected):
        assert postgres_dow(utc(day, 12)) == expected


class TestWhetherASlotIsDue:
    def test_a_slot_just_passed_is_due(self):
        assert due_slot(utc(8, 6, 1), daily(6)) == utc(8, 6)

    def test_the_exact_minute_is_due(self):
        assert due_slot(utc(8, 6, 0), daily(6)) == utc(8, 6)

    def test_a_minute_before_is_not(self):
        assert due_slot(utc(8, 5, 59), daily(6)) is None

    def test_the_slot_returned_is_the_instant_not_a_yes(self):
        # The instant is the idempotency key: the dispatcher stores it in
        # `scheduled_for`, which carries a unique index, so asking repeatedly
        # about one slot can only ever start one run.
        slot = due_slot(utc(8, 6, 30), daily(6))
        assert slot == utc(8, 6)
        assert due_slot(utc(8, 6, 45), daily(6)) == slot

    def test_the_minute_field_is_honoured(self):
        assert due_slot(utc(8, 7, 45), daily(7, 30)) == utc(8, 7, 30)
        assert due_slot(utc(8, 7, 15), daily(7, 30)) is None


class TestPausing:
    def test_a_paused_schedule_is_never_due(self):
        controls = daily(6, schedule_enabled=False)
        assert due_slot(utc(8, 6, 1), controls) is None
        # Including at the exact minute, and for the rest of the window.
        assert due_slot(utc(8, 6, 0), controls) is None
        assert due_slot(utc(8, 6, 30), controls) is None

    def test_pausing_does_not_change_when_it_would_have_been(self):
        # Unpausing must not need the time re-entering, so the hour and day
        # fields keep their meaning while paused.
        controls = daily(6, schedule_enabled=False)
        assert next_slot(utc(8, 12), controls) is None
        assert next_slot(utc(8, 12), daily(6)) == utc(9, 6)


class TestDays:
    def test_a_disabled_day_is_never_due(self):
        # Weekdays only: Monday(1) to Friday(5). The 12th is a Saturday.
        weekdays = daily(6, schedule_days=(1, 2, 3, 4, 5))
        assert due_slot(utc(12, 6, 5), weekdays) is None
        assert due_slot(utc(11, 6, 5), weekdays) == utc(11, 6)

    def test_a_single_day_a_week_works(self):
        sundays = daily(6, schedule_days=(0,))
        assert due_slot(utc(6, 6, 5), sundays) == utc(6, 6)
        assert due_slot(utc(7, 6, 5), sundays) is None

    def test_the_day_checked_is_the_slot_s_day_not_today(self):
        # A 23:30 Saturday schedule, asked at 00:10 on Sunday. The slot belongs
        # to Saturday, and checking today's weekday would consult the wrong
        # one -- accepting a run on a day the owner disabled, or refusing one
        # on a day they enabled.
        saturdays = daily(23, 30, schedule_days=(6,))
        assert due_slot(utc(13, 0, 10), saturdays) == utc(12, 23, 30)

        sundays = daily(23, 30, schedule_days=(0,))
        assert due_slot(utc(13, 0, 10), sundays) is None


class TestHowLateIsTooLate:
    def test_a_slot_inside_the_catch_up_window_still_fires(self):
        # The dispatcher can miss ticks -- a throttle, a run holding the single
        # in-flight lock, an unapplied deployment. Ordinary hiccups should not
        # cost a day of scouting.
        assert due_slot(utc(8, 6) + timedelta(minutes=CATCHUP_MINUTES - 1), daily(6)) == utc(8, 6)

    def test_a_slot_past_the_window_is_abandoned_rather_than_deferred(self):
        # The alternative is a 06:00 run starting at 21:40 because a stuck row
        # was written off at 21:39, which is not the schedule anyone set.
        assert due_slot(utc(8, 6) + timedelta(minutes=CATCHUP_MINUTES + 1), daily(6)) is None

    def test_the_window_is_configurable_for_a_caller_that_knows_better(self):
        late = utc(8, 8)
        assert due_slot(late, daily(6)) is None
        assert due_slot(late, daily(6), catchup_minutes=180) == utc(8, 6)


class TestSayingWhenTheNextOneIs:
    def test_the_next_slot_is_tomorrow_once_today_s_has_passed(self):
        assert next_slot(utc(8, 6, 1), daily(6)) == utc(9, 6)

    def test_the_next_slot_is_today_when_it_has_not(self):
        assert next_slot(utc(8, 5, 59), daily(6)) == utc(8, 6)

    def test_it_skips_forward_to_an_enabled_day(self):
        # Asked on Friday evening with a weekdays-only schedule: the answer is
        # Monday, not Saturday.
        weekdays = daily(6, schedule_days=(1, 2, 3, 4, 5))
        assert next_slot(utc(11, 20), weekdays) == utc(14, 6)

    def test_it_always_finds_a_day_because_a_schedule_always_has_one(self):
        for day in range(7):
            answer = next_slot(utc(8, 12), daily(6, schedule_days=(day,)))
            assert answer is not None
            assert postgres_dow(answer) == day


class TestTimezones:
    def test_a_non_utc_now_is_converted_rather_than_read_off(self):
        # The sweep hands us `datetime.now(timezone.utc)`, but a caller in the
        # app's timezone must not shift the schedule by its offset.
        tokyo = timezone(timedelta(hours=9))
        assert due_slot(utc(8, 6, 5).astimezone(tokyo), daily(6)) == utc(8, 6)
