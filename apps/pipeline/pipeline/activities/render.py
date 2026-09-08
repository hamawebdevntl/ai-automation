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
from pipeline.clients.heygen import HeyGenClient, HeyGenError, HeyGenRejected
from pipeline.clients.mpt import MptClient, MptError, MptQueueFull, MptTaskStateLost
from pipeline.clients.supa import Supa
from pipeline.config import settings
from pipeline.models import ProductionStatus, Transcript, VideoParams
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
HEYGEN = "heygen"            # HeyGen delivers a finished presenter reel
FAL_VIDEO = "fal_video"      # fal transforms footage the owner uploaded
CLIP = "clip"                # no provider: cut a range out of an uploaded recording

FAL_MODES = (FAL_VISUALS, FAL_FULL, FAL_VIDEO)

# Modes whose input is a file rather than text. These need three things on the
# row before a cent may be spent -- footage, an instruction, and a consent
# record -- and `_missing_source_input` is the one place that says which.
SOURCE_MODES = (FAL_VIDEO,)

# The longest instruction sent to a model. The database caps the column at the
# same number, so this only ever bites if the two drift apart.
MAX_INSTRUCTION_CHARS = 1500

# Lanes the quality check must not count cuts against.
#
# A presenter reel is one continuous shot by design. The source lane's cutting
# is whatever the owner uploaded, and a single-take piece to camera is a
# perfectly good source video -- so warning that it has no cuts would be
# reporting on their footage rather than on our render. Frozen fraction still
# applies to both: a render that stalled looks like a still either way.
# A clip is a continuous excerpt of one recording, so it has as many cuts as the
# owner's camera did -- usually none. Counting them against it would warn on
# every clip of a person talking, which is the whole intended input.
NO_CUTS_EXPECTED = (HEYGEN, FAL_VIDEO, CLIP)


def _phase_for(mode: str) -> str:
    """Which backend the job is sitting on right now.

    `phase` is not the same as `backend`. The visuals path starts on fal and
    finishes on MoneyPrinterTurbo, so the poller needs to know where the work
    currently is; the other two lanes never move.
    """
    if mode in FAL_MODES:
        return "fal"
    if mode == HEYGEN:
        return HEYGEN
    if mode == CLIP:
        # Never anywhere else. The clipping lane's work is ffmpeg on a file we
        # already hold, so there is no provider for the phase to name.
        return CLIP
    return MPT


