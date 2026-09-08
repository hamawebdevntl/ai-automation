"""The presenter catalogue, and the Gate 1 override that reads from it.

Everything here is about one property: an avatar or voice this account cannot
use fails *terminally* -- `avatar_not_found` and `voice_not_found` are in
TERMINAL_CODES -- and terminally means after Gate 1, having already spent a
review to find out. So the catalogue is what lets the pick be checked before
anything is committed, and the tests below are about the ways that checking
could quietly stop working:

  * a renamed field in HeyGen's response emptying the picker, or silently
    dropping the orientation warning and the engine constraint;
  * a refresh that dies mid-flight leaving the row claimed for ever;
  * an override that leaks into the keys it has no business changing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from pipeline.activities import presenter
from pipeline.activities.render import _presenter_config, _presenter_record
from pipeline.clients.heygen import HeyGenError, HeyGenRejected


def _iso(**delta: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# ---------------------------------------------------------------------------
# Reading a look
# ---------------------------------------------------------------------------


class TestOrientation:
    """The warning this feeds is the difference between a reel and a reel with
    the speaker's head cropped off: every look in HeyGen's public catalogue is
    landscape, and a 9:16 render from one crops to fill the frame."""

    def test_heygens_own_word_wins(self):
        assert presenter.orientation_of({"preferred_orientation": "portrait"}) == "portrait"

    def test_dimensions_are_the_fallback(self):
        # The look the preset ships with is 1536x2752 and obviously portrait,
        # so a response that stops carrying `preferred_orientation` must not
        # take the warning down with it.
        assert presenter.orientation_of({"width": 1536, "height": 2752}) == "portrait"
        assert presenter.orientation_of({"width": 1920, "height": 1080}) == "landscape"
        assert presenter.orientation_of({"width": 1080, "height": 1080}) == "square"

    def test_nested_dimensions_are_still_found(self):
        assert presenter.orientation_of({"size": {"width": 1920, "height": 1080}}) == "landscape"

    def test_unknown_rather_than_a_guess(self):
        # 'unknown' shows in the picker as "orientation not reported", which is
        # honest. Defaulting to portrait would suppress the one warning that
        # matters on exactly the looks we know least about.
        assert presenter.orientation_of({"name": "Anna"}) == "unknown"

    def test_a_value_we_do_not_recognise_is_not_passed_through(self):
        assert presenter.orientation_of({"preferred_orientation": "diagonal"}) == "unknown"


class TestEngines:
    """Omitting `engine` selects Avatar IV. A look advertising `avatar_iii` only
    fails on an engine it never claimed, which is why the list is stored."""

    def test_engines_are_found_wherever_they_are_named(self):
        # Deliberately not read from a fixed key: `tags` carries them
        # upper-cased on the v2 detail response, and the v3 looks response is
        # not a shape we control.
        assert presenter.engines_of({"supported_engines": ["avatar_iii", "avatar_iv"]}) == [
            "avatar_iii",
            "avatar_iv",
        ]
        assert presenter.engines_of({"tags": ["AVATAR_IV"]}) == ["avatar_iv"]
        assert presenter.engines_of({"meta": {"engines": ["avatar_v"]}}) == ["avatar_v"]

    def test_ids_and_names_are_not_mistaken_for_engines(self):
        look = {
            "id": "e6e4d0f4b4704568b32e8de751179c83",
            "name": "avatar person",
            "preview_image_url": "https://files.heygen.ai/avatar_iv/x.jpg",
        }
        assert presenter.engines_of(look) == []

    def test_saying_nothing_is_recorded_as_nothing(self):
        # Empty means "no opinion", and `presenter_choice` only constrains the
        # engine when the list is non-empty. A field HeyGen renames therefore
        # costs the picker a check rather than its ability to save anything.
        assert presenter.engines_of({"name": "Anna"}) == []


class TestLookRow:
    def test_a_look_becomes_a_row(self):
        row = presenter.look_row(
            {
                "id": "look-1",
                "name": "Anna",
                "preview_image_url": "https://files.heygen.ai/anna.jpg",
                "preferred_orientation": "portrait",
                "supported_engines": ["avatar_iv"],
                "default_voice_id": "v1",
            }
        )
        assert row["avatar_id"] == "look-1"
        assert row["orientation"] == "portrait"
        assert row["engines"] == ["avatar_iv"]
        assert row["default_voice_id"] == "v1"

    def test_an_item_with_no_id_is_dropped_rather_than_stored_blank(self):
        assert presenter.look_row({"name": "Anna"}) is None


# ---------------------------------------------------------------------------
# Deciding whether to refresh
# ---------------------------------------------------------------------------


class TestWhenARefreshIsDue:
    def test_a_request_from_the_app_is_honoured_at_once(self):
        assert presenter._due({"status": "requested", "refreshed_at": _iso(seconds=5)}, 0)

    def test_a_voice_waiting_for_its_first_answer_is_enough(self):
        # The owner is looking at "checking…" next to an id they just typed.
        assert presenter._due({"status": "idle", "refreshed_at": _iso(seconds=5)}, 1)

    def test_a_fresh_cache_with_nothing_asking_is_left_alone(self):
        assert not presenter._due({"status": "idle", "refreshed_at": _iso(seconds=60)}, 0)

    def test_a_stale_cache_refills_itself(self):
        assert presenter._due({"status": "idle", "refreshed_at": _iso(hours=7)}, 0)

    def test_a_refresh_in_flight_is_left_to_the_worker_that_has_it(self):
        assert not presenter._due({"status": "running", "started_at": _iso(seconds=30)}, 1)

    def test_a_worker_that_died_holding_it_does_not_hold_it_for_ever(self):
        # Without this the row says `running` until someone opens psql, and the
        # settings page shows a refresh that never ends and a button that never
        # works again.
        assert presenter._due({"status": "running", "started_at": _iso(hours=1)}, 0)

    def test_a_missing_row_means_the_migration_has_not_run_here(self):
        assert not presenter._due(None, 5)


# ---------------------------------------------------------------------------
# The refresh itself
# ---------------------------------------------------------------------------


class FakeHeyGen:
    def __init__(self, looks: list[dict[str, Any]], voices: dict[str, Any]) -> None:
        self._looks = looks
        self._voices = voices
        self.resolved: list[str] = []

    def looks(self, **_kw: Any) -> list[dict[str, Any]]:
        return self._looks

    def voice(self, voice_id: str) -> dict[str, Any]:
        self.resolved.append(voice_id)
        answer = self._voices[voice_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeSupa:
    def __init__(self, state: dict[str, Any] | None, voices: list[str], claimed: bool = True) -> None:
        self._state = state
        self._voices = voices
        self._claimed = claimed
        self.looks: list[dict[str, Any]] | None = None
        self.upserted: list[dict[str, Any]] = []
        self.finished: dict[str, Any] | None = None

    def heygen_catalogue(self):
        return self._state

    def pending_heygen_voices(self) -> int:
        return 0

    def claim_heygen_catalogue(self, stale_after_seconds: int = 900) -> bool:
        return self._claimed

    def replace_heygen_looks(self, rows):
        self.looks = rows

    def known_heygen_voices(self):
        return self._voices

    def upsert_heygen_voice(self, row):
        self.upserted.append(row)

    def finish_heygen_catalogue(self, **fields):
        self.finished = fields


@pytest.fixture
def stale_state():
    return {"status": "idle", "refreshed_at": _iso(hours=7)}


class TestRefresh:
    def test_looks_and_voices_are_written(self, stale_state):
        supa = FakeSupa(stale_state, ["v1"])
        heygen = FakeHeyGen(
            [{"id": "look-1", "name": "Anna", "preferred_orientation": "portrait"}],
            {"v1": {"name": "Anna's voice", "preview_audio_url": "https://x/a.mp3"}},
        )
        result = presenter.refresh_catalogue(supa, heygen)

        assert [row["avatar_id"] for row in supa.looks] == ["look-1"]
        assert supa.upserted[0]["status"] == "ok"
        assert supa.upserted[0]["name"] == "Anna's voice"
        assert supa.finished == {"status": "idle", "looks": 1, "voices": 1}
        assert result == {"looks": 1, "voices": 1, "unknown": 0}

    def test_nothing_happens_when_nothing_is_asking(self):
        supa = FakeSupa({"status": "idle", "refreshed_at": _iso(seconds=30)}, ["v1"])
        assert presenter.refresh_catalogue(supa, FakeHeyGen([], {})) == {}
        assert supa.looks is None

    def test_losing_the_claim_stops_before_spending_a_request(self, stale_state):
        # Two containers both holding a service-role key is the deployment we
        # ship, so this is a real race, and the loser must not walk the whole
        # voice list against a rate-limited API.
        supa = FakeSupa(stale_state, ["v1"], claimed=False)
        heygen = FakeHeyGen([], {})
        assert presenter.refresh_catalogue(supa, heygen) == {}
        assert heygen.resolved == []

    def test_a_failed_listing_says_why_on_the_row(self, stale_state):
        # The person who pressed Refresh is looking at a page, not at the
        # worker's log. 'unauthorized' is a sentence they can act on.
        supa = FakeSupa(stale_state, [])

        class Boom(FakeHeyGen):
            def looks(self, **_kw):
                raise HeyGenError("HeyGen GET /v3/avatars/looks -> 401 unauthorized")

        presenter.refresh_catalogue(supa, Boom([], {}))
        assert supa.finished["status"] == "failed"
        assert "unauthorized" in supa.finished["error"]
        assert supa.looks is None

    def test_one_unusable_voice_does_not_cost_the_others(self, stale_state):
        supa = FakeSupa(stale_state, ["gone", "v2"])
        heygen = FakeHeyGen(
            [{"id": "look-1"}],
            {
                "gone": HeyGenRejected("HeyGen GET /v3/voices/gone -> 404 voice_not_found"),
                "v2": {"name": "Second"},
            },
        )
        result = presenter.refresh_catalogue(supa, heygen)

        by_id = {row["voice_id"]: row for row in supa.upserted}
        assert by_id["gone"]["status"] == "unknown"
        assert by_id["v2"]["status"] == "ok"
        assert result["voices"] == 1 and result["unknown"] == 1

    def test_a_transient_failure_leaves_a_working_voice_alone(self, stale_state):
        # Marking it unknown would make the picker refuse to save a voice that
        # is perfectly usable, because HeyGen was briefly unreachable.
        supa = FakeSupa(stale_state, ["v1"])
        heygen = FakeHeyGen([{"id": "look-1"}], {"v1": HeyGenError("429 rate limited")})
        result = presenter.refresh_catalogue(supa, heygen)

        assert supa.upserted == []
        assert result["voices"] == 0 and result["unknown"] == 0


# ---------------------------------------------------------------------------
# The override at render time
# ---------------------------------------------------------------------------

PRESET = {
    "slug": "ai-presenter",
    "params": {
        "heygen": {
            "avatar_id": "preset-avatar",
            "voice_id": "preset-voice",
            "aspect_ratio": "9:16",
            "resolution": "1080p",
            "burn_captions": True,
        }
    },
}


class TestPresenterConfig:
    def test_no_override_is_the_preset_untouched(self):
        assert _presenter_config({}, PRESET) == PRESET["params"]["heygen"]

    def test_an_override_replaces_only_the_presenter(self):
        cfg = _presenter_config(
            {"presenter_override": {"avatar_id": "other", "voice_id": "other-voice"}}, PRESET
        )
        assert cfg["avatar_id"] == "other"
        assert cfg["voice_id"] == "other-voice"
        # Everything else is how the reel is cut, not who speaks.
        assert cfg["aspect_ratio"] == "9:16"
        assert cfg["resolution"] == "1080p"
        assert cfg["burn_captions"] is True

    def test_the_preset_is_not_mutated_by_a_render(self):
        # `preset` is a row read from the database and handed to the submit;
        # writing through it would leak one production's override into the
        # next thing that reads the same dict.
        _presenter_config({"presenter_override": {"avatar_id": "other"}}, PRESET)
        assert PRESET["params"]["heygen"]["avatar_id"] == "preset-avatar"

    def test_a_partial_override_keeps_the_presets_narrator(self):
        # Not HeyGen's default voice for that avatar: sending no voice_id makes
        # the narrator a property of HeyGen's catalogue, which is exactly what
        # the preset names one to avoid.
        cfg = _presenter_config({"presenter_override": {"avatar_id": "other"}}, PRESET)
        assert cfg["voice_id"] == "preset-voice"

    def test_an_engine_only_arrives_when_the_override_names_one(self):
        assert "engine" not in _presenter_config({}, PRESET)
        cfg = _presenter_config({"presenter_override": {"engine": "avatar_iii"}}, PRESET)
        assert cfg["engine"] == "avatar_iii"


class TestPresenterRecord:
    """A finished reel must be able to say who presented it. The preset is
    editable and the override is deleted with its idea, so nothing about this
    is reconstructable afterwards -- which is why `render_backend` is recorded
    the same way."""

    def test_a_preset_render_records_the_preset_pair(self):
        record = _presenter_record({}, _presenter_config({}, PRESET))
        assert record == {
            "avatar_id": "preset-avatar",
            "voice_id": "preset-voice",
            "source": "preset",
        }

    def test_an_override_render_says_so_and_carries_the_names(self):
        idea = {
            "presenter_override": {
                "avatar_id": "other",
                "avatar_name": "Marcus",
                "voice_id": "other-voice",
                "voice_name": "Marcus (calm)",
                "engine": "avatar_iii",
            }
        }
        record = _presenter_record(idea, _presenter_config(idea, PRESET))
        assert record == {
            "avatar_id": "other",
            "voice_id": "other-voice",
            "engine": "avatar_iii",
            "source": "override",
            "avatar_name": "Marcus",
            "voice_name": "Marcus (calm)",
        }

    def test_a_names_only_override_is_not_treated_as_one(self):
        # `presenter_choice` always returns ids alongside the names, so this
        # shape should not occur -- but 'source' deciding on a display string
        # would be a lie about which row rendered the reel.
        idea = {"presenter_override": {"avatar_name": "Marcus"}}
        assert _presenter_record(idea, _presenter_config(idea, PRESET))["source"] == "preset"
