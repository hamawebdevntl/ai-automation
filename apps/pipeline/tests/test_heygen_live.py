"""Live, end-to-end proof that the presenter lane actually renders.

`test_heygen.py` covers the shape of our code: the header the key goes in, the
script guards, which URL `output_url()` prefers, how a status branches. Every
one of those passes with the account closed, the key revoked and HeyGen down,
because every response in it is one we wrote ourselves.

This file covers the other half -- that a real presenter video comes back and
is the one we asked for. It walks the same calls `_submit_heygen` and
`_poll_heygen` make, with the configuration the live `ai-presenter` preset
actually holds, and then inspects the file that arrives.

Everything asserted below was checked against the live v3 API on 2026-09-07,
including three things that decided how it is written:

  * **The detail response does not echo `avatar_id` or `voice_id`.** A finished
    render returns id, status, the URLs, duration, timestamps, title and
    thumbnails -- and nothing naming the avatar or the voice. So "our
    configuration was honoured" cannot be read out of the payload. It is
    checked instead against the reference assets HeyGen itself holds for those
    ids: the look's `preview_image_url` and the voice's `preview_audio_url`.
  * **A submit returns `status: "waiting"`**, which appears in no documented
    list of these values. `is_running` treats anything non-terminal as running,
    which is why that is written as a negation rather than a membership test.
  * **Captions really do land on the other URL.** `video_url` and
    `captioned_video_url` are both returned and are different files; the
    captioned one carries the words burned in at bottom centre. `test_heygen.py`
    proves we *prefer* that URL, and only this proves the file behind it has
    captions in it.

The content checks read the rendered mp4 with Gemini rather than ffmpeg, so
they run wherever the pipeline's own dependencies do not. `ffprobe` is used for
the technical measurements it is better at -- exact dimensions, audio level,
frozen frames -- and those checks skip on their own when it is absent, without
blocking the render or the content checks.

It is off unless asked for. `pytest -q` runs everything with no marker filter,
so a marker alone would not be enough -- the gate is the environment:

    HEYGEN_LIVE=1

Cost, measured rather than estimated: **$0.40** for the 10.5-second render this
file submits, on a wallet that bills pay-as-you-go (~$2.29/minute), plus well
under a cent of Gemini. A rerun spends nothing. The submit carries a *stable*
Idempotency-Key derived from the narration and the preset values, so within
HeyGen's 24-hour window a resubmit replays the original response instead of
billing again; past that window the downloaded file is still in the local cache
and no submit happens at all.

Run it with:

    cd apps/pipeline
    set -a && . ./.env.local && set +a
    HEYGEN_LIVE=1 ./.venv/bin/pytest tests/test_heygen_live.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pipeline.clients.heygen import (
    HeyGenClient,
    HeyGenError,
    HeyGenInProgress,
    HeyGenRateLimited,
)
from pipeline.models import HeyGenVideo
from pipeline.qc.probe import ProbeError, frozen_seconds, mean_volume_db, probe
from pipeline.qc.slideshow import MIN_DURATION_S, SILENCE_DB

# Costs real money, so it is never collected by accident.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HEYGEN_LIVE", "") not in ("1", "true", "yes"),
        reason=(
            "live HeyGen render is opt-in: set HEYGEN_LIVE=1 to run it. "
            "One run costs about $0.40 against the pay-as-you-go wallet."
        ),
    ),
]

# The preset the presenter lane runs on. Read from the database rather than
# from the migration, because DEPLOY.md tells operators to change the avatar
# with an UPDATE and no deploy -- so the committed values are not authoritative
# and a test pinned to them would pass with a broken preset in force.
PRESET_SLUG = "ai-presenter"

# Deliberately short: HeyGen bills by rendered duration, and this is the
# smallest script that still clears the pipeline's 5s publishable floor with
# room to spare -- 26 words came back as 10.5s. The words are distinctive on
# purpose: "contractors", "enquiry", "sixty" are what make a transcript
# comparison mean something, where "the" and "and" would match any audio.
NARRATION = (
    "Most contractors lose the job by replying too late. "
    "We answer every new enquiry in under sixty seconds, automatically, "
    "so the client books with you first."
)

# Bounds for a vertical short-form reel. The floor is the pipeline's own
# publishable minimum; the ceiling is far above the ~10s this narration
# produces, and exists to catch a render that spoke something other than our
# script.
MAX_SHORT_FORM_S = 60.0
NARRATION_CEILING_S = 30.0

# A talking head is one continuous shot, so cuts are not a signal here (see
# `slideshow_risk(expect_cuts=False)`). A mostly-frozen frame still is: it is
# what a stalled avatar render produces, and it passes every codec check.
MAX_FROZEN_FRACTION = 0.5

# How much of what we sent has to survive into the transcript. Not 1.0: ASR
# drops articles and clips the last word. The real render scored 94%.
MIN_NARRATION_OVERLAP = 0.6
# Captions are read off the picture, where a word can be mid-transition or
# outside the sampled moment. The real render scored 88%.
MIN_CAPTION_OVERLAP = 0.35

# Below this the next render fails after Gate 1 has already been passed, which
# wastes a review instead of preventing one. One render is about $0.40, so this
# is roughly two of them in hand.
MIN_WALLET_BALANCE = 1.0

POLL_INTERVAL_S = 10.0
FETCH_ATTEMPTS = 3

# Gemini reads the whole mp4 rather than extracted frames. A 10s 1080p render is
# ~7.4 MB, comfortably inside the inline request limit.
MAX_INLINE_VIDEO_BYTES = 18_000_000


# ---------------------------------------------------------------------------
# The local artifact cache.
#
# A rerun must not pay twice. The Idempotency-Key covers 24 hours; this covers
# the rest, so iterating on the assertions below is free.
# ---------------------------------------------------------------------------

CACHE_DIR = Path(
    os.environ.get("HEYGEN_LIVE_CACHE") or Path(__file__).resolve().parents[1] / ".heygen-live"
)


class VerificationUnavailable(RuntimeError):
    """The inspection tooling failed -- which is not a HeyGen failure.

    Raised only after the gate has already confirmed Gemini answers, so it
    means something broke mid-run rather than being misconfigured. It is
    reported as a failure rather than a skip, because silently dropping the
    checks that matter most is how a test starts passing for no reason.
    """


# ---------------------------------------------------------------------------
# Keeping the key out of the output.
#
# Nothing here puts it in a message deliberately, but a message is assembled
# from response bodies and exception text, and one day something will echo a
# header back. Redacting on the way out is cheaper than auditing every path.
# ---------------------------------------------------------------------------

_KEY_SHAPED = re.compile(r"sk_[A-Za-z0-9_\-]{12,}")


def _redact(text: str) -> str:
    key = os.environ.get("HEYGEN_API_KEY", "")
    out = str(text)
    if len(key) > 8:
        out = out.replace(key, "<HEYGEN_API_KEY redacted>")
    return _KEY_SHAPED.sub("<redacted key>", out)


def _safe_url(url: str) -> str:
    """A URL without its query string.

    HeyGen's asset URLs are presigned: the path identifies the render, the
    query carries an AWS signature. Only the first half belongs in a failure
    message or a cache file.
    """
    return str(url).split("?", 1)[0]


def _fail(message: str) -> None:
    pytest.fail(_redact(message))


# ---------------------------------------------------------------------------
# ffprobe, for the measurements it is better at than a model. Optional: the
# checks that need it skip individually rather than gating the render.
# ---------------------------------------------------------------------------


def _tool_missing(*tools: str) -> str | None:
    for tool in tools:
        try:
            subprocess.run([tool, "-version"], capture_output=True, timeout=30, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return tool
    return None


# ---------------------------------------------------------------------------
# Inspection, via the SDK this repo already depends on.
# ---------------------------------------------------------------------------


def _fetch_bytes(url: str, timeout: float = 90.0) -> bytes:
    """Fetch a reference asset, with retries.

    HeyGen serves previews from a different host than the API and it drops
    connections often enough to matter; a flake here would read as a render
    defect, which is the one thing this file must not get wrong.
    """
    import httpx

    last: Exception | None = None
    for _ in range(FETCH_ATTEMPTS):
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True)
            if resp.status_code >= 400:
                raise VerificationUnavailable(
                    f"reference asset {_safe_url(url)} -> HTTP {resp.status_code}"
                )
            return resp.content
        except VerificationUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - transport faults of every kind
            last = exc
            time.sleep(2)
    raise VerificationUnavailable(
        f"could not fetch reference asset {_safe_url(url)}: {type(last).__name__}: {last}"
    )


def _gemini_json(client: Any, model: str, prompt: str, *parts: Any) -> dict[str, Any]:
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt, *parts] if parts else prompt,
            config={
                "response_mime_type": "application/json",
                # We pass no tools, so this only adds a warning per call.
                "automatic_function_calling": {"disable": True},
            },
        )
    except Exception as exc:
        raise VerificationUnavailable(f"Gemini call failed: {type(exc).__name__}: {exc}") from exc
    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise VerificationUnavailable("Gemini returned an empty body")
    try:
        data = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise VerificationUnavailable(f"Gemini returned no parseable JSON: {text[:300]}") from exc
    if not isinstance(data, dict):
        raise VerificationUnavailable(f"Gemini returned {type(data).__name__}, not an object")
    return data


def _part(data: bytes, mime_type: str) -> Any:
    # Imported here rather than at module scope, as trends/ideas.py does: only
    # this check needs the SDK.
    from google.genai import types

    return types.Part.from_bytes(data=data, mime_type=mime_type)


# ---------------------------------------------------------------------------
# Word comparison.
# ---------------------------------------------------------------------------

# Small on purpose: the point is to drop words that would match any English
# audio, not to do real linguistics.
_STOPWORDS = frozenset(
    [
        "a", "an", "the", "and", "or", "but", "if", "then", "than", "that",
        "this", "these", "those", "of", "in", "on", "at", "to", "for", "with",
        "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
        "am", "it", "its", "we", "our", "us", "you", "your", "they", "their",
        "he", "she", "his", "her", "i", "me", "my", "do", "does", "did", "so",
        "no", "not", "too", "very", "can", "will",
    ]
)


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _overlap(sent: str, heard: str) -> float:
    """Fraction of the content words we sent that appear in what came back."""
    expected = _content_words(sent)
    if not expected:
        return 0.0
    return len(expected & _content_words(heard)) / len(expected)


def _missing(sent: str, heard: str) -> list[str]:
    return sorted(_content_words(sent) - _content_words(heard))


# ---------------------------------------------------------------------------
# Failure diagnosis. A stack trace does not tell the next person whether the
# wallet is empty, the key is wrong or the avatar is gone.
# ---------------------------------------------------------------------------


def _diagnose(exc: Exception, cfg: dict[str, Any]) -> str:
    text = _redact(str(exc))
    lowered = text.lower()
    avatar_id, voice_id = cfg.get("avatar_id"), cfg.get("voice_id")

    # Matched on the message as well as the code, because the two are not
    # symmetrical in practice: a bad avatar comes back as `404
    # avatar_not_found`, but a bad voice comes back as `400 invalid_parameter:
    # Invalid voice_id: ... Voice not found.` -- so `voice_not_found`, which
    # the client lists among its terminal codes, is never actually emitted for
    # this. Keyed off the code alone, a stale voice fell through to the generic
    # branch and told the reader nothing.
    if "avatar_not_found" in lowered or ("avatar" in lowered and "not found" in lowered):
        return (
            f"THE CONFIGURED AVATAR DOES NOT EXIST ON THIS ACCOUNT. "
            f"{PRESET_SLUG}.params.heygen.avatar_id is {avatar_id!r} and HeyGen does not "
            f"recognise it. List the looks this account owns and update the preset -- see "
            f"DEPLOY.md section 5a. Underlying error: {text}"
        )
    if (
        "voice_not_found" in lowered
        or "invalid voice_id" in lowered
        or ("voice" in lowered and "not found" in lowered)
    ):
        return (
            f"THE CONFIGURED VOICE DOES NOT EXIST ON THIS ACCOUNT. "
            f"{PRESET_SLUG}.params.heygen.voice_id is {voice_id!r}; HeyGen rejected it. "
            f"Resolve it with GET /v3/voices/{{id}} -- the paginated listing does not contain "
            f"private or cloned voices. Underlying error: {text}"
        )
    if any(w in lowered for w in ("credit", "balance", "insufficient", "quota", "wallet")):
        return (
            f"THE ACCOUNT IS OUT OF CREDIT. The wallet behind HEYGEN_API_KEY cannot fund a "
            f"render. Top it up before approving a presenter production. Underlying error: {text}"
        )
    if "401" in text or "403" in text or "unauthorized" in lowered or "forbidden" in lowered:
        return (
            f"HEYGEN_API_KEY IS NOT ACCEPTED. It must be the raw key in X-Api-Key -- no "
            f"'Bearer ' prefix, and not in Authorization. A revoked or rotated key looks "
            f"exactly like this. Underlying error: {text}"
        )
    if isinstance(exc, HeyGenRateLimited):
        return (
            f"RATE LIMITED OR AT THE CONCURRENCY CEILING (ten jobs on pay-as-you-go, counting "
            f"every render, Video Agent session and translation in flight). Nothing was billed; "
            f"retry after {exc.retry_after or 'a minute'}s. Underlying error: {text}"
        )
    return f"HeyGen call failed: {type(exc).__name__}: {text}"


# ---------------------------------------------------------------------------
# Fixtures. Session-scoped, so one render serves every assertion below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rendered:
    video: HeyGenVideo
    path: Path
    url: str
    captioned: bool
    from_cache: bool
    idempotency_key: str
    config: dict[str, Any]
    payload: dict[str, Any]


@pytest.fixture(scope="session")
def heygen() -> HeyGenClient:
    if not os.environ.get("HEYGEN_API_KEY"):
        pytest.skip(
            "HEYGEN_API_KEY is not set. Add it to apps/pipeline/.env.local and export it "
            "(`set -a && . ./.env.local && set +a`) -- config.py sets env_file=None, so a "
            "value sitting unexported in the file is not read."
        )
    try:
        # The key is passed rather than left to `settings()`, which also
        # requires the Supabase pair: a run with a HeyGen key and nothing else
        # should reach the preset fixture and skip there with a message about
        # Supabase, not die validating an unrelated credential.
        return HeyGenClient(api_key=os.environ["HEYGEN_API_KEY"])
    except HeyGenError as exc:
        pytest.skip(_redact(f"HeyGen client could not be constructed: {exc}"))


def _default_gemini_model() -> str:
    """`Settings.gemini_model`'s default, without constructing `Settings`.

    Read off the field itself so it cannot drift from the model the pipeline
    uses, while still not requiring the Supabase credentials `Settings` needs.
    """
    from pipeline.config import Settings

    return str(Settings.model_fields["gemini_model"].default)


@pytest.fixture(scope="session")
def gemini() -> tuple[Any, str]:
    """Prove the inspection tooling answers *before* a render is paid for.

    Checked up front for a reason: if the transcript, caption and identity
    checks are going to be unavailable, the useful moment to find out is before
    spending, not after -- and a mid-run tooling failure must then be a failure
    rather than a quiet skip of the checks that matter most.
    """
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        pytest.skip(
            "GEMINI_API_KEY is not set. It is what transcribes the audio, reads the captions "
            "off the picture and matches the speaker against the configured avatar and voice, "
            "so none of the content checks can run without it."
        )
    model = os.environ.get("GEMINI_MODEL") or _default_gemini_model()
    try:
        from google import genai

        client = genai.Client(api_key=key)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"google-genai is unavailable: {type(exc).__name__}: {exc}")

    try:
        _gemini_json(client, model, 'Return JSON: {"ok": true}.')
    except VerificationUnavailable as exc:
        pytest.skip(
            f"Gemini does not answer, so the content checks could not be trusted. Not "
            f"spending on a render. {exc}"
        )
    return client, model


@pytest.fixture(scope="session")
def preset_config() -> dict[str, Any]:
    """`params.heygen` off the live preset -- the configuration in force."""
    from pipeline.clients.supa import Supa

    if not os.environ.get("SUPABASE_URL") or not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        pytest.skip(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not set. The avatar and voice "
            "under test come from the live style_presets row, not from the migration, "
            "because DEPLOY.md changes them with an UPDATE and no deploy."
        )
    try:
        res = (
            Supa().raw.table("style_presets")
            .select("*")
            .eq("slug", PRESET_SLUG)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not read style_presets from Supabase: {type(exc).__name__}: {exc}")

    rows = res.data or []
    assert rows, (
        f"there is no {PRESET_SLUG!r} style preset in this database, so the presenter lane "
        f"has nothing to render with. Apply "
        f"supabase/migrations/20260903120000_heygen_presenter_lane.sql."
    )
    preset = rows[0]
    assert preset.get("render_mode") == "heygen", (
        f"preset {PRESET_SLUG!r} has render_mode {preset.get('render_mode')!r}, so the "
        f"presenter lane would never run for it."
    )
    assert preset.get("is_active"), (
        f"preset {PRESET_SLUG!r} is inactive, so it cannot be chosen at Gate 1."
    )
    cfg = (preset.get("params") or {}).get("heygen") or {}
    assert cfg.get("avatar_id"), (
        f"preset {PRESET_SLUG!r} has no params.heygen.avatar_id; _submit_heygen raises on this "
        f"before it reaches HeyGen."
    )
    return cfg


@pytest.fixture(scope="session")
def account(heygen: HeyGenClient, preset_config: dict[str, Any]) -> dict[str, Any]:
    """`GET /v3/users/me`. Costs nothing, and fails before anything is billed."""
    try:
        return heygen.account()
    except Exception as exc:  # noqa: BLE001
        _fail(_diagnose(exc, preset_config))


@pytest.fixture(scope="session")
def configured_look(heygen: HeyGenClient, preset_config: dict[str, Any]) -> dict[str, Any]:
    """The account's own record of the look the preset names."""
    try:
        looks = heygen.looks()
    except Exception as exc:  # noqa: BLE001
        _fail(_diagnose(exc, preset_config))
    owned = {str(look.get("id")): look for look in looks if look.get("id")}
    avatar_id = preset_config["avatar_id"]
    if avatar_id not in owned:
        _fail(
            f"THE CONFIGURED AVATAR DOES NOT EXIST ON THIS ACCOUNT. "
            f"{PRESET_SLUG}.params.heygen.avatar_id is {avatar_id!r}, which is not among the "
            f"{len(owned)} looks this account owns."
        )
    return owned[avatar_id]


