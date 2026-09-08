"""The source-footage lane: "here is a video, make something from it".

Every other render mode generates from text. This one takes a file the owner
uploaded and an instruction they wrote, which introduces two failure modes
nothing else in the pipeline has:

  1. **Rendering an incomplete request.** Footage, instruction and consent are
     three separate writes, so a production can sit at the gate with one of them
     missing. Paying to transform footage nobody attached, or to run an
     instruction nobody approved, is the thing this lane must not do -- and the
     consent record is not a nicety here, it is the only thing saying the people
     in the footage agreed to it.
  2. **A signed URL that expires mid-render.** The provider fetches the upload
     when the job leaves the queue rather than when it is accepted, so a URL
     sized for the submit dies under a queued job and the render fails *after*
     being billed. That one is a configuration mistake, and it is caught before
     the claim rather than reported by fal twenty minutes later.

Both are checked here, and the first is checked in three places for the same
reason the script gate is: `approve_script` refuses it, a database trigger
refuses the `task_id` write underneath that, and `submit_render` refuses before
taking a render claim. Only the third is Python.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.activities import render
from tests.conftest import FakeSupa

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

READY = {
    "source_video_key": "sources/p1/1757000000000-clip.mp4",
    "render_instruction": "Cut this to a 30-second vertical reel and grade it warm.",
    "source_consent_at": "2026-09-08T09:00:00Z",
    "script": "The words that go over it.",
    "script_approved_at": "2026-09-08T09:05:00Z",
}


class SourceSupa(FakeSupa):
    """A FakeSupa holding one production on the footage lane."""

    def __init__(self, production: dict[str, Any] | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.row: dict[str, Any] = {
            "id": "p1",
            "idea_id": "i1",
            "style_preset_id": "s1",
            "status": "running",
            "task_id": None,
            **READY,
            **(production or {}),
        }
        self.updates: list[dict[str, Any]] = []
        self.claimed_slots: list[str] = []
        self.signed: list[tuple[str, int]] = []

    def production(self, production_id: str) -> dict[str, Any]:
        self.calls.append(("production", production_id))
        return dict(self.row)

    def idea(self, idea_id: str) -> dict[str, Any]:
        self.calls.append(("idea", idea_id))
        return {"id": idea_id, "title": "Our own office tour", "hook": "Thirty seconds, one take."}

    def style_preset(self, preset_id: str) -> dict[str, Any]:
        self.calls.append(("style_preset", preset_id))
        return {
            "id": preset_id,
            "slug": "fal-restyle",
            "render_mode": render.FAL_VIDEO,
            "video_source": "fal",
            "params": {
                "fal": {
                    "model": "fal-ai/ltx-2.3/video-to-video",
                    "resolution": "1080p",
                    "strength": 0.65,
                    "max_duration_seconds": 40,
                }
            },
        }

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status")))
        self.updates.append(fields)
        self.row.update(fields)
        return {"id": production_id, **fields}

    def claim_render_slot(self, production_id: str, task_id: str) -> bool:
        self.calls.append(("claim_render_slot", task_id))
        self.claimed_slots.append(task_id)
        return True

    def signed_render_url(self, key: str, expires_in: int = 3600) -> str:
        self.signed.append((key, expires_in))
        return f"https://storage.example/{key}?token=signed&exp={expires_in}"


class FakeFal:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict[str, Any]]] = []

    def submit(self, model: str, payload: dict[str, Any]) -> dict[str, str]:
        self.submitted.append((model, payload))
        return {
            "request_id": "req-1",
            "status_url": "https://queue.fal.run/x/requests/req-1/status",
            "response_url": "https://queue.fal.run/x/requests/req-1",
        }


class FakeMpt:
    """Present only to prove it is never spoken to on this lane."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def submit_render(self, params: Any) -> str:
        self.calls.append("submit_render")
        raise AssertionError("MoneyPrinterTurbo must not be asked to render the footage lane")

    def upload_material(self, path: Any) -> str:
        self.calls.append("upload_material")
        raise AssertionError("nothing is handed to MoneyPrinterTurbo on the footage lane")


@pytest.fixture
def cfg(monkeypatch):
    """Settings with a URL lifetime that does outlive the poll budget."""

    class Cfg:
        fal_poll_budget_seconds = 1800
        source_video_url_ttl_seconds = 6 * 3600

    monkeypatch.setattr(render, "settings", lambda: Cfg())
    return Cfg