def submit_render(
    event: dict[str, Any], supa: Supa | None = None, mpt: MptClient | None = None
) -> dict[str, Any]:
    """Start exactly one render for this production."""
    supa = supa or Supa()
    production_id = event["production_id"]

    production = supa.production(production_id)
    idea = supa.idea(production["idea_id"])
    preset = supa.style_preset(production["style_preset_id"])
    mode = preset.get("render_mode") or MPT

    # The script gate, restated locally.
    #
    # `claim_production` will not return a row at `await_script` until
    # `script_approved_at` is set, so in a correct system this branch is
    # unreachable -- which is exactly why it is here. It is the last statement
    # before money is spent, and the cost of the two checks being out of step
    # is a paid render of words nobody approved. Parking is the only safe
    # outcome: it spends nothing and it is visible.
    script = (production.get("script") or "").strip()
    if not production.get("script_approved_at"):
        return _fail(
            supa,
            production_id,
            "no render was submitted: this production reached the render step "
            "without an approved script. Approve the script and retry.",
        )
    if not script:
        return _fail(
            supa,
            production_id,
            "no render was submitted: the script is approved but empty.",
        )

    # The source lane's inputs and its configuration, both checked here rather
    # than in `_submit_fal_video`.
    #
    # The inputs are restated locally for exactly the reason the script check
    # above is: `approve_script` refuses to open the gate without them and
    # `productions_source_lane_is_ready` refuses the `task_id` write underneath
    # it, so in a correct system this is unreachable -- and reaching it would
    # mean paying to transform footage nobody attached.
    #
    # The *configuration* is checked here because of where the claim is taken.
    # A few lines below, `claim_render_slot` sets `task_id`, and that is
    # irreversible in practice: a row that parks holding one can never have its
    # script or its footage edited again, so a mistyped setting would strand the
    # production rather than stop it. Parking before the claim leaves it
    # retryable, which is the correct outcome for a fault a person can fix.
    if mode in SOURCE_MODES:
        problem = _missing_source_input(production) or _source_lane_config_problem(preset)
        if problem:
            return _fail(supa, production_id, f"no render was submitted: {problem}")

    # MoneyPrinterTurbo, but only on the lane that uses it.
    #
    # `MptClient.__init__` refuses to be constructed without `MPT_BASE_URL` and
    # says why: "nothing reaches this constructor unless a render is actually
    # about to be submitted". That was not true -- this function built one
    # eagerly for every lane -- so a deployment that renders only with HeyGen,
    # only with fal, or (now) only by clipping had to invent credentials for a
    # service it never contacts. The same argument `llm.text_client` makes for
    # not routing scripts through MPT.
    #
    # Constructed *before* the claim rather than in the branch below, which is
    # the ordering that matters: a misconfigured base URL discovered after
    # `claim_render_slot` would park the row holding a `task_id` that nothing an
    # owner can reach is able to unset, and its script and footage would be
    # uneditable forever.
    if mode == MPT:
        mpt = mpt or MptClient()

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
            "phase": _phase_for(mode),
            "task_id": existing or task_id,
            "deduplicated": True,
            "polls": 0,
            "started_at": time.time(),
        }

    supa.update_production(production_id, render_backend=mode)

    if mode == CLIP:
        return _submit_clip(production_id, idea, supa)

    if mode == FAL_VIDEO:
        return _submit_fal_video(production_id, production, preset, supa)

    if mode in FAL_MODES:
        return _submit_fal(production_id, idea, preset, mode, supa, script)

    if mode == HEYGEN:
        return _submit_heygen(production_id, idea, preset, supa, script)

    params = _build_params(idea, preset, task_id, script)

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
    script: str,
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

    # The end-to-end path used to generate its narration here, *after* the
    # billed submit above -- fal makes pictures and speech, not a script, so
    # there was no other source for it. That is now the script gate's job, and
    # the text arriving as an argument is text a person has read and approved.
    # `_assemble_fal_full` reads it back off the row when it runs the TTS pass.
    #
    # Nothing to write: the script is already on the row, which is where it came
    # from. The old branch here was the last place in the pipeline where a
    # script could appear after money had moved.
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


def _missing_source_input(production: dict[str, Any]) -> str | None:
    """Which of the source lane's three inputs is absent, in words.

    One function so that the pipeline, `approve_script` and the review screen
    are all answering the same question in the same order -- an owner told
    "upload a video" who then gets told "write an instruction" is being walked
    through a checklist, which is the point.
    """
    if not (production.get("source_video_key") or "").strip():
        return "this style renders from uploaded footage, and none is attached."
    if not (production.get("render_instruction") or "").strip():
        return "footage is attached but no instruction says what to do with it."
    if not production.get("source_consent_at"):
        return "no consent has been recorded for the people in the uploaded footage."
    return None


def _source_lane_config_problem(preset: dict[str, Any]) -> str | None:
    """Anything about this lane's setup that would fail the submit, in words.

    Separate from the inputs because it is a different kind of fault -- the
    owner supplied everything and an operator did not -- but it is checked in
    the same place and for the same reason: before the claim, so the production
    parks retryable rather than stranded.
    """
    model = ((preset.get("params") or {}).get("fal") or {}).get("model")
    if not model:
        return (
            f"the style preset {preset.get('slug')!r} renders from uploaded footage "
            f"but names no params.fal.model."
        )

    conf = settings()
    ttl = int(conf.source_video_url_ttl_seconds)
    budget = int(conf.fal_poll_budget_seconds)
    if ttl <= budget:
        return (
            f"SOURCE_VIDEO_URL_TTL_SECONDS is {ttl}s, which does not outlive the "
            f"{budget}s fal poll budget. The provider fetches the upload when the job "
            f"leaves the queue, so the signed URL must outlive the whole render."
        )
    return None