def _wallet_balance(account: dict[str, Any]) -> float | None:
    """The remaining balance, wherever this account's plan reports it.

    It sits at `wallet.remaining_balance` on this pay-as-you-go account, but a
    plan change is not a reason for the check to start calling a funded account
    empty, so it is searched for rather than read from a fixed path.
    """
    for key in ("remaining_balance", "balance", "remaining_credit", "remaining_quota"):
        found = _find_number(account, key)
        if found is not None:
            return found
    return None


def _find_number(payload: Any, key: str) -> float | None:
    if isinstance(payload, dict):
        for k, v in payload.items():
            if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            found = _find_number(v, key)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_number(item, key)
            if found is not None:
                return found
    return None


def _idempotency_key(cfg: dict[str, Any]) -> str:
    """Stable across runs, so a resubmit replays instead of billing again.

    Production uses the production id, which is unique per job -- correct
    there, since each production is a distinct render. Here the *opposite* is
    wanted: the same key every run, so HeyGen returns the render we already
    paid for. It is derived from the narration and the preset values, so
    changing either is what makes a new render, and only that.
    """
    material = json.dumps(
        {
            "narration": NARRATION,
            "avatar_id": cfg.get("avatar_id"),
            "voice_id": cfg.get("voice_id"),
            "aspect_ratio": cfg.get("aspect_ratio", "9:16"),
            "resolution": cfg.get("resolution", "1080p"),
            "burn_captions": bool(cfg.get("burn_captions", True)),
            "engine": cfg.get("engine"),
        },
        sort_keys=True,
    )
    return f"heygen-live-test.{hashlib.sha256(material.encode()).hexdigest()[:24]}"


