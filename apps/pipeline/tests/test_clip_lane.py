"""The clipping lane: captions, the cut, and the phases before the gate.

Three things are worth pinning down here, and they are the three that would fail
silently:

  * **The captions say what the owner approved.** Editing the script at the gate
    has to reach the burned-in text, or the gate on this lane is decoration.
  * **The captions are on the right frame.** Segment times are absolute and the
    cut starts at zero, so every caption has to be rebased or the whole track
    sits ahead of the words.
  * **A failure before the gate costs nothing.** No render exists, so a source
    that fails must stay retryable and a production that cannot find its
    recording must release its render claim rather than strand itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import assemble, clips
from pipeline.activities import clips as clip_activities
from pipeline.activities import render, script
from pipeline.models import ClipSourceStatus, Transcript, TranscriptSegment
from tests.conftest import FakeSupa


@pytest.fixture(autouse=True)
def clip_settings(monkeypatch):
    """The lane's own numbers, without a real environment.

    `monkeypatch.setattr(module, "settings", ...)` is the idiom the driver and
    source-lane tests use: `Settings` requires a Supabase URL and a service-role
    key at construction, deliberately, and a unit test of a phase should not
    have to invent either.
    """

    class Cfg:
        clip_lease_seconds = 3600
        clip_transcribe_budget_seconds = 2700
        clip_max_source_seconds = 4 * 3600
        source_video_url_ttl_seconds = 6 * 3600
        fal_transcribe_model = "fal-ai/whisper"

    monkeypatch.setattr(clip_activities, "settings", lambda: Cfg())
    return Cfg()


def transcript_of(*spans: tuple[float, float, str]) -> Transcript:
    return Transcript(
        text=" ".join(s[2] for s in spans),
        segments=[TranscriptSegment(start=a, end=b, text=t) for a, b, t in spans],
    )


SPOKEN = transcript_of(
    (100.0, 104.0, "Your quote went out on Friday."),
    (104.0, 108.0, "By Monday they had stopped replying."),
    (108.0, 112.0, "Here is what to send instead."),
)


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


def test_an_unedited_script_uses_the_segments_verbatim():
    """The common case: every caption on the frame the speaker said it."""
    approved = SPOKEN.excerpt(100.0, 112.0)
    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script=approved)

    assert [(s["start"], s["end"], s["text"]) for s in got] == [
        (100.0, 104.0, "Your quote went out on Friday."),
        (104.0, 108.0, "By Monday they had stopped replying."),
        (108.0, 112.0, "Here is what to send instead."),
    ]


def test_whitespace_differences_still_count_as_unedited():
    """`clean_script` trims, and the excerpt was joined with single spaces."""
    approved = "  Your quote went out on Friday.   By Monday they had stopped replying.\n\nHere is what to send instead.  "
    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script=approved)
    assert [s["text"] for s in got] == [
        "Your quote went out on Friday.",
        "By Monday they had stopped replying.",
        "Here is what to send instead.",
    ]


def test_a_corrected_word_reaches_the_burned_in_caption():
    """This is what the script gate is for on this lane.

    If an edit did not reach the captions, correcting a mis-transcribed name at
    the gate would fix a copy of the text nobody ever sees.
    """
    approved = (
        "Your quote went out on Friday. "
        "By Monday they had stopped replying. "
        "Here is what to send instead."
    ).replace("Friday", "Thursday")

    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script=approved)
    assert "Thursday" in " ".join(s["text"] for s in got)
    assert "Friday" not in " ".join(s["text"] for s in got)


def test_an_edit_keeps_every_word_and_every_segment_timing():
    """Redistribution must not lose words or invent timings."""
    approved = "One two three four five six seven eight nine"
    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script=approved)

    assert " ".join(s["text"] for s in got) == approved
    assert [(s["start"], s["end"]) for s in got] == [(100.0, 104.0), (104.0, 108.0), (108.0, 112.0)]


def test_a_rewrite_shorter_than_the_segment_count_still_spreads_out():
    """Otherwise every word lands in the first caption and the clip goes silent."""
    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script="Two words")
    assert " ".join(s["text"] for s in got) == "Two words"
    assert len(got) == 2


def test_captions_for_a_range_with_no_speech_are_empty():
    assert clips.caption_segments(SPOKEN, 500.0, 520.0, script="anything") == []


def test_no_script_falls_back_to_the_transcript():
    got = clips.caption_segments(SPOKEN, 100.0, 112.0, script=None)
    assert [s["text"] for s in got] == [
        "Your quote went out on Friday.",
        "By Monday they had stopped replying.",
        "Here is what to send instead.",
    ]


# ---------------------------------------------------------------------------
# The SRT, and the offset that is easy to forget
# ---------------------------------------------------------------------------


def test_the_srt_is_rebased_onto_the_clip_timeline(tmp_path: Path):
    """`cut` produces a file starting at zero; the segments are absolute.

    Without the offset every caption on this clip would appear 100 seconds in,
    which on a twelve-second clip means no captions at all.
    """
    segments = clips.caption_segments(SPOKEN, 100.0, 112.0, script=None)
    out = assemble.srt_from_segments(segments, tmp_path / "c.srt", offset=100.0)

    assert out is not None
    body = out.read_text()
    assert "00:00:00,000 --> 00:00:04,000" in body
    assert "00:00:08,000 --> 00:00:12,000" in body
    assert "00:01:40" not in body


def test_srt_from_segments_accepts_plain_dicts(tmp_path: Path):
    out = assemble.srt_from_segments(
        [{"start": 2.0, "end": 4.5, "text": "Hello."}], tmp_path / "c.srt"
    )
    assert out is not None
    assert "00:00:02,000 --> 00:00:04,500" in out.read_text()


def test_srt_drops_segments_that_end_before_the_clip_starts(tmp_path: Path):
    out = assemble.srt_from_segments(
        [
            {"start": 0.0, "end": 5.0, "text": "before the clip"},
            {"start": 20.0, "end": 25.0, "text": "inside it"},
        ],
        tmp_path / "c.srt",
        offset=20.0,
    )
    assert out is not None
    assert "inside it" in out.read_text()
    assert "before the clip" not in out.read_text()


def test_srt_returns_none_when_nothing_survives(tmp_path: Path):
    """A clip with no captions is still a clip; the QC report says so."""
    assert assemble.srt_from_segments([], tmp_path / "c.srt") is None
    assert assemble.srt_from_segments([{"text": "", "start": 1, "end": 2}], tmp_path / "c.srt") is None


def test_cut_refuses_a_range_that_does_not_run_forwards(tmp_path: Path):
    """Checked before ffmpeg, which would produce an empty file instead."""
    with pytest.raises(assemble.AssemblyError, match="run forwards"):
        assemble.cut(tmp_path / "in.mp4", 50.0, 20.0, tmp_path / "out.mp4")


# ---------------------------------------------------------------------------
# The pre-gate phases
# ---------------------------------------------------------------------------


class ClipSupa(FakeSupa):
    """A FakeSupa that holds one `clip_sources` row and its candidates."""

    def __init__(self, source: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.source: dict[str, Any] = {
            "id": "src-1",
            "storage_key": "sources/clips/u1/talk.mp4",
            "filename": "talk.mp4",
            "status": ClipSourceStatus.UPLOADED.value,
            "transcript": {},
            "candidate_cap": 6,
            "duration_seconds": None,
            "leased_by": "clips",
            "lease_expires_at": "2026-09-08T13:00:00Z",
            "style_preset_id": "s-clip",
            **(source or {}),
        }
        self.source_updates: list[dict[str, Any]] = []
        self.inserted: list[dict[str, Any]] = []
        self.failed: list[tuple[str, str]] = []

    def claim_clip_source(self, worker: str, lease_seconds: int) -> dict[str, Any] | None:
        self.calls.append(("claim_clip_source", worker))
        return dict(self.source)

    def clip_source(self, source_id: str) -> dict[str, Any]:
        self.calls.append(("clip_source", source_id))
        return dict(self.source)

    def update_clip_source(self, source_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_clip_source", fields.get("status")))
        self.source_updates.append(fields)
        self.source.update(fields)
        return dict(self.source)

    def fail_clip_source(self, source_id: str, error: str) -> dict[str, Any]:
        self.calls.append(("fail_clip_source", error))
        self.failed.append((source_id, error))
        self.source.update({"status": ClipSourceStatus.FAILED.value, "error": error})
        return dict(self.source)

    def insert_clip_candidates(self, source_id: str, rows: list[dict[str, Any]]):
        self.calls.append(("insert_clip_candidates", len(rows)))
        self.inserted = rows
        return rows


def test_proposing_writes_the_candidates_before_opening_the_gate(monkeypatch):
    """Order matters: the reverse shows an owner an empty list to decide on."""
    supa = ClipSupa({"transcript": SPOKEN.model_dump(), "duration_seconds": 200.0})
    monkeypatch.setattr(
        clips, "propose", lambda *a, **k: [{"rank": 1, "start_seconds": 100.0,
                                           "end_seconds": 112.0, "title": "The quote",
                                           "transcript_excerpt": "words"}]
    )

    clip_activities.propose_candidates(supa.source, supa)

    assert supa.call_names == [
        "insert_clip_candidates",
        "update_clip_source",
    ]
    assert supa.source["status"] == ClipSourceStatus.AWAITING_PICKS.value


def test_a_recording_with_no_worthwhile_moment_resolves_rather_than_failing(monkeypatch):
    """Not a fault, so not retryable: another call would reach the same answer."""
    supa = ClipSupa({"transcript": SPOKEN.model_dump()})
    monkeypatch.setattr(clips, "propose", lambda *a, **k: [])

    clip_activities.propose_candidates(supa.source, supa)

    assert supa.source["status"] == ClipSourceStatus.RESOLVED.value
    assert supa.inserted == []
    assert supa.failed == []


def test_a_failing_phase_puts_the_reason_on_the_row(monkeypatch):
    """`clip_sources_failure_is_explained` refuses a failed row with no error."""
    supa = ClipSupa({"status": ClipSourceStatus.PROPOSING.value,
                     "transcript": SPOKEN.model_dump()})

    def boom(*a: Any, **k: Any):
        raise RuntimeError("the model was unreachable")

    monkeypatch.setattr(clips, "propose", boom)
    result = clip_activities.advance_clip_sources(supa)

    assert result == {"failed": "src-1"}
    assert supa.failed and "unreachable" in supa.failed[0][1]


def test_a_transcript_with_no_segments_fails_rather_than_proposing(monkeypatch):
    """A model with no timecodes to choose between would invent some."""
    supa = ClipSupa({"status": ClipSourceStatus.PROPOSING.value, "transcript": {}})
    result = clip_activities.advance_clip_sources(supa)

    assert result == {"failed": "src-1"}
    assert "no transcript segments" in supa.failed[0][1]


def test_nothing_to_claim_does_nothing():
    class Empty(ClipSupa):
        def claim_clip_source(self, worker: str, lease_seconds: int):
            return None

    assert clip_activities.advance_clip_sources(Empty()) == {}


def test_a_source_claimed_at_an_unhandled_status_releases_rather_than_failing():
    """The claim and this function drifting apart is our bug, not the upload's."""
    supa = ClipSupa({"status": "awaiting_picks"})
    assert clip_activities.advance_clip_sources(supa) == {}
    assert supa.failed == []
    # Released: the lease is dropped by every `update_clip_source`.
    assert supa.source_updates == [{}]


