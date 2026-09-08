"""Uploading a finished cut into Gate 2.

The claim this feature makes is that the publishing half of the pipeline never
cared where a video came from, and that an upload can therefore enter at the
quality check and use all of it unchanged. These tests are that claim, written
down: the entrance is new, and everything after it must be provably the same
code doing the same thing.

Three of them are about the ways an upload could quietly become a second
pipeline rather than a second entrance -- the graph converging on
`generate_copy`, the driver never reaching a render step, and `script` staying
empty on a production nothing narrated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.activities import publish, upload
from pipeline.activities.upload import UploadUnreadable
from pipeline.clients.postiz import PostizClient
from pipeline.clients.supa import Supa, SupaError
from pipeline.driver import engine
from pipeline.driver import graph as g
from pipeline.models import SocialMetadata
from pipeline.qc.probe import MediaInfo
from tests.conftest import FakeSupa

PRODUCTION_ID = "11111111-1111-4111-8111-111111111111"


def media_info(**overrides) -> MediaInfo:
    base = {
        "duration_s": 32.0,
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "has_audio": True,
        "video_codec": "h264",
        "audio_codec": "aac",
        "size_bytes": 12_000_000,
    }
    return MediaInfo(**{**base, **overrides})


class FakeUploadSupa(FakeSupa):
    """FakeSupa plus the storage and row reads the upload path needs."""

    def __init__(self, row: dict | None = None, **kw) -> None:
        super().__init__(**kw)
        self._row = row or {}
        self.downloaded: list[str] = []
        self.uploaded: list[str] = []
        self.signed: list[tuple[str, int]] = []
        self.updates: list[dict] = []
        self.platforms: list[str] = ["instagram"]

    def production(self, production_id: str) -> dict:
        self.calls.append(("production", production_id))
        return {
            "id": production_id,
            "status": self._status,
            "run_state": self._run_state,
            "source": "generated",
            **self._row,
        }

    def idea(self, idea_id: str) -> dict:
        self.calls.append(("idea", idea_id))
        return {"id": idea_id, "title": "An idea the scout found"}

    def enabled_platforms(self) -> list[str]:
        return list(self.platforms)

    def download_render(self, key: str, dest: Path) -> Path:
        self.calls.append(("download_render", key))
        self.downloaded.append(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not really a video")
        return dest

    def upload_render(self, production_id: str, local: Path, filename: str = "final.mp4") -> str:
        key = f"{production_id}/{filename}"
        self.calls.append(("upload_render", key))
        self.uploaded.append(key)
        return key

    def signed_render_url(self, key: str, expires_in: int = 3600) -> str:
        self.signed.append((key, expires_in))
        return f"https://signed.example/{key}"

    def update_production(self, production_id: str, **fields) -> dict:
        self.updates.append(fields)
        return super().update_production(production_id, **fields)

    @property
    def last_update(self) -> dict:
        return self.updates[-1]


@pytest.fixture
def measured(monkeypatch):
    """Stub the four ffmpeg/ffprobe shell-outs, leaving the scoring real."""
    monkeypatch.setattr(upload.probe_mod, "probe", lambda path: media_info())
    monkeypatch.setattr(upload.probe_mod, "frozen_seconds", lambda path: 0.0)
    monkeypatch.setattr(upload.probe_mod, "scene_change_count", lambda path: 12)
    monkeypatch.setattr(upload.probe_mod, "mean_volume_db", lambda path: -18.0)
    monkeypatch.setattr(upload, "_extract_poster", lambda video, dest: dest)


# ---------------------------------------------------------------------------
# The entrance
# ---------------------------------------------------------------------------


class TestTheGraphHasOneExitAndTwoEntrances:
    def test_an_upload_converges_on_the_copy_step(self):
        # The whole design in one assertion. If this ever routed anywhere else,
        # an upload would be a second pipeline rather than a second way in, and
        # every fix after Gate 2 would have to be made twice.
        assert g.GRAPH["check_upload"].next == "generate_copy"

    def test_nothing_routes_into_it(self):
        # `check_upload` is reachable only by being written into `run_state` at
        # insert, by `create_upload_production`. A step that fell into it would
        # be a generated production skipping its own quality check.
        targets = {s.next for s in g.GRAPH.values() if isinstance(s.next, str)}
        assert "check_upload" not in targets

    def test_a_file_that_cannot_be_read_parks_rather_than_retrying_forever(self):
        assert g.GRAPH["check_upload"].catch == "parked"

    def test_it_never_reaches_a_step_that_spends_money(self, monkeypatch):
        # The acceptance criterion, driven rather than asserted about the graph:
        # start a row where the RPC puts it and take one step. Nothing may touch
        # write_script, submit_render or poll_render on the way.
        monkeypatch.setitem(
            engine.ACTIVITIES,
            "upload.check_upload",
            lambda event, supa: {"storage_key": "k", "qc_passed": True},
        )
        monkeypatch.setattr(
            engine,
            "settings",
            lambda: type("Cfg", (), {"publishing_enabled": False, "lease_seconds": 900})(),
        )
        supa = FakeUploadSupa()

        result = engine.advance(
            {
                "id": PRODUCTION_ID,
                "status": "queued",
                "run_state": {"step": "check_upload", "storage_key": "k"},
            },
            supa,
        )

        assert result["step"] == "generate_copy"
        touched = {step for step, _ in supa.event_trail}
        assert touched.isdisjoint({"write_script", "await_script", "submit_render", "poll_render"})


    def test_the_check_announces_itself_like_every_other_step(self, monkeypatch):
        # An upload is inserted straight onto `check_upload`, so the engine's
        # old "has this row got a step yet?" test read it as already announced
        # and the whole quality check ran with nothing in the log until it
        # finished.
        monkeypatch.setitem(
            engine.ACTIVITIES, "upload.check_upload", lambda event, supa: {"qc_passed": True}
        )
        monkeypatch.setattr(
            engine,
            "settings",
            lambda: type("Cfg", (), {"publishing_enabled": False, "lease_seconds": 900})(),
        )
        supa = FakeUploadSupa()

        engine.advance(
            {"id": PRODUCTION_ID, "status": "queued", "run_state": {"step": "check_upload"}}, supa
        )

        assert ("check_upload", "started") in supa.event_trail

    def test_the_worker_asks_for_the_hour_the_step_declares(self, monkeypatch):
        # `claim_production` hands out one lease length for every row, so the
        # step's own `lease_seconds` is only true if something asks for it. It
        # matters most here: the file is a person's, it can be 500 MB, and a
        # lease that lapses mid-check hands the row to a second worker and
        # eventually parks it as "the driver died on this step".
        monkeypatch.setitem(
            engine.ACTIVITIES, "upload.check_upload", lambda event, supa: {"qc_passed": True}
        )
        monkeypatch.setattr(
            engine,
            "settings",
            lambda: type("Cfg", (), {"publishing_enabled": False, "lease_seconds": 900})(),
        )
        supa = FakeUploadSupa()

        engine.advance(
            {"id": PRODUCTION_ID, "status": "queued", "run_state": {"step": "check_upload"}}, supa
        )

        assert supa.extended == [(PRODUCTION_ID, 3600)]


# ---------------------------------------------------------------------------
# The quality check
# ---------------------------------------------------------------------------


class TestTheUploadedFileIsChecked:
    def test_it_reads_the_object_the_browser_already_put_in_the_bucket(self, measured):
        supa = FakeUploadSupa()

        upload.check_upload({"production_id": PRODUCTION_ID, "storage_key": "k/final.mp4"}, supa)

        assert supa.downloaded == ["k/final.mp4"]

    def test_the_key_defaults_to_where_every_other_step_looks(self, measured):
        # `publish` falls back to exactly this string, so the two must agree.
        supa = FakeUploadSupa()

        result = upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        assert result["storage_key"] == f"{PRODUCTION_ID}/final.mp4"
        assert supa.downloaded == [f"{PRODUCTION_ID}/final.mp4"]

    def test_it_writes_the_report_the_review_screen_renders(self, measured):
        supa = FakeUploadSupa()

        upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        report = supa.last_update["qc"]
        assert report["passed"] is True
        assert {c["key"] for c in report["checks"]} >= {
            "container",
            "duration",
            "aspect",
            "audio",
            "captions",
            "slideshow_risk",
        }
        assert supa.last_update["duration_seconds"] == 32.0

    def test_it_returns_what_gate_2_branches_on(self, measured):
        # `open_gate2` reads `qc_passed` off the payload to decide between
        # awaiting_review and qc_failed. An upload has to supply it under the
        # same name or every upload would silently arrive as a pass.
        supa = FakeUploadSupa()

        result = upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        assert result["qc_passed"] is True
        assert isinstance(result["slideshow_risk"], float)

    def test_a_broken_file_arrives_flagged_rather_than_hidden(self, monkeypatch):
        monkeypatch.setattr(
            upload.probe_mod, "probe", lambda path: media_info(duration_s=2.0, has_audio=False)
        )
        monkeypatch.setattr(upload.probe_mod, "frozen_seconds", lambda path: 2.0)
        monkeypatch.setattr(upload.probe_mod, "scene_change_count", lambda path: 0)
        monkeypatch.setattr(upload.probe_mod, "mean_volume_db", lambda path: None)
        monkeypatch.setattr(upload, "_extract_poster", lambda video, dest: dest)
        supa = FakeUploadSupa()

        result = upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        # Flagged, not blocked: it still goes to a person, who can override.
        assert result["qc_passed"] is False

    def test_it_does_not_claim_captions_it_cannot_see(self, measured):
        # Every other lane knows whether it burned subtitles in, because it is
        # what burned them. Captions in an uploaded file are pixels, so the
        # honest answer is a warn -- never a pass we cannot support.
        supa = FakeUploadSupa()

        upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        captions = next(c for c in supa.last_update["qc"]["checks"] if c["key"] == "captions")
        assert captions["status"] == "warn"

    def test_a_missing_poster_does_not_block_review(self, monkeypatch, measured):
        def no_poster(video, dest):
            raise RuntimeError("ffmpeg is not installed")

        monkeypatch.setattr(upload, "_extract_poster", no_poster)
        supa = FakeUploadSupa()

        result = upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        assert result["qc_passed"] is True
        assert supa.last_update["thumbnail_url"] is None


    def test_a_missing_file_fails_the_step_rather_than_the_plumbing(self, measured):
        # The distinction decides whether this ever stops. `SupaError` is
        # reclassified as infrastructure by the engine, which consumes no
        # attempt and never reaches the graph's catch -- so a file that is not
        # there would be retried every five seconds for thirty days. Raised as
        # the step's own failure it gets three attempts and then parks, in
        # front of the person who can re-upload it.
        class Missing(FakeUploadSupa):
            def download_render(self, key, dest):
                raise SupaError(f"could not download {key}: HTTP 404")

        with pytest.raises(UploadUnreadable):
            upload.check_upload({"production_id": PRODUCTION_ID}, Missing())

    def test_and_the_engine_routes_that_to_the_graph(self, monkeypatch):
        # The other half of the same guarantee, driven: an `UploadUnreadable`
        # must consume an attempt, not bounce.
        def missing(event, supa):
            raise upload.UploadUnreadable("no such object")

        monkeypatch.setitem(engine.ACTIVITIES, "upload.check_upload", missing)
        monkeypatch.setattr(
            engine,
            "settings",
            lambda: type("Cfg", (), {"publishing_enabled": False, "lease_seconds": 900})(),
        )
        supa = FakeUploadSupa()

        result = engine.advance(
            {"id": PRODUCTION_ID, "status": "queued", "run_state": {"step": "check_upload"}}, supa
        )

        assert result["outcome"] == "retry"
        assert supa.last_saved["attempts"] == {"check_upload": 1}

    def test_the_review_url_outlives_a_weekend(self, measured):
        # Seven days, the same as `fetch_and_qc`. A reviewer who opens the queue
        # on Monday must not find a dead video element.
        supa = FakeUploadSupa()

        upload.check_upload({"production_id": PRODUCTION_ID}, supa)

        assert (f"{PRODUCTION_ID}/final.mp4", 7 * 24 * 3600) in supa.signed


# ---------------------------------------------------------------------------
# Copy, from an input that is not a script
# ---------------------------------------------------------------------------


class FakeTextGenerator:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def social_metadata(self, *, platform, video_subject, video_script, language):
        self.calls.append(
            {"platform": platform, "subject": video_subject, "script": video_script}
        )
        return SocialMetadata(title=video_subject, caption=video_script[:100], hashtags=["ai"])


class TestCopyForAnUpload:
    def test_it_is_written_from_the_title_and_brief(self):
        supa = FakeUploadSupa(
            row={
                "source": "upload",
                "title": "Three quoting mistakes",
                "brief": "A walk-through of the three things that lose the job.",
                "platform_copy": {},
            }
        )
        mpt = FakeTextGenerator()

        publish.generate_platform_copy({"production_id": PRODUCTION_ID}, supa, mpt)

        assert mpt.calls[0]["subject"] == "Three quoting mistakes"
        assert mpt.calls[0]["script"].startswith("A walk-through")

    def test_it_never_looks_for_an_idea_that_does_not_exist(self):
        # `productions.idea_id` is null on an upload. Reading it would raise
        # inside the activity and park a perfectly good video.
        supa = FakeUploadSupa(
            row={"source": "upload", "title": "T", "brief": "B", "platform_copy": {}}
        )

        publish.generate_platform_copy({"production_id": PRODUCTION_ID}, supa, FakeTextGenerator())

        assert "idea" not in supa.call_names

    def test_the_brief_is_never_laundered_into_the_script_column(self):
        # `script` is the narration and the thing `script_approved_at` refers
        # to. Writing a brief there would put words in front of a reviewer that
        # nobody spoke and nobody approved.
        supa = FakeUploadSupa(
            row={"source": "upload", "title": "T", "brief": "B", "platform_copy": {}}
        )

        publish.generate_platform_copy({"production_id": PRODUCTION_ID}, supa, FakeTextGenerator())

        assert "script" not in supa.last_update

    def test_a_generated_production_still_writes_from_its_idea_and_script(self):
        supa = FakeUploadSupa(
            row={"source": "generated", "idea_id": "i1", "script": "The narration.", "platform_copy": {}}
        )
        mpt = FakeTextGenerator()

        publish.generate_platform_copy({"production_id": PRODUCTION_ID}, supa, mpt)

        assert mpt.calls[0]["subject"] == "An idea the scout found"
        assert supa.last_update["script"] == "The narration."


# ---------------------------------------------------------------------------
# The disclosure only the uploader can answer
# ---------------------------------------------------------------------------


class TestAiDisclosure:
    """`settings_for` builds no state of its own, so it is exercised unbound --
    constructing a real `PostizClient` would demand a base URL and an API key
    for a question about a dictionary."""

    def build(self, **kw) -> dict:
        return PostizClient.settings_for(
            PostizClient.__new__(PostizClient), "tiktok", title="A title", **kw
        )

    def test_anything_we_rendered_discloses_by_default(self):
        # The default is true because it is true of everything this pipeline
        # renders, and because the two mistakes are not symmetrical: a label
        # nobody minds, against a platform sanction.
        assert self.build()["video_made_with_ai"] is True

    def test_an_uploader_may_say_it_is_not_ai(self):
        assert self.build(made_with_ai=False)["video_made_with_ai"] is False


# ---------------------------------------------------------------------------
# An upload nobody picked up
# ---------------------------------------------------------------------------


class FakeTable:
    """Just enough of the PostgREST builder for `unstarted_productions`."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def select(self, *_args, **_kw):
        return self

    def eq(self, *_args, **_kw):
        return self

    def is_(self, *_args, **_kw):
        return self

    def execute(self):
        return type("Res", (), {"data": self._rows})()