def _fal_video_payload(cfg: dict[str, Any], url: str, instruction: str) -> dict[str, Any]:
    """The request body, built from the preset and the two things the owner gave.

    Split out from the submit so that everything which can raise on a malformed
    preset -- `float()`, `int()` -- happens in the block that releases the claim
    rather than in the one that keeps it.
    """
    payload: dict[str, Any] = {
        "video_url": url,
        "prompt": instruction.strip()[:MAX_INSTRUCTION_CHARS],
    }
    # Asked for explicitly, because the quality check *fails* a render that is
    # not 9:16 and the source here is whatever the owner had on their phone. A
    # model that honours this turns a landscape upload into a publishable reel;
    # one that ignores it produces a landscape render that arrives at Gate 2
    # flagged, which is the honest outcome but a paid one. The plainest fix is
    # upstream of all of this -- the panel says portrait works best.
    if cfg.get("aspect_ratio"):
        payload["aspect_ratio"] = cfg["aspect_ratio"]
    if cfg.get("resolution"):
        payload["resolution"] = cfg["resolution"]
    if cfg.get("strength") is not None:
        payload["strength"] = float(cfg["strength"])
    if cfg.get("fps"):
        payload["fps"] = int(cfg["fps"])
    # A ceiling on the bill as much as on the runtime, and the only one there
    # is: the rate is per second of output, and on this lane the length comes
    # from a file the owner chose rather than from a preset. Models that ignore
    # the field follow the upload instead, so this is a request and not yet a
    # cap -- which is one of the two reasons the preset ships inactive.
    if cfg.get("max_duration_seconds"):
        payload["duration"] = int(cfg["max_duration_seconds"])
    return payload


def _submit_fal_video(
    production_id: str,
    production: dict[str, Any],
    preset: dict[str, Any],
    supa: Supa,
    fal: FalClient | None = None,
) -> dict[str, Any]:
    """Ask fal to make a new video out of one the owner uploaded.

    The instruction is the prompt, verbatim. That is the whole reason it is held
    to the script gate: on this lane the words a person approved are not
    narration, they are the thing the model is told to do, and nothing else in
    the request is a human decision.

    Everything that could *refuse* this submit has already been checked by
    `submit_render`, before the render claim was taken. That ordering is not
    tidiness: `task_id` cannot be unset by anything an owner can reach, so a row
    that parks holding one can never have its script or its footage edited
    again. It is stranded rather than retryable.

    The two blocks below divide on exactly that line, and the division is the
    most important thing in this function:

      * **Before `fal.submit`** nothing has been billed, so a failure releases
        the claim and the production is picked up again. Minting the signed URL
        is a Supabase Storage call and Storage can be down; a blip there is not
        in `submit_render`'s retry list, so without this it would strand the row.
      * **At `fal.submit`** it has, or may have. fal has no idempotency key --
        `docs/GAPS.md` B6 -- so an ambiguous failure must not become a second
        billed generation. The claim is kept and the production parks. Being
        uneditable afterwards is the correct outcome there: a render may exist
        that was paid for against exactly this footage and these words.

    Note what a resubmit here would *not* cost, against what B6 feared: the
    owner's file is already in our bucket, put there by their own browser before
    the gate, so nothing is re-uploaded. Only the generation is at stake.
    """
    fal = fal or FalClient()

    # Both already checked by `submit_render`, before the claim, so neither of
    # these raises on the real path. They are kept because this function is also
    # the unit under test, and because a raise here would strand a claimed row.
    problem = _missing_source_input(production) or _source_lane_config_problem(preset)
    if problem:
        raise ValueError(f"cannot submit a footage render: {problem}")

    cfg = (preset.get("params") or {}).get("fal") or {}
    model = cfg["model"]

    try:
        url = supa.signed_render_url(
            production["source_video_key"],
            expires_in=int(settings().source_video_url_ttl_seconds),
        )
        payload = _fal_video_payload(cfg, url, production.get("render_instruction") or "")
    except Exception:
        # Nothing billed yet. Releasing the claim is what keeps this a fault a
        # person can retry rather than a production nobody can touch.
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise

    try:
        handles = fal.submit(model, payload)
    except FalRefused as exc:
        # A refusal is about the footage or the instruction, not a fault. It
        # parks, and it must never be retried or routed to another backend.
        return _fail(supa, production_id, f"fal declined this footage or instruction: {exc}")

    supa.update_production(production_id, stage=f"fal transforming your footage ({model})")
    return {
        "production_id": production_id,
        "backend": FAL_VIDEO,
        "phase": "fal",
        "task_id": production_id,
        "fal_model": model,
        "fal_request_id": handles["request_id"],
        "fal_status_url": handles["status_url"],
        "fal_response_url": handles["response_url"],
        "started_at": time.time(),
        "polls": 0,
    }


