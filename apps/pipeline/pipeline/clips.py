"""Choosing what to clip out of a long recording.

The pipeline's other lanes decide what to *say*. This one decides what to
*keep*, which is a different problem with a different failure mode: a bad script
is words nobody approved, and a bad clip is a cut that starts mid-sentence or
ends before the point lands.

Two things guard against that here, and neither is the model:

  * **Segments, not words.** The transcript the model reads is a list of timed
    sentences, and it is asked to choose between them. A clip that begins where
    a sentence begins reads as deliberate; one that begins 0.4s earlier reads as
    broken. See `FalClient.transcribe`'s `chunk_level`.
  * **Everything it returns is re-checked.** `validate` clamps the timecodes to
    the recording, drops what the database would refuse anyway, discards
    near-duplicates and re-ranks what survives. A model that returns eleven
    candidates for a cap of six, one of them 400 seconds long and two of them
    the same moment, produces a clean list of six.

Nothing in this module renders, and nothing in it spends money except the one
structured LLM call in `propose`. That is the whole argument for the gate this
feeds: the owner discards nine candidates out of ten for the price of one call.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from pipeline.config import settings
from pipeline.llm import LlmError, structured
from pipeline.models import ClipCandidateList, Transcript, TranscriptSegment

log = logging.getLogger(__name__)

# The bounds `clip_candidates_publishable_length` enforces in Postgres, mirrored
# so a candidate is dropped here with a log line rather than refused there with a
# constraint violation that names no candidate.
#
# Both are the platforms' numbers rather than ours: under about three seconds
# there is no hook, and every one of the four targets caps a short at ninety
# seconds or less.
MIN_CLIP_SECONDS = 3.0
MAX_CLIP_SECONDS = 90.0

# How much of a candidate may be covered by a better-ranked one before it counts
# as the same moment proposed twice.
#
# Some overlap is legitimate -- a good ten-second hook often sits inside a good
# forty-second explanation, and those are genuinely two different clips. Half is
# the line: past it the reviewer is being asked the same question twice, which is
# exactly the load `docs/GAPS.md` B7 is about.
MAX_OVERLAP_FRACTION = 0.5

# Enough for twenty candidates with a reason each, *plus* the thinking.
#
# `propose` asks for adaptive thinking, and `llm.DirectLlm._claude` notes that
# it is billed against `max_tokens` -- "a caller that wants it passes a generous
# ceiling along with it". The judgement here is over an hour of transcript, so
# the thinking is the larger half of this number and the twenty candidates are
# the smaller one. Sized so that running out truncates neither: a stop on length
# comes back as an unparseable answer and fails the whole proposal.
MAX_TOKENS = 24000

SYSTEM = """You find the moments in a long recording that would work as standalone short vertical videos (Reels, Shorts, TikTok).

You are given a transcript with timecodes. Choose the ranges worth cutting.

What makes a clip:
- It stands alone. Someone who has not seen the rest of the recording understands it.
- It opens on a hook. The first sentence earns the next two seconds.
- It makes exactly one point, and the point lands before the clip ends.
- It starts and ends on sentence boundaries from the transcript. Never mid-sentence.

