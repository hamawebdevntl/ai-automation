"""Choosing candidate clips, and everything a model gets wrong doing it.

`clips.validate` is the reason this feature can trust an LLM with timecodes. The
model is asked for ranked ranges over an hour of transcript; what it returns is
re-checked against the recording, against the database's own constraints and
against itself before an owner is shown anything.

These tests are written against a hand-built transcript and a fake draft list
rather than a provider, because the interesting inputs are the malformed ones and
no provider can be asked for those on demand.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pipeline import clips
from pipeline.llm import LlmError
from pipeline.models import Transcript, TranscriptSegment


@dataclass
class Draft:
    """Stands in for `ClipCandidateDraft` without pydantic validation.

    Deliberately not the real model: half of these cases are values a model
    returned that the schema would have refused, and the point is what
    `validate` does with them rather than whether pydantic catches them first.
    """

    start_seconds: object
    end_seconds: object
    title: str = "A title"
    hook: str = "A hook."
    reason: str = "It stands alone."


def transcript(*spans: tuple[float, float, str]) -> Transcript:
    return Transcript(
        text=" ".join(s[2] for s in spans),
        segments=[TranscriptSegment(start=a, end=b, text=t) for a, b, t in spans],
    )


def talk() -> Transcript:
    """Two minutes of someone talking, in ten-second sentences."""
    return transcript(
        *[
            (float(i * 10), float(i * 10 + 10), f"Sentence number {i} with some words in it.")
            for i in range(12)
        ]
    )


# ---------------------------------------------------------------------------
# The transcript
# ---------------------------------------------------------------------------


def test_between_clamps_partial_segments_rather_than_dropping_them():
    """A boundary mid-sentence must keep the sentence, clamped.

    Dropping the partial segments at each end would silently lose the first and
    last words of every clip -- including the hook, which is the one line that
    decides whether anyone watches.
    """
    t = talk()
    got = t.between(15.0, 35.0)

    assert [(s.start, s.end) for s in got] == [(15.0, 20.0), (20.0, 30.0), (30.0, 35.0)]
    # The text is not truncated with the timing: a caption showing half a
    # sentence is worse than one showing it slightly early.
    assert got[0].text.startswith("Sentence number 1")


def test_between_excludes_segments_that_only_touch_the_boundary():
    t = transcript((0.0, 10.0, "before"), (10.0, 20.0, "inside"), (20.0, 30.0, "after"))
    assert [s.text for s in t.between(10.0, 20.0)] == ["inside"]


def test_excerpt_is_what_was_said_in_the_range():
    t = transcript((0.0, 5.0, "One."), (5.0, 10.0, "Two."), (10.0, 15.0, "Three."))
    assert t.excerpt(4.0, 11.0) == "One. Two. Three."
    assert t.excerpt(100.0, 200.0) == ""


def test_transcript_from_fal_accepts_both_output_shapes():
    """whisper returns `chunks` with `timestamp` pairs; others return `segments`."""
    chunked = clips.transcript_from_fal(
        {"text": "Hello there.", "chunks": [{"timestamp": [0.0, 2.0], "text": "Hello there."}]}
    )
    segmented = clips.transcript_from_fal(
        {"segments": [{"start": 0.0, "end": 2.0, "text": "Hello there."}]}
    )

    for t in (chunked, segmented):
        assert [(s.start, s.end, s.text) for s in t.segments] == [(0.0, 2.0, "Hello there.")]


def test_transcript_from_fal_refuses_output_with_no_timings():
    """The timecodes are the product here, so untimed text is not a transcript.

    On the captioning path a missing timing degrades to no captions. Here it
    would mean asking a model to choose ranges it has no way to name, so it
    fails the source instead -- retryable, and nothing has been rendered.
    """
    with pytest.raises(LlmError, match="no timed segments"):
        clips.transcript_from_fal({"text": "Words with no times."})


def test_transcript_from_fal_sorts_and_drops_unusable_segments():
    t = clips.transcript_from_fal(
        {
            "segments": [
                {"start": 10.0, "end": 20.0, "text": "second"},
                {"start": 0.0, "end": 5.0, "text": "first"},
                {"start": 30.0, "end": 30.0, "text": "zero length"},
                {"start": 40.0, "end": 50.0, "text": "   "},
                {"start": 60.0, "end": 55.0, "text": "backwards"},
                "not a dict",
            ]
        }
    )
    assert [s.text for s in t.segments] == ["first", "second"]


# ---------------------------------------------------------------------------
# validate -- clamping and refusing
# ---------------------------------------------------------------------------


def test_a_clean_list_survives_and_is_ranked_from_one():
    rows = clips.validate(
        [Draft(0.0, 30.0, title="First"), Draft(40.0, 80.0, title="Second")],
        transcript=talk(),
        cap=6,
    )
    assert [(r["rank"], r["title"]) for r in rows] == [(1, "First"), (2, "Second")]
    assert rows[0]["transcript_excerpt"].startswith("Sentence number 0")


def test_an_end_past_the_recording_is_clamped_to_it():
    """Otherwise ffmpeg runs to the end of the file, which is a clip nobody chose."""
    rows = clips.validate(
        [Draft(100.0, 9_999.0)], transcript=talk(), cap=6, duration_s=120.0
    )
    assert rows[0]["end_seconds"] == 120.0


def test_clamping_that_leaves_too_short_a_clip_drops_it():
    """A 9000-second overrun starting at 119s is not a 1-second clip."""
    rows = clips.validate(
        [Draft(119.0, 9_999.0)], transcript=talk(), cap=6, duration_s=120.0
    )
    assert rows == []


def test_the_duration_falls_back_to_the_transcript_when_unmeasured():
    """`clip_sources.duration_seconds` is null until ffprobe has run."""
    rows = clips.validate([Draft(60.0, 9_999.0)], transcript=talk(), cap=6)
    assert rows[0]["end_seconds"] == 120.0


@pytest.mark.parametrize(
    "start,end,why",
    [
        (10.0, 11.0, "far under the floor"),
        # 4s is the case the floor moved for: the quality check fails anything
        # under five seconds as "too short to publish", so a 4s candidate could
        # be accepted, rendered and then flagged for a reason settled before it
        # was proposed.
        (10.0, 14.0, "just under the quality check's own floor"),
        (0.0, 100.0, "over the ninety-second ceiling"),
        (50.0, 40.0, "runs backwards"),
        (50.0, 50.0, "has no length"),
    ],
)
def test_lengths_the_database_would_refuse_are_dropped_here(start, end, why):
    """Mirrors `clip_candidates_publishable_length`.

    Dropped here rather than refused there so the log names the candidate; a
    constraint violation on a batch insert names none of them, and would lose
    the whole proposal rather than one bad range.
    """
    assert clips.validate([Draft(start, end)], transcript=talk(), cap=6) == [], why


@pytest.mark.parametrize("bad", [None, "twenty", float("nan")])
def test_an_unusable_timecode_is_dropped(bad):
    assert clips.validate([Draft(bad, 30.0)], transcript=talk(), cap=6) == []
    assert clips.validate([Draft(0.0, bad)], transcript=talk(), cap=6) == []


def test_a_candidate_with_no_title_is_dropped():
    assert clips.validate([Draft(0.0, 30.0, title="   ")], transcript=talk(), cap=6) == []


def test_a_range_with_no_speech_in_it_is_dropped():
    """The script gate structurally requires words.

    `clean_script` refuses an empty script and
    `productions_approved_script_not_empty` refuses to record an approval of
    one, so a silent clip would rest at the gate forever with nothing an owner
    could approve. It is refused here instead.
    """
    t = transcript((0.0, 10.0, "Talking."), (200.0, 210.0, "Talking again."))
    assert clips.validate([Draft(60.0, 90.0)], transcript=t, cap=6) == []


def test_a_clip_exactly_at_the_floor_is_kept():
    """5s is publishable; the quality check passes it. Only under it fails."""
    rows = clips.validate([Draft(10.0, 15.0)], transcript=talk(), cap=6)
    assert len(rows) == 1
    assert rows[0]["end_seconds"] - rows[0]["start_seconds"] == 5.0


def test_the_floor_matches_the_quality_check_that_would_fail_the_render():
    """If these drift, a clip is accepted and then flagged for being too short.

    `slideshow.build_report` fails a render under `MIN_DURATION_S`, so a
    candidate shorter than that is one an owner would approve and wait for
    before being told it was never publishable.
    """
    from pipeline.qc import slideshow

    assert clips.MIN_CLIP_SECONDS >= slideshow.MIN_DURATION_S


def test_a_negative_start_is_clamped_to_zero():
    rows = clips.validate([Draft(-5.0, 30.0)], transcript=talk(), cap=6)
    assert rows[0]["start_seconds"] == 0.0


# ---------------------------------------------------------------------------
# validate -- duplicates and the cap
# ---------------------------------------------------------------------------


def test_the_same_moment_proposed_twice_keeps_the_better_ranked_one():
    rows = clips.validate(
        [Draft(0.0, 40.0, title="Keep"), Draft(2.0, 42.0, title="Duplicate")],
        transcript=talk(),
        cap=6,
    )
    assert [r["title"] for r in rows] == ["Keep"]


def test_a_short_hook_inside_a_kept_long_clip_is_a_duplicate():
    """Measured against the shorter of the two, so containment is caught.

    A ten-second hook inside a kept forty-second explanation shows the reviewer
    the same words twice, which is exactly the load GAPS B7 is about.
    """
    rows = clips.validate(
        [Draft(0.0, 40.0, title="Explanation"), Draft(5.0, 15.0, title="Hook inside it")],
        transcript=talk(),
        cap=6,
    )
    assert [r["title"] for r in rows] == ["Explanation"]


def test_clips_that_merely_abut_are_both_kept():
    """Adjacent ranges are two clips, not one proposed twice."""
    rows = clips.validate(
        [Draft(0.0, 30.0, title="A"), Draft(30.0, 60.0, title="B")],
        transcript=talk(),
        cap=6,
    )
    assert [r["title"] for r in rows] == ["A", "B"]


def test_a_small_overlap_is_tolerated():
    """40s and 30s clips sharing 5s are genuinely different clips."""
    rows = clips.validate(
        [Draft(0.0, 40.0, title="A"), Draft(35.0, 65.0, title="B")],
        transcript=talk(),
        cap=6,
    )
    assert [r["title"] for r in rows] == ["A", "B"]


def test_the_cap_is_honoured_however_many_the_model_returns():
    """The cap is reviewer attention, not model capability -- GAPS B7."""
    drafts = [Draft(float(i * 10), float(i * 10 + 8), title=f"C{i}") for i in range(12)]
    rows = clips.validate(drafts, transcript=talk(), cap=3)

    assert len(rows) == 3
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_ranks_are_reassigned_after_drops_so_they_never_collide():
    """`clip_candidates_rank_unique` is a unique index on (source_id, rank).

    A gap is harmless; two candidates sharing a rank after a drop would lose the
    whole insert.
    """
    rows = clips.validate(
        [
            Draft(0.0, 30.0, title="Kept"),
            Draft(0.0, 1.0, title="Too short"),
            Draft(60.0, 90.0, title="Also kept"),
        ],
        transcript=talk(),
        cap=6,
    )
    assert [(r["rank"], r["title"]) for r in rows] == [(1, "Kept"), (2, "Also kept")]


def test_an_empty_answer_is_an_answer():
    """Some recordings have no standalone moment. That is not a fault."""
    assert clips.validate([], transcript=talk(), cap=6) == []