@pytest.fixture(scope="session")
def rendered(
    heygen: HeyGenClient,
    preset_config: dict[str, Any],
    account: dict[str, Any],
    gemini: tuple[Any, str],
) -> Rendered:
    """One real render, cached.

    The order of the fixtures above is the point: the key, the wallet, the
    preset and the inspection tooling are all confirmed before this spends
    anything.
    """
    cfg = preset_config
    key = _idempotency_key(cfg)
    dest = CACHE_DIR / f"{key}.mp4"
    meta_path = CACHE_DIR / f"{key}.json"

    if dest.exists() and dest.stat().st_size > 0 and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return Rendered(
            video=HeyGenVideo.model_validate(meta["payload"]),
            path=dest,
            url=meta["url"],
            captioned=bool(meta["captioned"]),
            from_cache=True,
            idempotency_key=key,
            config=cfg,
            payload=meta["payload"],
        )

    wants_captions = bool(cfg.get("burn_captions", True))
    try:
        submitted = heygen.create_avatar_video(
            avatar_id=cfg["avatar_id"],
            script=NARRATION,
            idempotency_key=key,
            voice_id=cfg.get("voice_id"),
            aspect_ratio=cfg.get("aspect_ratio", "9:16"),
            resolution=cfg.get("resolution", "1080p"),
            burn_captions=wants_captions,
            fit=cfg.get("fit"),
            title=f"live integration check [{key}]",
            voice_settings=cfg.get("voice_settings"),
            engine=cfg.get("engine"),
            background=cfg.get("background"),
        )
    except Exception as exc:  # noqa: BLE001 - diagnosed, then re-reported
        _fail(_diagnose(exc, cfg))

    budget = float(os.environ.get("HEYGEN_LIVE_BUDGET_SECONDS") or 900)
    deadline = time.monotonic() + budget
    # The detail response, not the submit echo. The submit payload carries no
    # output URL, so a render that is already complete -- which an idempotent
    # replay can be -- must still be fetched rather than trusted as returned.
    try:
        video = heygen.video(submitted.id)
    except Exception as exc:  # noqa: BLE001
        _fail(_diagnose(exc, cfg))
    while video.is_running:
        if time.monotonic() > deadline:
            _fail(
                f"HeyGen render {video.id} was still {video.status!r} after {budget:.0f}s. "
                f"The render is paid for -- rerun and the Idempotency-Key will replay it "
                f"rather than billing again, or raise HEYGEN_LIVE_BUDGET_SECONDS."
            )
        time.sleep(POLL_INTERVAL_S)
        try:
            video = heygen.video(video.id)
        except Exception as exc:  # noqa: BLE001
            _fail(_diagnose(exc, cfg))

    if video.is_failed:
        _fail(
            f"HEYGEN RENDERED NOTHING: the job reached 'failed' -- {video.failure}. The submit "
            f"was accepted, so the key and the wallet were fine; this is the render itself. "
            f"(video {video.id})"
        )

    url = video.output_url(prefer_captioned=wants_captions)
    if not url:
        _fail(
            f"HeyGen reported {video.status!r} but returned no downloadable URL at all "
            f"(video {video.id}). This is what _poll_heygen parks a production on."
        )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        heygen.download(url, dest)
    except HeyGenError as exc:
        _fail(
            f"the render finished but its file could not be fetched: {exc} "
            f"(video {video.id}, {_safe_url(url)})"
        )

    payload = video.model_dump(exclude_none=True)
    meta_path.write_text(
        json.dumps(
            {
                "url": url,
                "captioned": bool(wants_captions and video.captioned_video_url),
                "narration": NARRATION,
                "payload": payload,
            },
            indent=2,
            default=str,
        )
    )
    return Rendered(
        video=video,
        path=dest,
        url=url,
        captioned=bool(wants_captions and video.captioned_video_url),
        from_cache=False,
        idempotency_key=key,
        config=cfg,
        payload=payload,
    )