# ---------------------------------------------------------------------------
# Which inputs are missing, and in what order
# ---------------------------------------------------------------------------


class TestMissingInputsAreNamedOneAtATime:
    """The order is the order an owner is asked for them, in all three places."""

    def test_a_complete_request_is_missing_nothing(self):
        assert render._missing_source_input(dict(READY)) is None

    def test_the_footage_is_asked_for_first(self):
        gap = render._missing_source_input({**READY, "source_video_key": None,
                                            "render_instruction": None, "source_consent_at": None})

        assert gap is not None and "footage" in gap

    def test_then_the_instruction(self):
        gap = render._missing_source_input({**READY, "render_instruction": None,
                                            "source_consent_at": None})

        assert gap is not None and "instruction" in gap

    def test_then_consent(self):
        gap = render._missing_source_input({**READY, "source_consent_at": None})

        assert gap is not None and "consent" in gap

    def test_whitespace_is_not_an_instruction(self):
        # The same trap `20260907170000_script_gate_whitespace.sql` records: a
        # gate that can be passed by pressing Enter is not a gate.
        gap = render._missing_source_input({**READY, "render_instruction": "  \n  "})

        assert gap is not None and "instruction" in gap


# ---------------------------------------------------------------------------
# Nothing renders on an incomplete request
# ---------------------------------------------------------------------------


class TestSubmitRefusesAnIncompleteRequest:
    @pytest.mark.parametrize(
        "absent", ["source_video_key", "render_instruction", "source_consent_at"]
    )
    def test_it_parks_without_taking_a_render_claim(self, absent, cfg):
        # `claim_render_slot` is the moment `task_id` is set, and `task_id` is
        # this system's "money may be moving" marker. Refusing before it is what
        # makes this check free.
        supa = SourceSupa({absent: None})

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.parked, f"a missing {absent} must stop the production"

    def test_missing_consent_parks_even_though_everything_else_is_ready(self, cfg):
        # Worth its own test rather than only a parameter: consent is the one
        # input that is about people rather than about the render, and it is the
        # one a well-meaning change is most likely to treat as optional.
        supa = SourceSupa({"source_consent_at": None})

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert "consent" in supa.parked[0][1]

    def test_an_unapproved_script_still_stops_it(self, cfg):
        # The footage lane does not replace the script gate, it sits behind it.
        supa = SourceSupa({"script_approved_at": None})

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert "without an approved script" in supa.parked[0][1]


# ---------------------------------------------------------------------------
# The submit itself
# ---------------------------------------------------------------------------


