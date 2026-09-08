"""Keeping the presenter catalogue in Postgres, so the app can offer a choice.

The approval app is a static bundle talking to Supabase from the browser. It
holds no secrets, which is the whole reason `HEYGEN_API_KEY` is safe -- and the
reason the picker in Settings cannot ask HeyGen anything. So the worker asks on
its behalf: this sweep fills `heygen_looks` and `heygen_voices` from the live
account, and the app reads those two tables.

Two things are deliberately asymmetric between looks and voices, and both come
from the API rather than from taste:

  * **Looks are listed; voices are resolved one at a time.** `GET /v3/voices` is
    3,089 entries over 62 pages on this account, and does not contain the cloned
    voice the preset names. `GET /v3/voices/{id}` does. So the voice table is
    filled by ids someone asks about, not by a mirror of a listing that would be
    both enormous and incomplete.
  * **A look that stops being listed is deleted; a voice never is.** The looks
    endpoint is the account's current truth, so a look missing from it is gone
    and must stop being offered. A voice row is a question that was asked and
    answered, and forgetting the answer would make the picker re-ask it.

Field names are read defensively. Everything the lane depends on was verified
against the live v3 API, but the two attributes this module adds -- orientation
and the engines a look advertises -- are read from a response whose exact
spelling we do not control, and getting them wrong in the safe direction (a
warning not shown, an engine not constrained) is far better than a refresh that
raises and leaves the picker with an empty catalogue.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from pipeline.clients.heygen import HeyGenClient, HeyGenError, HeyGenRejected
from pipeline.clients.supa import Supa

log = logging.getLogger(__name__)

# How stale the cache may get before the sweep refills it unasked. Looks change
# when someone creates an avatar in HeyGen's own UI, which is rare and not
# something this system is told about; six hours is short enough that a new
# avatar appears the same working day and long enough to be free.
REFRESH_AFTER_SECONDS = 6 * 60 * 60

# How long a refresh may claim the row before another worker may take it back.
# A worker killed between the claim and the write would otherwise leave the row
# saying `running` for ever, which shows in Settings as a refresh that never
# ends. Generous against a slow HeyGen, short against an owner waiting.
STALE_CLAIM_SECONDS = 15 * 60

# HeyGen names its avatar engines in roman numerals -- avatar_iii, avatar_iv,
# avatar_v -- and they turn up in different fields on different endpoints
# (`tags` carries them upper-cased on the v2 detail response). Rather than
# guess which key the looks response uses, every string in the payload is
# matched against this. Nothing else in a look is shaped like it: ids are hex,
# names are words, urls have slashes.
_ENGINE = re.compile(r"^avatar_[ivx]+$", re.IGNORECASE)

_ORIENTATIONS = ("portrait", "landscape", "square")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strings(payload: Any) -> list[str]:
    """Every string anywhere in a look, so a renamed field cannot hide one."""
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, dict):
        return [s for v in payload.values() for s in _strings(v)]
    if isinstance(payload, list):
        return [s for v in payload for s in _strings(v)]
    return []


def _number(payload: Any, key: str) -> float | None:
    """The first numeric `key` at any depth. Same shape as the live test's
    wallet lookup, and for the same reason: a nested response is not a contract
    we control."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            found = _number(v, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _number(item, key)
            if found is not None:
                return found
    return None


def engines_of(look: dict[str, Any]) -> list[str]:
    """The engines this look advertises, lower-cased and in a stable order.

    Empty means the response said nothing about engines. That is recorded as
    "no opinion" rather than "supports none": `presenter_choice` only
    constrains the engine when this list is non-empty, so a field HeyGen
    renames costs the picker a check, not its ability to save anything.
    """
    found = {s.lower() for s in _strings(look) if _ENGINE.match(s)}
    return sorted(found)


def orientation_of(look: dict[str, Any]) -> str:
    """portrait / landscape / square, or 'unknown' when it cannot be told.

    `preferred_orientation` is what the live test reads and what DEPLOY.md
    tells an operator to look at, so it wins. Dimensions are the fallback,
    because a look that carries 1536x2752 and no orientation field is still
    obviously portrait, and the warning this feeds is the one thing standing
    between an owner and a reel that crops the speaker's head off.
    """
    stated = str(look.get("preferred_orientation") or "").strip().lower()
    if stated in _ORIENTATIONS:
        return stated

    width, height = _number(look, "width"), _number(look, "height")
    if not width or not height:
        return "unknown"
    if width == height:
        return "square"
    return "portrait" if height > width else "landscape"