class TestTheReconcilerCanSeeAnAbandonedUpload:
    """The gap this closes is silent, which is why it needs a test.

    A generated production is recognised as never-claimed by having no step at
    all. An upload is inserted *with* one, so under the original rule it was
    invisible to the reconciler: a file uploaded while no worker was running
    would sit at `check_upload` forever, never parked and never reported.
    """

    def unstarted(self, rows: list[dict]) -> list[str]:
        supa = Supa.__new__(Supa)
        supa._c = type("C", (), {"table": staticmethod(lambda _name: FakeTable(rows))})()
        return [row["id"] for row in supa.unstarted_productions()]

    def test_a_generated_row_with_no_step_still_counts(self):
        assert self.unstarted([{"id": "g1", "source": "generated", "run_state": {}}]) == ["g1"]

    def test_an_upload_nobody_claimed_counts_too(self):
        rows = [{"id": "u1", "source": "upload", "run_state": {"step": "check_upload"}}]
        assert self.unstarted(rows) == ["u1"]

    def test_an_upload_a_worker_is_holding_does_not(self):
        # The check is minutes of ffmpeg on a large file and the step takes an
        # hour's lease before it starts. Parking mid-check would abandon work
        # that is actually happening.
        future = (datetime.now(timezone.utc) + timedelta(minutes=50)).isoformat()
        rows = [
            {
                "id": "u1",
                "source": "upload",
                "run_state": {"step": "check_upload"},
                "lease_expires_at": future,
            }
        ]
        assert self.unstarted(rows) == []

    def test_an_expired_lease_means_the_worker_went_away(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        rows = [
            {
                "id": "u1",
                "source": "upload",
                "run_state": {"step": "check_upload"},
                "lease_expires_at": past,
            }
        ]
        assert self.unstarted(rows) == ["u1"]

    def test_an_upload_that_has_moved_on_is_not_unstarted(self):
        rows = [{"id": "u1", "source": "upload", "run_state": {"step": "generate_copy"}}]
        assert self.unstarted(rows) == []
