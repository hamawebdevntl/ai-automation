"""Spend caps.

The requirement is one sentence -- *no render is submitted that cannot be paid
for* -- and, like the script gate, it is enforced in more than one place because
the failure mode is money:

  1. `spend_block_reason()` in Postgres refuses a capped style at Gate 1. That
     is SQL and is not tested here.
  2. `submit_render` asks the same function before taking a render claim, so a
     production already in flight parks rather than billing.
  3. A provider that reports a balance is asked whether it can actually pay,
     because our own counter is only as right as what we have recorded.

What these tests are really pinning is the *direction* of every trade-off,
since almost all of them are recoverable one way and not the other:

  * a refusal happens before `claim_render_slot`, so it costs nothing and the
    production stays retryable;
  * spend is recorded before the backend is called, so an unrecordable render
    does not happen;
  * a retry cannot count twice;
  * the provider's own figure replaces ours, and never the other way round.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline import spend
from pipeline.activities import render
from pipeline.models import HeyGenVideo
from tests.conftest import FakeSupa


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
    """The poll budgets, without requiring a Supabase URL to read them.

    `_poll_heygen` reads its wall-clock budget from `settings()`, which refuses
    to construct without the service-role credentials -- correctly, since the
    real thing must fail loudly rather than write nowhere.
    """

    class Cfg:
        heygen_poll_budget_seconds = 1800
        fal_poll_budget_seconds = 1800
        mpt_render_budget_seconds = 3600
        fal_transcribe_model = "fal-ai/whisper"

    monkeypatch.setattr(render, "settings", lambda: Cfg())
    return Cfg


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class SpendSupa(FakeSupa):
    """A FakeSupa holding one production and one preset.

    The same shape `ScriptSupa` in test_script_gate.py needs, and for the same
    reason: `submit_render` reads a row, a preset and an idea before it decides
    anything.
    """

    def __init__(
        self,
        production: dict[str, Any] | None = None,
        preset: dict[str, Any] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self.row: dict[str, Any] = {
            "id": "p1",
            "idea_id": "i1",
            "style_preset_id": "s1",
            "status": "running",
            "script": "Two systems, one truth. Here is what it costs you.",
            "script_approved_at": "2026-09-08T09:00:00Z",
            "task_id": None,
            **(production or {}),
        }
        self.preset: dict[str, Any] = {
            "id": "s1",
            "slug": "stock-broll",
            "render_mode": "mpt",
            "video_source": "pexels",
            "params": {},
            **(preset or {}),
        }
        self.claimed_slots: list[str] = []

    def production(self, production_id: str) -> dict[str, Any]:
        self.calls.append(("production", production_id))
        return dict(self.row)

    def idea(self, idea_id: str) -> dict[str, Any]:
        self.calls.append(("idea", idea_id))
        return {"id": idea_id, "title": "The double-entry tax", "hook": "Two systems, one truth."}

    def style_preset(self, preset_id: str) -> dict[str, Any]:
        self.calls.append(("style_preset", preset_id))
        return dict(self.preset)

    def update_production(self, production_id: str, **fields: Any) -> dict[str, Any]:
        self.calls.append(("update_production", fields.get("status")))
        self.row.update(fields)
        return {"id": production_id, **fields}

    def claim_render_slot(self, production_id: str, task_id: str) -> bool:
        self.calls.append(("claim_render_slot", task_id))
        self.claimed_slots.append(task_id)
        return True


class FakeMpt:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit_render(self, params: Any) -> str:
        self.submitted.append(params)
        return "p1"


class FakeHeyGen:
    """Just the two calls the spend path makes."""

    def __init__(self, balances: list[float | None] | None = None, video_id: str = "v1") -> None:
        self._balances = list(balances if balances is not None else [10.0])
        self.video_id = video_id
        self.reads = 0

    def balance(self) -> float | None:
        self.reads += 1
        # The last figure repeats, so a test that only cares about the opening
        # balance does not have to supply one per read.
        index = min(self.reads - 1, len(self._balances) - 1)
        return self._balances[index]

    def create_avatar_video(self, **kw: Any) -> Any:
        return HeyGenVideo(id=self.video_id, status="processing")


PRESENTER = {
    "id": "s1",
    "slug": "ai-presenter",
    "render_mode": "heygen",
    "video_source": "heygen",
    "params": {"heygen": {"avatar_id": "av1"}},
}

FAL_PRESET = {
    "id": "s1",
    "slug": "fal-generative",
    "render_mode": "fal_visuals",
    "video_source": "fal",
    "params": {"fal": {"model": "fal-ai/ltx-2.3/text-to-video/fast", "clips": 6, "clip_seconds": 5}},
}

# The end-to-end lane: the one preset that bills three models rather than one.
E2E_PRESET = {
    "id": "s1",
    "slug": "fal-end-to-end",
    "render_mode": "fal_full",
    "video_source": "fal",
    "params": {
        "fal": {
            "model": "fal-ai/ltx-2.3/text-to-video",
            "clips": 4,
            "clip_seconds": 5,
            "tts_model": "fal-ai/elevenlabs/tts/turbo-v2.5",
            "burn_captions": True,
        }
    },
}

FAL_KEY = {"provider": "fal", "model": "fal-ai/ltx-2.3/text-to-video/fast"}
FAL_RATE = {**FAL_KEY, "unit": "second", "rate_usd": 0.04}


# ---------------------------------------------------------------------------
# The arithmetic
# ---------------------------------------------------------------------------


class TestRate:
    def test_a_per_second_rate_multiplies_the_duration(self):
        rate = spend.Rate("fal", "ltx", "second", 0.04)
        assert rate.amount_usd(seconds=30) == 1.2

    def test_a_per_character_rate_multiplies_the_text(self):
        rate = spend.Rate("fal", "tts", "character", 0.00005)
        assert rate.amount_usd(characters=1000) == 0.05

    def test_a_per_clip_rate_multiplies_the_clips(self):
        rate = spend.Rate("mpt", "wavespeed", "clip", 0.1)
        assert rate.amount_usd(clips=6) == 0.6

    def test_a_per_render_rate_ignores_every_quantity(self):
        # The stock lane. Free footage and a free local voice, so the figure is
        # the same whatever the reel's length -- and recording that zero is
        # what makes "a cost is recorded for every provider" true rather than
        # "for the two that bill".
        rate = spend.Rate("mpt", "pexels", "render", 0.0)
        assert rate.amount_usd(seconds=45, characters=900, clips=9) == 0.0

    def test_a_missing_quantity_refuses_rather_than_pricing_at_zero(self):
        # The whole point of `Unpriceable`. A render whose cost silently came
        # out at zero would spend against a ceiling without moving it, which is
        # the exact hole these caps exist to close.
        rate = spend.Rate("fal", "ltx", "second", 0.04)
        with pytest.raises(spend.Unpriceable):
            rate.amount_usd(characters=100)

    def test_an_unrecognised_unit_refuses(self):
        rate = spend.Rate("fal", "ltx", "furlong", 1.0)
        with pytest.raises(spend.Unpriceable):
            rate.amount_usd(seconds=1)

    def test_a_row_becomes_a_rate(self):
        rate = spend.Rate.from_row(
            {"provider": "fal", "model": "ltx", "unit": "second", "rate_usd": "0.04"}
        )
        assert (rate.provider, rate.unit, rate.rate_usd) == ("fal", "second", 0.04)


class TestSpeechSeconds:
    def test_a_reel_length_script_prices_as_a_reel_length_render(self):
        # 2.5 words a second is the assumption the drafting prompt writes to
        # ("aim for 30-60 seconds when read aloud"), so a 100-word script is
        # forty seconds rather than an arbitrary number.
        assert spend.speech_seconds(" ".join(["word"] * 100)) == 40.0

    def test_no_script_is_no_seconds(self):
        assert spend.speech_seconds("   ") == 0.0

    def test_clips_cover_the_runtime(self):
        assert spend.clip_count(21, 5) == 5
        assert spend.clip_count(0, 5) == 0
        assert spend.clip_count(10, 0) == 0


class TestQuantities:
    def test_fal_is_priced_on_the_duration_it_is_actually_asked_for(self):
        # Not on the script. fal bills per second of *output*, and the output
        # length is the one `_submit_fal` sends -- so these two must be the
        # same number or the cap is checked against a render nobody ordered.
        cfg = FAL_PRESET["params"]["fal"]
        quantity = render._quantities(spend.FAL, FAL_PRESET, "a short script")
        assert quantity["seconds"] == float(render._fal_duration(cfg))

    def test_the_other_lanes_are_priced_on_the_narration(self):
        # Nothing else exists yet: MoneyPrinterTurbo cuts footage to the
        # narration and HeyGen reads it aloud, so the script is the runtime.
        script = " ".join(["word"] * 75)
        quantity = render._quantities(spend.HEYGEN, PRESENTER, script)
        assert quantity["seconds"] == 30.0
        assert quantity["characters"] == len(script)


# ---------------------------------------------------------------------------
# The render step refuses
# ---------------------------------------------------------------------------


class TestACappedStyleCannotRender:
    def test_it_parks_without_taking_a_render_claim(self):
        # `claim_render_slot` is where `task_id` is set, and `task_id` is this
        # system's "money may be moving" marker. Refusing before it is what
        # keeps the refusal free and the production retryable.
        supa = SpendSupa(block_reason="Daily spend cap reached for mpt: $10.00 of $10.00 today.")

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.parked, "a capped style must stop the production"
        assert "Daily spend cap reached" in supa.parked[0][1]

    def test_it_records_no_spend_and_submits_nothing(self):
        supa = SpendSupa(block_reason="Monthly spend cap reached for fal: $600.00 of $600.00.")
        mpt = FakeMpt()

        render.submit_render({"production_id": "p1"}, supa, mpt)

        assert supa.recorded_spend == []
        assert mpt.submitted == []

    def test_the_verdict_comes_from_postgres_rather_than_from_here(self):
        # The one thing this feature cannot survive is the app and the worker
        # disagreeing about whether a style can be paid for, so the worker asks
        # the same function Gate 1 asks instead of summing the ledger itself.
        supa = SpendSupa()

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        asked = [args for name, args in supa.calls if name == "spend_block_reason"]
        assert asked == [("mpt", "pexels")]

    def test_an_unpriced_model_is_refused_rather_than_costed_at_zero(self):
        # The realistic way in: an owner points `params.fal.model` at a premium
        # tier. Those bill $0.20-0.75 a second, which is $1,500-5,400 a month
        # at ten reels a day -- so a model nothing prices must not render.
        supa = SpendSupa(
            preset=FAL_PRESET,
            spend_key={"provider": "fal", "model": "fal-ai/veo-3/audio-native"},
            rate=None,
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert "nothing prices fal / fal-ai/veo-3/audio-native" in supa.parked[0][1]

    def test_heygen_still_renders_unpriced_because_the_wallet_prices_it(self):
        # The one exception, and the reason `REPORTS_ITS_OWN_COST` exists: a
        # presenter render is measured from the wallet, so a missing rate costs
        # accuracy in the estimate rather than the ability to account for it.
        supa = SpendSupa(
            preset=PRESENTER, spend_key={"provider": "heygen", "model": ""}, rate=None
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), FakeHeyGen())

        assert supa.parked == []
        assert supa.claimed_slots == ["p1"]

    def test_heygen_with_neither_a_rate_nor_a_readable_wallet_is_refused(self):
        # The exception is only an exception because the wallet can price the
        # render instead. With neither, nothing could ever say what a presenter
        # reel cost, and the heygen cap would read zero however many were made.
        supa = SpendSupa(
            preset=PRESENTER, spend_key={"provider": "heygen", "model": ""}, rate=None
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), FakeHeyGen(balances=[None]))

        assert supa.claimed_slots == []
        assert "nothing prices heygen" in supa.parked[0][1]


class TestTheProviderIsAskedWhetherItCanPay:
    def test_a_wallet_below_the_render_refuses_before_submitting(self):
        # The acceptance criterion: refused *before* the request, not after.
        supa = SpendSupa(
            preset=PRESENTER,
            spend_key={"provider": "heygen", "model": ""},
            rate={"provider": "heygen", "model": "", "unit": "second", "rate_usd": 0.05},
        )
        heygen = FakeHeyGen(balances=[0.10])

        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), heygen)

        assert supa.claimed_slots == []
        assert "the HeyGen wallet holds $0.10" in supa.parked[0][1]

    def test_a_wallet_that_cannot_be_read_does_not_park_the_render(self):
        # "This plan reports no balance" and "this account is empty" must not
        # lead to the same decision. The counter still applies either way.
        supa = SpendSupa(
            preset=PRESENTER,
            spend_key={"provider": "heygen", "model": ""},
            rate={"provider": "heygen", "model": "", "unit": "second", "rate_usd": 0.05},
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), FakeHeyGen(balances=[None]))

        assert supa.parked == []
        assert supa.claimed_slots == ["p1"]

    def test_a_wallet_below_the_floor_refuses_even_when_the_rate_says_otherwise(self):
        # The rate is a row an owner can edit; the wallet is not. A rate edited
        # to zero would otherwise make every empty account look affordable.
        supa = SpendSupa(
            preset=PRESENTER,
            spend_key={"provider": "heygen", "model": ""},
            rate={"provider": "heygen", "model": "", "unit": "second", "rate_usd": 0.0},
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), FakeHeyGen(balances=[0.01]))

        assert supa.claimed_slots == []
        assert supa.parked


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


class TestSpendIsRecordedForEveryProvider:
    def test_the_stock_lane_records_its_zero(self):
        supa = SpendSupa()

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        rows = supa.spend_of("render")
        assert len(rows) == 1
        assert (rows[0]["provider"], rows[0]["model"], rows[0]["amount_usd"]) == ("mpt", "pexels", 0.0)
        assert rows[0]["source"] == "derived"

    def test_fal_records_the_duration_it_asked_for(self):
        supa = SpendSupa(
            preset=FAL_PRESET,
            spend_key={"provider": "fal", "model": "fal-ai/ltx-2.3/text-to-video/fast"},
            rate={
                "provider": "fal",
                "model": "fal-ai/ltx-2.3/text-to-video/fast",
                "unit": "second",
                "rate_usd": 0.04,
            },
        )

        class Fal:
            def __init__(self) -> None:
                self.payload: dict[str, Any] = {}

            def submit(self, model: str, payload: dict[str, Any]) -> dict[str, str]:
                self.payload = payload
                return {"request_id": "r1", "status_url": "s", "response_url": "r"}

        fal = Fal()
        render._submit_fal("p1", supa.idea("i1"), FAL_PRESET, "fal_visuals", supa, "words", fal)
        plan = render._spend_gate(supa, "p1", FAL_PRESET, "words")

        # 6 clips x 5s, clamped to the model's 20-second ceiling, at $0.04/s.
        assert fal.payload["duration"] == 20
        assert plan.estimate_usd == 0.8

    def test_the_ledger_is_written_before_the_claim_and_the_backend(self):
        """Both orderings matter, for different reasons.

        Before the *backend*, because a render this ledger will not accept is a
        render whose cost nothing would ever count.

        Before the *claim*, because of what a failure would otherwise leave
        behind. `claim_render_slot` sets `task_id`, and a raise after that parks
        the production holding a claim -- and `retry_production` will not clear
        `task_id`, so the retry deduplicates straight into `poll_render` and
        parks again on a render that was never submitted. Writing first makes a
        ledger failure a plain retry of a step that has done nothing.
        """
        supa = SpendSupa()
        order: list[str] = []

        class Mpt(FakeMpt):
            def submit_render(self, params: Any) -> str:
                order.append("backend")
                return "p1"

        original_record = supa.record_spend
        original_claim = supa.claim_render_slot

        def record(*args: Any, **kw: Any) -> None:
            order.append("ledger")
            original_record(*args, **kw)

        def claim(*args: Any, **kw: Any) -> bool:
            order.append("claim")
            return original_claim(*args, **kw)

        supa.record_spend = record  # type: ignore[method-assign]
        supa.claim_render_slot = claim  # type: ignore[method-assign]

        render.submit_render({"production_id": "p1"}, supa, Mpt())

        assert order == ["ledger", "claim", "backend"]

    def test_a_ledger_that_refuses_the_write_leaves_no_claim_behind(self):
        # The unrecoverable state this ordering exists to prevent: parked, with
        # `task_id` set, on a render nothing ever submitted.
        supa = SpendSupa()
        mpt = FakeMpt()

        def explode(*args: Any, **kw: Any) -> None:
            raise RuntimeError("PostgREST is having a moment")

        supa.record_spend = explode  # type: ignore[method-assign]

        with pytest.raises(RuntimeError):
            render.submit_render({"production_id": "p1"}, supa, mpt)

        assert supa.claimed_slots == []
        assert supa.row["task_id"] is None
        assert mpt.submitted == []

    def test_losing_the_claim_race_records_no_second_charge(self):
        # Writing before the claim means a loser writes too. Harmless: the
        # winner writes the same row under the same key, and the write is an
        # upsert that ignores a duplicate.
        supa = SpendSupa()
        supa.claim_render_slot = lambda production_id, task_id: False  # type: ignore[method-assign]

        result = render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert result["deduplicated"] is True
        assert len(supa.spend_of("render")) == 1

    def test_a_resubmitted_render_is_not_counted_twice(self):
        # MoneyPrinterTurbo answers a saturated queue with a 429, and the graph
        # retries it ten times. A ledger that counted each attempt would close
        # a cap that had not been reached.
        supa = SpendSupa()

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())
        supa.row["task_id"] = None
        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert len(supa.spend_of("render")) == 1



class TestOnlyFalIsChargedTwiceForAResubmit:
    """The one lane where a resubmit is a second bill.

    MoneyPrinterTurbo deduplicates on our fork's caller-supplied task id and
    HeyGen on its `Idempotency-Key`, and both are given the production id -- so
    a submit retried through a 429 or a transport error bills once. fal has no
    idempotency key of any kind: `_submit_fal` releases the claim on an
    ambiguous failure, and the generation the retry starts is a fresh billed
    one. Its `request_id` is the only thing that tells the two apart.

    A submit attempt number was tried first and does not work: the driver
    strips `attempts` from the payload it hands an activity, and
    `retry_production` clears the counter for the step being retried -- so the
    number reads zero on exactly the retry that bills the second time.
    """

    class Fal:
        def __init__(self, request_ids: list[str]) -> None:
            self._ids = list(request_ids)
            self.calls = 0

        def submit(self, model: str, payload: dict[str, Any]) -> dict[str, str]:
            request_id = self._ids[min(self.calls, len(self._ids) - 1)]
            self.calls += 1
            return {"request_id": request_id, "status_url": "s", "response_url": "r"}

    def _fal_supa(self) -> SpendSupa:
        return SpendSupa(preset=FAL_PRESET, spend_key=FAL_KEY, rate=FAL_RATE)

    def _submit(self, supa: SpendSupa, fal: Any) -> None:
        render.submit_render({"production_id": "p1"}, supa, FakeMpt(), fal=fal)

    def test_the_first_generation_stamps_the_reservation_rather_than_adding_a_row(self):
        # One render, one charge. The reservation written before the submit is
        # the charge; fal's request id is attached to it once fal answers.
        supa = self._fal_supa()

        self._submit(supa, self.Fal(["req-1"]))

        rows = supa.spend_of("render")
        assert len(rows) == 1
        assert rows[0]["external_ref"] == ""
        assert rows[0]["detail"]["request_id"] == "req-1"
        assert rows[0]["amount_usd"] == 0.8

    def test_re_entering_the_step_without_fal_billing_again_adds_nothing(self):
        # The same request id: the step was re-entered, fal was not paid twice.
        supa = self._fal_supa()
        fal = self.Fal(["req-1"])

        self._submit(supa, fal)
        supa.row["task_id"] = None
        self._submit(supa, fal)

        assert len(supa.spend_of("render")) == 1

    def test_a_second_generation_is_a_second_charge(self):
        # A new request id means fal started -- and billed -- a fresh
        # generation, which is what an owner retrying a parked fal production
        # causes. Counting it once would leave the cap short by a whole render.
        supa = self._fal_supa()

        self._submit(supa, self.Fal(["req-1"]))
        supa.row["task_id"] = None
        self._submit(supa, self.Fal(["req-2"]))

        rows = supa.spend_of("render")
        assert sorted(row["external_ref"] for row in rows) == ["", "req-2"]
        assert sum(row["amount_usd"] for row in rows) == 1.6

    def test_a_deduplicating_provider_keeps_one_charge_however_often_it_resubmits(self):
        # `HeyGenUnreachable` is retried ten times by the graph and every retry
        # replays the same render. Ten charges for one render would slam a cap
        # shut over nothing.
        supa = SpendSupa(
            preset=PRESENTER,
            spend_key={"provider": "heygen", "model": ""},
            rate={"provider": "heygen", "model": "", "unit": "second", "rate_usd": 0.05},
        )

        for _ in range(3):
            supa.row["task_id"] = None
            render.submit_render({"production_id": "p1"}, supa, FakeMpt(), FakeHeyGen())

        assert len(supa.spend_of("render")) == 1


class TestEveryModelAReelWillBillIsCheckedAtTheGate:
    """The end-to-end lane bills three models, and a cap on any of them binds.

    Checked at the gate rather than when each call comes round: the narration
    happens inside `fetch_and_qc`, long after the visuals have been paid for,
    so refusing there would park a production holding a generation already
    billed.
    """

    def test_the_narration_and_caption_models_are_collected(self):
        models = render._secondary_models(spend.FAL, E2E_PRESET)
        assert "fal-ai/elevenlabs/tts/turbo-v2.5" in models
        assert "fal-ai/whisper" in models

    def test_no_other_lane_bills_a_second_model(self):
        assert render._secondary_models(spend.HEYGEN, PRESENTER) == ()
        assert render._secondary_models(spend.FAL, FAL_PRESET) == ()

    def test_a_cap_on_the_narration_model_stops_the_render(self):
        capped = "Daily spend cap reached for fal / fal-ai/elevenlabs/tts/turbo-v2.5: $2.00 of $2.00 today."

        class Supa(SpendSupa):
            def spend_block_reason(self, provider: str, model: str = "") -> str | None:
                self.calls.append(("spend_block_reason", (provider, model)))
                return capped if model == "fal-ai/elevenlabs/tts/turbo-v2.5" else None

        supa = Supa(
            preset=E2E_PRESET,
            spend_key={"provider": "fal", "model": "fal-ai/ltx-2.3/text-to-video"},
            rate={
                "provider": "fal",
                "model": "fal-ai/ltx-2.3/text-to-video",
                "unit": "second",
                "rate_usd": 0.08,
            },
        )

        render.submit_render({"production_id": "p1"}, supa, FakeMpt())

        assert supa.claimed_slots == []
        assert supa.recorded_spend == []
        assert capped in supa.parked[0][1]


class TestTheProvidersOwnFigureWins:
    def _completed(self, duration: float = 30.0) -> HeyGenVideo:
        return HeyGenVideo(
            id="v1", status="completed", video_url="https://x/v.mp4", duration=duration
        )

    def _event(self, balance_before: float | None) -> dict[str, Any]:
        return {
            "production_id": "p1",
            "backend": "heygen",
            "phase": "heygen",
            "heygen_video_id": "v1",
            "heygen_captions": False,
            "started_at": 0,
            "spend": {
                "provider": "heygen",
                "model": "",
                "unit": "second",
                "rate_usd": 0.05,
                "estimate_usd": 1.0,
                "balance_before": balance_before,
            },
        }

    def test_the_wallet_delta_replaces_the_estimate_and_is_marked_reported(self):
        supa = SpendSupa(preset=PRESENTER)
        supa.record_spend(
            "p1", provider="heygen", model="", kind="render", amount_usd=1.0, source="derived"
        )

        class Done(FakeHeyGen):
            def video(self, video_id: str) -> HeyGenVideo:
                return TestTheProvidersOwnFigureWins()._completed()

        render._poll_heygen(self._event(10.0), supa, 2, Done(balances=[8.60]))

        row = supa.spend_of("render")[0]
        assert row["amount_usd"] == 1.4
        assert row["source"] == "reported"

    def test_an_implausible_delta_is_ignored_in_favour_of_our_own_figure(self):
        # The wallet is shared with every other job in the account, so a delta
        # taken across a render that ran alongside two others would bill all
        # three here.
        supa = SpendSupa(preset=PRESENTER)
        supa.record_spend(
            "p1", provider="heygen", model="", kind="render", amount_usd=1.0, source="derived"
        )

        class Done(FakeHeyGen):
            def video(self, video_id: str) -> HeyGenVideo:
                return TestTheProvidersOwnFigureWins()._completed()

        render._poll_heygen(self._event(100.0), supa, 2, Done(balances=[1.00]))

        row = supa.spend_of("render")[0]
        # 30 seconds at $0.05, from the duration HeyGen reported rather than
        # from the guess at how long the script would take to read.
        assert row["amount_usd"] == 1.5
        assert row["source"] == "derived"

    def test_with_no_wallet_reading_the_finished_duration_still_improves_the_figure(self):
        supa = SpendSupa(preset=PRESENTER)
        supa.record_spend(
            "p1", provider="heygen", model="", kind="render", amount_usd=1.0, source="derived"
        )

        class Done(FakeHeyGen):
            def video(self, video_id: str) -> HeyGenVideo:
                return TestTheProvidersOwnFigureWins()._completed(duration=44.0)

        render._poll_heygen(self._event(None), supa, 2, Done())

        row = supa.spend_of("render")[0]
        assert row["amount_usd"] == 2.2
        assert row["source"] == "derived"

    def test_a_receipt_that_cannot_be_written_does_not_park_a_finished_render(self):
        # The submit-time row is already counted against the cap, so the worst
        # a failure here costs is a coarser number on a production that is
        # otherwise complete.
        supa = SpendSupa(preset=PRESENTER)

        def explode(*args: Any, **kw: Any) -> None:
            raise RuntimeError("PostgREST is having a moment")

        supa.record_spend = explode  # type: ignore[method-assign]

        class Done(FakeHeyGen):
            def video(self, video_id: str) -> HeyGenVideo:
                return TestTheProvidersOwnFigureWins()._completed()

        result = render._poll_heygen(self._event(10.0), supa, 2, Done(balances=[8.60]))

        assert result["state"] == "complete"
        assert supa.parked == []


class TestTheEndToEndLaneBillsTwice:
    def test_narration_is_recorded_per_character_under_its_own_model(self):
        # Its own charge rather than part of the render's: it is priced per
        # character of script, not per second of video, and a per-model cap on
        # the TTS model should be able to bind on its own.
        supa = SpendSupa(
            rate={
                "provider": "fal",
                "model": "fal-ai/elevenlabs/tts/turbo-v2.5",
                "unit": "character",
                "rate_usd": 0.00005,
            }
        )

        render._record_fal_extra(
            supa, "p1", "fal-ai/elevenlabs/tts/turbo-v2.5", spend.KIND_TTS, characters=2000
        )

        row = supa.spend_of("tts")[0]
        assert (row["provider"], row["amount_usd"]) == ("fal", 0.1)

    def test_an_unpriced_extra_is_logged_rather_than_parking_a_paid_call(self):
        # This runs after the visuals have been paid for and recorded, so the
        # cap is not blind either way, and a production must not park over the
        # accounting of a call that already succeeded.
        supa = SpendSupa(rate=None)

        render._record_fal_extra(supa, "p1", "fal-ai/unknown-tts", spend.KIND_TTS, characters=10)

        assert supa.recorded_spend == []
        assert supa.parked == []
