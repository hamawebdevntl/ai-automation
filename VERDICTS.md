# Repo verification — measured, not from READMEs
Verified 2026-08-30 by cloning all seven candidates and reading the code + real git history.

## Activity (real history, not shallow clones)

| Repo | Last commit | 90d | 365d | Authors | Concentration |
|---|---|---|---|---|---|
| postiz-app | 2026-08-29 | 260 | 1032 | 22 | top 3 ≈ 95% (funded co.) |
| OpenMontage | 2026-08-22 | 345 | 448 | 53 | 310/448 one maintainer |
| ViralMint | 2026-08-26 | 142 | 154 | 3 | single maintainer + 2 |
| revideo | 2026-07-15 | ~19 | 19 | 1 | all 19 in one week, July |
| short-video-maker | 2025-06-21 | 0 | 0 | 0 | abandoned 14 months |

Revideo commits by year: 2023=183, 2024=303, **2025=5, 2026=19**. The org moved
havenhq -> midrender; the July 2026 burst was one person shipping 0.11.0 and
fixing docs. npm `@revideo/core` last published 2026-07-10 vs `remotion`
4.0.518 on 2026-08-26.

## Where the research was wrong

- **Vanta — discard.** Claims "40+ open source repos, replaces Adobe CC /
  Synthesia / Runway / HeyGen / ElevenLabs." Actual size: **2,723 LOC across 23
  files.** `ai-avatar.ts` is 58 lines; `ai-video.ts` is 70. They are thin
  `fetch()` wrappers that POST to servers you must install and run yourself. No
  models, no pipeline, no avatar code. The README carries a Skool paid-community
  affiliate link (`?ref=6752...`) three times. The "maintainer audited every
  integration for commercial-license safety" line the research praised is that
  same README's marketing copy. There is no parts bin here.
- **short-video-maker — discard.** Abandoned since 2025-06-21, 4,018 LOC.
  Remotion is hardcoded (`libraries/Remotion.ts`); there is no engine
  abstraction, so it cannot host a two-engine strategy. Forking a dead 4k-LOC
  repo buys days, not weeks.
- **OpenMontage — promote, don't just read.** The research said "read only."
  It is the most active video repo in the set and it already implements the
  exact thing we need: `pipeline_defs/` holds **13 named styles**
  (cinematic, talking-head, avatar-spokesperson, animated-explainer,
  screen-demo, documentary-montage, clip-factory, podcast-repurpose, hybrid,
  localization-dub, animation, character-animation), each with a
  `budget_default_usd` and `max_wall_time_minutes`.
  `tools/cost_tracker.py` (523 LOC) does preflight estimate -> reserve ->
  reconcile with warn/cap budget modes and an `ApprovalRequiredError`.
  `tools/video/video_selector.py` auto-discovers providers from a registry and
  scores them. QC is real: `composition_validator.py` (275),
  `source_media_review.py` (395), `lib/slideshow_risk.py` wired into
  `video_compose.py` to block a render that would look like a slideshow.
- **ViralMint — better than described.** 68,737 LOC, 113 test files, real
  service layer. Research said "no Instagram": `backend/services/
  instagram_uploader.py` is 274 LOC. `trend_velocity_service.py` (337 LOC)
  implements baseline-relative velocity — splits the Google Trends window
  75/25, scores current vs baseline, classifies at 3.0x / 1.5x / 0.8x.

## Licenses (verified from LICENSE files)
AGPL-3.0: postiz-app, OpenMontage, ViralMint.
MIT: short-video-maker, vanta, revideo, MoneyPrinterTurbo.