def _clip_plan(idea: dict[str, Any], supa: Supa) -> tuple[dict[str, Any], float, float]:
    """The recording, and the range of it this production is.

    Read from the *idea* rather than the candidate, and that is deliberate.
    `accept_clip_candidate` copies the range onto the idea at the moment the
    owner accepts it, so this reproduces exactly what they said yes to even if
    the candidate row were later corrected -- the same reason
    `productions.render_backend` records the mode that was in force rather than
    the preset's current one.
    """
    source_id = idea.get("clip_source_id")
    start = idea.get("clip_start_seconds")
    end = idea.get("clip_end_seconds")
    if not source_id or start is None or end is None:
        raise ValueError(
            "this style cuts a clip out of an uploaded recording, and this idea "
            "does not name one. It was not created by accepting a clip candidate."
        )

    source = supa.clip_source(str(source_id))
    if not (source.get("storage_key") or "").strip():
        raise ValueError(f"clip source {source_id} has no stored recording")

    start, end = float(start), float(end)
    if end <= start:
        raise ValueError(f"this clip does not run forwards: {start}s to {end}s")
    return source, start, end


def _submit_clip(production_id: str, idea: dict[str, Any], supa: Supa) -> dict[str, Any]:
    """Begin a clip. Nothing is submitted anywhere, because there is nowhere.

    The only lane with no provider and therefore no bill. Everything it needs is
    a file in our own bucket and a range on the idea, so this step is a
    validation and a handoff: it confirms the recording is still there and the
    range is sane, and leaves the work to `fetch_and_qc`, which is where the
    hour-long lease and ffmpeg are.

    A failure here releases the render claim. On every other lane that would be
    unsafe -- releasing a claim after a provider may have started billing is how
    you pay twice -- and here it is simply correct: nothing has been spent, so
    the production should be retryable rather than stranded holding a `task_id`
    nothing can unset.
    """
    try:
        source, start, end = _clip_plan(idea, supa)
    except Exception:
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise

    supa.update_production(
        production_id,
        stage=f"cutting {end - start:.0f}s from {source.get('filename') or 'your recording'}",
    )
    return {
        "production_id": production_id,
        "backend": CLIP,
        "phase": CLIP,
        "task_id": production_id,
        # The recording, not the output. `fetch_and_qc` reads this the way every
        # other lane reads a provider's download URL.
        "video_ref": source["storage_key"],
        "clip_source_id": source["id"],
        "clip_start_seconds": start,
        "clip_end_seconds": end,
        "started_at": time.time(),
        "polls": 0,
    }


