"""Tests for the quality-check scoring.

The scoring is deliberately pure so it can be tested without ffmpeg or a
rendered file. The names say what each case is protecting, because the point of
this module is to stop a specific class of bad video reaching a reviewer.
"""

from pipeline.qc.probe import MediaInfo
from pipeline.qc.slideshow import build_report, slideshow_risk


def info(duration=30.0, w=1080, h=1920, audio=True) -> MediaInfo:
    return MediaInfo(
        duration_s=duration, width=w, height=h, fps=30.0, has_audio=audio,
        video_codec="h264", audio_codec="aac" if audio else None, size_bytes=8_000_000,
    )


class TestSlideshowRisk:
    def test_a_well_cut_reel_scores_near_zero(self):
        assert slideshow_risk(30.0, frozen_s=0.0, scene_changes=12) < 0.1

    def test_a_mostly_frozen_video_is_caught(self):
        # The failure this whole module exists for: valid file, no real motion.
        assert slideshow_risk(30.0, frozen_s=25.0, scene_changes=1) >= 0.6

    def test_no_cuts_at_all_warns_even_when_nothing_is_frozen(self):
        # A slow pan never triggers freezedetect, so cut rate has to carry it.
        risk = slideshow_risk(30.0, frozen_s=0.0, scene_changes=0)
        assert 0.35 <= risk < 0.6

    def test_zero_duration_is_maximum_risk_not_a_crash(self):
        assert slideshow_risk(0.0, frozen_s=0.0, scene_changes=0) == 1.0

    def test_score_is_bounded(self):
        assert slideshow_risk(10.0, frozen_s=999.0, scene_changes=0) <= 1.0


class TestReport:
    def test_a_good_render_passes(self):
        r = build_report(info(), frozen_s=0.0, scene_changes=12,
                         mean_volume_db=-18.0, has_subtitles=True)
        assert r.passed is True
        assert all(c.status != "fail" for c in r.checks)

    def test_silent_audio_fails_because_narration_probably_broke(self):
        r = build_report(info(), frozen_s=0.0, scene_changes=12,
                         mean_volume_db=-70.0, has_subtitles=True)
        assert r.passed is False
        assert any(c.key == "audio" and c.status == "fail" for c in r.checks)

    def test_missing_audio_stream_fails(self):
        r = build_report(info(audio=False), frozen_s=0.0, scene_changes=12,
                         mean_volume_db=None, has_subtitles=True)
        assert r.passed is False

    def test_landscape_render_fails_the_aspect_check(self):
        r = build_report(info(w=1920, h=1080), frozen_s=0.0, scene_changes=12,
                         mean_volume_db=-18.0, has_subtitles=True)
        assert any(c.key == "aspect" and c.status == "fail" for c in r.checks)

    def test_missing_captions_warns_but_does_not_block(self):
        r = build_report(info(), frozen_s=0.0, scene_changes=12,
                         mean_volume_db=-18.0, has_subtitles=False)
        assert r.passed is True
        assert any(c.key == "captions" and c.status == "warn" for c in r.checks)

    def test_a_slideshow_fails_the_report_not_just_the_score(self):
        r = build_report(info(), frozen_s=25.0, scene_changes=1,
                         mean_volume_db=-18.0, has_subtitles=True)
        assert r.passed is False
        assert r.slideshow_risk >= 0.6
