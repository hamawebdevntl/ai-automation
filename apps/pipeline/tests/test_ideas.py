"""Tests for provider selection and the two drafting paths.

The point of having two providers is that they are interchangeable, so what is
worth pinning is the ways they could quietly stop being interchangeable: the
same prompt has to reach both, and a response neither can parse has to come
back as *nothing* rather than as an empty batch, because an empty batch is
indistinguishable from a quiet trend week.

Both providers are exercised through injected fakes. Neither SDK is called and
no key is needed, which is also what keeps this file runnable without network.
"""

from __future__ import annotations

import pytest

from pipeline.trends.ideas import (
    CLAUDE,
    CLAUDE_MODEL,
    GEMINI,
    SEARCH_ADDENDUM,
    SYSTEM,
    IdeaBatch,
    ReelIdea,
    RelevantIdeaBatch,
    RelevantReelIdea,
    generate_ideas,
    resolve_provider,
    split_relevant,
    to_rows,
)
from pipeline.trends.velocity import Signal

BRIEF = "We rehome retired greyhounds. We never give veterinary advice."


def signal(keyword: str = "adoption") -> Signal:
    return Signal(
        source="tiktok",
        source_url="https://tiktok.com/@x/video/1",
        title="A retired racer meets a sofa for the first time",
        keyword=keyword,
        plays=90_000,
        ratio=4.2,
        engagement=0.11,
        age_days=1.5,
    )


def batch(n: int = 2) -> IdeaBatch:
    return IdeaBatch(
        ideas=[
            ReelIdea(
                title=f"Idea {i}",
                hook=f"Hook {i}",
                angle="adoption day, first hour at home",
                rationale="the adoption keyword is outperforming its own baseline",
            )
            for i in range(n)
        ]
    )


class FakeClaude:
    """Shaped like `anthropic.Anthropic` as far as this module touches it."""

    def __init__(self, parsed_output=None, stop_reason="end_turn"):
        self.calls: list[dict] = []
        self.messages = self._Messages(self)
        self._parsed_output = parsed_output
        self._stop_reason = stop_reason

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def parse(self, **kw):
            self.outer.calls.append(kw)
            return type(
                "Response",
                (),
                {
                    "parsed_output": self.outer._parsed_output,
                    "stop_reason": self.outer._stop_reason,
                },
            )()


class FakeGemini:
    """Shaped like `genai.Client` as far as this module touches it."""

    def __init__(self, parsed=None, text=None, finish_reason="STOP"):
        self.calls: list[dict] = []
        self.models = self._Models(self)
        self._parsed = parsed
        self._text = text
        self._finish_reason = finish_reason

    class _Models:
        def __init__(self, outer):
            self.outer = outer

        def generate_content(self, **kw):
            self.outer.calls.append(kw)
            candidate = type("Candidate", (), {"finish_reason": self.outer._finish_reason})()
            return type(
                "Response",
                (),
                {
                    "parsed": self.outer._parsed,
                    "text": self.outer._text,
                    "candidates": [candidate],
                },
            )()


