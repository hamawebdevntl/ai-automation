"""The clipping lane's pre-gate work: transcribe, propose, stop.

Three phases, one claim each, none of them rendering anything. That is the
property the whole feature rests on -- by the time a person is asked to decide,
the total spend is one transcription and one LLM call for the *recording*, not
per clip. The script gate's argument, applied to cuts: "the gate costs one LLM
call and no video generation, so rejecting a script is free in a way that
rejecting a cut is not."

  uploaded -> transcribing -> proposing -> awaiting_picks -> [a person] -> N ideas
                                              ^
                                              +-- the row rests here, for
                                                  minutes or for weeks

Each phase is one call of `advance_clip_sources`, which claims a row, does the
work for whatever status it is in, and writes the next status. Nothing sleeps
holding the claim, and a phase that fails marks the source `failed` with the
reason on the row -- retryable by an owner through `retry_clip_source`, which
costs nothing because nothing was rendered.

Why this is a thread of its own rather than a sweep: transcribing an hour of
audio is minutes, and `driver/sweeps.py` says why that matters -- "the only
argument for splitting them out is that a slow one could starve production
stepping". A sweep slot holding for twenty minutes would do exactly that. The
trend scout is in its own thread for the same reason and is the pattern this
follows.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from pipeline import clips
from pipeline.clients.fal import FalClient
from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.models import ClipSourceStatus, Transcript
from pipeline.qc import probe as probe_mod

log = logging.getLogger(__name__)

# Sentences, not words. A model choosing where a clip begins is choosing between
# sentences, and an hour of word-level chunks is far more tokens than that
# decision needs -- see `FalClient.transcribe`.
CHUNK_LEVEL = "segment"


def advance_clip_sources(supa: Supa | None = None, worker: str = "clips") -> dict[str, Any]:
    """Claim one recording and move it one phase. Returns what it did.

    One phase per call rather than a loop to completion, for the same reason the
    production driver advances one step per claim: a process that dies mid-work
    should lose one phase, not a whole pipeline, and the lease is what makes
    that recoverable.
    """
    supa = supa or Supa()
    source = supa.claim_clip_source(worker, settings().clip_lease_seconds)
    if not source:
        return {}

    source_id = source["id"]
    status = source.get("status")
    try:
        if status == ClipSourceStatus.UPLOADED.value:
            return {"transcribed": transcribe_source(source, supa)}
        if status == ClipSourceStatus.TRANSCRIBING.value:
            # Claimed at `transcribing` means a previous attempt's lease expired
            # part way through -- the row was moved to `transcribing` and the
            # worker died before writing a transcript. Start that phase again:
            # fal was either never asked or its answer is lost, and it is the
            # same call either way.
            return {"transcribed": transcribe_source(source, supa)}
        if status == ClipSourceStatus.PROPOSING.value:
            return {"proposed": propose_candidates(source, supa)}
    except Exception as exc:
        log.exception("clip source %s failed at %s", source_id, status)
        supa.fail_clip_source(source_id, f"{type(exc).__name__}: {exc}")
        return {"failed": source_id}

    # A status the claim admitted and this function does not handle. Releasing
    # the lease rather than failing the row: the claim predicate and this
    # function having drifted apart is our bug, not the owner's upload's.
    log.error("clip source %s claimed at unexpected status %r", source_id, status)
    supa.update_clip_source(source_id)
    return {}


def transcribe_source(
    source: dict[str, Any], supa: Supa | None = None, fal: FalClient | None = None
) -> str:
    """Measure the recording, then transcribe it with timings.

    The duration is measured first, and that ordering is the only cost control
    on this lane: fal bills transcription per minute of audio, and the length is
    chosen by whoever dragged the file in rather than by any setting of ours. A
    recording past `CLIP_MAX_SOURCE_SECONDS` is refused here, before the bill.

    ffprobe needs the bytes, so the file is fetched either way -- which is not
    wasted work: the duration it returns is also what `clips.validate` clamps
    every candidate timecode against, and without it a model's overrun would
    become a cut that runs to the end of the recording.
    """
    supa = supa or Supa()
    fal = fal or FalClient()
    source_id = source["id"]
    cfg = settings()

    supa.update_clip_source(
        source_id,
        status=ClipSourceStatus.TRANSCRIBING.value,
        leased_by=source.get("leased_by"),
        lease_expires_at=source.get("lease_expires_at"),
    )

    with tempfile.TemporaryDirectory(prefix=f"clip-src-{source_id}-") as tmp:
        local = supa.download_render(source["storage_key"], Path(tmp) / "source")
        info = probe_mod.probe(local)

    duration = float(info.duration_s or 0.0)
    if duration <= 0:
        raise ValueError(
            "could not read a duration from that file. It may not be a video, "
            "or it may have finished uploading incompletely."
        )
    if duration > cfg.clip_max_source_seconds:
        raise ValueError(
            f"that recording is {duration / 3600:.1f} hours long; the limit is "
            f"{cfg.clip_max_source_seconds / 3600:.1f}. Transcription is billed per "
            f"minute of audio, so this is refused before it is charged rather than after."
        )

    # fal fetches the URL itself, so the signed link has to outlive the whole
    # transcription rather than the submit. The footage lane's TTL is reused
    # because it was chosen for exactly this property and is checked against the
    # poll budget there.
    audio_url = supa.signed_render_url(
        source["storage_key"], expires_in=int(cfg.source_video_url_ttl_seconds)
    )

    # Resolved here rather than left to the client so the same name is both the
    # endpoint called and the one recorded on the transcript.
    endpoint = cfg.fal_transcribe_model
    handles = fal.transcribe(audio_url, model=endpoint, chunk_level=CHUNK_LEVEL)
    result = fal.wait(handles, budget_s=float(cfg.clip_transcribe_budget_seconds))
    transcript = clips.transcript_from_fal(result, model=endpoint)

    supa.update_clip_source(
        source_id,
        status=ClipSourceStatus.PROPOSING.value,
        duration_seconds=round(duration, 2),
        transcript=transcript.model_dump(exclude_none=True),
    )
    log.info(
        "clip source %s transcribed: %.0fs, %d segments",
        source_id,
        duration,
        len(transcript.segments),
    )
    return source_id


def propose_candidates(source: dict[str, Any], supa: Supa | None = None) -> str:
    """Ask a model for ranked candidates, write them, and open the gate.

    The candidates and the status are written in one statement each, and in this
    order: candidates first, then `awaiting_picks`. The reverse would open the
    gate on a source with nothing in it, and an owner arriving in that window
    would be shown an empty list and told to decide.

    A recording with nothing worth clipping resolves rather than failing. It is
    not a fault -- some uploads have no standalone moment in them -- and marking
    it `failed` would invite a retry that spends another LLM call to reach the
    same answer.
    """
    supa = supa or Supa()
    source_id = source["id"]

    transcript = Transcript.model_validate(source.get("transcript") or {})
    if not transcript.segments:
        # Only reachable if the transcript was written without segments, which
        # `transcript_from_fal` refuses to do. Failing rather than proposing
        # from nothing, because a model with no timecodes to choose between
        # would invent some.
        raise ValueError("this recording has no transcript segments to choose a clip from")

    rows = clips.propose(
        transcript,
        cap=int(source.get("candidate_cap") or 6),
        duration_s=_f(source.get("duration_seconds")),
    )

    if not rows:
        log.info("clip source %s: no candidate survived checking", source_id)
        supa.update_clip_source(
            source_id,
            status=ClipSourceStatus.RESOLVED.value,
            error=None,
        )
        return source_id

    supa.insert_clip_candidates(source_id, rows)
    supa.update_clip_source(source_id, status=ClipSourceStatus.AWAITING_PICKS.value)
    log.info("clip source %s is at the clip gate with %d candidate(s)", source_id, len(rows))
    return source_id


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