# ---------------------------------------------------------------------------
# The render lane
# ---------------------------------------------------------------------------


class LaneSupa(ClipSupa):
    """Holds a production, its clip idea, and the recording behind it."""

    def __init__(
        self,
        idea: dict[str, Any] | None = None,
        production: dict[str, Any] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.row: dict[str, Any] = {
            "id": "p1",
            "idea_id": "i1",
            "style_preset_id": "s-clip",
            "status": "running",
            # Null, as a fresh production's is. `write_script` keeps an existing
            # script, so a fixture that pre-filled this would never reach the
            # branch under test. The render tests, which run after the gate, use
            # `submitting()` below instead.
            "script": None,
            "script_approved_at": "2026-09-08T12:00:00Z",
            "task_id": None,
            **(production or {}),
        }
        self.idea_row: dict[str, Any] = {
            "id": "i1",
            "title": "The quote that went quiet",
            "hook": "You sent the quote. They went silent.",
            "clip_source_id": "src-1",
            "clip_candidate_id": "cand-1",
            "clip_start_seconds": 100.0,
            "clip_end_seconds": 112.0,
            **(idea or {}),
        }
        self.updates: list[dict[str, Any]] = []
        self.claimed = True

    def production(self, production_id: str) -> dict[str, Any]:
        self.calls.append(("production", production_id))
        return dict(self.row)

    def idea(self, idea_id: str) -> dict[str, Any]:
        self.calls.append(("idea", idea_id))
        return dict(self.idea_row)

    def style_preset(self, preset_id: str) -> dict[str, Any]:
        self.calls.append(("style_preset", preset_id))
        return {"id": preset_id, "slug": "clip-cut", "render_mode": "clip",
                "video_source": "local", "params": {"clip": {"reframe": "crop"}}}

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status") or fields.get("stage")))
        self.updates.append(fields)
        self.row.update(fields)
        return dict(self.row)

    def claim_render_slot(self, production_id: str, task_id: str) -> bool:
        self.calls.append(("claim_render_slot", task_id))
        self.row["task_id"] = task_id if self.claimed else None
        return self.claimed

    def clip_candidate_for_idea(self, idea_id: str) -> dict[str, Any] | None:
        self.calls.append(("clip_candidate_for_idea", idea_id))
        return {"id": "cand-1", "transcript_excerpt": "Your quote went out on Friday."}


def submitting(**kw: Any) -> LaneSupa:
    """A clip production as it reaches `submit_render`: past the script gate.

    `submit_render` refuses a production whose approved script is empty -- the
    last check before money moves on every other lane -- so a render test has to
    start from a row that has one.
    """
    kw.setdefault("production", {})
    kw["production"] = {"script": "Your quote went out on Friday.", **kw["production"]}
    return LaneSupa(**kw)


def test_submitting_a_clip_calls_no_provider_and_hands_over_the_range():
    supa = submitting()
    out = render.submit_render({"production_id": "p1"}, supa)

    assert out["backend"] == "clip"
    assert out["phase"] == "clip"
    assert out["video_ref"] == "sources/clips/u1/talk.mp4"
    assert (out["clip_start_seconds"], out["clip_end_seconds"]) == (100.0, 112.0)


def test_a_clip_that_cannot_find_its_recording_releases_the_render_claim():
    """The one lane where releasing a claim is always safe: nothing is billed.

    Keeping it would strand the production. `task_id` cannot be unset by
    anything an owner can reach, so a row that parks holding one can never have
    its script edited again -- and here there is no render to protect.
    """
    supa = submitting(idea={"clip_source_id": None, "clip_start_seconds": None,
                            "clip_end_seconds": None})

    with pytest.raises(ValueError, match="does not name one"):
        render.submit_render({"production_id": "p1"}, supa)

    assert supa.row["task_id"] is None
    assert supa.row["status"] == "queued"


def test_a_backwards_range_releases_the_claim_too():
    supa = submitting(idea={"clip_start_seconds": 112.0, "clip_end_seconds": 100.0})
    with pytest.raises(ValueError, match="run forwards"):
        render.submit_render({"production_id": "p1"}, supa)
    assert supa.row["task_id"] is None


def test_a_recording_with_no_stored_file_releases_the_claim():
    supa = submitting(source={"storage_key": ""})
    with pytest.raises(ValueError, match="no stored recording"):
        render.submit_render({"production_id": "p1"}, supa)
    assert supa.row["task_id"] is None


def test_polling_a_clip_completes_immediately():
    """There is no provider holding this render, so there is nothing to poll."""
    supa = submitting()
    out = render.poll_render(
        {"production_id": "p1", "backend": "clip", "phase": "clip",
         "video_ref": "sources/clips/u1/talk.mp4", "clip_start_seconds": 100.0,
         "clip_end_seconds": 112.0, "polls": 0},
        supa,
    )

    assert out["state"] == "complete"
    assert out["progress"] == 100
    # The cut parameters survive the poll: `fetch_and_qc` reads them.
    assert out["video_ref"] == "sources/clips/u1/talk.mp4"
    assert out["clip_end_seconds"] == 112.0


def test_a_clip_is_not_judged_on_having_no_cuts():
    """A clip has as many cuts as the owner's camera did -- usually none."""
    assert "clip" in render.NO_CUTS_EXPECTED


# ---------------------------------------------------------------------------
# The script gate on this lane
# ---------------------------------------------------------------------------


def test_a_clips_script_is_its_own_transcript_and_costs_no_llm_call():
    """Drafting narration for a clip would invent words the video does not say.

    The excerpt is also what the owner saw at the clip gate, so the two gates
    show them the same text rather than two versions of it.
    """
    supa = LaneSupa()

    def refuse(*a: Any, **k: Any):
        raise AssertionError("a clip must not call a text generator")

    out = script.write_script({"production_id": "p1"}, supa, mpt=type("M", (), {"generate_script": refuse})())

    assert out["script_source"] == "transcript"
    assert supa.row["script"] == "Your quote went out on Friday."


def test_an_owners_edited_script_survives_a_re_entry():
    """`write_script` is idempotent, and an owner's words live in that column."""
    supa = LaneSupa()
    supa.row["script"] = "The words I typed myself."

    out = script.write_script({"production_id": "p1"}, supa)

    assert out["script_source"] == "kept"
    assert supa.row["script"] == "The words I typed myself."


def test_a_clip_whose_candidate_has_vanished_parks_rather_than_drafting():
    """Our inconsistency, not the owner's. A draft would replace the real words."""
    supa = LaneSupa()
    supa.clip_candidate_for_idea = lambda idea_id: None

    with pytest.raises(ValueError, match="no longer exists"):
        script.write_script({"production_id": "p1"}, supa)


def test_a_candidate_with_no_transcribed_words_parks():
    """There would be nothing for an owner to approve; `clean_script` refuses it."""
    supa = LaneSupa()
    supa.clip_candidate_for_idea = lambda idea_id: {"id": "cand-1", "transcript_excerpt": "  "}

    with pytest.raises(ValueError, match="no transcribed words"):
        script.write_script({"production_id": "p1"}, supa)


def test_a_non_clip_idea_still_goes_to_the_text_generator():
    """The lane must not change behaviour for every other production."""
    supa = LaneSupa(idea={"clip_source_id": None, "clip_candidate_id": None,
                          "clip_start_seconds": None, "clip_end_seconds": None})

    class Drafter:
        def generate_script(self, subject: str, language: str = "", paragraphs: int = 1) -> str:
            return "Drafted narration."

    out = script.write_script({"production_id": "p1"}, supa, mpt=Drafter())
    assert out["script_source"] == "drafted"
    assert supa.row["script"] == "Drafted narration."