@pytest.fixture(scope="session")
def video_part(rendered: Rendered) -> Any:
    """The rendered mp4, ready to hand to Gemini."""
    data = rendered.path.read_bytes()
    if len(data) > MAX_INLINE_VIDEO_BYTES:
        pytest.skip(
            f"the render is {len(data) / 1e6:.1f} MB, over the {MAX_INLINE_VIDEO_BYTES / 1e6:.0f} "
            f"MB inline limit, so the content checks would need the Files API instead."
        )
    return _part(data, "video/mp4")


@pytest.fixture(scope="session")
def info(rendered: Rendered):
    """ffprobe's view of the file. Skips rather than failing when absent."""
    missing = _tool_missing("ffprobe")
    if missing:
        pytest.skip(
            f"{missing} is not installed, so the technical measurements are unavailable. "
            f"The content checks do not need it. (The media image installs both; see "
            f"apps/pipeline/Dockerfile.media.)"
        )
    try:
        return probe(rendered.path)
    except ProbeError as exc:
        _fail(
            f"ffprobe could not read the file HeyGen returned ({rendered.path}): {exc}. "
            f"A file that arrives but does not parse is worse than one that never arrives."
        )


@pytest.fixture(scope="session")
def transcript(rendered: Rendered, gemini: tuple[Any, str], video_part: Any) -> str:
    client, model = gemini
    data = _gemini_json(
        client,
        model,
        "Transcribe the spoken words in this video verbatim. Return JSON: "
        '{"transcript": "<the words, or an empty string if there is no speech>"}.',
        video_part,
    )
    return str(data.get("transcript") or "")


