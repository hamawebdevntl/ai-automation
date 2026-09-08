"""Text generation without MoneyPrinterTurbo.

The presenter lane never needed MoneyPrinterTurbo for pictures -- HeyGen returns
a finished, voiced, captioned reel -- but two steps on it still routed their
*text* through MPT's LLM proxy: `write_script` (`POST /scripts`) and
`generate_platform_copy` (`POST /social-metadata`). So a deployment that only
ever rendered with HeyGen had to run MoneyPrinterTurbo, a Redis and an ffmpeg
stack in order to obtain two paragraphs of narration. Meanwhile idea generation
already spoke to Gemini or Claude directly, with the key that is in every
deployment because the trend scout needs it.

This gives the script and copy steps that same direct route. `DirectLlm` has
exactly the two method signatures the activities already call on `MptClient`,
so the activities, the graph and every existing test that injects a fake are
untouched; only the default they fall back to changes. `text_client()` picks
the direct route whenever the configured provider has its credential, and MPT
otherwise -- so a deployment that renders with MoneyPrinterTurbo and has no
LLM key of its own carries on exactly as before.

A failure here is the step's, not the plumbing's: it is raised as `LlmError`
rather than allowed to surface as an `httpx` transport error, which the engine
would read as our own infrastructure hiccuping and retry every five seconds
without ever consuming an attempt.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field, ValidationError

from pipeline.clients.mpt import MptClient, SocialMetadata
from pipeline.config import settings

log = logging.getLogger(__name__)

CLAUDE = "claude"
GEMINI = "gemini"
CLAUDE_MODEL = "claude-opus-5"

# Generous for a 5,000-character script plus a little JSON around it, and far
# below anything that costs real money on either provider.
MAX_TOKENS = 4000


class LlmError(RuntimeError):
    """The model did not return usable text. Routed by the graph like any step failure."""


T = TypeVar("T", bound=BaseModel)


class TextGenerator(Protocol):
    """The two things the script and copy steps ask of whatever writes text.

    `MptClient` satisfies this already; `DirectLlm` was written to it.
    """

    def generate_script(self, subject: str, language: str = "", paragraphs: int = 1) -> str: ...

    def social_metadata(
        self, platform: str, video_subject: str, video_script: str, language: str = ""
    ) -> SocialMetadata: ...


# ---------------------------------------------------------------------------
# What the model is asked for
# ---------------------------------------------------------------------------


class ScriptDraft(BaseModel):
    script: str = Field(description="The narration as spoken, word for word. Nothing else.")


class CopyDraft(BaseModel):
    title: str | None = Field(default=None, description="A title, only where the platform has one.")
    caption: str = Field(description="The post text. No hashtags inside it.")
    hashtags: list[str] = Field(default_factory=list, description="Without the # sign.")


SCRIPT_SYSTEM = """You write the narration for short vertical videos (Reels, Shorts, TikTok) for an organisation's own channels.

Rules:
- Spoken word only. No scene directions, no camera notes, no headings, no speaker labels, no hashtags, no emojis.
- Open with the hook in the first sentence; a viewer decides in two seconds.
- Plain, confident, specific. Short sentences. Contractions are fine.
- Close with one clear next step for the viewer.
- Aim for 30-60 seconds when read aloud: roughly 80-150 words per paragraph asked for."""

COPY_SYSTEM = """You write the post copy that accompanies a short vertical video on one social platform, from the video's subject and its narration.

