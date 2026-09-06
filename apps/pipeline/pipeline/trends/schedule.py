"""When a scheduled trend run is due.

The daily run used to be an EventBridge cron, which meant the one setting most
likely to need changing -- what time this happens -- was a `terraform apply`.
It is now a row, and this is the arithmetic that turns that row into "start one
now".

The whole file is deliberately pure. `dispatch_trend_runs` runs every minute,
so this is asked the same question sixty times an hour, and the interesting
cases -- the clock passing the slot, a paused schedule, a day that is not
enabled, a dispatcher that missed several ticks -- are all a matter of what
`now` happens to be. That is worth being able to test by passing a datetime
rather than by waiting until Tuesday.

UTC throughout. See the migration header for why there is no timezone setting.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline.trends.controls import ScoutControls

# How late a slot may still be started.
#
# There has to be some bound. The dispatcher can miss its ticks -- a Lambda
# throttle, a concurrent run holding the single in-flight slot for hours, an
# unapplied deployment -- and a slot with no expiry would then fire whenever
# the obstruction cleared. A 06:00 run starting at 21:40 because a stuck run
# was written off at 21:39 is not the schedule the owner set; it is a surprise
# hour of scraping at a time nobody chose.
#
# An hour is comfortably more than any ordinary hiccup at a one-minute cadence,
# and comfortably less than the gap to the next slot.
CATCHUP_MINUTES = 60


def postgres_dow(moment: datetime) -> int:
    """The weekday as `schedule_days` stores it: 0 = Sunday .. 6 = Saturday.

    Named and given a home because it is the one place two conventions meet.
    Postgres `extract(dow)` counts from Sunday, Python's `weekday()` counts
    from Monday, and silently mixing them puts every run on the wrong day --
    a bug that looks like a working schedule for six days out of seven.
    """
    return moment.isoweekday() % 7


def due_slot(
    now: datetime,
    controls: ScoutControls,
    *,
    catchup_minutes: int = CATCHUP_MINUTES,
) -> datetime | None:
    """The schedule slot that should have started by `now`, if any.

    Returns the slot's exact instant rather than a boolean, because that
    instant is the idempotency key: the dispatcher inserts it into
    `trend_runs.scheduled_for`, which carries a unique index, so asking this
    question repeatedly about the same slot can only ever start one run.

    None means nothing is due -- paused, wrong day, not yet, or too late to
    still be the run that was asked for.
    """
    if not controls.schedule_enabled:
        return None

    now = now.astimezone(timezone.utc)
    slot = now.replace(
        hour=controls.schedule_hour_utc,
        minute=controls.schedule_minute_utc,
        second=0,
        microsecond=0,
    )
    # Before today's slot, the most recent one was yesterday's. Checking
    # yesterday matters more than it looks: at 00:10 with a 23:30 schedule, the
    # slot that is due is on the previous date, and a same-day-only check would
    # never fire that schedule at all.
    if slot > now:
        slot -= timedelta(days=1)

    if postgres_dow(slot) not in controls.schedule_days:
        return None

    if now - slot > timedelta(minutes=catchup_minutes):
        return None

    return slot


def next_slot(now: datetime, controls: ScoutControls) -> datetime | None:
    """When the next run will start, for saying so out loud.

    Not used by the dispatcher -- it only ever asks about the past. This is for
    the app and the logs, so that a schedule change can be confirmed by reading
    a date rather than by waiting for it.
    """
    if not controls.schedule_enabled:
        return None

    now = now.astimezone(timezone.utc)
    slot = now.replace(
        hour=controls.schedule_hour_utc,
        minute=controls.schedule_minute_utc,
        second=0,
        microsecond=0,
    )
    if slot <= now:
        slot += timedelta(days=1)

    # At most a week: every reachable weekday is inside one, and an enabled
    # schedule always has at least one day, so this cannot fall through.
    for _ in range(7):
        if postgres_dow(slot) in controls.schedule_days:
            return slot
        slot += timedelta(days=1)
    return None