# ---------------------------------------------------------------------------
# Preflight. Cheap, and worth having on its own: it is the same pair of checks
# DEPLOY.md section 5a asks an operator to run by hand before the first
# approval, which means it is the pair that has already caught something.
# ---------------------------------------------------------------------------


class TestPreflight:
    def test_the_key_is_accepted_by_the_live_api(self, account):
        # Reaching here at all means GET /v3/users/me answered 2xx: the fixture
        # turns a 401 into a readable diagnosis. This pins that the response
        # identifies an account rather than being an empty 200.
        assert account, (
            "HeyGen accepted the key but returned an empty account body, which is not a "
            "shape we can act on."
        )

    def test_the_wallet_can_fund_a_render(self, account):
        balance = _wallet_balance(account)
        if balance is None:
            pytest.skip(
                f"this account's /v3/users/me carries no balance field, so there is nothing "
                f"to check before spending. Keys present: {sorted(account)}"
            )
        assert balance >= MIN_WALLET_BALANCE, (
            f"THE ACCOUNT IS OUT OF CREDIT: {balance} left, which is below the "
            f"{MIN_WALLET_BALANCE} floor -- about two renders. A presenter production "
            f"approved now would pass Gate 1 and then fail in the render, wasting the "
            f"review. Top up the wallet."
        )

    def test_the_avatar_the_preset_names_exists_on_this_account(
        self, configured_look, preset_config
    ):
        """The stale-id failure, caught before it costs anything.

        `avatar_not_found` is terminal in the pipeline -- correctly, since it
        fails identically on retry -- so the row parks and a human is paged.
        Finding it here instead costs nothing.
        """
        assert configured_look.get("id") == preset_config["avatar_id"]
        # Portrait matters enough to say out loud rather than fail on: a 9:16
        # render from a landscape source crops the speaker to fill the frame,
        # which is a quality problem rather than a broken lane.
        if configured_look.get("preferred_orientation") != "portrait":
            pytest.skip(
                f"the configured look {configured_look.get('name')!r} is "
                f"{configured_look.get('preferred_orientation')!r} rather than portrait, so a "
                f"9:16 render crops the speaker. It still renders; see DEPLOY.md section 5a."
            )

    def test_the_voice_the_preset_names_resolves_on_this_account(self, heygen, preset_config):
        """A stale voice fails as `voice_not_found`, which is also terminal.

        Resolved by direct lookup rather than by scanning the catalogue: a
        cloned or private voice is absent from `GET /v3/voices` entirely -- the
        one this preset names is not among the 3,089 that listing returns --
        and answers only at `GET /v3/voices/{id}`.
        """
        voice_id = preset_config.get("voice_id")
        if not voice_id:
            pytest.skip(
                f"{PRESET_SLUG} names no voice_id, so HeyGen's own default for the avatar is "
                f"the correct narrator and there is nothing to pin."
            )
        try:
            voice = heygen.voice(str(voice_id))
        except Exception as exc:  # noqa: BLE001
            _fail(_diagnose(exc, preset_config))
        assert voice, (
            f"THE CONFIGURED VOICE DOES NOT RESOLVE. "
            f"{PRESET_SLUG}.params.heygen.voice_id is {voice_id!r} and GET /v3/voices/{voice_id} "
            f"returned nothing. A render with it fails terminally as voice_not_found."
        )
        assert voice.get("preview_audio_url"), (
            f"voice {voice_id!r} resolves but carries no preview_audio_url, so the check that "
            f"the render actually used it has nothing to compare against. Keys: {sorted(voice)}"
        )


