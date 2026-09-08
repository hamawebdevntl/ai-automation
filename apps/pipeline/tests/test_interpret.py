"""Reading a search description into terms the scout can use.

The description is a sentence; the scout wants a list. What is worth pinning
is the seam: that the model is told which vocabulary it is writing for and how
many terms it may return, that what it returns is cleaned the way the app
cleans a typed term, and that an answer nobody can parse is an error rather
than a quiet run on the saved list.

Both providers are exercised through injected fakes. No SDK is called and no
key is needed; `model="m"` everywhere keeps the settings unread.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import llm
from pipeline.trends import interpret, sources
from pipeline.trends.controls import BOUNDS, ScoutControls

BRIEF = "We are a software and AI automation agency for small businesses."
DESCRIPTION = "I want to start a small home fitness brand for busy parents"


def reading(**over) -> interpret.Interpretation:
    fields = {
        "restatement": "A home fitness brand for parents short on time.",
        "terms": ["home workout for parents", "quick workout at home"],
        "vague": False,
        "nudge": None,
        "suggestions": ["Home fitness for parents of toddlers, in the US"],
    }
    fields.update(over)
    return interpret.Interpretation(**fields)


class FakeGemini:
    def __init__(self, *, parsed=None, text=None) -> None:
        self.calls: list[dict] = []
        self._parsed = parsed
        self._text = text
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed=self._parsed, text=self._text, candidates=[])


class FakeClaude:
    def __init__(self, parsed_output=None, stop_reason="end_turn") -> None:
        self.calls: list[dict] = []
        self._parsed_output = parsed_output
        self._stop_reason = stop_reason
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed_output=self._parsed_output, stop_reason=self._stop_reason)


def read(
    source_name: str = "google_trends",
    *,
    cap: int = 6,
    client=None,
    provider: str = "gemini",
    description: str = DESCRIPTION,
):
    client = client or FakeGemini(parsed=reading())
    result = interpret.interpret(
        description,
        BRIEF,
        sources.spec(source_name),
        cap=cap,
        provider=provider,
        model="m",
        client=client,
    )
    return result, client


class TestWhatTheModelIsTold:
    def test_the_prompt_carries_the_description_the_brief_and_the_cap(self):
        _, client = read(cap=4)
        call = client.calls[0]
        assert DESCRIPTION in call["contents"]
        assert BRIEF in call["contents"]
        assert "Return at most 4 search terms." in call["contents"]
        assert call["config"]["response_schema"] is interpret.Interpretation
        assert call["config"]["system_instruction"] == interpret.SYSTEM

    @pytest.mark.parametrize(
        "source_name, word",
        [("google_trends", "search terms"), ("youtube", "search terms"), ("apify", "hashtags")],
    )
    def test_the_prompt_names_the_source_and_its_vocabulary(self, source_name, word):
        # The two lists are not interchangeable, and the model has to be told
        # which one it is writing for or it will answer in the other.
        _, client = read(source_name)
        contents = client.calls[0]["contents"]
        assert source_name in contents
        assert f"Return at most 6 {word}." in contents

    def test_both_providers_get_the_same_prompt(self):
        _, gemini = read(client=FakeGemini(parsed=reading()), provider="gemini")
        _, claude = read(client=FakeClaude(parsed_output=reading()), provider="claude")
        assert gemini.calls[0]["contents"] == claude.calls[0]["messages"][0]["content"]
        assert gemini.calls[0]["config"]["system_instruction"] == claude.calls[0]["system"]

    def test_the_call_is_small_and_does_not_think(self):
        # A sentence and a handful of terms. Idea drafting thinks; this does not
        # need to, and the ceiling says so.
        _, claude = read(client=FakeClaude(parsed_output=reading()), provider="claude")
        assert claude.calls[0]["max_tokens"] == interpret.MAX_TOKENS
        assert "thinking" not in claude.calls[0]


class TestWhatComesBack:
    def test_hashtags_are_cleaned_the_way_the_app_cleans_them(self):
        client = FakeGemini(parsed=reading(terms=["#Busy Parents", "busyparents", " #HomeWorkout ", ""]))
        result, _ = read("apify", client=client)
        assert result.terms == ["busyparents", "homeworkout"]

    def test_search_terms_keep_their_words_and_drop_repeats(self):
        client = FakeGemini(
            parsed=reading(terms=["  home   workout ", "Home Workout", "quick workout at home"])
        )
        result, _ = read(client=client)
        assert result.terms == ["home workout", "quick workout at home"]

    def test_the_cap_is_enforced_when_the_model_over_returns(self):
        client = FakeGemini(parsed=reading(terms=[f"term {i}" for i in range(10)]))
        result, _ = read(cap=3, client=client)
        assert len(result.terms) == 3

    def test_suggestions_are_capped_and_blanks_dropped(self):
        client = FakeGemini(parsed=reading(suggestions=["a", " ", "b", "c", "d"]))
        result, _ = read(client=client)
        assert result.suggestions == ["a", "b", "c"]

    def test_no_terms_is_passed_through_for_the_runner_to_judge(self):
        client = FakeGemini(parsed=reading(terms=[], vague=True, nudge="Say who it is for."))
        result, _ = read(client=client)
        assert result.terms == []
        assert result.vague is True
        assert result.nudge == "Say who it is for."

    def test_a_blank_nudge_is_none(self):
        result, _ = read(client=FakeGemini(parsed=reading(nudge="   ")))
        assert result.nudge is None

    def test_json_in_text_is_read_when_the_sdk_did_not_parse_it(self):
        result, _ = read(client=FakeGemini(text=reading().model_dump_json()))
        assert result.terms == reading().terms

    def test_an_unparseable_answer_is_an_error_not_a_quiet_fallback(self):
        # The alternative is scouting the saved list, which answers a question
        # nobody asked and reports success.
        with pytest.raises(llm.LlmError):
            read(client=FakeGemini(parsed=None, text="Sure! Here are some ideas:"))


class TestTheCap:
    def test_defaults_when_the_run_carries_no_length(self):
        assert interpret.term_cap(ScoutControls()) == interpret.AI_TERMS_DEFAULT

    def test_follows_the_runs_own_length(self):
        assert interpret.term_cap(ScoutControls(hashtags_per_run=3)) == 3

    def test_never_exceeds_the_settings_ceiling(self):
        ceiling = int(BOUNDS["hashtags_per_run"][1])
        assert interpret.term_cap(ScoutControls(hashtags_per_run=ceiling + 50)) == ceiling


class TestTheDocument:
    def test_names_the_source_the_vocabulary_and_the_model(self):
        doc = interpret.payload(
            reading(vague=True, nudge="Say where."),
            source="apify",
            vocabulary=sources.HASHTAGS,
            provider="gemini",
            model="gemini-test",
        )
        assert doc == {
            "restatement": "A home fitness brand for parents short on time.",
            "terms": ["home workout for parents", "quick workout at home"],
            "vocabulary": "hashtags",
            "source": "apify",
            "vague": True,
            "nudge": "Say where.",
            "suggestions": ["Home fitness for parents of toddlers, in the US"],
            "provider": "gemini",
            "model": "gemini-test",
        }