Rules:
- Write for the platform named. Match its conventions for length, tone and hashtags.
- The caption must stand alone: someone who has not pressed play should know what the video is about and why it matters to them.
- Never invent claims that are not in the narration.
- Put hashtags in the hashtags field, never inside the caption."""

PLATFORM_GUIDE: dict[str, str] = {
    "instagram": "Instagram Reels. Caption up to ~300 characters, conversational, a line break before a short call to action. 5-10 relevant hashtags.",
    "tiktok": "TikTok. Caption under 150 characters, punchy, first person is fine. 3-5 hashtags.",
    "youtube": "YouTube Shorts. A title under 90 characters that says the payoff, and a caption of one or two sentences as the description. 2-4 hashtags.",
    "linkedin": "LinkedIn. Professional and direct, 2-4 short sentences, no emoji. At most 3 hashtags, or none.",
}


def _script_prompt(subject: str, language: str, paragraphs: int, brief: str) -> str:
    parts = [f"Write the narration for a video about: {subject}"]
    parts.append(f"Length: {paragraphs} paragraph{'s' if paragraphs != 1 else ''}.")
    if language:
        parts.append(f"Language: {language}.")
    if brief:
        parts.append(f"Who is speaking, and to whom:\n{brief}")
    return "\n\n".join(parts)


def _copy_prompt(platform: str, subject: str, script: str, language: str) -> str:
    guide = PLATFORM_GUIDE.get(platform, platform)
    parts = [f"Platform: {guide}", f"Video subject: {subject}"]
    if language:
        parts.append(f"Language: {language}.")
    parts.append(f"Narration:\n{script.strip() or '(no narration available)'}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class DirectLlm:
    """Gemini or Claude, called directly, wearing `MptClient`'s two method signatures."""

    def __init__(
        self, provider: str, *, client: Any | None = None, model: str | None = None
    ) -> None:
        provider = (provider or "").strip().lower()
        if provider not in (GEMINI, CLAUDE):
            raise LlmError(f"unknown LLM provider {provider!r}; expected {GEMINI!r} or {CLAUDE!r}")
        self.provider = provider
        self._client = client
        self._model = model

    # -- the two methods the activities call --------------------------------

    def generate_script(self, subject: str, language: str = "", paragraphs: int = 1) -> str:
        brief = settings().niche_brief.strip()
        draft = self._structured(
            SCRIPT_SYSTEM,
            _script_prompt(subject, language, max(1, int(paragraphs)), brief),
            ScriptDraft,
        )
        script = draft.script.strip()
        if not script:
            raise LlmError(f"{self.provider} returned an empty script")
        return script

    def social_metadata(
        self, platform: str, video_subject: str, video_script: str, language: str = ""
    ) -> SocialMetadata:
        draft = self._structured(
            COPY_SYSTEM, _copy_prompt(platform, video_subject, video_script, language), CopyDraft
        )
        return SocialMetadata(
            title=draft.title.strip() if draft.title else None,
            caption=draft.caption.strip(),
            hashtags=[tag.strip().lstrip("#") for tag in draft.hashtags if tag and tag.strip()],
        )

    # -- one structured call, either provider --------------------------------

    def _structured(
        self,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        *,
        max_tokens: int = MAX_TOKENS,
        thinking: bool = False,
    ) -> Any:
        if self.provider == GEMINI:
            return self._gemini(system, prompt, schema, max_tokens=max_tokens)
        return self._claude(system, prompt, schema, max_tokens=max_tokens, thinking=thinking)

    def _gemini(
        self, system: str, prompt: str, schema: type[BaseModel], *, max_tokens: int = MAX_TOKENS
    ) -> Any:
        # The call shape that has been proven against the live API. The SDK
        # import is local so a test with an injected client needs neither the
        # package nor a key -- and the settings read is local for the same
        # reason: a test that injects both a client and a model should not need
        # a Supabase URL in the environment to construct a `Settings`.
        model = self._model
        client = self._client
        if model is None or client is None:
            cfg = settings()
            model = model or cfg.gemini_model
            if client is None:
                from google import genai

                client = self._client = genai.Client(api_key=cfg.gemini_api_key)

        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "system_instruction": system,
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json",
                "response_schema": schema,
                "automatic_function_calling": {"disable": True},
            },
        )
        return _coerce(response, schema, provider=GEMINI)

    def _claude(
        self,
        system: str,
        prompt: str,
        schema: type[BaseModel],
        *,
        max_tokens: int = MAX_TOKENS,
        thinking: bool = False,
    ) -> Any:
        client = self._client
        if client is None:
            import anthropic

            client = self._client = anthropic.Anthropic()

        request: dict[str, Any] = {
            "model": self._model or CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_format": schema,
        }
        if thinking:
            # What idea drafting has always asked for. The thinking is billed
            # against `max_tokens`, so a caller that wants it passes a generous
            # ceiling along with it.
            request["thinking"] = {"type": "adaptive"}
        response = client.messages.parse(**request)
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise LlmError(
                f"claude returned nothing parseable (stop_reason={getattr(response, 'stop_reason', '?')})"
            )
        return parsed


