"""The Google Trends flow, from a requested row to a finished one.

Written after a production incident in which the button did nothing. Every
individual piece had tests and every piece passed: the row was created, the
workflow ran hourly and exited green, Google Trends worked. What nothing
covered was the seam between them -- the sweeper that runs immediately before
the claim -- and that is where the run was being destroyed.

So these tests are deliberately end-to-end over the worker rather than over
`runner.run`: request a run, let the worker do what the scheduled job does,
and assert the row ends up `succeeded` with ideas in the queue. The bug this
file exists for cannot be seen from either side alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pipeline.trends import gtrends, runner, worker
from pipeline.trends import ideas as ideas_mod

KEYWORDS = ["invoice software", "bookkeeping software", "excel alternative"]


def ago(**kw: Any) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


class FakeFrame:
    """Enough of a pandas DataFrame for the scout."""

    def __init__(self, columns: dict[str, list[float]]) -> None:
        self._columns = columns
        self.empty = not columns

    def __contains__(self, key: str) -> bool:
        return key in self._columns

    def __getitem__(self, key: str) -> Any:
        values = self._columns[key]
        return type("Col", (), {"tolist": lambda self, v=values: list(v)})()


class FakeTrends:
    """pytrends, answering with a rising series for every term asked for."""

    def __init__(self, series: list[float] | None = None) -> None:
        # Flat then climbing, which is what `window_velocity` reads as a rise.
        self.series = series or ([10.0] * 15 + [40.0] * 5)
        self.asked: list[str] = []
        self._batch: list[str] = []

    def build_payload(self, kw_list, timeframe: str = "", geo: str = "", **_kw: Any) -> None:
        self._batch = list(kw_list)
        self.asked.extend(kw_list)

    def interest_over_time(self) -> FakeFrame:
        return FakeFrame({k: list(self.series) for k in self._batch})


class FakeIdeasTable:
    """`supa.raw.table("ideas")` for duplicate suppression."""

    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    def select(self, *_a: Any, **_kw: Any) -> FakeIdeasTable:
        return self

    def gte(self, *_a: Any, **_kw: Any) -> FakeIdeasTable:
        return self

    def execute(self) -> Any:
        return type("Res", (), {"data": [{"title": t} for t in self._titles]})()


class FakeRaw:
    def __init__(self, titles: list[str]) -> None:
        self._titles = titles

    def table(self, name: str) -> Any:
        assert name == "ideas"
        return FakeIdeasTable(self._titles)


class FakeSupa:
    """The database, doing what Postgres does for this flow.

    Deliberately models the *state machine* rather than just answering calls:
    `claim_trend_run` only claims a `requested` row and flips it to `running`,
    which is the invariant the incident turned on.
    """

    def __init__(
        self,
        *,
        requested_at: str | None = None,
        settings_row: dict[str, Any] | None = None,
        existing_titles: list[str] | None = None,
    ) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.raw = FakeRaw(existing_titles or [])
        self.inserted: list[dict[str, Any]] = []
        self.cursor_saves: list[int] = []
        self.stale_calls: list[dict[str, Any]] = []
        self._settings = settings_row if settings_row is not None else {
            "niche_brief": "we automate operations for small businesses",
            "trend_source": "google_trends",
            "trend_keywords": list(KEYWORDS),
            "schedule_enabled": False,
        }
        if requested_at is not None:
            self.rows["run-1"] = {
                "id": "run-1",
                "status": "requested",
                "trigger": "manual",
                "requested_at": requested_at,
                "started_at": None,
            }

    # -- what the worker calls ------------------------------------------

    def stale_trend_runs(self, *, running_hours: int, requested_minutes: int | None):
        self.stale_calls.append(
            {"running_hours": running_hours, "requested_minutes": requested_minutes}
        )
        now = datetime.now(timezone.utc)
        out = []
        for row in self.rows.values():
            if row["status"] == "running":
                if row["started_at"] and row["started_at"] < (
                    now - timedelta(hours=running_hours)
                ).isoformat():
                    out.append(row)
            elif (
                row["status"] == "requested"
                and requested_minutes is not None
                and row["requested_at"]
                < (now - timedelta(minutes=requested_minutes)).isoformat()
            ):
                out.append(row)
        return out

    def claim_trend_run(self):
        for row in self.rows.values():
            if row["status"] == "requested":
                row["status"] = "running"
                row["started_at"] = datetime.now(timezone.utc).isoformat()
                return dict(row)
        return None

    def finish_trend_run(self, run_id: str, **fields: Any) -> None:
        row = self.rows.get(run_id)
        if row is None or row["status"] not in ("requested", "running"):
            return
        row.update(fields)

    def scheduled_run_exists(self, _slot) -> bool:
        return True

    def open_scheduled_trend_run(self, _slot):
        return None

    def record_cancelled_progress(self, run_id: str, **_kw: Any) -> None:
        pass

    # -- what the runner calls ------------------------------------------

    def trend_settings(self):
        return self._settings

    def trend_run(self, run_id: str):
        return self.rows.get(run_id)

    def save_hashtag_cursor(self, cursor: int) -> None:
        self.cursor_saves.append(cursor)

    def trend_run_is_cancelled(self, _run_id: str) -> bool:
        return False

    def enabled_platforms(self) -> list[str]:
        return ["instagram"]

    def insert_ideas(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.inserted.extend(rows)
        return rows


class FakeCfg:
    niche_brief = ""
    idea_provider = "gemini"
    gemini_api_key = "test-key"
    tiktok_ms_token = None
    apify_token = ""
    youtube_api_key = ""
    trend_run_id = ""

    @property
    def hashtag_list(self) -> list[str]:
        return []

    @property
    def keyword_list(self) -> list[str]:
        return []


@pytest.fixture(autouse=True)
def google_trends_only(monkeypatch):
    """A working Google Trends, a working model, and no sleeping."""
    client = FakeTrends()

    monkeypatch.setattr(gtrends.time, "sleep", lambda _s: None)
    monkeypatch.setattr(runner, "settings", lambda: FakeCfg())
    monkeypatch.setattr(worker, "Supa", lambda: (_ for _ in ()).throw(AssertionError("inject supa")))

    # The real one builds a pytrends client, which would reach the network.
    monkeypatch.setattr(gtrends, "_client", lambda _config: client)

    # Drafting is not what these tests are about, but it must be exercised --
    # the incident's second failure was a run that claimed fine and then died
    # in the drafting step for a missing dependency.
    def fake_generate(signals, brief, *, count, provider):
        return [
            ideas_mod.ReelIdea(
                title=f"Idea about {s.keyword}",
                hook="A hook that earns two seconds",
                angle="What this reel argues.",
                rationale=f"{s.keyword} is rising at {s.ratio}x its own recent history.",
            )
            for s in signals[:count]
        ]

    monkeypatch.setattr(ideas_mod, "generate_ideas", fake_generate)
    monkeypatch.setattr(ideas_mod, "resolve_provider", lambda *_a, **_kw: "gemini")
    return client


def run_worker(supa: FakeSupa, monkeypatch) -> int:
    monkeypatch.setattr(worker, "Supa", lambda: supa)
    return worker.main()


class TestARequestIsClaimedAndCompleted:
    """The happy path, over the seam that broke."""

    def test_a_fresh_request_runs_to_completion(self, monkeypatch):
        supa = FakeSupa(requested_at=ago(seconds=30))

        assert run_worker(supa, monkeypatch) == 0

        row = supa.rows["run-1"]
        assert row["status"] == "succeeded"
        assert row["inserted"] == len(supa.inserted) > 0

    def test_the_scout_was_given_the_search_terms(self, monkeypatch, google_trends_only):
        # Google Trends reads `trend_keywords`, not the hashtag list. Handing it
        # hashtags produces a run that reports success and finds nothing.
        supa = FakeSupa(requested_at=ago(seconds=30))

        run_worker(supa, monkeypatch)

        assert google_trends_only.asked == KEYWORDS

    def test_ideas_reach_the_queue(self, monkeypatch):
        supa = FakeSupa(requested_at=ago(seconds=30))

        run_worker(supa, monkeypatch)

        assert len(supa.inserted) > 0
        assert all(row["title"] for row in supa.inserted)

    def test_the_breakdown_names_google_trends(self, monkeypatch):
        supa = FakeSupa(requested_at=ago(seconds=30))

        run_worker(supa, monkeypatch)

        rejections = supa.rows["run-1"]["rejections"]
        assert rejections["source"] == "google_trends"

        # The funnel has to add up or it cannot be read: every candidate seen
        # either survived to drafting or is counted against exactly one
        # video-level stage.
        video_drops = sum(
            stage["dropped"] for stage in rejections["stages"] if stage["level"] == "video"
        )
        assert video_drops + rejections["surfaced"] == rejections["seen"]

        # And the labels are the ones this source can actually justify. A
        # Google Trends run reporting "under your minimum view count" would be
        # pointing at a filter that had nothing to do with it.
        labels = {stage["key"]: stage["label"] for stage in rejections["stages"]}
        assert labels["too_few_plays"] == "Below your minimum search interest"
        assert "not applicable to a search trend" in labels["too_old"]


class TestAnOldRequestIsRunRatherThanReaped:
    """The incident.

    The scout runs hourly on a CI cron. The sweeper it calls first was written
    for a dispatcher that runs every minute, and gave up on any request older
    than ten. Those two facts together meant a request made more than ten
    minutes before the cron fired was written off a few lines before the same
    process would have claimed it -- so the button worked for ten minutes in
    every sixty and reported "gave up on a run left requested with nothing
    running it" for the other fifty.
    """

    def test_a_request_older_than_the_old_threshold_still_runs(self, monkeypatch):
        # Forty minutes: a button pressed just after one hourly scout, read by
        # the next one. This is the case that used to fail.
        supa = FakeSupa(requested_at=ago(minutes=40))

        assert run_worker(supa, monkeypatch) == 0

        assert supa.rows["run-1"]["status"] == "succeeded"
        assert len(supa.inserted) > 0

    def test_a_request_from_hours_ago_still_runs(self, monkeypatch):
        # There is no age at which abandoning the owner's request is better
        # than running it: the queue is the thing they asked for.
        supa = FakeSupa(requested_at=ago(hours=5))

        run_worker(supa, monkeypatch)

        assert supa.rows["run-1"]["status"] == "succeeded"

    def test_the_sweeper_is_told_not_to_reap_requests(self, monkeypatch):
        # Asserted on the call rather than only on the outcome, because the
        # outcome would also pass if the threshold were merely raised -- and a
        # raised threshold is the same bug waiting for a slower cron.
        supa = FakeSupa(requested_at=ago(minutes=40))

        run_worker(supa, monkeypatch)

        assert supa.stale_calls == [{"running_hours": 3, "requested_minutes": None}]

    def test_no_run_is_written_off_on_the_way_past(self, monkeypatch):
        supa = FakeSupa(requested_at=ago(minutes=40))

        run_worker(supa, monkeypatch)

        assert supa.rows["run-1"].get("error") is None


class TestWhatTheSweeperStillDoes:
    """Removing the request sweep must not remove the one that matters.

    A claimed run whose machine vanished holds the single in-flight lock, and
    that really does disable the button until something clears it. On a CI
    runner it is the more likely failure, not the less.
    """

    def test_a_run_abandoned_mid_scout_is_still_written_off(self, monkeypatch):
        supa = FakeSupa()
        supa.rows["dead"] = {
            "id": "dead",
            "status": "running",
            "trigger": "manual",
            "requested_at": ago(hours=6),
            "started_at": ago(hours=5),
        }

        run_worker(supa, monkeypatch)

        assert supa.rows["dead"]["status"] == "failed"
        assert "gave up" in supa.rows["dead"]["error"]

    def test_a_run_still_within_its_budget_is_left_alone(self, monkeypatch):
        supa = FakeSupa()
        supa.rows["live"] = {
            "id": "live",
            "status": "running",
            "trigger": "manual",
            "requested_at": ago(minutes=30),
            "started_at": ago(minutes=29),
        }

        run_worker(supa, monkeypatch)

        assert supa.rows["live"]["status"] == "running"


class TestDoingNothingQuickly:
    def test_no_request_means_no_scouting(self, monkeypatch, google_trends_only):
        supa = FakeSupa()

        assert run_worker(supa, monkeypatch) == 0

        assert google_trends_only.asked == []
        assert supa.inserted == []


class TestFailureIsRecordedRatherThanLost:
    def test_a_scout_that_raises_leaves_the_row_failed_not_in_flight(self, monkeypatch):
        # Without this the in-flight lock is held by a run that has already
        # crashed, and the button stays shut until the sweeper runs an hour on.
        supa = FakeSupa(requested_at=ago(seconds=30))

        def exploding(_config):
            raise RuntimeError("No module named 'google'")

        monkeypatch.setattr(gtrends, "scout", exploding)

        assert run_worker(supa, monkeypatch) == 1

        row = supa.rows["run-1"]
        assert row["status"] == "failed"
        assert "No module named" in row["error"]