class TestResolveProvider:
    def test_defaults_to_claude(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert resolve_provider(None) == CLAUDE
        assert resolve_provider("") == CLAUDE

    def test_is_case_and_whitespace_insensitive(self):
        assert resolve_provider("  Gemini ", gemini_api_key="k") == GEMINI

    def test_an_unknown_provider_names_the_ones_that_exist(self):
        with pytest.raises(ValueError, match="claude, gemini"):
            resolve_provider("gpt")

    def test_gemini_without_a_key_fails_before_anything_is_scouted(self):
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            resolve_provider(GEMINI, gemini_api_key="   ")

    def test_claude_without_a_key_fails_the_same_way(self, monkeypatch):
        # The Anthropic SDK would raise on construction anyway -- but only
        # after the scout has already run, which is the cost this avoids.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            resolve_provider(CLAUDE)

    def test_gemini_does_not_require_an_anthropic_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert resolve_provider(GEMINI, gemini_api_key="k") == GEMINI


class TestBothProvidersGetTheSamePrompt:
    """The brief and the signals must reach either model identically.

    If they drift, the two lanes stop being comparable and the setting becomes
    a quality change disguised as a configuration change.
    """

    def _prompt_sent_to(self, provider, client):
        generate_ideas(
            [signal()], BRIEF, count=5, provider=provider, model="m", client=client
        )
        call = client.calls[0]
        if provider == CLAUDE:
            return call["system"], call["messages"][0]["content"]
        return call["config"]["system_instruction"], call["contents"]

    def test_the_system_prompt_and_user_prompt_match(self):
        claude_system, claude_user = self._prompt_sent_to(CLAUDE, FakeClaude(batch()))
        gemini_system, gemini_user = self._prompt_sent_to(GEMINI, FakeGemini(batch()))
        assert claude_system == gemini_system
        assert claude_user == gemini_user

    def test_the_prompt_carries_the_brief_and_the_signal(self):
        _, prompt = self._prompt_sent_to(GEMINI, FakeGemini(batch()))
        assert BRIEF in prompt
        assert "adoption" in prompt
        assert "4.2x" in prompt

    def test_gemini_is_asked_for_the_schema_not_for_prose(self):
        client = FakeGemini(batch())
        generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client)
        config = client.calls[0]["config"]
        assert config["response_mime_type"] == "application/json"
        assert config["response_schema"] is IdeaBatch

    def test_claude_defaults_to_the_pinned_model(self):
        client = FakeClaude(batch())
        generate_ideas([signal()], BRIEF, provider=CLAUDE, client=client)
        assert client.calls[0]["model"] == CLAUDE_MODEL


class TestShapeOfTheResult:
    @pytest.mark.parametrize(
        "provider,client",
        [(CLAUDE, FakeClaude(batch(4))), (GEMINI, FakeGemini(batch(4)))],
    )
    def test_the_count_is_a_ceiling_not_a_target(self, provider, client):
        ideas = generate_ideas(
            [signal()], BRIEF, count=2, provider=provider, model="m", client=client
        )
        assert len(ideas) == 2

    def test_no_signals_means_no_call_at_all(self):
        client = FakeClaude(batch())
        assert generate_ideas([], BRIEF, provider=CLAUDE, client=client) == []
        assert client.calls == []

    def test_an_unknown_provider_raises_rather_than_drafting_with_the_other(self):
        # The failure mode this guards is a typo drafting with Claude while
        # someone believes they are demoing on the free Gemini key.
        client = FakeClaude(batch())
        with pytest.raises(ValueError, match="unknown idea provider"):
            generate_ideas([signal()], BRIEF, provider="gemni", client=client)
        assert client.calls == []

    def test_a_missing_brief_is_refused_for_either_provider(self):
        for provider in (CLAUDE, GEMINI):
            with pytest.raises(ValueError, match="niche_brief is required"):
                generate_ideas([signal()], "   ", provider=provider)


class TestGeminiResponsesThatAreNotABatch:
    """`parsed` is None whenever generation stopped early.

    A safety stop is realistic here: the prompt carries scraped TikTok titles
    that nobody vetted. Every one of these must yield an empty list, never a
    half-populated one, and must not raise.
    """

    def test_json_in_text_is_used_when_the_sdk_did_not_parse_it(self):
        client = FakeGemini(parsed=None, text=batch(3).model_dump_json())
        ideas = generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client)
        assert len(ideas) == 3

    def test_a_dict_rather_than_a_model_is_validated(self):
        client = FakeGemini(parsed=batch(2).model_dump())
        ideas = generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client)
        assert len(ideas) == 2

    def test_a_safety_stop_returns_nothing_and_says_why(self, caplog):
        client = FakeGemini(parsed=None, text=None, finish_reason="SAFETY")
        with caplog.at_level("ERROR"):
            ideas = generate_ideas(
                [signal()], BRIEF, provider=GEMINI, model="m", client=client
            )
        assert ideas == []
        assert "SAFETY" in caplog.text

    def test_prose_instead_of_json_returns_nothing(self):
        client = FakeGemini(parsed=None, text="Sure! Here are some ideas:")
        assert generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client) == []

    def test_json_missing_a_required_field_returns_nothing(self):
        client = FakeGemini(parsed=None, text='{"ideas": [{"title": "only a title"}]}')
        assert generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client) == []


