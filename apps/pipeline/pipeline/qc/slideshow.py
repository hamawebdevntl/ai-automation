"""Slideshow-risk scoring, and assembly of the report the review UI renders.

The specific failure this exists to catch: a render that is technically a valid
video but is really a sequence of barely-moving stills. It passes every codec
and duration check, and it is the single thing that makes an AI-generated reel
look cheap. Nobody should have to watch ten videos a day to notice it.

Everything in this module is pure -- it takes measurements and returns a score
-- so it is testable without ffmpeg or a rendered file.
"""

from __future__ import annotations

from pipeline.models import QcCheck, QcReport
from pipeline.qc.probe import MediaInfo

# A reel with real motion cuts often. Below this rate the frame is mostly
# static, whatever the codec says.
TARGET_CUTS_PER_10S = 3.0

FAIL_AT = 0.60
WARN_AT = 0.35

# Narration that failed silently still yields a valid file with a silent track.
SILENCE_DB = -50.0
QUIET_DB = -30.0

MIN_DURATION_S = 5.0
MAX_DURATION_S = 180.0


def slideshow_risk(
    duration_s: float,
    frozen_s: float,
    scene_changes: int,
    expect_cuts: bool = True,
) -> float:
    """Score 0.0 (lively) to 1.0 (a slideshow).

    Two independent signals, because each alone has a blind spot. Frozen
    fraction catches long held stills but misses a slow pan that never cuts;
    cut rate catches the absence of editing but misses a file that is 80%
    frozen between a handful of cuts.

    `expect_cuts=False` drops the cut-rate signal and scores on frozen fraction
    alone. That is for the presenter lane, where a single continuous shot of a
    person talking is the intended output rather than a defect: scored the
    normal way, every talking head lands at 0.40 for the crime of not being
    edited, and a warning that fires on every video in a lane is a warning
    nobody reads. Frozen fraction still applies, because an avatar render that
    froze is a real failure and looks identical to a still image.
    """
    if duration_s <= 0:
        return 1.0

    frozen_fraction = max(0.0, min(1.0, frozen_s / duration_s))

    if not expect_cuts:
        return round(max(0.0, min(1.0, frozen_fraction)), 3)

    cuts_per_10s = scene_changes / (duration_s / 10.0)
    motion_deficit = max(0.0, min(1.0, 1.0 - (cuts_per_10s / TARGET_CUTS_PER_10S)))

    return round(max(0.0, min(1.0, 0.6 * frozen_fraction + 0.4 * motion_deficit)), 3)


def build_report(
    info: MediaInfo,
    *,
    frozen_s: float,
    scene_changes: int,
    mean_volume_db: float | None,
    has_subtitles: bool,
    expect_portrait: bool = True,
    expect_cuts: bool = True,
) -> QcReport:
    """Assemble `productions.qc`.

    The shape is fixed by what the review screen already parses, so the keys
    and the pass/warn/fail vocabulary are not ours to change.
    """
    checks: list[QcCheck] = []

    checks.append(
        QcCheck(
            key="container",
            label="Video stream present",
            status="pass",
            detail=f"{info.video_codec or 'unknown'} {info.width}x{info.height} @ {info.fps:.1f}fps",
        )
    )

    if MIN_DURATION_S <= info.duration_s <= MAX_DURATION_S:
        duration_status, duration_detail = "pass", f"{info.duration_s:.1f}s"
    elif info.duration_s < MIN_DURATION_S:
        duration_status = "fail"
        duration_detail = f"{info.duration_s:.1f}s is too short to publish"
    else:
        duration_status = "warn"
        duration_detail = f"{info.duration_s:.1f}s exceeds the {MAX_DURATION_S:.0f}s short-form norm"
    checks.append(
        QcCheck(key="duration", label="Duration", status=duration_status, detail=duration_detail)
    )

    if expect_portrait:
        ok = info.is_portrait_9x16
        checks.append(
            QcCheck(
                key="aspect",
                label="Portrait 9:16",
                status="pass" if ok else "fail",
                detail=f"{info.width}x{info.height}"
                + ("" if ok else " is not 9:16; it will be letterboxed or cropped"),
            )
        )

    if not info.has_audio:
        checks.append(
            QcCheck(key="audio", label="Audio track", status="fail", detail="no audio stream")
        )
    elif mean_volume_db is None:
        checks.append(
            QcCheck(
                key="audio", label="Audio track", status="warn",
                detail="present, but the level could not be measured",
            )
        )
    elif mean_volume_db <= SILENCE_DB:
        checks.append(
            QcCheck(
                key="audio", label="Audio track", status="fail",
                detail=f"effectively silent at {mean_volume_db:.1f} dB -- narration probably failed",
            )
        )
    elif mean_volume_db <= QUIET_DB:
        checks.append(
            QcCheck(
                key="audio", label="Audio track", status="warn",
                detail=f"quiet at {mean_volume_db:.1f} dB",
            )
        )
    else:
        checks.append(
            QcCheck(key="audio", label="Audio track", status="pass",
                    detail=f"mean {mean_volume_db:.1f} dB")
        )

    checks.append(
        QcCheck(
            key="captions",
            label="Captions burned in",
            status="pass" if has_subtitles else "warn",
            detail="subtitle track was generated"
            if has_subtitles
            else "the render reported no subtitle file; captions may be missing",
        )
    )

    risk = slideshow_risk(info.duration_s, frozen_s, scene_changes, expect_cuts=expect_cuts)
    if risk >= FAIL_AT:
        risk_status = "fail"
    elif risk >= WARN_AT:
        risk_status = "warn"
    else:
        risk_status = "pass"
    frozen_pct = (frozen_s / info.duration_s * 100) if info.duration_s else 100.0
    # The detail has to match how the score was actually computed, or a
    # reviewer reads "0 cuts" next to a pass and stops trusting the number.
    detail = (
        f"risk {risk:.2f} -- {scene_changes} cuts over {info.duration_s:.0f}s, "
        f"{frozen_pct:.0f}% of the runtime frozen"
        if expect_cuts
        else (
            f"risk {risk:.2f} -- single-shot lane, so cuts are not counted; "
            f"{frozen_pct:.0f}% of the runtime frozen"
        )
    )
    checks.append(
        QcCheck(key="slideshow_risk", label="Motion", status=risk_status, detail=detail)
    )

    return QcReport(
        passed=not any(c.status == "fail" for c in checks),
        slideshow_risk=risk,
        checks=checks,
    )