def _submit_heygen(
    production_id: str,
    idea: dict[str, Any],
    preset: dict[str, Any],
    supa: Supa,
    script: str,
    heygen: HeyGenClient | None = None,
) -> dict[str, Any]:
    """Start a presenter render.

    The order this lane always insisted on -- script first, submit second -- is
    now the whole pipeline's order, and enforced a step earlier by the script
    gate rather than by this function's own care. A presenter video is nothing
    but a script delivered to camera, so a failure to write one must cost
    nothing; and once HeyGen has the script there is no cheap way to change it.
    That was the argument for drafting here, and it is the argument for the
    gate.

    Unlike the fal path, an ambiguous submit failure releases the claim. That is
    safe only because `POST /v3/videos` accepts an Idempotency-Key and we send
    the production id: a resubmit within 24 hours replays the original response
    and returns the same video id instead of billing a second render. This is
    the same property our MoneyPrinterTurbo fork gives us, arrived at
    differently.
    """
    heygen = heygen or HeyGenClient()

    cfg = (preset.get("params") or {}).get("heygen") or {}
    avatar_id = cfg.get("avatar_id")
    if not avatar_id:
        raise ValueError(
            f"preset {preset.get('slug')!r} has render_mode {HEYGEN} but no params.heygen.avatar_id"
        )

    subject = " -- ".join(p for p in (idea.get("title") or "", idea.get("hook") or "") if p)
    subject = subject.strip() or (idea.get("title") or "Untitled")

    # The approved words, verbatim. This lane no longer reaches for
    # MoneyPrinterTurbo at all -- `write_script` did that once, before the gate,
    # and a presenter reel whose script is approved now renders with the
    # drafting service entirely down.
    try:
        video = heygen.create_avatar_video(
            avatar_id=avatar_id,
            script=script,
            # The production id, so a retry replays rather than re-renders.
            idempotency_key=production_id,
            voice_id=cfg.get("voice_id"),
            aspect_ratio=cfg.get("aspect_ratio", "9:16"),
            resolution=cfg.get("resolution", "1080p"),
            # On by default. Our quality check looks for a caption track, and
            # a silent-scrolling feed makes captions the difference between a
            # watched reel and a skipped one.
            burn_captions=bool(cfg.get("burn_captions", True)),
            # Only meaningful for a look whose source frames are landscape:
            # 'cover' crops to fill 9:16 rather than pillarboxing the speaker.
            fit=cfg.get("fit"),
            title=f"{subject} [{production_id}]",
            voice_settings=cfg.get("voice_settings"),
            engine=cfg.get("engine"),
            background=cfg.get("background"),
        )
    except HeyGenRejected as exc:
        # An unknown avatar or a rejected field fails the same way every time,
        # so there is nothing to retry.
        return _fail(supa, production_id, f"HeyGen rejected the request: {exc}")
    except Exception:
        # Ambiguous, and safe: the idempotency key makes a resubmit a replay.
        supa.update_production(production_id, task_id=None, status=ProductionStatus.QUEUED.value)
        raise

    supa.update_production(production_id, stage="presenter rendering")
    return {
        "production_id": production_id,
        "backend": HEYGEN,
        "phase": HEYGEN,
        "task_id": production_id,
        "heygen_video_id": video.id,
        "heygen_captions": bool(cfg.get("burn_captions", True)),
        "started_at": time.time(),
        "polls": 0,
    }


