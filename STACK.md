# Final stack — revised 2026-08-30 (production core swapped)

## Change: MoneyPrinterTurbo replaces OpenMontage as the production core

Made on the user's judgement that OpenMontage's **output** is weak next to
MoneyPrinterTurbo's. My earlier promotion of OpenMontage was based on repo
signals (commit velocity, architecture, declarative pipelines) — those measure
the project, not the videos. Re-measured MPT properly and it wins on the repo
signals too; my earlier "no valid data" reading came from a shallow clone.

| | MoneyPrinterTurbo | OpenMontage |
|---|---|---|
| License | **MIT** | AGPL-3.0 |
| Last commit | 2026-08-30 | 2026-08-22 |
| Commits 90d | 224 | 345 |
| Contributors 365d | **79** | 53 |
| Top-author share | 59% | 69% |
| Orchestration | **deterministic REST API** | requires an AI coding assistant |
| Render engine | FFmpeg (no license) | Remotion |

## Final repo set

| # | Repo | License | Role | How |
|---|---|---|---|---|
| 1 | MoneyPrinterTurbo | MIT | Production core, stages 4-8 | **Fork and own.** Drive its REST API. |
| 2 | postiz-app | AGPL-3.0 | Publish + analytics, 11-12 | Deploy as-is, never fork, call over REST. |
| 3 | OpenMontage | AGPL-3.0 | **Read-only QC reference** | Never imported. Port the *ideas* into our MIT code. |
| 4 | ViralMint | AGPL-3.0 | Trend research reference | Reimplement velocity scoring (337 LOC) rather than import. |
| 5 | TikTok-Api | MIT | TikTok trend source | Dependency, wrapped. 1 author — expect it to break. |

## What MoneyPrinterTurbo actually gives us
- **Script**: `llm.py` — Azure / Gemini / Qwen / Ollama and others.
- **Voice**: `voice.py` — Azure, Edge, **ElevenLabs**, SiliconFlow.
- **Visuals**: 7 sources — `pexels`, `pixabay`, `coverr`, `local`, plus two
  **generative** lanes (`wavespeed` text-to-video and `volcengine_seedance`).
- **Captions**: `subtitle.py` with full styling — font, size, fore/stroke
  colour, stroke width, position, rounded background.
- **Music**: `bgm.py`, `elevenlabs_music.py`, `sonilo.py` (AI music from a prompt).
- **API**: `POST /videos`, `POST /audio`, `POST /subtitle`, `GET /tasks`,
  `GET /download`, `GET /stream`. Headless, deterministic, Step-Functions-friendly.
- **Portrait 9:16 is the default** (`schema.py:84`).

## What this change buys
1. **The AGPL problem on the production core disappears.** Only Postiz stays
   AGPL, and it was always behind a network boundary. The containment rule
   gets much easier to hold.
2. **Remotion is gone entirely** — no license question at all, not even the
   non-profit exemption to track.
3. **The orchestration decision mostly dissolves.** MPT is deterministic Python
   behind REST. Agentic work retreats to stages 1-2 (trend research, idea
   generation) where variability is actually wanted. "Hybrid" now means
   agentic research + wholly deterministic production.
4. **Better AWS fit.** Python + FFmpeg on Fargate is far simpler to run than
   headless Remotion in a container.

## What we must build to not break requirements
| Lost with OpenMontage | Replacement | Est. |
|---|---|---|
| QC ("quality-checked" is an explicit requirement) | Our own MIT service: ffprobe validation, audio-level check, caption presence, and a slideshow-risk score. Port the *design* from OpenMontage, write our own code. | ~1 week |
| Cost estimate per style at Gate 1 | Cost table per provider x preset. Simpler than OpenMontage's estimate/reserve/reconcile because `video_source` is the dominant cost driver: pexels/pixabay/coverr are free, wavespeed/seedance bill per request. | ~3 days |
| 13 declarative pipeline styles | **Named presets over `VideoParams`** (~35 knobs: video_source, concat/transition mode, clip duration/speed, voice, bgm, full subtitle styling, `custom_system_prompt` for brand voice). Cleaner than the YAML because it is our code. | ~3 days |
| Avatar / talking-head lane | **MPT has none** — no avatar, heygen, lipsync or musetalk anywhere in `app/`. Open question: do we need this style at all? If yes, call HeyGen directly. | ~3 days if wanted |

## Requirements check after the swap
- R1 trend research — unchanged (still the weakest area, see GAPS.md).
- R2 human approval — unchanged, custom Next.js app, both gates.
- R3 scripted / voiced / visual / captioned / **quality-checked** — first four
  native to MPT; QC is the one genuine rebuild.
- R4 publish to IG / TikTok / YT Shorts / LinkedIn — unchanged, Postiz.
  (MPT ships `upload_post.py` for a third-party cross-poster; we ignore it —
  it lacks LinkedIn and Postiz is better.)
- R5 AWS — unchanged, and now a better fit.
- R6 10/day — easier than before. Platform quotas remain the binding constraint.

**No requirement breaks. QC moves from third-party to ours.**

## Decision: HeyGen presenter lane (confirmed)
MPT has no avatar capability, so the presenter style is added as its own lane
beside MPT's, fed the same script and caption styling so output stays on-brand
across lanes. ~3 days. It becomes the premium option at Gate 1.

**Budget flag:** HeyGen runs roughly $1-2/reel against ~$0.10-0.40 for the
stock lanes. If the owner picks it for all 10 reels/day that is **$300-600/month**
versus ~$30-120 for stock. This is exactly why the Gate 1 cost display matters
— it is the control, so build it before the lane goes live, and set a monthly
ceiling on the presenter lane specifically.

## Final style registry (presets over VideoParams + one external lane)
| Style | Source | Rough cost/reel | Notes |
|---|---|---|---|
| Stock b-roll | pexels / pixabay / coverr | ~$0.10-0.40 | Default. TTS + kinetic captions. |
| Generative | wavespeed / volcengine_seedance | billed per request | Novel visuals; verify per-model pricing before enabling. |
| Presenter | HeyGen (external lane) | ~$1-2 | Premium. Best for LinkedIn. Cap monthly spend. |