# ---------------------------------------------------------------------------
# The render itself.
# ---------------------------------------------------------------------------


class TestPresenterRenderComesBack:
    def test_a_real_playable_file_lands_on_disk(self, rendered):
        assert rendered.path.exists()
        # Not just non-empty: ten seconds of 1080p is megabytes, and a
        # truncated or error-body download is the failure this catches.
        size = rendered.path.stat().st_size
        assert size > 200_000, (
            f"the file HeyGen returned is only {size} bytes, which is too small to be a "
            f"rendered reel."
        )

    def test_the_reported_duration_is_short_form(self, rendered):
        """HeyGen's own duration, so this holds without ffprobe."""
        duration = rendered.video.duration
        assert duration is not None, (
            "the completed render reports no duration, so the pipeline has nothing to record "
            "and this check has nothing to judge."
        )
        assert MIN_DURATION_S <= duration <= MAX_SHORT_FORM_S, (
            f"the render is {duration:.1f}s, outside the {MIN_DURATION_S:.0f}-"
            f"{MAX_SHORT_FORM_S:.0f}s short-form window."
        )
        assert duration <= NARRATION_CEILING_S, (
            f"the render is {duration:.1f}s for a {len(NARRATION.split())}-word script. That is "
            f"far longer than this narration can account for, which means the video is not "
            f"speaking the script we sent."
        )

    def test_it_is_a_vertical_nine_by_sixteen_reel(self, rendered, info):
        assert info.is_portrait_9x16, (
            f"HeyGen returned {info.width}x{info.height}, which is not 9:16. The preset asks "
            f"for {rendered.config.get('aspect_ratio', '9:16')!r}; a landscape file would be "
            f"cropped or letterboxed on every platform we publish to."
        )
        if str(rendered.config.get("resolution", "1080p")) == "1080p":
            assert min(info.width, info.height) >= 1080, (
                f"the preset asks for 1080p but HeyGen returned {info.width}x{info.height}."
            )
        assert info.video_codec, "the file carries no video stream"
        assert info.fps > 0, f"the file reports {info.fps} fps"

    def test_the_avatar_is_moving_rather_than_a_frozen_frame(self, rendered, info):
        """A stalled avatar render is a valid file that looks like a photo.

        Cuts are not counted -- a talking head is one continuous shot, which is
        why `slideshow_risk` takes `expect_cuts=False` for this lane -- but a
        frame that never changes is a real failure and passes every codec check.
        """
        frozen = frozen_seconds(rendered.path)
        fraction = frozen / info.duration_s if info.duration_s else 1.0
        assert fraction <= MAX_FROZEN_FRACTION, (
            f"{fraction:.0%} of the {info.duration_s:.1f}s render is a frozen frame "
            f"({frozen:.1f}s). An avatar that never moves is a failed render, not a video."
        )