def _build_params(
    idea: dict[str, Any], preset: dict[str, Any], task_id: str, script: str
) -> VideoParams:
    """Turn an approved idea plus its style preset into a render request.

    `style_presets.params` is documented as VideoParams overrides and is merged
    last, so a new upstream field can be driven from the database without a code
    change here.

    `video_script` is what makes the gate mean anything on this lane.
    MoneyPrinterTurbo generates its own narration when the field is empty, which
    is what it did on every render before the script gate existed -- so leaving
    it unset would have left the two default styles, Stock b-roll and
    Generative, rendering words the owner never saw while the editor claimed
    otherwise. Sending it is the difference between an editor and a decoration.

    It is set *before* the preset overrides are merged, and the merge below
    deliberately cannot reach it: `params` on a style preset is operator-set
    configuration, and no configuration should be able to silently replace a
    script a person approved.
    """
    subject_parts = [idea.get("title") or "", idea.get("hook") or ""]
    subject = " -- ".join(p for p in subject_parts if p).strip() or "Untitled"

    params = VideoParams(
        video_subject=subject,
        video_script=script,
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
            # A style preset is operator configuration. Letting it overwrite the
            # narration would mean a row in `style_presets` could silently
            # replace words a person approved, which is the one thing this whole
            # gate exists to prevent.
            if key == "video_script":
                log.warning(
                    "preset %s sets params.video_script; ignoring it in favour of "
                    "the approved script",
                    preset.get("slug"),
                )
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
    production_id = event["production_id"]
    task_id = event.get("task_id") or production_id
    polls = int(event.get("polls", 0)) + 1
    started_at = float(event.get("started_at") or 0)
    backend = event.get("backend") or MPT

    # While generation is still on fal, poll fal. Once the visuals path has
    # handed its clips to MoneyPrinterTurbo the phase flips and the rest of this
    # function serves both backends unchanged -- which is why the state machine
    # does not need to know which one ran.
    if backend in FAL_MODES and event.get("phase") == "fal":
        # The visuals lane hands its clips to MoneyPrinterTurbo when generation
        # finishes, so this one branch does need a client -- and only this one.
        return _poll_fal(event, supa, mpt or MptClient(), polls)

    if backend == HEYGEN:
        return _poll_heygen(event, supa, polls)

    if backend == CLIP:
        # Nothing to poll. There is no provider holding this render: the cut
        # happens in `fetch_and_qc`, where the lease is an hour and ffmpeg is,
        # so this step exists only because the graph routes submit -> poll ->
        # fetch and there is no reason to give this lane its own arc.
        #
        # The one visible cost is `poll_render`'s `wait_before=30`: a clip waits
        # half a minute before a free local cut starts. Worth less than a branch
        # in the graph that every other lane would have to be read around.
        return {
            **{k: v for k, v in event.items() if k != "state"},
            "state": "complete",
            "polls": polls,
            "progress": 100,
        }

    mpt = mpt or MptClient()
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

    if backend in (FAL_FULL, FAL_VIDEO):
        # Neither hands anything to MoneyPrinterTurbo. `fal_full` still has
        # assembly ahead of it, in `_assemble_fal_full`; `fal_video` has none at
        # all, because what fal returned is the reel.
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


def _poll_heygen(
    event: dict[str, Any], supa: Supa, polls: int, heygen: HeyGenClient | None = None
) -> dict[str, Any]:
    """Poll a presenter render.

    The simplest of the three lanes: HeyGen returns a finished 9:16 reel that
    is already voiced and captioned, so there is no handoff and no assembly --
    the phase never changes and this is the only poller the job ever sees.
    """
    heygen = heygen or HeyGenClient()
    production_id = event["production_id"]
    video_id = event.get("heygen_video_id")
    started_at = float(event.get("started_at") or 0)
    wants_captions = bool(event.get("heygen_captions", True))

    carry = {
        "production_id": production_id,
        "backend": HEYGEN,
        "phase": HEYGEN,
        "task_id": event.get("task_id") or production_id,
        "heygen_video_id": video_id,
        "heygen_captions": wants_captions,
        "started_at": started_at,
        "polls": polls,
    }

    if not video_id:
        return _fail(supa, production_id, "presenter render has no HeyGen video id to poll")

    budget = settings().heygen_poll_budget_seconds
    if started_at and (time.time() - started_at) > budget:
        return _fail(
            supa, production_id,
            f"HeyGen render exceeded its {budget}s budget (video {video_id})",
        )

    try:
        video = heygen.video(video_id)
    except HeyGenError as exc:
        # A poll is a read: a transient fault here has cost nothing and the
        # state machine's own retry on this state is the right place to absorb
        # it, so the error propagates rather than parking a paid render.
        log.warning("HeyGen poll failed for %s: %s", production_id, exc)
        raise

    if video.is_failed:
        return _fail(supa, production_id, f"HeyGen render failed -- {video.failure}")

    if video.is_running:
        supa.update_production(production_id, stage=f"presenter render ({video.status})")
        # HeyGen reports no percentage, only a state. Reporting a made-up
        # number would be worse than reporting none, so the progress figure is
        # coarse and honest.
        return {**carry, "state": "running", "progress": 50 if video.status == "processing" else 10}

    url = video.output_url(prefer_captioned=wants_captions)
    if not url:
        return _fail(
            supa, production_id,
            f"HeyGen reported completed but returned no downloadable video (video {video_id})",
        )

    # Says whether the file we are about to fetch actually carries burned-in
    # captions, rather than assuming it does because we asked. The quality
    # report reads this, and a mismatch here would have it lie in both
    # directions.
    captioned = bool(wants_captions and video.captioned_video_url)
    if wants_captions and not captioned:
        log.warning(
            "production %s asked HeyGen for burned-in captions but only the clean cut "
            "came back; shipping it uncaptioned for the reviewer to judge",
            production_id,
        )

    supa.update_production(production_id, stage="presenter rendered")
    return {
        **carry,
        "state": "complete",
        "progress": 100,
        "video_ref": url,
        "heygen_captioned": captioned,
        "heygen_subtitle_url": video.subtitle_url,
        "heygen_duration": video.duration,
        "subtitle_path": None,
        "script": None,
    }


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

    # The approved script, on the submit that actually renders. This is the
    # second `_build_params` call in the file and it is the one that matters for
    # the fal_visuals lane -- the first submit only asked fal for footage, and
    # this is where narration, captions and assembly are decided.
    params = _build_params(idea, preset, production_id, (production.get("script") or "").strip())
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
    at MPT directly. The presenter lane needs the same hop for a different
    reason: HeyGen's download URLs are presigned and expire, so the file has to
    be ours before anything downstream depends on it.
    """
    supa = supa or Supa()
    production_id = event["production_id"]
    video_ref = event["video_ref"]
    backend = event.get("backend") or MPT

    has_subtitles = bool(event.get("subtitle_path"))

    with tempfile.TemporaryDirectory(prefix=f"render-{production_id}-") as tmp:
        if backend == HEYGEN:
            # Nothing to assemble. HeyGen delivers a finished, voiced,
            # captioned 9:16 reel, so this lane only moves bytes -- which is
            # also why it is the one lane whose output MoneyPrinterTurbo never
            # touches. `heygen_captioned` is what the poller observed rather
            # than what the preset asked for.
            local = HeyGenClient().download(video_ref, Path(tmp) / "final.mp4")
            has_subtitles = bool(event.get("heygen_captioned"))
        elif backend == FAL_FULL:
            # No MoneyPrinterTurbo render exists to fetch: fal produced raw
            # clips and speech, and everything MPT would have done has to
            # happen here.
            local, has_subtitles = _assemble_fal_full(event, supa, Path(tmp))
        elif backend == CLIP:
            # The only lane whose input is a file we already own. Everything
            # happens here rather than at submit because this is the step with
            # the hour-long lease -- the download alone can be gigabytes.
            local, has_subtitles = _assemble_clip(event, supa, Path(tmp))
        elif backend == FAL_VIDEO:
            # The one lane with nothing to assemble and nothing to voice. The
            # model was given a finished video and an instruction, and what it
            # returned is a finished video -- so this only moves bytes, exactly
            # as the presenter lane does. fal's media URLs expire, which is why
            # it happens now rather than later.
            #
            # `has_subtitles` stays false, and that is honest rather than a gap:
            # nothing in this lane produces a caption track, so the quality
            # report says captions are missing and the reviewer decides at Gate
            # 2. Claiming otherwise would make the report lie about a real reel.
            local = FalClient().download(video_ref, Path(tmp) / "final.mp4")
        else:
            # Both the pure-MPT path and the visuals path converge here: the
            # visuals path's finished file is an ordinary MPT render. The client
            # is built here rather than at the top of the function so that a
            # lane which never touches MoneyPrinterTurbo does not require its
            # credentials -- see the note in `submit_render`.
            local = (mpt or MptClient()).download_artifact(video_ref, Path(tmp) / "final.mp4")

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
        expect_cuts=backend not in NO_CUTS_EXPECTED,
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


def _assemble_clip(
    event: dict[str, Any], supa: Supa, tmp: Path
) -> tuple[Path, bool]:
    """Cut the clip, reframe it to 9:16 and burn its own captions in.

    Four ffmpeg passes on a file from our own bucket, and no provider call at
    any point. The order matters: cut first, so every pass after it works on
    twenty seconds rather than on an hour.

    The captions are the reason the transcript is stored on `clip_sources`
    rather than recomputed. They come from the very segments the model chose
    this range from, so a caption cannot claim a word was said at a moment the
    range does not contain -- and the approved script is what supplies the
    words, so a correction made at the script gate reaches the burned-in text.
    See `clips.caption_segments`.

    Captions failing does not discard the clip. Unlike the generative lanes
    there is nothing paid to protect here, but the argument is the same one
    `_assemble_fal_full` makes: a reviewer looking at a real clip is more useful
    than a parked production, and the quality report is what says the captions
    are missing.
    """
    from pipeline import assemble, clips

    production_id = event["production_id"]
    production = supa.production(production_id)
    preset = supa.style_preset(production["style_preset_id"])
    cfg = (preset.get("params") or {}).get("clip") or {}

    start = float(event["clip_start_seconds"])
    end = float(event["clip_end_seconds"])

    supa.update_production(production_id, stage="fetching your recording")
    source_file = supa.download_render(event["video_ref"], tmp / "source")

    supa.update_production(production_id, stage=f"cutting {start:.0f}s-{end:.0f}s")
    piece = assemble.cut(source_file, start, end, tmp / "cut.mp4")

    # The source is deleted as soon as the cut exists. A long recording is the
    # largest file this pipeline ever handles and every clip from it downloads
    # its own copy, so holding both while ffmpeg reframes is what would fill the
    # disk on a host running two production threads.
    source_file.unlink(missing_ok=True)

    reframe = str(cfg.get("reframe") or assemble.CROP)
    supa.update_production(production_id, stage=f"reframing to 9:16 ({reframe})")
    portrait = assemble.reframe_portrait(piece, tmp / "portrait.mp4", mode=reframe)

    if not cfg.get("burn_captions", True):
        return portrait, False

    try:
        source = supa.clip_source(str(event["clip_source_id"]))
        transcript = Transcript.model_validate(source.get("transcript") or {})
        segments = clips.caption_segments(
            transcript, start, end, script=production.get("script")
        )
        srt = assemble.srt_from_segments(segments, tmp / "captions.srt", offset=start)
        if not srt:
            return portrait, False
        captioned = assemble.burn_subtitles(
            portrait, srt, tmp / "captioned.mp4", font_size=int(cfg.get("font_size", 60))
        )
        return captioned, True
    except Exception as exc:  # noqa: BLE001 - a missing caption track must not lose the cut
        log.warning("captions incomplete for clip %s: %s", production_id, exc)
        return portrait, False


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