def _coerce(response: Any, schema: type[BaseModel], *, provider: str) -> Any:
    """Get a `schema` instance out of a response, or say exactly why there isn't one.

    `parsed` is the schema-validated object when the SDK could build one and
    None when generation stopped early; the raw text is the fallback. Mirrors
    `trends.ideas._coerce_gemini`, except that this raises -- a missing script
    is a step failure with a retry policy behind it, not a quiet empty batch.
    """
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    if isinstance(parsed, dict):
        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            raise LlmError(
                f"{provider} returned {schema.__name__} that does not fit: {exc}"
            ) from exc

    text = getattr(response, "text", None)
    if not text:
        raise LlmError(f"{provider} returned no content ({_finish_reason(response)})")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        raise LlmError(f"{provider} returned unparseable {schema.__name__}: {exc}") from exc


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        return f"finish_reason={getattr(candidates[0], 'finish_reason', 'unknown')}"
    return f"prompt_feedback={getattr(response, 'prompt_feedback', 'unknown')}"


def structured(
    provider: str,
    system: str,
    prompt: str,
    schema: type[T],
    *,
    client: Any | None = None,
    model: str | None = None,
    max_tokens: int = MAX_TOKENS,
    thinking: bool = False,
) -> T:
    """One structured call, either provider, as a function.

    `DirectLlm` wears MoneyPrinterTurbo's two method signatures for the steps
    that used to call MPT. Everything else that wants a model to fill a schema
    -- drafting ideas, reading a search description -- wants exactly this and
    nothing of that interface, so it is exposed as a function rather than by
    asking callers to reach for a private method.

    Raises `LlmError`; never returns None. A caller that would rather have an
    empty result than an exception catches it and says why.
    """
    return DirectLlm(provider, client=client, model=model)._structured(
        system, prompt, schema, max_tokens=max_tokens, thinking=thinking
    )


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def text_client() -> TextGenerator:
    """Whatever should write text for this deployment.

    The direct route whenever the configured idea provider has its credential
    -- the same key the trend scout already depends on, so a deployment that
    generates ideas can also write scripts with no further setup. Otherwise
    MoneyPrinterTurbo, which is what every lane used before this existed, so
    nothing that worked stops working. The fallback is logged rather than
    silent: a HeyGen deployment that meant to run without MPT should find out
    here, not from a connection refused at the script step.
    """
    cfg = settings()
    provider = (cfg.idea_provider or "").strip().lower()
    if provider == GEMINI and cfg.gemini_api_key.strip():
        return DirectLlm(GEMINI)
    if provider == CLAUDE and os.environ.get("ANTHROPIC_API_KEY"):
        return DirectLlm(CLAUDE)
    log.warning(
        "no direct LLM provider is usable (IDEA_LLM_PROVIDER=%r, key present=%s); "
        "scripts and copy fall back to MoneyPrinterTurbo at %s",
        cfg.idea_provider,
        bool(cfg.gemini_api_key.strip() or os.environ.get("ANTHROPIC_API_KEY")),
        cfg.mpt_base_url or "<MPT_BASE_URL unset>",
    )
    return MptClient()
