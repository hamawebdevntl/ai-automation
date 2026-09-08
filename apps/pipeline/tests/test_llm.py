"""Text without MoneyPrinterTurbo.

The presenter lane renders at HeyGen and never needed MPT for pictures, but
its script and its platform copy were still fetched through MPT's LLM proxy --
so a HeyGen-only deployment had to run the whole MPT stack for two paragraphs
of text. These cover the direct route: that it wears MPT's method signatures,
that it fails as a *step* rather than as plumbing, and that the choice between
it and MPT follows the credential that is actually present.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline import llm
from pipeline.activities import script
from pipeline.clients.mpt import SocialMetadata


class FakeGemini:
    """Just enough of `google.genai.Client` to answer one structured call."""

    def __init__(self, *, parsed=None, text: str | None = None) -> None:
        self.calls: list[dict] = []
        self._parsed = parsed
        self._text = text
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(parsed=self._parsed, text=self._text, candidates=[])


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    class Cfg:
        idea_provider = "gemini"
        gemini_api_key = "g-key"
        gemini_model = "gemini-test"
        niche_brief = "We are a software agency. We talk to owners of small trades businesses."
        mpt_base_url = "http://mpt:8080"
        mpt_api_key = "k"

    monkeypatch.setattr(llm, "settings", lambda: Cfg())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # The real one reads `pipeline.config.settings()` in its constructor, which
    # wants a Supabase URL and key. Only the *choice* is under test here.
    monkeypatch.setattr(llm, "MptClient", FakeMptClient)
    return Cfg


class FakeMptClient:
    """Stands in for `MptClient` so choosing it does not need MPT settings."""


class TestGenerateScript:
    def test_returns_the_narration_stripped(self):
        fake = FakeGemini(parsed=llm.ScriptDraft(script="  Stop losing quotes. Here is the fix.  "))
        client = llm.DirectLlm("gemini", client=fake)

        assert client.generate_script("Quoting software") == "Stop losing quotes. Here is the fix."

    def test_the_prompt_carries_subject_length_and_brief(self):
        # The brief is what makes a script sound like this organisation rather
        # than like a generic explainer; it is in every deployment already.
        fake = FakeGemini(parsed=llm.ScriptDraft(script="x" * 100))
        llm.DirectLlm("gemini", client=fake).generate_script("Quoting software", paragraphs=2)

        call = fake.calls[0]
        assert "Quoting software" in call["contents"]
        assert "2 paragraphs" in call["contents"]
        assert "small trades businesses" in call["contents"]
        assert call["config"]["response_schema"] is llm.ScriptDraft
        assert call["config"]["system_instruction"] == llm.SCRIPT_SYSTEM

    def test_reads_raw_json_when_the_sdk_did_not_parse(self):
        fake = FakeGemini(text=json.dumps({"script": "From the text field."}))
        assert llm.DirectLlm("gemini", client=fake).generate_script("s") == "From the text field."

    def test_an_empty_script_is_a_step_failure(self):
        fake = FakeGemini(parsed=llm.ScriptDraft(script="   "))
        with pytest.raises(llm.LlmError):
            llm.DirectLlm("gemini", client=fake).generate_script("s")

    def test_no_content_is_a_step_failure_not_a_transport_error(self):
        # An httpx error would be read by the engine as *our* plumbing failing
        # and retried every five seconds forever without consuming an attempt.
        fake = FakeGemini(parsed=None, text=None)
        with pytest.raises(llm.LlmError):
            llm.DirectLlm("gemini", client=fake).generate_script("s")


class TestSocialMetadata:
    def test_returns_mpts_shape(self):
        fake = FakeGemini(
            parsed=llm.CopyDraft(
                title=" Why quotes die ",
                caption=" Three fixes. ",
                hashtags=["#trades", "quoting", " "],
            )
        )
        meta = llm.DirectLlm("gemini", client=fake).social_metadata(
            "youtube", video_subject="Quoting", video_script="Narration."
        )

        assert isinstance(meta, SocialMetadata)
        assert meta.title == "Why quotes die"
        assert meta.caption == "Three fixes."
        # `#` stripped and blanks dropped, because `copy._hashtags` adds the sign.
        assert meta.hashtags == ["trades", "quoting"]

    def test_the_prompt_names_the_platform_and_carries_the_narration(self):
        fake = FakeGemini(parsed=llm.CopyDraft(caption="c"))
        llm.DirectLlm("gemini", client=fake).social_metadata(
            "linkedin", video_subject="S", video_script="The words."
        )

        contents = fake.calls[0]["contents"]
        assert "LinkedIn" in contents
        assert "The words." in contents


class TestChoosingAClient:
    def test_gemini_when_its_key_is_present(self):
        client = llm.text_client()
        assert isinstance(client, llm.DirectLlm)
        assert client.provider == "gemini"

    def test_falls_back_to_mpt_when_no_key(self, cfg):
        # What every lane did before this module existed. A deployment that
        # renders with MoneyPrinterTurbo and has no LLM key keeps working.
        cfg.gemini_api_key = ""
        assert isinstance(llm.text_client(), FakeMptClient)

    def test_claude_needs_its_environment_variable(self, cfg, monkeypatch):
        cfg.idea_provider = "claude"
        assert isinstance(llm.text_client(), FakeMptClient)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
        client = llm.text_client()
        assert isinstance(client, llm.DirectLlm)
        assert client.provider == "claude"

    def test_an_unknown_provider_is_refused_loudly(self):
        with pytest.raises(llm.LlmError):
            llm.DirectLlm("openai")


class TestTheScriptStepUsesIt:
    def test_write_script_drafts_through_text_client_by_default(self, monkeypatch):
        # The whole point: the presenter lane's script no longer needs MPT.
        fake = FakeGemini(parsed=llm.ScriptDraft(script="Direct draft, no MoneyPrinterTurbo."))
        monkeypatch.setattr(script, "text_client", lambda: llm.DirectLlm("gemini", client=fake))

        class Supa:
            def __init__(self):
                self.updates: list[dict] = []

            def production(self, _id):
                return {"id": "p1", "idea_id": "i1", "style_preset_id": "s1", "script": None}

            def idea(self, _id):
                return {"title": "Quoting software", "hook": "Why quotes die"}

            def style_preset(self, _id):
                return {"slug": "ai-presenter", "params": {"heygen": {"paragraphs": 1}}}

            def update_production(self, _id, **fields):
                self.updates.append(fields)
                return {"id": "p1", **fields}

            def record_event(self, *a, **k):
                pass

        supa = Supa()
        result = script.write_script({"production_id": "p1"}, supa)

        assert fake.calls, "the direct client was never asked"
        assert any(u.get("script") == "Direct draft, no MoneyPrinterTurbo." for u in supa.updates)
        assert result.get("script_source") != "kept"


class TestStructured:
    """One structured call as a function.

    For callers -- idea drafting, reading a search description -- that want a
    schema filled and nothing of MoneyPrinterTurbo's interface.
    """

    def test_returns_the_schema_instance_at_the_default_ceiling(self):
        fake = FakeGemini(parsed=llm.ScriptDraft(script="Words."))
        draft = llm.structured("gemini", "sys", "prompt", llm.ScriptDraft, client=fake, model="m")
        assert isinstance(draft, llm.ScriptDraft)
        assert fake.calls[0]["config"]["max_output_tokens"] == llm.MAX_TOKENS

    def test_passes_the_token_ceiling_through(self):
        fake = FakeGemini(parsed=llm.ScriptDraft(script="Words."))
        llm.structured(
            "gemini", "sys", "prompt", llm.ScriptDraft, client=fake, model="m", max_tokens=123
        )
        assert fake.calls[0]["config"]["max_output_tokens"] == 123

    def test_an_injected_client_and_model_never_read_settings(self, monkeypatch):
        # The trend tests inject both and set no Supabase URL. A settings read
        # here would construct one and fail on the missing variable.
        def refuse():
            raise AssertionError("settings were read")

        monkeypatch.setattr(llm, "settings", refuse)
        fake = FakeGemini(parsed=llm.ScriptDraft(script="Words."))
        llm.structured("gemini", "sys", "prompt", llm.ScriptDraft, client=fake, model="m")

    def test_thinking_is_asked_for_only_when_wanted(self):
        class FakeClaude:
            def __init__(self):
                self.calls: list[dict] = []
                self.messages = SimpleNamespace(parse=self._parse)

            def _parse(self, **kw):
                self.calls.append(kw)
                return SimpleNamespace(
                    parsed_output=llm.ScriptDraft(script="Words."), stop_reason="end_turn"
                )

        fake = FakeClaude()
        llm.structured("claude", "sys", "prompt", llm.ScriptDraft, client=fake)
        assert "thinking" not in fake.calls[0]
        assert fake.calls[0]["model"] == llm.CLAUDE_MODEL

        llm.structured(
            "claude", "sys", "prompt", llm.ScriptDraft, client=fake, thinking=True, max_tokens=999
        )
        assert fake.calls[1]["thinking"] == {"type": "adaptive"}
        assert fake.calls[1]["max_tokens"] == 999
