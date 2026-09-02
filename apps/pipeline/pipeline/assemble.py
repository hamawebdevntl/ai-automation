"""Assembly for the end-to-end fal path.

This exists only because MoneyPrinterTurbo cannot be borrowed for a render it
did not produce: its captioner is chosen by global config rather than per
request, and `video_source="local"` is the only way in, which is the
visuals-only path. So a genuinely end-to-end fal render has to be concatenated,
voiced and captioned here.

Everything shells out to ffmpeg, which the media image already carries for the
quality checks.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class AssemblyError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 900) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        # ffmpeg's useful diagnostics are on stderr, and its last few lines are
        # what actually say why.
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-6:])
        raise AssemblyError(f"{cmd[0]} failed ({proc.returncode}): {tail}")


def concat(clips: list[Path], dest: Path) -> Path:
    """Join clips in order.

    Re-encodes rather than stream-copying. Clips from a generative model are
    not guaranteed to share a codec, GOP structure or timebase, and the concat
    demuxer produces silently corrupt output when they differ -- a file that
    plays for the first clip and then freezes.
    """
    if not clips:
        raise AssemblyError("nothing to concatenate")
    if len(clips) == 1:
        return clips[0]

    listing = dest.parent / "concat.txt"
    listing.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-y", str(dest),
    ])
    return dest


def mux_narration(video: Path, audio: Path, dest: Path) -> Path:
    """Replace the video's audio with the narration.

    `-shortest` so the result ends with whichever runs out first: narration
    running past the visuals would leave a frozen final frame, and visuals
    running past the narration would leave dead air.
    """
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-y", str(dest),
    ])
    return dest


def srt_from_transcription(result: dict[str, Any], dest: Path) -> Path | None:
    """Build an SRT from fal's transcription output.

    Real timings, not a script split by punctuation. A caption track derived
    from text alone drifts out of sync within a couple of sentences, which is
    worse than no captions because it reads as broken rather than absent.

    Output shapes vary across fal's transcription models, so the known variants
    are accepted rather than binding to one.
    """
    chunks = (
        result.get("chunks")
        or result.get("segments")
        or result.get("words")
        or []
    )
    lines: list[str] = []
    index = 1
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "").strip()
        start, end = _timestamps(chunk)
        if not text or start is None or end is None or end <= start:
            continue
        lines.append(f"{index}\n{_ts(start)} --> {_ts(end)}\n{text}\n")
        index += 1

    if not lines:
        log.warning("transcription produced no usable timings; skipping captions")
        return None
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def _timestamps(chunk: dict[str, Any]) -> tuple[float | None, float | None]:
    stamp = chunk.get("timestamp")
    if isinstance(stamp, (list, tuple)) and len(stamp) == 2:
        return _f(stamp[0]), _f(stamp[1])
    return _f(chunk.get("start")), _f(chunk.get("end"))


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def burn_subtitles(video: Path, srt: Path, dest: Path, font_size: int = 60) -> Path:
    """Burn captions in.

    Burned in rather than a soft track, because none of the four platforms
    render an embedded subtitle stream -- a soft track would simply be invisible
    on all of them.
    """
    style = (
        f"FontSize={font_size // 3},"          # ASS sizes are not pixel heights
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,"
        "Alignment=2,MarginV=60"
    )
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"subtitles={srt.as_posix()}:force_style='{style}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-y", str(dest),
    ])
    return dest


def to_portrait(video: Path, dest: Path) -> Path:
    """Force 1080x1920, padding rather than cropping.

    A generative model asked for 9:16 usually obliges, but not always. Cropping
    to fit would silently remove part of the frame the model was told to
    compose; padding is visible and honest.
    """
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-y", str(dest),
    ])
    return dest