class TestWhatIsSentToFal:
    def test_the_instruction_is_the_prompt_verbatim(self, cfg):
        supa = SourceSupa()
        fal = FakeFal()

        render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        _, payload = fal.submitted[0]
        assert payload["prompt"] == READY["render_instruction"]

    def test_the_footage_is_sent_as_a_signed_url(self, cfg):
        supa = SourceSupa()
        fal = FakeFal()

        render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        _, payload = fal.submitted[0]
        assert supa.signed[0][0] == READY["source_video_key"]
        assert payload["video_url"].startswith("https://storage.example/sources/p1/")

    def test_the_url_outlives_the_whole_render(self, cfg):
        # Not the submit: fal fetches the file when the job leaves the queue.
        supa = SourceSupa()
        fal = FakeFal()

        render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        _, ttl = supa.signed[0]
        assert ttl > cfg.fal_poll_budget_seconds

    def test_a_ttl_inside_the_poll_budget_is_refused_before_anything_is_billed(
        self, monkeypatch
    ):
        class Cfg:
            fal_poll_budget_seconds = 1800
            source_video_url_ttl_seconds = 600

        monkeypatch.setattr(render, "settings", lambda: Cfg())
        supa = SourceSupa()
        fal = FakeFal()

        with pytest.raises(ValueError, match="does not outlive"):
            render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        assert fal.submitted == []

    def test_a_bad_ttl_parks_before_the_claim_rather_than_stranding_the_row(self, monkeypatch):
        # The distinction this test exists for: `task_id` cannot be unset by
        # anything an owner can reach, so a row that parks holding one can never
        # have its script or its footage edited again. A mistyped setting must
        # therefore stop the production *before* the claim, where it stays
        # retryable, rather than after it.
        class Cfg:
            fal_poll_budget_seconds = 1800
            source_video_url_ttl_seconds = 600

        monkeypatch.setattr(render, "settings", lambda: Cfg())
        supa = SourceSupa()

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.row["task_id"] is None
        assert "does not outlive" in supa.parked[0][1]

    def test_a_preset_with_no_model_also_parks_before_the_claim(self, cfg):
        supa = SourceSupa()
        supa.style_preset = lambda pid: {  # type: ignore[method-assign]
            "id": pid, "slug": "broken", "render_mode": render.FAL_VIDEO,
            "video_source": "fal", "params": {},
        }

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert "params.fal.model" in supa.parked[0][1]

    def test_the_preset_pins_the_model(self, cfg):
        supa = SourceSupa()
        fal = FakeFal()

        render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        assert fal.submitted[0][0] == "fal-ai/ltx-2.3/video-to-video"

    def test_a_preset_with_no_model_is_a_configuration_error(self, cfg):
        supa = SourceSupa()

        with pytest.raises(ValueError, match="params.fal.model"):
            render._submit_fal_video(
                "p1", dict(supa.row), {"slug": "broken", "params": {}}, supa, FakeFal()
            )

    def test_the_duration_ceiling_is_sent(self, cfg):
        # The only lever on the bill: the rate is per second of *output*, and on
        # this lane the length comes from a file the owner chose.
        supa = SourceSupa()
        fal = FakeFal()

        render._submit_fal_video("p1", dict(supa.row), supa.style_preset("s1"), supa, fal)

        assert fal.submitted[0][1]["duration"] == 40

    def test_the_backend_that_ran_is_recorded_and_the_lane_is_routed_to(self, cfg, monkeypatch):
        # `render_backend` is the column a parked production is read out of, and
        # a preset can be edited afterwards -- so what actually ran has to be
        # written rather than inferred.
        routed: list[str] = []
        monkeypatch.setattr(
            render,
            "_submit_fal_video",
            lambda pid, production, preset, supa, fal=None: routed.append(pid) or {"backend": render.FAL_VIDEO},
        )
        supa = SourceSupa()

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert routed == ["p1"], "a fal_video preset must reach the footage lane"
        assert any(u.get("render_backend") == render.FAL_VIDEO for u in supa.updates)


# ---------------------------------------------------------------------------
# Where the job sits, and what happens when it finishes
# ---------------------------------------------------------------------------


class TestTheLaneNeverTouchesMoneyPrinterTurbo:
    def test_the_job_is_polled_on_fal(self):
        assert render._phase_for(render.FAL_VIDEO) == "fal"

    def test_it_is_one_of_the_fal_modes(self):
        # `poll_render` routes on this, so a mode missing from it is a job that
        # is polled against the wrong backend forever.
        assert render.FAL_VIDEO in render.FAL_MODES

    def test_a_finished_generation_completes_rather_than_handing_off(self, cfg):
        # The `fal_visuals` lane uploads its clips into MoneyPrinterTurbo here.
        # This lane must not: what fal returned is already the reel, and MPT
        # would re-cut it against a script it was never asked to narrate.
        supa = SourceSupa()

        class DoneFal:
            def status(self, url):
                return {"status": "COMPLETED"}

            @staticmethod
            def is_terminal(body):
                return True

            def result(self, url):
                return {"video": {"url": "https://fal.media/out.mp4"}}

            @staticmethod
            def video_urls(result):
                return ["https://fal.media/out.mp4"]

        event = {
            "production_id": "p1",
            "backend": render.FAL_VIDEO,
            "phase": "fal",
            "fal_status_url": "https://queue.fal.run/s",
            "fal_response_url": "https://queue.fal.run/r",
            "started_at": 0,
        }
        mpt = FakeMpt()

        result = render._poll_fal(event, supa, mpt, polls=2, fal=DoneFal())

        assert result["state"] == "complete"
        assert result["phase"] == "fal"
        assert result["video_ref"] == "https://fal.media/out.mp4"
        assert mpt.calls == [], "the footage lane must never reach MoneyPrinterTurbo"

    def test_the_quality_check_does_not_count_cuts_against_the_owners_footage(self):
        # A single-take piece to camera is a perfectly good source video, so
        # warning that the render has no cuts would be reporting on their
        # footage rather than on our work. Same exemption the presenter lane has,
        # and the three lanes that do generate their own cutting keep the check.
        assert render.FAL_VIDEO in render.NO_CUTS_EXPECTED
        assert render.HEYGEN in render.NO_CUTS_EXPECTED
        for mode in (render.MPT, render.FAL_VISUALS, render.FAL_FULL):
            assert mode not in render.NO_CUTS_EXPECTED
