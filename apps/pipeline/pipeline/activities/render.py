"""Render activities: submit, poll, fetch.

The whole design here turns on one fact: because our MoneyPrinterTurbo fork
accepts a caller-supplied task id, we set `task_id = production_id`. That makes
the render's identity *derivable* rather than something we must successfully
persist, which removes the worst failure window in the pipeline -- MPT
returning 200 while we time out before recording the id it gave us.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from pipeline.clients.fal import FalClient, FalError, FalRefused
from pipeline.clients.mpt import MptClient, MptError, MptQueueFull, MptTaskStateLost
from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.models import ProductionStatus, VideoParams
from pipeline.qc import probe as probe_mod
from pipeline.qc import slideshow

log = logging.getLogger(__name__)

# A 404 immediately after submitting more likely means "never registered" than
# "state lost", so the poller tolerates a few before calling it terminal.
GRACE_POLLS_ON_404 = 3

# render_mode values, from style_presets.render_mode.
MPT = "mpt"                  # MoneyPrinterTurbo does everything
FAL_VISUALS = "fal_visuals"  # fal generates clips, MPT assembles them
FAL_FULL = "fal_full"        # fal generates visuals and voice, we assemble


def submit_render(
    event: dict[str, Any], supa: Supa | None = None, mpt: MptClient | None = None
) -> dict[str, Any]:
    """Start exactly one render for this production."""
    supa = supa or Supa()
    mpt = mpt or MptClient()
    production_id = event["production_id"]

    production = supa.production(production_id)
    idea = supa.idea(production["idea_id"])
    preset = supa.style_preset(production["style_preset_id"])
    mode = preset.get("render_mode") or MPT

    task_id = production_id

    # Claim the right to submit. Only one caller can win, so a duplicated
    # activity invocation cannot start a second paid render. The claim is taken
    # before the backend is chosen, so it guards fal spend exactly as it guards
    # MoneyPrinterTurbo's.
    if not supa.claim_render_slot(production_id, task_id):
        existing = supa.production(production_id).get("task_id")
        log.info("production %s already claimed with task %s", production_id, existing)
        # `polls` and `started_at` are seeded here as well as on the happy path,
        # because the state machine reads both on its first poll and ASL fails
        # on a missing path rather than treating it as null.
        return {
            "production_id": production_id,
            "backend": mode,
            "phase": MPT if mode == MPT else "fal",
            "task_id": existing or task_id,
            "deduplicated": True,
            "polls": 0,
            "started_at": time.time(),
        }

    supa.update_production(production_id, render_backend=mode)

    if mode in (FAL_VISUALS, FAL_FULL):
        return _submit_fal(production_id, idea, preset, mode, supa)

    params = _build_params(idea, preset, task_id)

    try:
        returned = mpt.submit_render(params)
    except MptQueueFull:
        # Provably leaves no orphan render: MPT writes its state row before
        # scheduling and deletes it again when the queue rejects the task. Safe
        # to release the claim so a retry can submit.
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise
    except Exception:
        # Ambiguous. Releasing the claim is still safe because the fork
        # deduplicates on our task id: a resubmit either creates the render or
        # returns the one that already exists. This is the single behaviour that
        # depends on running our fork rather than upstream.
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise

    if returned != task_id:
        # Upstream ignores the supplied id. Everything above still works, but
        # idempotency is gone and a retry would double-render, so make it loud.
        log.error(
            "MPT returned task id %s instead of %s -- this is NOT our fork, and "
            "render submission is no longer idempotent",
            returned,
            task_id,
        )
        supa.update_production(production_id, task_id=returned)

    return {
        "production_id": production_id,
        "backend": MPT,
        "phase": MPT,
        "task_id": returned,
        "started_at": time.time(),
        "polls": 0,
    }


def _submit_fal(
    production_id: str,
    idea: dict[str, Any],
    preset: dict[str, Any],
    mode: str,
    supa: Supa,
    fal: FalClient | None = None,
) -> dict[str, Any]:
    """Enqueue generation on fal.

    fal has no idempotency key of its own, so the conditional claim taken by
    the caller is the only guard against paying twice. Unlike the
    MoneyPrinterTurbo path, the claim is *not* released on an ambiguous
    failure: a resubmit there is safe because our fork deduplicates on our task
    id, whereas here it would be a second billed generation. An ambiguous fal
    failure parks instead.
    """
    fal = fal or FalClient()
    cfg = (preset.get("params") or {}).get("fal") or {}
    model = cfg.get("model")
    if not model:
        raise ValueError(
            f"preset {preset.get('slug')!r} has render_mode {mode} but no params.fal.model"
        )

    subject = " -- ".join(p for p in (idea.get("title") or "", idea.get("hook") or "") if p)

    # One generation of the whole runtime, not one request per clip. The models
    # we use accept a duration up to their own ceiling, so a single request is
    # one thing to poll and one thing billed -- and on the visuals path
    # MoneyPrinterTurbo subdivides it anyway, since preprocess_video cuts
    # supplied material to video_clip_duration.
    wanted = int(cfg.get("clips", 1)) * int(cfg.get("clip_seconds", 5))
    duration = max(1, min(wanted, int(cfg.get("max_duration_seconds", 20))))

    payload: dict[str, Any] = {
        "prompt": (idea.get("angle") or subject or "").strip()[:1500],
        "aspect_ratio": cfg.get("aspect_ratio", "9:16"),
        "resolution": cfg.get("resolution", "1080p"),
        "duration": duration,
    }
    if cfg.get("fps"):
        payload["fps"] = int(cfg["fps"])

    try:
        handles = fal.submit(model, payload)
    except FalRefused as exc:
        # A refusal is about the content, so it parks rather than retrying or
        # quietly falling back to another backend.
        return _fail(supa, production_id, f"fal declined the prompt: {exc}")
    except Exception:
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise

    # The end-to-end path has no other source of narration text: fal generates
    # pictures and speech, not a script. Written now rather than at assembly
    # time so a failure here costs nothing -- no generation has been billed yet.
    if mode == FAL_FULL:
        try:
            script = MptClient().generate_script(subject or idea.get("title") or "", paragraphs=1)
            supa.update_production(production_id, script=script)
        except Exception as exc:  # noqa: BLE001
            return _fail(supa, production_id, f"could not write a narration script: {exc}")

    supa.update_production(production_id, stage=f"fal generating ({model})")
    return {
        "production_id": production_id,
        "backend": mode,
        "phase": "fal",
        "task_id": production_id,
        "fal_model": model,
        "fal_request_id": handles["request_id"],
        "fal_status_url": handles["status_url"],
        "fal_response_url": handles["response_url"],
        "started_at": time.time(),
        "polls": 0,
    }


def _build_params(idea: dict[str, Any], preset: dict[str, Any], task_id: str) -> VideoParams:
    """Turn an approved idea plus its style preset into a render request.

    `style_presets.params` is documented as VideoParams overrides and is merged
    last, so a new upstream field can be driven from the database without a code
    change here.
    """
    subject_parts = [idea.get("title") or "", idea.get("hook") or ""]
    subject = " -- ".join(p for p in subject_parts if p).strip() or "Untitled"

    params = VideoParams(
        video_subject=subject,
        task_id=task_id,
        video_source=preset.get("video_source") or "pexels",
        video_aspect="9:16",
    )
    overrides = preset.get("params") or {}
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            # `lane` is our own routing hint, not an MPT field.
            if key == "lane":
                continue
            setattr(params, key, value)
    return params


def poll_render(
    event: dict[str, Any], supa: Supa | None = None, mpt: MptClient | None = None
) -> dict[str, Any]:
    """Report the render's state for the state machine to branch on.

    Returns `state` as one of running / complete / failed, never an exception
    for an ordinary in-progress render.
    """
    supa = supa or Supa()
    mpt = mpt or MptClient()
    production_id = event["production_id"]
    task_id = event.get("task_id") or production_id
    polls = int(event.get("polls", 0)) + 1
    started_at = float(event.get("started_at") or 0)
    backend = event.get("backend") or MPT

    # While generation is still on fal, poll fal. Once the visuals path has
    # handed its clips to MoneyPrinterTurbo the phase flips and the rest of this
    # function serves both backends unchanged -- which is why the state machine
    # does not need to know which one ran.
    if backend in (FAL_VISUALS, FAL_FULL) and event.get("phase") == "fal":
        return _poll_fal(event, supa, mpt, polls)

    try:
        status = mpt.get_task(task_id)
    except MptTaskStateLost:
        if polls <= GRACE_POLLS_ON_404:
            # Probably not registered yet rather than lost.
            return {"production_id": production_id, "backend": backend, "phase": MPT,
                    "state": "running", "polls": polls, "progress": 0,
                    "task_id": task_id, "started_at": started_at}
        return _fail(
            supa, production_id,
            "render state was lost -- MPT restarted without Redis, or the task never registered",
        )

    # An interrupted render sits at state=4 forever and is never recovered, so a
    # wall-clock budget is the only way out.
    budget = settings().mpt_render_budget_seconds
    if started_at and status.is_processing and (time.time() - started_at) > budget:
        return _fail(
            supa, production_id,
            f"render exceeded its {budget}s budget at {status.progress}% -- treating as stranded",
        )

    if status.is_failed:
        return _fail(
            supa, production_id,
            f"render failed at stage {status.failed_stage or 'unknown'}: "
            f"{status.error or 'no detail'}",
        )

    if status.is_complete:
        videos = status.videos or []
        if not videos:
            return _fail(supa, production_id, "render reported complete but produced no video")
        supa.update_production(production_id, stage="rendered")
        return {
            "production_id": production_id,
            "backend": backend,
            "phase": MPT,
            "state": "complete",
            "task_id": task_id,
            "polls": polls,
            "progress": 100,
            "video_ref": videos[0],
            "subtitle_path": getattr(status, "subtitle_path", None),
            "script": getattr(status, "script", None),
        }

    supa.update_production(production_id, stage=f"render {status.progress}%")
    return {"production_id": production_id, "backend": backend, "phase": MPT,
            "state": "running", "polls": polls, "progress": status.progress,
            "task_id": task_id, "started_at": started_at}


def _poll_fal(
    event: dict[str, Any], supa: Supa, mpt: MptClient, polls: int, fal: FalClient | None = None
) -> dict[str, Any]:
    """Poll a fal generation, and hand off when it finishes.

    On the visuals path this is where the two backends meet: the clips are
    uploaded into MoneyPrinterTurbo and an ordinary render is submitted against
    them, after which the phase flips to `mpt` and every later poll takes the
    original code path.
    """
    fal = fal or FalClient()
    production_id = event["production_id"]
    backend = event["backend"]
    started_at = float(event.get("started_at") or 0)

    carry = {
        "production_id": production_id,
        "backend": backend,
        "phase": "fal",
        "task_id": event.get("task_id") or production_id,
        "fal_model": event.get("fal_model"),
        "fal_request_id": event.get("fal_request_id"),
        "fal_status_url": event.get("fal_status_url"),
        "fal_response_url": event.get("fal_response_url"),
        "started_at": started_at,
        "polls": polls,
    }

    budget = settings().fal_poll_budget_seconds
    if started_at and (time.time() - started_at) > budget:
        return _fail(
            supa, production_id,
            f"fal generation exceeded its {budget}s budget (request {event.get('fal_request_id')})",
        )

    try:
        body = fal.status(event["fal_status_url"])
        if not fal.is_terminal(body):
            supa.update_production(production_id, stage=f"fal {str(body.get('status','')).lower()}")
            return {**carry, "state": "running", "progress": 25}
        # COMPLETED is not success: it means no longer queued. The result call
        # raises on an error carried alongside that status.
        result = fal.result(event["fal_response_url"])
    except FalRefused as exc:
        return _fail(supa, production_id, f"fal declined this content: {exc}")
    except FalError as exc:
        return _fail(supa, production_id, f"fal generation failed: {exc}")

    urls = fal.video_urls(result)
    if not urls:
        return _fail(supa, production_id, f"fal reported success but returned no video: {result}")

    if backend == FAL_FULL:
        # Nothing else to do on fal's side; assembly happens where ffmpeg is.
        supa.update_production(production_id, stage="fal generated")
        return {
            **carry,
            "state": "complete",
            "progress": 100,
            "phase": "fal",
            "fal_video_urls": urls,
            "video_ref": urls[0],
            "subtitle_path": None,
            "script": None,
        }

    return _handoff_to_mpt(event, supa, mpt, fal, urls, carry)


def _handoff_to_mpt(
    event: dict[str, Any],
    supa: Supa,
    mpt: MptClient,
    fal: FalClient,
    urls: list[str],
    carry: dict[str, Any],
) -> dict[str, Any]:
    """Upload fal's clips into MoneyPrinterTurbo and start the real render.

    `video_source="local"` is the only mode that accepts material MPT did not
    fetch itself, and it resolves each entry as a path inside
    storage/local_videos -- so the bytes must be uploaded, not linked. fal's
    URLs also expire, which is the other reason this happens now rather than
    later.
    """
    production_id = event["production_id"]
    production = supa.production(production_id)
    idea = supa.idea(production["idea_id"])
    preset = supa.style_preset(production["style_preset_id"])

    supa.update_production(production_id, stage="uploading fal clips")
    names: list[str] = []
    with tempfile.TemporaryDirectory(prefix=f"fal-{production_id}-") as tmp:
        for index, url in enumerate(urls):
            local = fal.download(url, Path(tmp) / f"clip-{index:02d}.mp4")
            names.append(mpt.upload_material(local))

    params = _build_params(idea, preset, production_id)
    # Forced, not taken from the preset: any other source would make MPT fetch
    # its own footage and silently discard everything fal just generated.
    params.video_source = "local"
    params.video_materials = [{"provider": "local", "url": name} for name in names]

    try:
        returned = mpt.submit_render(params)
    except MptQueueFull:
        # The clips are uploaded and paid for, so releasing the claim would
        # risk regenerating them. Keep the claim and let the state machine
        # retry this state instead.
        raise
    except Exception as exc:  # noqa: BLE001
        return _fail(
            supa, production_id,
            f"fal clips were generated but the assembling render could not start: {exc}",
        )

    supa.update_production(production_id, stage="assembling")
    return {
        **carry,
        "phase": MPT,
        "task_id": returned,
        "state": "running",
        "progress": 50,
        # The clock restarts: the render budget applies to the render, not to
        # the fal generation that preceded it.
        "started_at": time.time(),
    }


def _fail(supa: Supa, production_id: str, message: str) -> dict[str, Any]:
    """Park rather than retry.

    The requirement is explicit: a failure stops for a human instead of
    spending again automatically.
    """
    log.warning("parking production %s: %s", production_id, message)
    supa.park(production_id, message)
    return {"production_id": production_id, "state": "failed", "error": message}


def fetch_and_qc(
    event: dict[str, Any], supa: Supa | None = None, mpt: MptClient | None = None
) -> dict[str, Any]:
    """Move the render into object storage, then inspect it.

    Fetching and checking are one activity on purpose: both need the actual
    bytes, and a 9:16 render can be hundreds of megabytes, so downloading it
    twice to keep the steps tidy would be a poor trade.

    The hop through storage is load-bearing rather than an optimisation.
    MoneyPrinterTurbo has no object-storage support and serves artifacts from
    behind its own API key, while Postiz fetches through an SSRF-safe
    dispatcher that rejects private addresses -- so Postiz can never be pointed
    at MPT directly.
    """
    supa = supa or Supa()
    mpt = mpt or MptClient()
    production_id = event["production_id"]
    video_ref = event["video_ref"]
    backend = event.get("backend") or MPT

    has_subtitles = bool(event.get("subtitle_path"))

    with tempfile.TemporaryDirectory(prefix=f"render-{production_id}-") as tmp:
        if backend == FAL_FULL:
            # No MoneyPrinterTurbo render exists to fetch: fal produced raw
            # clips and speech, and everything MPT would have done has to
            # happen here.
            local, has_subtitles = _assemble_fal_full(event, supa, Path(tmp))
        else:
            # Both the pure-MPT path and the visuals path converge here: the
            # visuals path's finished file is an ordinary MPT render.
            local = mpt.download_artifact(video_ref, Path(tmp) / "final.mp4")

        info = probe_mod.probe(local)
        key = supa.upload_render(production_id, local, "final.mp4")

        thumb_key = None
        try:
            thumb = _extract_poster(local, Path(tmp) / "poster.jpg")
            thumb_key = supa.upload_render(production_id, thumb, "poster.jpg")
        except Exception as exc:  # noqa: BLE001 - a missing poster must not block review
            log.warning("could not extract a poster frame for %s: %s", production_id, exc)

        # Measure while the file is still local. Each of these shells out to
        # ffmpeg and is tolerant of failure: a measurement we could not take
        # becomes a warn in the report, never a hard stop, because a reviewer
        # looking at a real video is more useful than a blocked pipeline.
        frozen_s, cuts, volume = 0.0, 0, None
        try:
            frozen_s = probe_mod.frozen_seconds(local)
            cuts = probe_mod.scene_change_count(local)
            volume = probe_mod.mean_volume_db(local)
        except Exception as exc:  # noqa: BLE001
            log.warning("partial QC measurement for %s: %s", production_id, exc)

    report = slideshow.build_report(
        info,
        frozen_s=frozen_s,
        scene_changes=cuts,
        mean_volume_db=volume,
        has_subtitles=has_subtitles,
    )

    supa.update_production(
        production_id,
        stage="checked",
        video_url=supa.signed_render_url(key, expires_in=7 * 24 * 3600),
        thumbnail_url=supa.signed_render_url(thumb_key, expires_in=7 * 24 * 3600)
        if thumb_key
        else None,
        duration_seconds=round(info.duration_s, 2),
        qc=report.model_dump(exclude_none=True),
    )
    return {
        "production_id": production_id,
        "storage_key": key,
        "duration_s": info.duration_s,
        "size_bytes": info.size_bytes,
        # Drives the branch into Gate 2: a failed check still enters the gate,
        # it just arrives flagged, so an owner can override it and publish.
        "qc_passed": report.passed,
        "slideshow_risk": report.slideshow_risk,
    }


def _assemble_fal_full(
    event: dict[str, Any], supa: Supa, tmp: Path, fal: FalClient | None = None
) -> tuple[Path, bool]:
    """Turn fal's raw output into a finished reel.

    This is the cost of the end-to-end path, and the reason the visuals path is
    the default: everything MoneyPrinterTurbo does after generation has to be
    reproduced here.

    Captions come from transcribing the narration we just synthesised, not from
    splitting the script on punctuation. A caption track built from text alone
    drifts within a couple of sentences, and captions that are visibly out of
    sync read as broken rather than absent.
    """
    from pipeline import assemble

    fal = fal or FalClient()
    production_id = event["production_id"]
    production = supa.production(production_id)
    preset = supa.style_preset(production["style_preset_id"])
    cfg = (preset.get("params") or {}).get("fal") or {}

    urls = event.get("fal_video_urls") or [event["video_ref"]]
    clips = [
        fal.download(url, tmp / f"clip-{i:02d}.mp4") for i, url in enumerate(urls)
    ]

    supa.update_production(production_id, stage="assembling (fal)")
    joined = assemble.concat(clips, tmp / "joined.mp4")
    portrait = assemble.to_portrait(joined, tmp / "portrait.mp4")

    script = (production.get("script") or "").strip()
    if not script:
        # Without narration there is nothing to voice or caption, so ship the
        # visuals and let the quality report say what is missing rather than
        # discarding a generation that has already been paid for.
        log.warning("production %s has no script; shipping silent visuals", production_id)
        return portrait, False

    tts_model = cfg.get("tts_model")
    if not tts_model:
        return portrait, False

    voiced = portrait
    has_subtitles = False
    try:
        speech = fal.wait(fal.submit(tts_model, {"text": script[:4000]}))
        audio_url = fal.audio_url(speech)
        if not audio_url:
            raise FalError(f"tts returned no audio: {speech}")
        audio = fal.download(audio_url, tmp / "narration.mp3")
        voiced = assemble.mux_narration(portrait, audio, tmp / "voiced.mp4")

        if cfg.get("burn_captions", True):
            words = fal.wait(fal.transcribe(audio_url))
            srt = assemble.srt_from_transcription(words, tmp / "captions.srt")
            if srt:
                voiced = assemble.burn_subtitles(
                    voiced, srt, tmp / "captioned.mp4", font_size=cfg.get("font_size", 60)
                )
                has_subtitles = True
    except Exception as exc:  # noqa: BLE001
        # Narration or captions failing does not justify throwing away paid
        # visuals. The quality report records what is missing and the reviewer
        # decides at Gate 2.
        log.warning("fal narration/captions incomplete for %s: %s", production_id, exc)

    return voiced, has_subtitles


def _extract_poster(video: Path, dest: Path) -> Path:
    """A frame for the review screen's video poster.

    Taken a second in rather than at zero, because the first frame of a
    generated video is very often black.
    """
    import subprocess

    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "1", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", "-y", str(dest)],
        check=True, capture_output=True, timeout=120,
    )
    if not dest.exists() or dest.stat().st_size == 0:
        raise MptError("ffmpeg produced no poster frame")
    return dest