Rules:
- Use only timecodes that exist in the transcript you were given. Never invent a time past the end of it.
- Each clip must be between 3 and 90 seconds. Aim for 20-60.
- Do not propose the same moment twice. Overlapping ranges are wasted review.
- Rank them best first: the one you would publish if you could only publish one goes first.
- State for each clip why it stands alone. If you cannot say why in a sentence, do not propose it.
- Propose fewer than asked rather than padding the list with weak ranges."""


def transcript_from_fal(result: dict[str, Any], model: str | None = None) -> Transcript:
    """Read fal's transcription output into the shape we store.

    Output shapes vary across fal's catalogue -- `chunks` on whisper, `segments`
    elsewhere, `timestamp` pairs in some and `start`/`end` in others -- so the
    known variants are accepted rather than binding to one model's schema. Same
    reasoning, and the same variants, as `assemble.srt_from_transcription`.

    Raises `LlmError` when nothing timed came back. A transcript without timings
    is useless here in a way it is not on the captioning path: the timecodes are
    the product, so continuing without them would mean asking a model to choose
    ranges it has no way to name.

    `model` is recorded rather than resolved. Reaching for `settings()` for a
    label would mean a test of this function needed a Supabase URL in the
    environment -- the same reason `llm._gemini` reads its settings locally, and
    `transcribe_source` knows the endpoint it called anyway.
    """
    raw = result.get("chunks") or result.get("segments") or []
    segments: list[TranscriptSegment] = []
    for chunk in raw:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        start, end = _timestamps(chunk)
        if not text or start is None or end is None or end <= start:
            continue
        segments.append(TranscriptSegment(start=start, end=end, text=text))

    if not segments:
        raise LlmError(
            "the transcription returned no timed segments, so there is nothing to "
            f"choose a clip from (keys returned: {sorted(result)})"
        )

    segments.sort(key=lambda s: s.start)
    return Transcript(
        text=str(result.get("text") or "").strip()
        or " ".join(s.text for s in segments),
        language=(result.get("language") or result.get("detected_language") or None),
        model=model,
        segments=segments,
    )


def _timestamps(chunk: dict[str, Any]) -> tuple[float | None, float | None]:
    stamp = chunk.get("timestamp")
    if isinstance(stamp, (list, tuple)) and len(stamp) == 2:
        return _f(stamp[0]), _f(stamp[1])
    return _f(chunk.get("start")), _f(chunk.get("end"))


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # A NaN start would pass every comparison below and reach ffmpeg as the
    # literal `nan`. `math.isnan` rather than `out != out` so it reads as the
    # check it is.
    return None if math.isnan(out) else out


def format_transcript(transcript: Transcript) -> str:
    """The transcript as the model reads it: one timed line per segment.

    Seconds with one decimal rather than `mm:ss`, because these come back as
    numbers we act on. Asking for a clock format and parsing it back would add a
    failure mode -- a model writing `1:05:30` where we expected `65:30` -- for no
    gain to the model's judgement.
    """
    return "\n".join(f"[{s.start:.1f}] {s.text.strip()}" for s in transcript.segments if s.text.strip())


def _prompt(transcript: Transcript, cap: int, brief: str) -> str:
    duration = transcript.segments[-1].end if transcript.segments else 0.0
    parts = [
        f"The recording is {duration:.0f} seconds long. Propose at most {cap} clips.",
        (
            "Each line below is a segment of the transcript, prefixed with the second "
            "it starts at. Start and end your clips on these boundaries."
        ),
    ]
    if brief:
        # The same brief idea generation and script drafting read. A clip is
        # worth cutting because of who is watching, and this is the only thing
        # in the request that says who that is.
        parts.append(f"Who is speaking, and to whom:\n{brief}")
    parts.append(f"Transcript:\n{format_transcript(transcript)}")
    return "\n\n".join(parts)


def propose(
    transcript: Transcript,
    *,
    cap: int = 6,
    duration_s: float | None = None,
    provider: str | None = None,
    client: Any | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Ask a model for ranked candidates, and return only what survives checking.

    One structured call. `thinking` is on, as it is for idea drafting, because
    this is the same kind of judgement over a large input -- and because the
    output is small, so the tokens go where they are worth spending.

    The return value is a list of dicts ready to insert into `clip_candidates`,
    ranked from 1. Never raises for an empty answer: a recording with nothing
    worth clipping is a real outcome, and the caller marks the source
    accordingly rather than treating it as a fault.
    """
    cfg = settings()
    provider = provider or cfg.idea_provider
    ceiling = max(1, min(int(cap), 20))

    drafted = structured(
        provider,
        SYSTEM,
        _prompt(transcript, ceiling, cfg.niche_brief.strip()),
        ClipCandidateList,
        client=client,
        model=model,
        max_tokens=MAX_TOKENS,
        thinking=True,
    )

    return validate(
        drafted.candidates,
        transcript=transcript,
        cap=ceiling,
        duration_s=duration_s,
    )


def validate(
    drafts: list[Any],
    *,
    transcript: Transcript,
    cap: int,
    duration_s: float | None = None,
) -> list[dict[str, Any]]:
    """Turn what the model said into rows the database will accept.

    Separate from `propose` so it can be tested against a hand-written list of
    the ways a model gets this wrong, without a provider in the loop. In order:

      1. **Clamp to the recording.** A timecode past the end produces an ffmpeg
         command that runs to the end of the file, which is a clip nobody chose.
         The bound is the measured duration where we have one and the last
         segment's end otherwise.
      2. **Drop what Postgres would refuse.** Under `MIN_CLIP_SECONDS` or over
         `MAX_CLIP_SECONDS` after clamping, or running backwards.
      3. **Drop near-duplicates**, keeping the better-ranked one.
      4. **Re-rank from 1** in the model's own order, and take the first `cap`.

    Ranks are reassigned rather than taken from the model's position because
    steps 1-3 leave gaps, and `clip_candidates_rank_unique` is a unique index on
    `(source_id, rank)`: a list with a gap in it is not the problem, but two
    candidates sharing a rank after a drop would be.
    """
    limit = duration_s if duration_s and duration_s > 0 else (
        transcript.segments[-1].end if transcript.segments else 0.0
    )

    kept: list[dict[str, Any]] = []
    for draft in drafts:
        start = _f(getattr(draft, "start_seconds", None))
        end = _f(getattr(draft, "end_seconds", None))
        title = str(getattr(draft, "title", "") or "").strip()
        if start is None or end is None or not title:
            log.warning("clip candidate dropped: incomplete (%r)", draft)
            continue

        start = max(0.0, start)
        if limit:
            end = min(end, limit)
        length = end - start
        if length < MIN_CLIP_SECONDS or length > MAX_CLIP_SECONDS:
            log.info(
                "clip candidate %r dropped: %.1fs is outside %.0f-%.0fs",
                title, length, MIN_CLIP_SECONDS, MAX_CLIP_SECONDS,
            )
            continue

        if _overlaps_kept(start, end, kept):
            log.info("clip candidate %r dropped: overlaps a better-ranked clip", title)
            continue

        # Taken from the transcript rather than from the model, which is the
        # point: this becomes the production's script, so it must be what the
        # recording actually says and not a paraphrase.
        excerpt = transcript.excerpt(start, end)
        if not excerpt:
            # A range with no speech in it cannot go through the script gate:
            # `clean_script` refuses an empty script and
            # `productions_approved_script_not_empty` refuses to record an
            # approval of one, so such a clip would rest at the gate forever
            # with nothing an owner could approve.
            #
            # This lane therefore requires words in the range, and says so here
            # rather than by stranding a production. A silent moment worth
            # publishing -- a demo, a montage -- is a real thing and not one this
            # lane can carry; it needs a caption written by hand, which is a
            # different feature.
            log.info(
                "clip candidate %r dropped: no speech between %.1fs and %.1fs",
                title, start, end,
            )
            continue

        kept.append({
            "start_seconds": round(start, 2),
            "end_seconds": round(end, 2),
            "title": title[:200],
            "hook": (str(getattr(draft, "hook", "") or "").strip() or None),
            "reason": (str(getattr(draft, "reason", "") or "").strip() or None),
            "transcript_excerpt": excerpt,
        })
        if len(kept) >= cap:
            break

    for rank, row in enumerate(kept, start=1):
        row["rank"] = rank
    return kept