class TestClaudeResponsesThatAreNotABatch:
    def test_an_unparseable_batch_returns_nothing(self, caplog):
        client = FakeClaude(parsed_output=None, stop_reason="max_tokens")
        with caplog.at_level("ERROR"):
            ideas = generate_ideas([signal()], BRIEF, provider=CLAUDE, client=client)
        assert ideas == []
        assert "max_tokens" in caplog.text


class TestDraftingAgainstADescription:
    """A described run asks for a score and a connection.

    The ordinary run must not change by a character -- its schema and prompt
    are what every idea in the queue so far was drafted with -- so the extra
    ask is a second schema and an addendum, switched on by the description.
    """

    DESCRIPTION = "I want to start a small home fitness brand for busy parents"

    def relevant(self, *scores: int) -> RelevantIdeaBatch:
        return RelevantIdeaBatch(
            ideas=[
                RelevantReelIdea(
                    title=f"Idea {i}",
                    hook=f"Hook {i}",
                    angle="a",
                    rationale="r",
                    relevance=score,
                    connection=f"For the parents you mentioned ({i}).",
                )
                for i, score in enumerate(scores)
            ]
        )

    def test_the_schema_and_the_prompt_change_with_a_description(self):
        client = FakeGemini(self.relevant(80))
        generate_ideas(
            [signal()], BRIEF, provider=GEMINI, model="m", client=client,
            description=self.DESCRIPTION,
        )
        call = client.calls[0]
        assert call["config"]["response_schema"] is RelevantIdeaBatch
        assert call["config"]["system_instruction"].endswith(SEARCH_ADDENDUM)
        assert "# What the person is working on\n" + self.DESCRIPTION in call["contents"]

    def test_without_a_description_nothing_changes(self):
        client = FakeGemini(batch())
        generate_ideas([signal()], BRIEF, provider=GEMINI, model="m", client=client)
        call = client.calls[0]
        assert call["config"]["response_schema"] is IdeaBatch
        assert call["config"]["system_instruction"] == SYSTEM
        assert "working on" not in call["contents"]

    def test_ideas_come_back_best_fit_first_within_the_count(self):
        client = FakeGemini(self.relevant(40, 95, 70))
        ideas = generate_ideas(
            [signal()], BRIEF, count=2, provider=GEMINI, model="m", client=client,
            description=self.DESCRIPTION,
        )
        assert [i.relevance for i in ideas] == [95, 70]

    def test_split_relevant_drops_below_the_floor_and_keeps_the_unscored(self):
        scored = self.relevant(90, 39, 40).ideas
        plain = batch(1).ideas
        kept, dropped = split_relevant(scored + plain)
        assert [getattr(i, "relevance", None) for i in kept] == [90, 40, None]
        assert [i.relevance for i in dropped] == [39]

    def test_rows_carry_the_run_the_score_and_the_connection(self):
        rows = to_rows(self.relevant(85).ideas, [signal()], ["instagram"], run_id="run-1")
        assert rows[0]["trend_run_id"] == "run-1"
        assert rows[0]["relevance"] == 85
        assert rows[0]["connection"] == "For the parents you mentioned (0)."

    def test_plain_ideas_under_a_run_carry_null_scores(self):
        rows = to_rows(batch(1).ideas, [signal()], ["instagram"], run_id="run-1")
        assert rows[0]["trend_run_id"] == "run-1"
        assert rows[0]["relevance"] is None
        assert rows[0]["connection"] is None

    def test_without_a_run_the_row_is_shaped_as_it_always_was(self):
        rows = to_rows(batch(1).ideas, [signal()], ["instagram"])
        assert "trend_run_id" not in rows[0]
        assert "relevance" not in rows[0]
        assert "connection" not in rows[0]
