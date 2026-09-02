"""Technical inspection of a rendered file, via ffprobe and ffmpeg.

Design ported from OpenMontage's pre-compose validation and post-render
self-review, reimplemented here. OpenMontage is AGPL-3.0 and STACK.md commits
us to treating it as a read-only reference, so nothing is copied -- only the
idea that a render should be inspected mechanically before a person is asked to
look at it.

Subprocess calls are kept in this module and the scoring is pure, so the
interesting logic can be tested without ffmpeg present.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    size_bytes: int

    @property
    def is_portrait_9x16(self) -> bool:
        if not self.width or not self.height:
            return False
        # Allow a little slack: 1080x1920 is exact, but encoders occasionally
        # round to an even macroblock.
        return abs((self.height / self.width) - (16 / 9)) < 0.02


def _run(cmd: list[str], timeout: int = 120) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise ProbeError(f"{cmd[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{cmd[0]} timed out after {timeout}s") from exc
    # ffmpeg writes its filter output to stderr, so both streams matter.
    return (proc.stdout or "") + (proc.stderr or "")


def probe(path: Path) -> MediaInfo:
    out = _run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    try:
        data = json.loads(out[out.index("{") :])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProbeError(f"ffprobe returned no parseable JSON for {path}: {out[:300]}") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError(f"{path} has no video stream")

    fmt = data.get("format") or {}
    return MediaInfo(
        duration_s=float(fmt.get("duration") or video.get("duration") or 0.0),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
        has_audio=audio is not None,
        video_codec=video.get("codec_name"),
        audio_codec=(audio or {}).get("codec_name"),
        size_bytes=int(fmt.get("size") or (path.stat().st_size if path.exists() else 0)),
    )


def _parse_fps(ratio: str) -> float:
    try:
        num, _, den = ratio.partition("/")
        d = float(den or 1)
        return float(num) / d if d else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def mean_volume_db(path: Path) -> float | None:
    """Mean volume via ffmpeg's volumedetect.

    A render whose narration failed silently still produces a valid file with a
    silent audio track, which is exactly the failure a person should never have
    to catch by watching.
    """
    out = _run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", out)
    return float(match.group(1)) if match else None


def frozen_seconds(path: Path, noise_db: float = -60.0, min_freeze_s: float = 1.0) -> float:
    """Total duration covered by ffmpeg's freezedetect."""
    out = _run(
        [
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-vf", f"freezedetect=n={noise_db}dB:d={min_freeze_s}",
            "-map", "0:v:0", "-f", "null", "-",
        ]
    )
    return sum(float(m) for m in re.findall(r"freeze_duration:\s*(\d+(?:\.\d+)?)", out))


def scene_change_count(path: Path, threshold: float = 0.3) -> int:
    """Number of detected scene changes -- a proxy for how much the frame actually changes."""
    out = _run(
        [
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-vf", f"select='gt(scene,{threshold})',metadata=print",
            "-an", "-f", "null", "-",
        ]
    )
    return len(re.findall(r"lavfi\.scene_score", out))
