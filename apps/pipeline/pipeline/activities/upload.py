"""The quality check for a cut somebody uploaded.

This is `fetch_and_qc` with the provider removed. The rendered lanes exist to
get bytes out of MoneyPrinterTurbo, fal or HeyGen and into our own bucket
before anything downstream depends on them; an upload is already there -- the
browser put it in the renders bucket, and `create_upload_production` refused to
open the row until it had. So the only work left is the half that was always
provider-agnostic: probe the file, take a poster frame, measure it, and write
the report the review screen renders.

Which is also the argument for uploads entering here rather than anywhere
later. An upload is precisely the case the quality check was written for --
nobody has verified this file mechanically, and unlike a render there is not
even a provider that claimed success. Letting it skip the check to reach Gate 2
sooner would be trusting the one input with the least evidence behind it.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

# Imported rather than reimplemented, private name and all. Poster extraction
# is one ffmpeg invocation with three arguments that matter -- seek a second in,
# one frame, quality 3 -- and two copies of it would drift into two different
# poster frames for the same review screen. Moving it into `qc/` would be the
# tidier home and a wider change than this feature earns.
from pipeline.activities.render import _extract_poster
from pipeline.clients.supa import Supa, SupaError
from pipeline.qc import probe as probe_mod
from pipeline.qc import slideshow

log = logging.getLogger(__name__)


class UploadUnreadable(RuntimeError):
    """The uploaded object could not be fetched.

    Its own class, and this is load-bearing rather than tidy. `Supa` raises
    `SupaError` for everything, and `engine._invoke` reclassifies that as an
    `InfrastructureError` -- which deliberately consumes no attempt and never
    reaches the graph's `catch`, because a PostgREST blip is not the step
    failing. That is right for a blip and wrong for this: an object that is not
    there will not be there in five seconds either, and the row would bounce
    every five seconds indefinitely, looking from the outside exactly like a
    production making progress.

    So the failure is re-raised as the step's own. It gets `check_upload`'s
    three attempts -- enough to ride out a genuine network stumble -- and then
    parks, in front of the person who uploaded the file and can do something
    about it. `HeyGenClient.download` makes the same call for the same reason.
    """


def check_upload(event: dict[str, Any], supa: Supa | None = None) -> dict[str, Any]:
    """Inspect an uploaded cut and open it for review.

    Returns the same keys `fetch_and_qc` does, because the steps after this one
    read them and must not learn that two kinds of production exist:
    `open_gate2` branches on `qc_passed`, and `publish` takes `storage_key`
    from the payload.
    """
    supa = supa or Supa()
    production_id = event["production_id"]
    storage_key = event.get("storage_key") or f"{production_id}/final.mp4"

    with tempfile.TemporaryDirectory(prefix=f"upload-{production_id}-") as tmp:
        try:
            local = supa.download_render(storage_key, Path(tmp) / "final.mp4")
        except SupaError as exc:
            raise UploadUnreadable(f"could not read the uploaded file at {storage_key}: {exc}") from exc

        info = probe_mod.probe(local)

        thumb_key = None
        try:
            thumb = _extract_poster(local, Path(tmp) / "poster.jpg")
            thumb_key = supa.upload_render(production_id, thumb, "poster.jpg")
        except Exception as exc:  # noqa: BLE001 - a missing poster must not block review
            log.warning("could not extract a poster frame for %s: %s", production_id, exc)

        # Measured while the file is local, and tolerant of failure for the
        # same reason as the rendered lanes: a measurement we could not take
        # becomes a warn in the report, never a hard stop.
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
        # We genuinely cannot tell. Every other lane knows whether it burned a
        # subtitle track in because it is the thing that burned it; captions in
        # an uploaded file are pixels, and reading them back would mean OCR.
        # So the check warns, which is the honest answer -- and a warn is not a
        # block: it lands in front of the reviewer, who is looking at the video
        # anyway, next to a note saying to check.
        has_subtitles=False,
        # An uploaded reel is ordinary edited footage, so the cut-rate signal
        # applies. `expect_cuts=False` is for the presenter lane, where a
        # single continuous shot is the intended output.
        expect_cuts=True,
    )

    supa.update_production(
        production_id,
        stage="checked",
        video_url=supa.signed_render_url(storage_key, expires_in=7 * 24 * 3600),
        thumbnail_url=supa.signed_render_url(thumb_key, expires_in=7 * 24 * 3600)
        if thumb_key
        else None,
        duration_seconds=round(info.duration_s, 2),
        qc=report.model_dump(exclude_none=True),
    )
    return {
        "production_id": production_id,
        "storage_key": storage_key,
        "duration_s": info.duration_s,
        "size_bytes": info.size_bytes,
        "qc_passed": report.passed,
        "slideshow_risk": report.slideshow_risk,
    }
