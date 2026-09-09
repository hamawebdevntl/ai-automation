"""The script gate: draft the narration, then stop for a person.

This is the step the pipeline never had. Before it, `productions.script` was
something the render happened to leave behind -- MoneyPrinterTurbo wrote its own
inside the render on two lanes, the fal end-to-end lane wrote one *after*
submitting a billed generation, and only HeyGen wrote one first. So the single
decision the README reserves for a human -- "decide *what we say*" -- was the
one thing nobody could see before paying for it.

Two activities, kept apart for the same reason `open_gate2` is separate from
the work before it: one writes the words, the other makes the row wait. A step
that did both would have to write the script and the gate status in the same
breath as routing, and the ordering hazard that creates is documented at length
in `gates.py`. Here it costs nothing to keep them apart.

Neither of these spends anything on video. `write_script` is one LLM call and
`open_script_gate` is one UPDATE, so failing on either is cheap -- which is the
whole argument for putting them before `submit_render` rather than after.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from pipeline.clients.supa import Supa
from pipeline.llm import TextGenerator, text_client
from pipeline.models import ProductionStatus

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# HeyGen's `POST /v3/videos` refuses a longer script outright rather than
# truncating it, and `productions_script_length` in the script-gate migration
# enforces the same number. Mirrored here so a draft that comes back too long is
# cut before the write rather than being refused by Postgres -- an over-long
# draft is a bad draft, not a broken pipeline, and the owner can edit it down.
MAX_SCRIPT_CHARS = 5000


def _subject(idea: dict[str, Any]) -> str:
    """What the drafter is writing about.

    Title and hook together, which is what every lane already built for itself
    before this step existed -- `_build_params`, `_submit_fal` and
    `_submit_heygen` each assembled the same string. Now they do not have to.
    """
    parts = [idea.get("title") or "", idea.get("hook") or ""]
    joined = " -- ".join(p for p in parts if p).strip()
    return joined or (idea.get("title") or "Untitled")


def _paragraphs(preset: dict[str, Any]) -> int:
    """How long a script this style wants.

    Resolution order preserves what each lane did before: the presenter lane
    read `params.heygen.paragraphs`, and the MoneyPrinterTurbo lanes passed
    `paragraph_number` through `VideoParams`. `params.script.paragraphs` is new
    and wins, so a style can set it without either of the older keys.
    """
    params = preset.get("params") or {}
    if not isinstance(params, dict):
        return 1
    for value in (
        (params.get("script") or {}).get("paragraphs")
        if isinstance(params.get("script"), dict)
        else None,
        (params.get("heygen") or {}).get("paragraphs")
        if isinstance(params.get("heygen"), dict)
        else None,
        params.get("paragraph_number"),
    ):
        try:
            if value is not None:
                return max(1, int(value))
        except (TypeError, ValueError):
            continue
    return 1


def _clip_excerpt(idea: dict[str, Any], supa: Supa) -> str | None:
    """The words spoken in this clip, or None if this is not a clip.

    Read from the candidate rather than recomputed from the transcript. The
    candidate's excerpt is what the owner saw at the clip gate when they
    accepted it, so using anything else here would show them different words at
    the script gate than the ones they said yes to.

    An excerpt that is empty parks the production, and that is the correct
    outcome rather than a harsh one. The script gate structurally requires words
    -- `clean_script` refuses an empty script and
    `productions_approved_script_not_empty` refuses to record an approval of one
    -- so a clip with nothing said in it would rest at the gate forever with
    nothing an owner could approve. `clips.validate` drops such a range before
    it is ever proposed, so reaching here means the two have drifted apart.
    """
    if not idea.get("clip_candidate_id"):
        return None
    candidate = supa.clip_candidate_for_idea(idea["id"])
    if candidate is None:
        # The idea says it came from a candidate and the candidate is gone.
        # Parking is right: this is our inconsistency, not the owner's, and a
        # drafted script would silently replace the recording's own words.
        raise ValueError(
            f"idea {idea['id']} was accepted from clip candidate "
            f"{idea['clip_candidate_id']}, which no longer exists"
        )
    excerpt = (candidate.get("transcript_excerpt") or "").strip()
    if not excerpt:
        raise ValueError(
            f"clip candidate {candidate['id']} has no transcribed words, so there "
            f"is no script for an owner to approve. `clips.validate` should have "
            f"dropped this candidate before it was proposed."
        )
    return excerpt


def write_script(
    event: dict[str, Any], supa: Supa | None = None, mpt: TextGenerator | None = None
) -> dict[str, Any]:
    """Draft the narration, unless there already is one.

    Idempotent on purpose, and that is the important property rather than a
    nicety. This step has a retry policy and can be re-entered by
    `retry_production` after a park, and an owner's edited script lives in the
    very column a re-run would overwrite. Keeping an existing script means the
    worst a spurious re-entry can do is nothing.

    `run_state.redraft` is what overrides that, and it is a counter rather than
    a flag precisely so the two cases stay distinguishable: a number that has
    gone up since the last draft means an owner asked for different words, and
    anything else means the step is simply running again.
    """
    supa = supa or Supa()
    production_id = event["production_id"]

    production = supa.production(production_id)
    idea = supa.idea(production["idea_id"])
    preset = supa.style_preset(production["style_preset_id"])

    existing = (production.get("script") or "").strip()
    requested = int(event.get("redraft") or 0)
    drafted = int(event.get("drafted_redraft") or 0)
    wants_new = requested > drafted

    if existing and not wants_new:
        log.info("production %s already has a script; keeping it", production_id)
        return {
            "script_chars": len(existing),
            "script_source": "kept",
            "drafted_redraft": drafted,
            "redraft": requested,
        }

    # A clip already has its words: they were spoken in the recording. Drafting
    # narration for one would be inventing a script for a video that says
    # something else -- and the transcript excerpt is the *point* of the gate on
    # this lane, because it is what the owner reads to check the clip says what
    # the candidate claimed, and what gets burned in as captions if they correct
    # it. See `clips.caption_segments`.
    #
    # No LLM call, so no retry policy is consumed and a deployment with no model
    # key at all can still clip.
    excerpt = _clip_excerpt(idea, supa)
    if excerpt is not None:
        supa.update_production(
            production_id,
            script=excerpt,
            script_updated_at=_now_iso(),
            stage="transcript ready to review",
        )
        log.info(
            "production %s: script is the transcript of its clip (%d characters)",
            production_id,
            len(excerpt),
        )
        return {
            "script_chars": len(excerpt),
            "script_source": "transcript",
            "drafted_redraft": requested,
            "redraft": requested,
        }

    # Gemini or Claude directly when a key is configured, MoneyPrinterTurbo
    # otherwise -- see `llm.text_client`. The parameter keeps its old name so
    # every caller and test that injects a drafter is untouched; what it is
    # given only has to write text.
    mpt = mpt or text_client()
    subject = _subject(idea)

    # Not caught. A failure here routes by the graph to `parked`, which is the
    # honest outcome: nothing has been billed, the row keeps whatever script it
    # had, and an owner can either retry it or -- because `approve_script`
    # accepts a production parked at this step -- simply write the words
    # themselves and carry on without the drafting service at all.
    script = mpt.generate_script(subject, paragraphs=_paragraphs(preset))
    script = (script or "").strip()
    if not script:
        raise ValueError("the script generator returned nothing usable")

    truncated = len(script) > MAX_SCRIPT_CHARS
    if truncated:
        # Cut on a sentence boundary where there is one within reach, so what
        # the owner opens reads as a script rather than as a severed clause.
        head = script[:MAX_SCRIPT_CHARS]
        cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
        script = (head[: cut + 1] if cut > MAX_SCRIPT_CHARS // 2 else head).strip()
        log.warning(
            "production %s: draft was %d characters, cut to %d",
            production_id,
            MAX_SCRIPT_CHARS,
            len(script),
        )

    # `script_updated_by` is deliberately left alone: a draft has no human
    # author, and the column exists to say who last changed the words by hand.
    supa.update_production(
        production_id,
        script=script,
        script_updated_at=_now_iso(),
        stage="script drafted",
    )

    log.info(
        "production %s: drafted a %d character script (redraft %d)",
        production_id,
        len(script),
        requested,
    )
    return {
        "script_chars": len(script),
        "script_source": "redrafted" if wants_new else "drafted",
        "script_truncated": truncated,
        # Records which redraft this draft answered, so the next entry into this
        # step can tell "already done that one" from "asked again".
        "drafted_redraft": requested,
        "redraft": requested,
    }


def open_script_gate(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Make the script reviewable, and stop.

    The same shape as `open_gate2`, and it inherits the same ordering rule:
    the status and the step marker must not disagree, or the row is in a state
    the claim predicate never matches and it waits forever while the interface
    reports it as fine. Here the status is written by this activity and the
    marker by the engine's `_enter`, which is a window of milliseconds --
    `approve_script` closes it by refusing a row whose marker has not landed
    yet, rather than by hoping the window is small.
    """
    supa = supa or Supa()
    production_id = event["production_id"]

    supa.update_production(
        production_id,
        status=ProductionStatus.AWAITING_SCRIPT.value,
        stage="script review",
    )

    log.info("production %s is at the script gate", production_id)
    return {"status": ProductionStatus.AWAITING_SCRIPT.value}
