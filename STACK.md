# Final stack — decided 2026-08-30

Four repos kept. Two roles are "deploy/fork and own", two are "harvest".

| # | Repo | License | Role | How we use it |
|---|---|---|---|---|
| 1 | postiz-app | AGPL-3.0 | Publish + analytics (stages 11-12) | Deploy as-is to AWS. **Never fork.** Call REST v1 over the network. |
| 2 | OpenMontage | AGPL-3.0 | Production core (stages 4-9) | **Fork and import.** Pipeline styles, cost governance, QC, remotion-composer. |
| 3 | ViralMint | AGPL-3.0 | Trend research (stage 1) | Harvest `trend_velocity_service`, `youtube_scout`, `google_trends_svc`, `news_scout`. |
| 4 | TikTok-Api | MIT | TikTok trend source (stage 1) | Dependency. **Thin: 12 commits/365d, 1 author** — wrap behind an interface. |

Removed: `vanta` (2,723 LOC of fetch wrappers + affiliate funnel), `short-video-maker`
(abandoned 14 months, Remotion hardcoded), `revideo` (24 commits in 2 years, 1 author),
`MoneyPrinterTurbo` (no role once we chose style-variety over engine-variety;
its activity was never successfully measured — repo stayed shallow through two fetches).

## Licensing position
- **Remotion: free tier.** Non-profit, so the 4+ headcount rule does not apply. $0.
- **AGPL: accepted, internal-only.** Never distributed, never offered to third
  parties, so the source-provision trigger never fires.
- **Containment rule (enforce architecturally):** AGPL code lives only in the
  render service and the trend service. It must never be linked into the staff
  approval web app, and that app must never be exposed publicly. If this
  system ever becomes customer-facing or is sold, the obligation activates and
  this decision must be revisited.

## Pipeline map
```
1  Trend research      ViralMint scouts + TikTok-Api + Google Trends
2  Idea generation     custom (Claude on Bedrock)
3  GATE 1 approval     custom Next.js app - shows style options + $ estimate each
4  Script              OpenMontage script-director
5  Voice               OpenMontage tts_selector
6  Visuals             OpenMontage video_selector / image_selector
7  Compose             OpenMontage remotion-composer (Remotion, free tier)
8  Captions            OpenMontage subtitle burn-in
9  QC                  composition_validator + slideshow_risk + final_review
10 GATE 2 approval     custom Next.js app
11 Publish             Postiz POST /upload -> POST /posts
12 Analytics           Postiz GET /analytics/:integration
13 Orchestration       AWS Step Functions, both gates via task tokens
```

## How "owner picks a style with cost shown" is wired
`pipeline_defs/*.yaml` each declare `budget_default_usd` and
`max_wall_time_minutes`. `tools/cost_tracker.py` produces a preflight estimate
before any paid call. We expose a `/estimate` endpoint on the render service
that runs the preflight across the candidate styles for a given idea and
returns cost + ETA per style. Gate 1 in the Next.js app renders that as the
choice the owner makes. Budget `cap` mode then enforces the ceiling at runtime.

## Decisions (all confirmed 2026-08-30)

| Question | Decision |
|---|---|
| Variety | **Style, not engine.** One engine (Remotion), 13 pipeline styles at different price points. |
| Publishing | Self-host Postiz, call REST v1. |
| AGPL | Fork + import, internal-only. Containment rule above is binding. |
| Remotion license | Free tier — non-profit. $0. |
| Approval gates | Custom Next.js app, both gates. |
| AWS depth | **Infrastructure only.** Step Functions / Fargate / S3 / RDS / Secrets Manager on AWS; generation stays with Kling / Runway / HeyGen / ElevenLabs, which OpenMontage already supports. No Bedrock provider to write. |
| Orchestration | **Hybrid.** Agentic for research, script and scene planning; deterministic Python for asset generation, compose, QC and publish. |

### What hybrid orchestration means concretely
- **Agentic zone** (Claude Agent SDK inside the render service, invoked per job):
  stages 1-2 research and idea generation, stage 4 scripting, stage 6 scene
  planning. Variability is a feature here.
- **Deterministic zone** (plain Python over OpenMontage's importable modules —
  `tools/tool_registry.py`, `tools/cost_tracker.py`, the `*_selector.py` tools,
  `composition_validator.py`, `lib/slideshow_risk.py`): asset generation,
  compose, captions, QC, publish. Variability is a liability here, and none of
  this layer needs an agent.
- Step Functions owns the job state machine and both human gates via task tokens.

## Build order
1. **Postiz on AWS**, publish one hand-made video to all four platforms via its
   API. Proves the hardest external dependency first.
   **Start TikTok app approval on day one — it takes 2-4 weeks and is critical path.**
2. **Fork OpenMontage**, render one reel through one pipeline style with real
   provider keys, deterministic path only.
3. **`/estimate` endpoint** — preflight cost across candidate styles. This is
   what makes Gate 1 meaningful.
4. **Next.js approval app**, both gates, wired to Step Functions task tokens.
5. **Step Functions** state machine end to end.
6. **Trend research service** — harvest ViralMint's scouts, wrap TikTok-Api
   behind an interface (it is a 1-author repo; assume it breaks).
7. **Analytics loop** — Postiz `/analytics/:integration` back into idea scoring.

## Known risks
- **OpenMontage bus factor is 1.** 310 of 448 commits in the last year are the
  maintainer's. Very active now, but pin a commit and vendor the fork.
- **TikTok-Api is 1 author, 12 commits/year**, scraping an API that fights
  scrapers. Wrap it; expect to replace it.
- **Brand templates built in `remotion-composer` are AGPL-derived.** Fine while
  internal-only, per the containment rule.