def _overlaps_kept(start: float, end: float, kept: list[dict[str, Any]]) -> bool:
    """Whether this range is mostly inside one already accepted.

    Measured against the *candidate's own* length, not the kept one's, so a
    ten-second hook sitting inside a kept forty-second explanation is recognised
    as the duplicate it is -- the reviewer would be shown the same words twice.
    The reverse case, a long clip that happens to contain a kept short one, is
    also caught: it is the same fraction seen from the other side, and whichever
    the model ranked higher is the one that survives.
    """
    length = end - start
    if length <= 0:
        return True
    for row in kept:
        overlap = min(end, float(row["end_seconds"])) - max(start, float(row["start_seconds"]))
        if overlap <= 0:
            continue
        kept_length = float(row["end_seconds"]) - float(row["start_seconds"])
        shorter = min(length, kept_length) or length
        if overlap / shorter > MAX_OVERLAP_FRACTION:
            return True
    return False


# ---------------------------------------------------------------------------
# Captions for a clip
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Whitespace-insensitive comparison, for "did the owner change this?"."""
    return " ".join((text or "").split()).strip()


def caption_segments(
    transcript: Transcript,
    start: float,
    end: float,
    script: str | None = None,
) -> list[dict[str, Any]]:
    """The caption track for a clip, with absolute source timings.

    This is where the script gate earns its place on this lane. The words the
    owner approved are what gets burned in -- not the raw transcription -- so
    correcting a mis-transcribed name at the gate actually fixes the caption
    rather than fixing a copy of it nobody sees.

    Two paths, and the first is the one that almost always runs:

      * **Unchanged.** The approved script still matches what the transcript
        says, so the segments are used verbatim and every caption is on the
        frame the speaker said it. Compared whitespace-insensitively, because
        `clean_script` trims and the excerpt was joined with single spaces.

      * **Edited.** The owner rewrote something, so there is no longer a
        word-for-word mapping to fall back on. The approved words are
        redistributed across the original segment timings in proportion to how
        much of the original text each segment held. A corrected word lands in
        the right segment; a wholesale rewrite drifts proportionally, which is
        the best available answer short of paying to transcribe again -- and it
        is still far better than captions built from text alone, which have no
        timings at all.

    Times are absolute, as the transcript stores them. `assemble.srt_from_segments`
    rebases them onto the clip's own timeline with `offset=start`.
    """
    segments = transcript.between(start, end)
    if not segments:
        return []

    verbatim = [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments
        if s.text.strip()
    ]
    spoken = " ".join(s.text.strip() for s in segments if s.text.strip())
    words = (script or "").split()
    if not words or _norm(script or "") == _norm(spoken):
        return verbatim

    weights = [max(1, len(s.text.strip())) for s in segments]
    total = sum(weights) or 1
    out: list[dict[str, Any]] = []
    cursor = 0
    last = len(segments) - 1

    for index, (segment, weight) in enumerate(zip(segments, weights)):
        if index == last:
            take = len(words) - cursor
        else:
            # Leave at least one word for each segment still to come, so a short
            # rewrite does not put every word in the first caption and leave the
            # rest of the clip silent.
            remaining = last - index
            take = round(len(words) * weight / total)
            take = max(1, min(take, len(words) - cursor - remaining))
        chunk = words[cursor : cursor + max(0, take)]
        cursor += max(0, take)
        if chunk:
            out.append(
                {"start": segment.start, "end": segment.end, "text": " ".join(chunk)}
            )
        if cursor >= len(words):
            break

    return out or verbatim