class TestThereIsSpeechAndItIsOurs:
    def test_the_audio_is_not_silent(self, rendered, info):
        assert info.has_audio, (
            "the render has no audio stream at all. A presenter reel is a script delivered "
            "to camera; without audio there is nothing delivered."
        )
        level = mean_volume_db(rendered.path)
        assert level is not None, "the audio level could not be measured"
        assert level > SILENCE_DB, (
            f"the audio track is effectively silent at {level:.1f} dB (the pipeline's own "
            f"threshold is {SILENCE_DB} dB). A voice that failed silently still produces a "
            f"valid file with a silent track, which is exactly this."
        )

    def test_the_words_spoken_are_the_words_we_sent(self, transcript):
        assert transcript.strip(), (
            "the audio transcribed to nothing, so there is no speech in it -- only sound. "
            "This is what a wrong or empty voice configuration produces."
        )
        score = _overlap(NARRATION, transcript)
        assert score >= MIN_NARRATION_OVERLAP, (
            f"the narration does not match what we sent: only {score:.0%} of our content "
            f"words are in the transcript (floor {MIN_NARRATION_OVERLAP:.0%}).\n"
            f"  sent:    {NARRATION}\n"
            f"  heard:   {transcript}\n"
            f"  missing: {_missing(NARRATION, transcript)}"
        )


class TestCaptionsAreInThePicture:
    """The bug that has bitten us, checked in the pixels rather than the JSON.

    Asking for burned-in captions leaves `video_url` as the clean cut and puts
    the captioned render on `captioned_video_url`. Reading the obvious field
    ships an uncaptioned reel *and* makes our own quality report claim captions
    are missing on a render that has them. `test_heygen.py` proves we prefer
    the right URL; only this proves the file behind it is actually captioned.
    """

    def test_the_captioned_render_is_the_one_we_downloaded(self, rendered):
        if not bool(rendered.config.get("burn_captions", True)):
            pytest.skip(f"{PRESET_SLUG} has burn_captions off, so there is nothing to check")
        assert rendered.video.captioned_video_url, (
            f"we asked for burned-in captions and HeyGen returned no captioned_video_url, "
            f"only the clean cut. The pipeline ships this uncaptioned and logs a warning; "
            f"a reel with no captions is skipped in a muted feed. (video {rendered.video.id})"
        )
        assert rendered.url == rendered.video.captioned_video_url, (
            f"the file was fetched from the clean cut rather than the captioned render. "
            f"This is the exact bug: got {_safe_url(rendered.url)}, wanted "
            f"{_safe_url(rendered.video.captioned_video_url)}"
        )
        assert rendered.captioned

    def test_the_captions_are_visible_in_the_picture(self, rendered, gemini, video_part):
        if not bool(rendered.config.get("burn_captions", True)):
            pytest.skip(f"{PRESET_SLUG} has burn_captions off, so there is nothing to check")
        client, model = gemini
        read = _gemini_json(
            client,
            model,
            "Look for subtitle or caption text burned into the picture of this video -- "
            "words overlaid on the frames. Ignore logos, watermarks, and anything printed "
            "on clothing or scenery. Return JSON: {\"captions_visible\": true|false, "
            "\"caption_text\": \"<all the caption text you can read>\", "
            "\"where_on_screen\": \"<where they appear>\"}.",
            video_part,
        )
        text = str(read.get("caption_text") or "")
        assert read.get("captions_visible"), (
            f"captions were asked for and the captioned render was downloaded, but there is "
            f"no caption text in the picture. This is the failure that ships an uncaptioned "
            f"reel. File came from {_safe_url(rendered.url)}; model read {text[:200]!r}."
        )
        score = _overlap(NARRATION, text)
        assert score >= MIN_CAPTION_OVERLAP, (
            f"there is text burned into the picture, but it is not our narration: {score:.0%} "
            f"of our content words appear in it (floor {MIN_CAPTION_OVERLAP:.0%}).\n"
            f"  sent: {NARRATION}\n"
            f"  read: {text}\n"
            f"  seen at: {read.get('where_on_screen')}"
        )


