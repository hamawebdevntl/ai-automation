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
    ms = round(seconds * 1000)
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
        "-vf", ("scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-y", str(dest),
    ])
    return dest


# ---------------------------------------------------------------------------
# Cutting an uploaded recording down -- the `clip` lane
# ---------------------------------------------------------------------------
#
# Nothing below calls a provider. The whole of this lane is ffmpeg on a file we
# already hold, which is why it is the one lane where a failed render has cost
# nothing but CPU.

CROP = "crop"
PAD = "pad"


def cut(source: Path, start: float, end: float, dest: Path) -> Path:
    """Extract [start, end] from a longer recording.

    Re-encodes rather than stream-copying, and this is the important choice
    here. `-c copy` can only cut on a keyframe, so it silently moves the cut to
    the nearest one -- up to several seconds away on a screen recording with a
    long GOP. On a clip whose first line *is* the hook, losing the first two
    seconds loses the reason the clip was chosen. Re-encoding puts the cut where
    it was asked for.

    `-ss` before `-i` so the decoder seeks rather than decoding and discarding
    everything up to the start; on an hour-long source that is the difference
    between seconds and minutes. Accurate seek is still guaranteed, because the
    re-encode that follows resolves the frame exactly.
    """
    if end <= start:
        raise AssemblyError(f"a clip must run forwards: {start}s to {end}s")

    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(source),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        # The output timeline starts at zero. Without this the cut keeps the
        # source's timestamps, and every caption -- whose times are relative to
        # the clip -- would be offset by however far in the clip began.
        "-avoid_negative_ts", "make_zero",
        "-y", str(dest),
    ])
    return dest


def reframe_portrait(source: Path, dest: Path, mode: str = CROP) -> Path:
    """Make a 1080x1920 reel out of whatever shape the recording was.

    Two modes, because there is no answer that is right for both kinds of
    source, and the preset chooses:

      `crop`  Centre-crop to 9:16, then scale. What every clipping tool does,
              and right for the intended input -- a person talking, framed in
              the middle of a 16:9 frame. It fills the screen, and it does
              remove the sides. That is the honest cost of a full-bleed reel.

      `pad`   Pillarbox instead, which is what `to_portrait` does for the
              generative lanes and for the reason recorded there: padding is
              visible where cropping is silent. Right when the sides carry the
              content -- a slide, a screen recording, a two-shot -- where a
              centre crop would cut away half of what the clip is about.

    Speaker tracking is deliberately absent. Following a face would need
    per-frame detection and a smoothed crop path, which is a different size of
    feature and a new dependency; a fixed centre crop is what this lane ships
    with.

    An unknown mode pads rather than raising. It comes from `style_presets.params`,
    which is raw JSON a person edits, and the failure mode of a typo should be a
    reel with black bars rather than a production that parks after the owner
    approved it.
    """
    if mode == CROP:
        # `min(iw,ih*9/16)` is what makes this safe on a source that is already
        # portrait, or square: the crop window can never be wider or taller than
        # the frame it is taken from, so a 9:16 upload passes through untouched
        # rather than being cropped to a sliver.
        vf = (
            "crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='(iw-ow)/2':y='(ih-oh)/2',"
            "scale=1080:1920:flags=lanczos,setsar=1"
        )
    else:
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
        )

    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy", "-y", str(dest),
    ])
    return dest


def srt_from_segments(
    segments: list[dict[str, Any]] | list[Any],
    dest: Path,
    offset: float = 0.0,
) -> Path | None:
    """Build an SRT from transcript segments, rebased onto a clip's timeline.

    `offset` is where the clip starts in the source. The segments' times are
    absolute -- they describe the whole recording -- and `cut` produces a file
    whose timeline starts at zero, so every caption has to move back by that
    much or the whole track sits ahead of the words by however far in the clip
    began.

    Accepts either dicts or `TranscriptSegment`s so a caller does not have to
    round-trip through the model to write a file.

    Returns None when nothing usable survives, matching
    `srt_from_transcription`: a clip with no captions is still a clip, and the
    quality report is what says they are missing.
    """
    lines: list[str] = []
    index = 1
    for segment in segments:
        if isinstance(segment, dict):
            text = str(segment.get("text") or "").strip()
            start, end = _f(segment.get("start")), _f(segment.get("end"))
        else:
            text = str(getattr(segment, "text", "") or "").strip()
            start, end = _f(getattr(segment, "start", None)), _f(getattr(segment, "end", None))

        if not text or start is None or end is None:
            continue
        start, end = start - offset, end - offset
        # A segment that ends at or before zero belongs to the part of the
        # recording this clip does not contain.
        if end <= 0 or end <= start:
            continue
        lines.append(f"{index}\n{_ts(max(0.0, start))} --> {_ts(end)}\n{text}\n")
        index += 1

    if not lines:
        log.warning("no usable caption timings for this clip; shipping it without captions")
        return None
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest
