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
| 5 | TikTok-Api | MIT | ~~TikTok trend source~~ | **Retired.** "1 author — expect it to break" was right: refused on every feed, from every kind of address. Dormant in the tree, not selectable. |
| 6 | Apify actors | Commercial | TikTok + Instagram trend source, 1 | Rented scrapers. Billed per result, which is the trade for not running the arms race ourselves. |
| 7 | YouTube Data API v3 | Commercial (free tier) | YouTube trend source, 1 | Documented and supported. Capped at ~100 searches/day, which is what bounds a run rather than time. |

## What MoneyPrinterTurbo actually gives us
- **Script**: `llm.py` — Azure / Gemini / Qwen / Ollama and others.
- **Voice**: `voice.py` — Azure, Edge, **ElevenLabs**, SiliconFlow.
- **Visuals**: 7 sources — `pexels`, `pixabay`, `coverr`, `local`, plus two
  **generative** lanes (`wavespeed` text-to-video and `volcengine_seedance`).
- **Captions**: `subtitle.py` with full styling — font, size, fore/stroke
  colour, stroke width, position, rounded background.
- **Music**: `bgm.py`, `elevenlabs_music.py`, `sonilo.py` (AI music from a prompt).
- **API**: `POST /videos`, `POST /audio`, `POST /subtitle`, `GET /tasks`,
  `GET /download`, `GET /stream`. Headless and deterministic, which is what lets
  anything drive it -- a state machine then, a worker loop now.
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
4. **It runs anywhere.** Python and FFmpeg in a container is far simpler to host
   than headless Remotion -- which mattered when this ran on Fargate, and
   mattered more when it stopped: the whole stack now fits on one VPS with
   nothing cloud-specific left in it.

## What we must build to not break requirements
| Lost with OpenMontage | Replacement | Est. |
|---|---|---|
| QC ("quality-checked" is an explicit requirement) | Our own MIT service: ffprobe validation, audio-level check, caption presence, and a slideshow-risk score. Port the *design* from OpenMontage, write our own code. | ~1 week |
| Cost estimate per style at Gate 1 | Cost table per provider x preset. Simpler than OpenMontage's estimate/reserve/reconcile because `video_source` is the dominant cost driver: pexels/pixabay/coverr are free, wavespeed/seedance bill per request. | ~3 days |
| 13 declarative pipeline styles | **Named presets over `VideoParams`** (~35 knobs: video_source, concat/transition mode, clip duration/speed, voice, bgm, full subtitle styling, `custom_system_prompt` for brand voice). Cleaner than the YAML because it is our code. | ~3 days |
| Avatar / talking-head lane | **MPT has none** — no avatar, heygen, lipsync or musetalk anywhere in `app/`. Open question: do we need this style at all? If yes, call HeyGen directly. | ~3 days if wanted — **built**, see below |

## Requirements check after the swap
- R1 trend research — the source is now a setting rather than an import, with
  three to choose from: Google Trends, Apify (TikTok + Instagram) and the
  YouTube Data API. GAPS.md called this the weakest area and recommended
  re-scoping it honestly onto YouTube + Google Trends; both are now real, and
  Apify covers the short-form video signal that TikTok-Api was supposed to.
  Instagram has a trend source for the first time.
- R2 human approval — unchanged, custom web app, both gates. Now a Vite +
  TanStack Router SPA on Supabase (`web/`), replacing the Next.js template it
  was scaffolded from; see `web/README.md` for why the no-server shape changes
  where access control lives.
- R3 scripted / voiced / visual / captioned / **quality-checked** — first four
  native to MPT; QC is the one genuine rebuild.
- R4 publish to IG / TikTok / YT Shorts / LinkedIn — unchanged, Postiz.
  (MPT ships `upload_post.py` for a third-party cross-poster; we ignore it —
  it lacks LinkedIn and Postiz is better.)
- R5 hosting — since revised. This said "AWS — unchanged, and now a better
  fit", and the fit was real: Python and FFmpeg suit Fargate. The requirement
  turned out to be the wrong one to hold, though. What the stack actually needed
  was a process that can sit there and wait, and paying a cloud orchestrator for
  that bought a state machine, callback tokens, a webhook bridge and a NAT
  gateway to hold two human gates open. It is one worker loop over Postgres on a
  VPS now, and no requirement moved.
- R6 10/day — easier than before. Platform quotas remain the binding constraint.

**No requirement breaks. QC moves from third-party to ours.**

## Decision: HeyGen presenter lane (confirmed — now built)
MPT has no avatar capability, so the presenter style is added as its own lane
beside MPT's, fed the same script and caption styling so output stays on-brand
across lanes. ~3 days. It becomes the premium option at Gate 1.

**Built** as `render_mode = 'heygen'`. It turned out simpler than the estimate:
HeyGen returns a finished 9:16 reel, already voiced and captioned, so there is
no assembly step and no handoff — the lane fetches, quality-checks and stores,
and MoneyPrinterTurbo is reached for only to write the script.

Five things were verified against the live v3 API rather than taken from
documentation, and each of them would have been a silent defect:

- **v3, not v2.** `POST /v2/video/generate` — the call most examples still show
  — is deprecated and removed on 2026-10-31. The lane is built on
  `POST /v3/videos` and `GET /v3/videos/{id}`.
- **`X-Api-Key`, raw.** Not `Authorization`, and no `Bearer` prefix. The same
  class of trap Postiz sets, in a different header.
- **Burned-in captions land on a different URL.** Asking for them does not
  change `video_url`, which stays the clean cut; the captioned render arrives
  as `captioned_video_url`. Reading the obvious field would have published
  uncaptioned reels *and* had our own quality check report captions missing on
  videos that had them.
- **`POST /v3/videos` accepts an `Idempotency-Key`**, replayed for 24 hours.
  Sending the production id makes this lane retry-safe the way our MPT fork is,
  and unlike fal — which has no such key and where an ambiguous failure has to
  park rather than resubmit.
- **The engine must match the look.** Omitting `engine` selects Avatar IV, but
  studio avatars in the public catalogue advertise `avatar_iii` only, so a
  preset naming one has to say so or the render fails on an engine it never
  claimed.

One consequence for QC: the slideshow-risk score counts cuts, and a talking
head is one continuous shot by design. Scored the normal way every presenter
reel lands at 0.40 and warns, so the check is now lane-aware — cuts are not
counted on a single-shot lane, while frozen-frame detection still is, because
an avatar render that stalled looks exactly like a still image.

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
| Presenter | HeyGen (external lane) | ~$1-2 | Premium. Best for LinkedIn. Cap monthly spend. Billed against a pay-as-you-go wallet, so the balance is a hard ceiling rather than a soft one. |