class TestTheAvatarAndVoiceAreOurs:
    """Not whatever default the service felt like using.

    Checked against HeyGen's own reference assets for the configured ids,
    because the completed render reports neither: `GET /v3/videos/{id}` returns
    the URLs, duration, timestamps, title and thumbnails, and nothing naming
    the avatar or the voice. Comparing the finished video to the look's preview
    image and the voice's preview audio is the only signal that distinguishes
    "our configuration was honoured" from "something rendered".
    """

    def test_the_speaker_is_the_avatar_the_preset_names(
        self, rendered, gemini, video_part, configured_look
    ):
        client, model = gemini
        preview = _fetch_bytes(str(configured_look["preview_image_url"]))
        verdict = _gemini_json(
            client,
            model,
            "The first input is a video; the second is a reference photo of an avatar. Is "
            "the person speaking in the video the same person as in the reference photo? "
            'Return JSON: {"same_person": true|false, "confidence": "high|medium|low", '
            '"reasoning": "one sentence", "video_person": "brief description"}.',
            video_part,
            _part(preview, "image/webp"),
        )
        assert verdict.get("same_person"), (
            f"THE RENDER IS NOT THE CONFIGURED AVATAR. "
            f"{PRESET_SLUG}.params.heygen.avatar_id is {rendered.config['avatar_id']!r} "
            f"({configured_look.get('name')!r}), but the speaker does not match that look's "
            f"own preview image.\n"
            f"  in the video: {verdict.get('video_person')}\n"
            f"  reasoning:    {verdict.get('reasoning')}\n"
            f"  confidence:   {verdict.get('confidence')}"
        )

    def test_the_voice_is_the_one_the_preset_names(self, rendered, gemini, video_part, heygen):
        voice_id = rendered.config.get("voice_id")
        if not voice_id:
            pytest.skip(f"{PRESET_SLUG} names no voice_id, so the avatar's default is correct")
        client, model = gemini
        voice = heygen.voice(str(voice_id))
        preview_url = voice.get("preview_audio_url")
        if not preview_url:
            pytest.skip(
                f"voice {voice_id!r} carries no preview_audio_url, so there is no reference "
                f"recording to compare the render against."
            )
        preview = _fetch_bytes(str(preview_url))
        verdict = _gemini_json(
            client,
            model,
            "The first input is a video of a person speaking. The second is a short "
            "reference audio clip of a particular voice. Judge ONLY the voice timbre and "
            "identity, not the words: is the speaker in the video the same voice as the "
            'reference clip? Return JSON: {"same_voice": true|false, "confidence": '
            '"high|medium|low", "reasoning": "one sentence"}.',
            video_part,
            _part(preview, "audio/mpeg"),
        )
        assert verdict.get("same_voice"), (
            f"THE RENDER IS NOT USING THE CONFIGURED VOICE. "
            f"{PRESET_SLUG}.params.heygen.voice_id is {voice_id!r} ({voice.get('name')!r}), "
            f"but the narration does not match that voice's own preview recording -- which "
            f"is what happens when HeyGen falls back to a default instead.\n"
            f"  reasoning:  {verdict.get('reasoning')}\n"
            f"  confidence: {verdict.get('confidence')}"
        )


class TestARerunDoesNotPayTwice:
    def test_a_resubmit_replays_the_original_render(self, heygen, rendered):
        """The property that makes this lane safe to retry.

        `POST /v3/videos` with an Idempotency-Key replays the original response
        for 24 hours instead of billing a second render. The pipeline leans on
        this: an ambiguous submit failure releases the claim and resubmits,
        where the same failure on fal cannot. Nothing tested it until now.

        A cached run skips it -- there is nothing to compare against without
        having submitted this session, and re-submitting to find out would
        spend money on a 24-hour-expired key, which is precisely what the cache
        exists to avoid.
        """
        if rendered.from_cache:
            pytest.skip(
                "this run reused the cached render, so no submit happened to replay. Clear "
                f"{CACHE_DIR} to exercise the submit path again -- that costs a new render."
            )
        cfg = rendered.config
        try:
            replay = heygen.create_avatar_video(
                avatar_id=cfg["avatar_id"],
                script=NARRATION,
                idempotency_key=rendered.idempotency_key,
                voice_id=cfg.get("voice_id"),
                aspect_ratio=cfg.get("aspect_ratio", "9:16"),
                resolution=cfg.get("resolution", "1080p"),
                burn_captions=bool(cfg.get("burn_captions", True)),
                fit=cfg.get("fit"),
                title=f"live integration check [{rendered.idempotency_key}]",
                voice_settings=cfg.get("voice_settings"),
                engine=cfg.get("engine"),
                background=cfg.get("background"),
            )
        except HeyGenInProgress:
            pytest.skip(
                "HeyGen reported the original submit still in progress, which is itself the "
                "idempotency key being honoured rather than a second render being started."
            )
        except Exception as exc:  # noqa: BLE001
            _fail(_diagnose(exc, cfg))

        assert replay.id == rendered.video.id, (
            f"A RESUBMIT WITH THE SAME IDEMPOTENCY-KEY STARTED A SECOND RENDER AND BILLED FOR "
            f"IT: got video {replay.id}, expected the original {rendered.video.id}. The "
            f"pipeline releases its claim and resubmits on an ambiguous failure on the "
            f"assumption that this replays. If HeyGen has stopped honouring the key, "
            f"_submit_heygen's retry is no longer free."
        )