def look_row(look: dict[str, Any]) -> dict[str, Any] | None:
    """One `heygen_looks` row, or None for a response item with no id."""
    avatar_id = str(look.get("id") or look.get("avatar_id") or "").strip()
    if not avatar_id:
        return None
    return {
        "avatar_id": avatar_id,
        "name": look.get("name"),
        "preview_image_url": look.get("preview_image_url"),
        "preview_video_url": look.get("preview_video_url"),
        "orientation": orientation_of(look),
        "engines": engines_of(look),
        "default_voice_id": look.get("default_voice_id"),
        "gender": look.get("gender"),
        "ownership": str(look.get("ownership") or "private"),
        "seen_at": _now_iso(),
    }


def voice_row(voice_id: str, voice: dict[str, Any]) -> dict[str, Any]:
    """One resolved `heygen_voices` row."""
    return {
        "voice_id": voice_id,
        "status": "ok",
        "name": voice.get("name") or voice.get("display_name"),
        "language": voice.get("language") or voice.get("locale"),
        "gender": voice.get("gender"),
        # HeyGen's own reference recording of the voice. It is what lets an
        # owner hear a narrator before committing a reel to it, and what the
        # live test compares a finished render against.
        "preview_audio_url": voice.get("preview_audio_url") or voice.get("preview_audio"),
        "error": None,
        "resolved_at": _now_iso(),
    }


def refresh_catalogue(supa: Supa | None = None, heygen: HeyGenClient | None = None) -> dict[str, Any]:
    """Refill the look and voice caches, if anything is asking for it.

    Runs on a short period and does nothing most times it runs. The cheap
    read that decides is the point: an owner pressing "Refresh" in Settings
    wants an answer in seconds, and a sweep that only fired every six hours
    would make the button a lie.
    """
    supa = supa or Supa()

    state = supa.heygen_catalogue()
    pending = supa.pending_heygen_voices()
    if not _due(state, pending):
        return {}

    if not supa.claim_heygen_catalogue(STALE_CLAIM_SECONDS):
        # Another worker is already doing it. Two containers both holding a
        # service-role key is the deployment we ship, so this is a real race
        # rather than a defensive flourish.
        return {}

    heygen = heygen or HeyGenClient()
    try:
        looks = [row for row in (look_row(item) for item in heygen.looks()) if row]
    except HeyGenError as exc:
        # Recorded on the row rather than only in the log: the person who
        # pressed the button is looking at a page, not at CloudWatch, and "the
        # key is unauthorised" is a sentence they can act on.
        supa.finish_heygen_catalogue(status="failed", error=str(exc)[:500])
        return {"error": str(exc)[:200]}

    supa.replace_heygen_looks(looks)

    # Every id we hold an answer for, re-asked. There are a handful of these --
    # the preset's narrator, plus whatever the owner has tried -- and a voice
    # that has quietly stopped resolving is exactly the thing worth catching
    # before a render does.
    resolved, unknown = 0, 0
    for voice_id in supa.known_heygen_voices():
        try:
            supa.upsert_heygen_voice(voice_row(voice_id, heygen.voice(voice_id)))
            resolved += 1
        except HeyGenRejected as exc:
            # Terminal and about this id specifically: `voice_not_found` on one
            # voice is not a reason to abandon the others.
            supa.upsert_heygen_voice(
                {
                    "voice_id": voice_id,
                    "status": "unknown",
                    "error": str(exc)[:500],
                    "resolved_at": _now_iso(),
                }
            )
            unknown += 1
        except HeyGenError as exc:
            # Transient. Left as it is, so a blip does not turn a working voice
            # into one the picker refuses to save.
            log.warning("could not resolve HeyGen voice %s: %s", voice_id, exc)

    supa.finish_heygen_catalogue(status="idle", looks=len(looks), voices=resolved)
    return {"looks": len(looks), "voices": resolved, "unknown": unknown}


def _due(state: dict[str, Any] | None, pending: int) -> bool:
    """Whether there is anything to do.

    Asked before the claim so that the common case -- nothing requested,
    nothing stale -- costs one select and no write at all.
    """
    if state is None:
        # No row means the migration has not run here. Refreshing would write
        # to tables that do not exist; the sweep stays quiet instead.
        return False
    if state.get("status") == "running":
        # Only if the worker holding it has plainly gone. `claim_heygen_catalogue`
        # applies the same cutoff, so the two cannot disagree about whose it is.
        return _older_than(state.get("started_at"), STALE_CLAIM_SECONDS)
    if state.get("status") == "requested" or pending:
        return True
    return _older_than(state.get("refreshed_at"), REFRESH_AFTER_SECONDS)


def _older_than(stamp: Any, seconds: int) -> bool:
    """True for a missing or unparseable timestamp: never having refreshed and
    not being able to tell both mean the cache cannot be trusted."""
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() >= seconds
