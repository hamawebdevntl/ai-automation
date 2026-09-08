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
from dataclasses import dataclass
from dataclasses import replace as replace_dataclass
from pathlib import Path
from typing import Any

from pipeline import spend
from pipeline.clients.fal import FalClient, FalError, FalRefused
from pipeline.clients.heygen import HeyGenClient, HeyGenError, HeyGenRejected
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
HEYGEN = "heygen"            # HeyGen delivers a finished presenter reel

FAL_MODES = (FAL_VISUALS, FAL_FULL)


# A wallet with less in it than this cannot fund a presenter render on any
# plan, so a balance below it refuses even when the estimate says otherwise --
# which is the case worth catching, since the estimate is derived from a rate
# an owner can edit and the wallet is not.
MIN_WALLET_BALANCE_USD = 0.50


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
    return MPT


@dataclass(frozen=True)
class _Spend:
    """What this render will be billed as, decided before it is submitted.

    Carried from the spend gate to the ledger write so that the figure a cap
    was checked against and the figure recorded against that cap are the same
    number, rather than two computations of it.
    """

    provider: str
    model: str
    rate: spend.Rate | None
    estimate_usd: float
    quantity: dict[str, Any]
    """What the estimate was computed from. Written to `render_spend.detail`,
    so a surprising figure can be read back rather than re-derived."""

    balance_before: float | None = None
    """The provider's wallet before this render, where it reports one. HeyGen
    only, and the basis of the measured figure recorded when the render lands."""

    secondary_models: tuple[str, ...] = ()
    """Other models this render will bill, beyond the one it is capped under.

    The fal end-to-end lane bills three times -- generation, narration,
    transcription -- and each is priced per its own model. They are collected at
    the gate so that a per-model cap on any of them refuses the render *before*
    the visuals are paid for, rather than after, when parking would throw away a
    generation already billed."""

    def as_payload(self) -> dict[str, Any]:
        """The plan, small and JSON-safe, for `run_state` to carry.

        Only the presenter lane carries it, because it is the only one with a
        better figure to come: HeyGen reports a wallet and a finished duration,
        where fal's cost is exactly the duration we already asked it for and
        MoneyPrinterTurbo's stock lane is free.
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "unit": self.rate.unit if self.rate else None,
            "rate_usd": self.rate.rate_usd if self.rate else None,
            "estimate_usd": self.estimate_usd,
            "balance_before": self.balance_before,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> _Spend | None:
        """Rebuild a plan a poll was handed. None when the payload predates it."""
        if not isinstance(payload, dict) or not payload.get("provider"):
            return None
        unit = payload.get("unit")
        rate = (
            spend.Rate(
                provider=str(payload["provider"]),
                model=str(payload.get("model") or ""),
                unit=str(unit),
                rate_usd=float(payload.get("rate_usd") or 0.0),
            )
            if unit
            else None
        )
        balance = payload.get("balance_before")
        return cls(
            provider=str(payload["provider"]),
            model=str(payload.get("model") or ""),
            rate=rate,
            estimate_usd=float(payload.get("estimate_usd") or 0.0),
            quantity={},
            balance_before=float(balance) if balance is not None else None,
        )


def _quantities(provider: str, preset: dict[str, Any], script: str) -> dict[str, Any]:
    """Everything a rate might be per, for this render.

    All three are computed regardless of the unit in force, so that changing a
    rate's unit in the database is a change in one place rather than a change
    here as well. `Rate.amount_usd` picks the one it needs.
    """
    params = preset.get("params") or {}
    if provider == spend.FAL:
        cfg = params.get("fal") or {}
        # The same arithmetic `_submit_fal` sends, so the cap is checked
        # against the duration fal is actually asked for.
        seconds = float(_fal_duration(cfg))
        clips = int(cfg.get("clips", 1) or 1)
    else:
        # No duration exists yet on the other two lanes: MoneyPrinterTurbo cuts
        # footage to the narration and HeyGen reads it aloud, so the script is
        # what decides the runtime in both cases.
        seconds = spend.speech_seconds(script)
        clips = spend.clip_count(seconds, float(params.get("video_clip_duration") or 0) or 5.0)
    return {"seconds": seconds, "characters": len(script or ""), "clips": clips}


def _secondary_models(provider: str, preset: dict[str, Any]) -> tuple[str, ...]:
    """Every other model this render will be billed for.

    Only the fal end-to-end lane has any: it makes the pictures, then the
    speech, then transcribes the speech to place the captions, and each is a
    separate model on a separate rate. Collected here so the gate can refuse a
    render whose *narration* model is at its ceiling -- checking that only when
    the TTS call comes round would mean parking a production whose visuals had
    already been paid for.
    """
    if provider != spend.FAL:
        return ()
    cfg = (preset.get("params") or {}).get("fal") or {}
    tts = str(cfg.get("tts_model") or "")
    if not tts:
        return ()
    models = [tts]
    if cfg.get("burn_captions", True):
        models.append(str(cfg.get("transcribe_model") or settings().fal_transcribe_model))
    return tuple(dict.fromkeys(model for model in models if model))


def _spend_gate(
    supa: Supa,
    production_id: str,
    preset: dict[str, Any],
    script: str,
    heygen: HeyGenClient | None = None,
) -> _Spend | dict[str, Any]:
    """Refuse a render there is no budget for, and price the one there is.

    Sits beside the script gate's restatement and for the same reason: this is
    the last statement before money moves, and the two things worth checking
    twice are "nobody approved these words" and "there is nothing left to say
    them with". Returns a plan, or the park result of whichever refusal fired.

    Three refusals, in the order they cost anything to find out:

      1. **The counter.** `spend_block_reason()` in Postgres -- the same
         function Gate 1 calls, so a style the app called unpickable cannot
         somehow be rendered by the worker.
      2. **No rate.** A provider that reports no figure of its own and has no
         rate is a render whose cost cannot be counted against any ceiling.
         The realistic way in is a preset pointed at a premium fal tier, which
         is the $1,500-5,400-a-month case, so this refuses rather than
         defaulting the cost to zero.
      3. **The provider's own balance.** Our counter can only be as right as
         what we have recorded; the wallet is the account's own answer. HeyGen
         is the one provider that reports one.

    All three happen before `claim_render_slot`, so a refusal leaves `task_id`
    null and the production retryable rather than parked holding a claim it
    never spent.
    """
    key = supa.style_preset_spend(preset["id"])
    provider = str(key.get("provider") or "")
    model = str(key.get("model") or "")

    reason = supa.spend_block_reason(provider, model)
    if reason:
        return _fail(supa, production_id, f"no render was submitted: {reason}")

    # And the models this render will bill *besides* the one it is capped
    # under. Without this a per-model cap on the narration model would show as
    # reached in Settings while renders went on using it.
    secondary = _secondary_models(provider, preset)
    for extra in secondary:
        reason = supa.spend_block_reason(provider, extra)
        if reason:
            return _fail(supa, production_id, f"no render was submitted: {reason}")

    row = supa.spend_rate(provider, model)
    if row is None and provider not in spend.REPORTS_ITS_OWN_COST:
        named = f"{provider} / {model}" if model else provider
        return _fail(
            supa,
            production_id,
            f"no render was submitted: nothing prices {named}, so what it costs could "
            f"not be counted against a spend cap. Add one under Provider rates in "
            f"Settings, then retry.",
        )

    quantity = _quantities(provider, preset, script)
    rate = spend.Rate.from_row(row) if row else None
    try:
        estimate = rate.amount_usd(**quantity) if rate else 0.0
    except spend.Unpriceable as exc:
        return _fail(supa, production_id, f"no render was submitted: {exc}")

    balance: float | None = None
    if provider == spend.HEYGEN:
        balance = (heygen or HeyGenClient()).balance()
        # The rate is optional on this lane only because the wallet can price
        # it instead. With neither, nothing can say what the render cost, and
        # the cap would go on reading zero however many presenter reels were
        # made -- which is the same hole an unpriced fal model would open.
        if rate is None and balance is None:
            return _fail(
                supa,
                production_id,
                "no render was submitted: nothing prices heygen and its wallet balance "
                "could not be read, so what this render costs could not be counted "
                "against a spend cap. Add a rate for it under Provider rates in Settings.",
            )
        if balance is not None and balance < max(estimate, MIN_WALLET_BALANCE_USD):
            return _fail(
                supa,
                production_id,
                f"no render was submitted: the HeyGen wallet holds ${balance:.2f} and this "
                f"render needs about ${estimate:.2f}. Top the account up, or produce this "
                f"idea in another style.",
            )

    return _Spend(
        provider=provider,
        model=model,
        rate=rate,
        estimate_usd=estimate,
        quantity=quantity,
        balance_before=balance,
        secondary_models=secondary,
    )


def _record_spend(
    supa: Supa,
    production_id: str,
    plan: _Spend,
    *,
    kind: str = spend.KIND_RENDER,
    amount_usd: float | None = None,
    source: str = "derived",
    external_ref: str = "",
    replace: bool = False,
    **detail: Any,
) -> None:
    """Put one charge in the ledger.

    Never swallows. `record_event` does, because a missing step log is a gap in
    an explanation; a missing ledger row is a ceiling that has stopped
    counting, and the whole point of this feature is that spend cannot happen
    unobserved. At submit it is called *before* the backend, so a ledger that
    will not accept the write stops the render instead of being outrun by it.
    """
    supa.record_spend(
        production_id,
        provider=plan.provider,
        model=plan.model,
        kind=kind,
        amount_usd=plan.estimate_usd if amount_usd is None else amount_usd,
        source=source,
        external_ref=external_ref,
        replace=replace,
        detail={
            "unit": plan.rate.unit if plan.rate else None,
            "rate_usd": plan.rate.rate_usd if plan.rate else None,
            "quantity": plan.quantity,
            **detail,
        },
    )


def submit_render(
    event: dict[str, Any],
    supa: Supa | None = None,
    mpt: MptClient | None = None,
    heygen: HeyGenClient | None = None,
    fal: FalClient | None = None,
) -> dict[str, Any]:
    """Start exactly one render for this production."""
    supa = supa or Supa()
    mpt = mpt or MptClient()
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

    # One HeyGen client for the whole submit, because two of the three things
    # that happen below want one: the spend gate reads the wallet, and the
    # submit sends the render. Built here rather than twice inside them so a
    # missing key is one failure rather than two identical ones.
    if mode == HEYGEN and heygen is None:
        heygen = HeyGenClient()

    # The spend gate. Deliberately after the script gate and before the claim:
    # the cheapest refusals first, and neither of them costs a claim.
    #
    plan = _spend_gate(supa, production_id, preset, script, heygen)
    if not isinstance(plan, _Spend):
        # A refusal, already parked. Returned rather than raised so it reads the
        # same way as the script gate's two refusals just above.
        return plan

    # The ledger, before the claim as well as before the backend.
    #
    # Before the *backend* because a render this ledger will not accept is a
    # render whose cost nothing would ever count. Before the *claim* because of
    # what a failure here would otherwise leave behind: a raise after
    # `claim_render_slot` has set `task_id` parks the production with the claim
    # held, and `retry_production` will not clear `task_id`, so the retry
    # deduplicates straight into `poll_render` and parks again on a render that
    # was never submitted. Writing first makes the failure a plain retry of a
    # step that has done nothing.
    #
    # This is the *reservation*: one charge per production, at the estimate. On
    # the fal lane `_record_fal_generation` then stamps it with the request id
    # fal answered with, so a resubmit that fal really did bill again lands
    # beside it rather than on top of it.
    #
    # Losing the claim race afterwards is harmless: the winner writes the same
    # row under the same key, and the write is an upsert that ignores a
    # duplicate.
    #
    # It also means a submit that then fails leaves an over-count rather than a
    # gap. That is the safe direction and it is deliberate: over-counting stops
    # renders that would have been affordable, and an owner can raise the cap;
    # under-counting bills for renders nobody authorised, and nobody can unbill
    # those.
    _record_spend(supa, production_id, plan, at="submit")

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

    if mode in FAL_MODES:
        return _submit_fal(production_id, idea, preset, mode, supa, script, fal, plan=plan)

    if mode == HEYGEN:
        return _submit_heygen(production_id, idea, preset, supa, script, plan, heygen)

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


def _fal_duration(cfg: dict[str, Any]) -> int:
    """Seconds of generation to ask fal for.

    One generation of the whole runtime, not one request per clip. The models
    we use accept a duration up to their own ceiling, so a single request is
    one thing to poll and one thing billed -- and on the visuals path
    MoneyPrinterTurbo subdivides it anyway, since preprocess_video cuts
    supplied material to video_clip_duration.

    Its own function because the spend gate needs the same number: fal bills
    per second of output, so this *is* the cost, and a second copy of the
    arithmetic would be a cap checked against a duration nobody ordered.
    """
    wanted = int(cfg.get("clips", 1)) * int(cfg.get("clip_seconds", 5))
    return max(1, min(wanted, int(cfg.get("max_duration_seconds", 20))))


def _submit_fal(
    production_id: str,
    idea: dict[str, Any],
    preset: dict[str, Any],
    mode: str,
    supa: Supa,
    script: str,
    fal: FalClient | None = None,
    plan: _Spend | None = None,
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

    duration = _fal_duration(cfg)

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
    # fal has answered with a request id, which is the only durable identity a
    # billed generation has here. Attaching it now is what lets a resubmit fal
    # really did bill again be counted as the second charge it is.
    if plan is not None:
        _record_fal_generation(supa, production_id, plan, handles["request_id"])

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


def _submit_heygen(
    production_id: str,
    idea: dict[str, Any],
    preset: dict[str, Any],
    supa: Supa,
    script: str,
    plan: _Spend | None = None,
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
        # Carried so the poller can subtract: the wallet before this render and
        # the wallet after it is HeyGen's own answer to what it charged, which
        # beats any rate we hold. Absent when the plan could not read a
        # balance, and the derived figure then stands.
        "spend": plan.as_payload() if plan else None,
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
    if backend in FAL_MODES and event.get("phase") == "fal":
        return _poll_fal(event, supa, mpt, polls)

    if backend == HEYGEN:
        return _poll_heygen(event, supa, polls)

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

    # The render is finished, so this is the last chance at a real figure --
    # and the first at HeyGen's own. Never allowed to fail the step: the
    # submit-time row is already counted against the cap, so the worst a
    # failure here costs is a coarser number on a production that is otherwise
    # complete.
    try:
        _record_measured_presenter_spend(supa, event, video, heygen)
    except Exception as exc:  # noqa: BLE001 - a finished render must not park over its own receipt
        log.warning("could not measure the presenter spend for %s: %s", production_id, exc)

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


# How far a wallet delta may exceed the derived figure and still be believed.
# The wallet is shared with every other job in the account, so a delta taken
# across a render that ran alongside two others would bill all three here. Three
# times is loose enough for a rate that is merely out of date and tight enough
# that a stranger's render does not land on this production.
WALLET_DELTA_SANITY = 3.0


def _record_measured_presenter_spend(
    supa: Supa, event: dict[str, Any], video: Any, heygen: HeyGenClient
) -> None:
    """Replace the submit-time estimate with what the render actually cost.

    Two figures, in order of authority:

      * **The wallet delta.** HeyGen's own number, and the only `reported`
        figure anywhere in this system. Believed when it is positive and within
        `WALLET_DELTA_SANITY` of the derived figure -- the account's wallet is
        shared, so an implausible delta is more likely another job's render than
        a surprising bill for this one.
      * **The rate against the finished duration.** Still derived, but from the
        length HeyGen actually produced rather than from a guess at how long
        the script would take to read.

    Either way it replaces the submit-time row rather than adding to it: the
    ledger holds one charge per production per provider per kind, and this is
    the same charge, better known.
    """
    plan = _Spend.from_payload(event.get("spend"))
    if plan is None:
        return
    production_id = event["production_id"]

    seconds = float(getattr(video, "duration", 0) or 0) or None
    derived = plan.estimate_usd
    if plan.rate and seconds:
        try:
            derived = plan.rate.amount_usd(seconds=seconds)
        except spend.Unpriceable:
            derived = plan.estimate_usd

    amount, source = derived, "derived"
    if plan.balance_before is not None:
        after = heygen.balance()
        if after is not None:
            delta = round(plan.balance_before - after, 4)
            # The sanity ceiling needs something sane to compare against. With
            # no rate for this lane there is no derived figure -- the gate
            # allows that only because the wallet can price the render instead
            # -- and applying a ceiling built from a zero would reject every
            # real charge and leave the cap reading nothing however many
            # presenter reels were made.
            plausible = (
                delta <= max(derived, MIN_WALLET_BALANCE_USD) * WALLET_DELTA_SANITY
                if plan.rate is not None
                else True
            )
            if delta > 0 and plausible:
                amount, source = delta, "reported"
            elif delta != 0:
                log.info(
                    "ignoring an implausible HeyGen wallet delta of %s on %s (derived %s); "
                    "the account's wallet is shared with every other job on it",
                    delta,
                    production_id,
                    derived,
                )

    _record_spend(
        supa,
        production_id,
        replace_dataclass(plan, quantity={"seconds": seconds}),
        amount_usd=amount,
        source=source,
        replace=True,
        at="complete",
    )


def _record_fal_generation(
    supa: Supa, production_id: str, plan: _Spend, request_id: str
) -> None:
    """Attach fal's own identity to the charge for this generation.

    fal is the only backend that deduplicates nothing: our MoneyPrinterTurbo
    fork replays a resubmit on the caller-supplied task id, and HeyGen replays
    one on its `Idempotency-Key`, but every successful `fal.submit` is a fresh
    billed generation. So one production can genuinely owe fal for two, and
    only fal's `request_id` can tell them apart.

    Three cases, and the reservation written before the submit is what makes
    them distinguishable:

      * **This request is already on the ledger.** The step was re-entered
        without fal billing again. Nothing to do.
      * **The reservation has not been stamped yet.** This is the production's
        first generation, so the reservation *is* its charge -- stamped in
        place, keeping one row rather than two for one render.
      * **Every charge on the ledger names a different request.** fal has
        billed again. A second row, keyed by this request id.

    A submit attempt number would not work in place of this: the driver strips
    `attempts` from the payload it hands an activity, and `retry_production`
    clears the counter for the step being retried, so it would read zero on the
    retry that bills the second time.
    """
    if not request_id:
        return
    try:
        charges = supa.render_charges(production_id, spend.FAL, plan.model, spend.KIND_RENDER)
    except Exception as exc:  # noqa: BLE001 - the reservation already stands
        log.warning("could not read the fal charges for %s: %s", production_id, exc)
        return

    named = {str((row.get("detail") or {}).get("request_id") or "") for row in charges}
    if request_id in named:
        return
    if "" in named or not charges:
        # The reservation, not yet attached to a generation.
        _record_spend(
            supa, production_id, plan, replace=True, request_id=request_id, at="submit"
        )
        return

    # Everything on the ledger belongs to some other generation, so this one is
    # additional and fal has been paid twice.
    log.warning(
        "fal billed production %s a second generation (%s); recording it as a separate charge",
        production_id,
        request_id,
    )
    _record_spend(
        supa,
        production_id,
        plan,
        external_ref=request_id,
        request_id=request_id,
        at="resubmit",
    )


def _record_fal_extra(
    supa: Supa,
    production_id: str,
    model: str,
    kind: str,
    *,
    seconds: float | None = None,
    characters: int | None = None,
) -> None:
    """Record narration or transcription on the fal end-to-end lane.

    Its own charge rather than part of the render's, because it is priced
    differently -- per character of script, not per second of video -- and so
    that a per-model cap on the TTS model can bind on its own. That cap is
    checked at the gate, not here: `_secondary_models` collects every model this
    lane will bill and `_spend_gate` refuses on any of them, so the refusal
    happens before the visuals are paid for rather than after.

    Swallows its own failures, unlike the submit-time write. This runs after
    the visuals have already been paid for and recorded, so the cap is not
    blind either way, and a production must not park over the accounting of a
    call that succeeded.
    """
    try:
        row = supa.spend_rate(spend.FAL, model)
        if row is None:
            log.warning(
                "nothing prices fal / %s, so its %s spend is unrecorded -- add a rate "
                "under Provider rates in Settings",
                model,
                kind,
            )
            return
        rate = spend.Rate.from_row(row)
        amount = rate.amount_usd(seconds=seconds, characters=characters, clips=None)
        supa.record_spend(
            production_id,
            provider=spend.FAL,
            model=model,
            kind=kind,
            amount_usd=amount,
            source="derived",
            detail={
                "unit": rate.unit,
                "rate_usd": rate.rate_usd,
                "quantity": {"seconds": seconds, "characters": characters},
                "at": "assemble",
            },
        )
    except Exception as exc:  # noqa: BLE001 - accounting must not undo a paid call
        log.warning("could not record fal %s spend for %s: %s", kind, production_id, exc)


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
    mpt = mpt or MptClient()
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
        # A presenter reel is one continuous shot by design, so counting cuts
        # against it would warn on every video in the lane. Frozen fraction
        # still applies: an avatar render that stalled looks like a still.
        expect_cuts=backend != HEYGEN,
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
        narration = script[:4000]
        speech = fal.wait(fal.submit(tts_model, {"text": narration}))
        # Billed per character, and billed now: the call has returned.
        _record_fal_extra(
            supa, production_id, tts_model, spend.KIND_TTS, characters=len(narration)
        )
        audio_url = fal.audio_url(speech)
        if not audio_url:
            raise FalError(f"tts returned no audio: {speech}")
        audio = fal.download(audio_url, tmp / "narration.mp3")
        voiced = assemble.mux_narration(portrait, audio, tmp / "voiced.mp4")

        if cfg.get("burn_captions", True):
            # Resolved here and passed in, rather than left to `transcribe()`'s
            # own default. The charge has to name the model that was actually
            # called: pricing it against a different one would file the cost
            # under a model nobody used, and drop it entirely if that model has
            # no rate.
            transcribe_model = cfg.get("transcribe_model") or settings().fal_transcribe_model
            words = fal.wait(fal.transcribe(audio_url, transcribe_model))
            # Priced per second of audio, and the narration we just made is the
            # audio. Rounding error beside the video, and recorded anyway --
            # a ledger with a hole in it is a ledger nobody can reconcile.
            _record_fal_extra(
                supa,
                production_id,
                transcribe_model,
                spend.KIND_TRANSCRIBE,
                seconds=spend.speech_seconds(narration),
            )
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
