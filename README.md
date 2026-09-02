# Reels Automation

An end-to-end system that researches what is working in our niche, proposes
Reel ideas for a human to approve, produces the finished videos, and publishes
them to Instagram, TikTok, YouTube Shorts and LinkedIn — at roughly **10 Reels
per day**, with a person in control at two points and never more.

---

## Why this exists

Short-form vertical video is where attention is, but the economics of it are
hostile to a small organisation. Ten Reels a day is roughly **300 a month**.
Done by hand — research, scripting, voicing, sourcing footage, editing,
captioning, checking, and posting to four platforms with four different sets of
rules — that is a full-time team we do not have and, as a non-profit, will not
be hiring.

The purpose of this project is to move the human effort **from production to
judgement**. Nobody on our side should be cutting video or writing captions.
What a person should do is decide *what we say* and *whether it is good enough
to go out under our name*. Everything on either side of those two decisions is
machine work.

Concretely, the system is designed so that:

- **Research is continuous, not occasional.** Trends are scored against a
  channel's own baseline rather than absolute view counts, so we react to what
  is actually accelerating rather than what is merely large.
- **A human approves every idea before a cent is spent generating it.** Video
  generation costs real money; the approval gate sits *before* the spend, and
  shows the estimated cost of each production style so the choice is informed.
- **Nothing publishes without a second human sign-off.** Reputation risk for an
  organisation is asymmetric. The cost of a slow post is small; the cost of a
  bad one under our name is not.
- **Quality is checked mechanically before a human ever looks.** Reviewers
  should spend their attention on whether the message is right, not on catching
  a video that rendered as a slideshow or lost its audio.

---

## How it works

```
  1  Trend research        continuous scouting + baseline-relative scoring
  2  Idea generation       ideas drafted from what is trending in our niche
       |
  3  ==== GATE 1: HUMAN ====   owner approves the idea AND picks a production
       |                        style, with cost + ETA shown for each option
  4  Script                LLM writes to the approved idea and our brand voice
  5  Voice                 TTS narration
  6  Visuals               stock b-roll, generative clips, or an AI presenter
  7  Compose               assembly, transitions, music
  8  Captions              burned-in, styled to brand
  9  Quality check         automated: file integrity, audio levels, caption
       |                    presence, slideshow-risk score
 10  ==== GATE 2: HUMAN ====   owner approves the finished cut
       |
 11  Publish               Instagram / TikTok / YouTube Shorts / LinkedIn
 12  Analytics             per-platform performance pulled back in
       |
       +--> feeds back into (1) so idea scoring learns from our own results
```

Stages 3 and 10 are the only places a person is required. Everything else runs
unattended on a schedule.

---

## Architecture

**Orchestration** is AWS Step Functions. Both human gates are implemented as
task tokens, so a job simply waits — for minutes or for days — until someone
approves it, without holding any compute open.

**Production** runs as a forked service driven over its REST API. It is
deterministic Python and FFmpeg, which means a given input produces a
predictable output and failures are debuggable. It runs on AWS Fargate.

**Publishing** is a self-hosted Postiz deployment, called over its REST API and
never modified. It owns the four platform integrations, OAuth token refresh,
scheduling and analytics — the part of this system most likely to break when a
platform changes its rules, deliberately isolated behind a boundary.

**Approval** is a small internal web app (`web/`): a queue of pending ideas with
cost per style at Gate 1, and a preview with approve/reject at Gate 2. It is a
static Vite + TanStack Router SPA talking to Supabase directly, with no server
of its own — which means row-level security and a pair of Postgres functions,
not the browser, are what decide who may pass a gate.

**Generation** is deliberately not on AWS. Infrastructure is (Step Functions,
Fargate, S3, RDS, Secrets Manager), but the actual AI calls go to the best
available providers for voice and video. This was a considered trade: AWS's own
generative video and voice offerings are behind the specialists for this use
case, and output quality is the whole point.

### Style lanes

The owner picks a production style per idea at Gate 1. The styles differ mainly
in cost, which is why the cost estimate is shown at the moment of choosing:

| Style | Roughly | Best for |
|---|---|---|
| Stock b-roll | $0.10–0.40 / reel | The default. Volume. |
| Generative | billed per request | Visual novelty, concept-led ideas. |
| AI presenter | $1–2 / reel | Authority content, LinkedIn especially. |

---

## Repositories used

| Repo | License | Role |
|---|---|---|
| `MoneyPrinterTurbo` | MIT | Production core. Forked and owned. |
| `postiz-app` | AGPL-3.0 | Publishing + analytics. Deployed as-is, never forked. |
| `TikTok-Api` | MIT | TikTok trend data. Wrapped behind an interface. |
| `OpenMontage` | AGPL-3.0 | **Read-only reference** for quality-check design. Never imported. |
| `ViralMint` | AGPL-3.0 | **Read-only reference** for trend velocity scoring. Reimplemented, not imported. |

See [VERDICTS.md](docs/VERDICTS.md) for how these were chosen — every claim there was
verified by reading the code and the real commit history, not the READMEs. Two
widely-recommended projects were rejected on inspection.

### Licensing position

Our own code is MIT-compatible throughout. The one AGPL component we run,
Postiz, is a separate deployment we call over the network and never modify,
which is the standard arrangement that keeps it out of scope. AGPL projects we
learned from are read, not imported.

**One binding rule:** this system is internal. It is not distributed, not sold,
and not exposed to third parties. If that ever changes, the licensing position
must be re-examined before it does.

---

## Status

**Built and deployed to code:** the two-gate approval app, the full pipeline
(trend research, render, quality check, per-platform copy, publish, analytics),
seven scheduled reconcilers, and the AWS infrastructure for all of it.
Five migrations are applied to the live database.

**Needs your credentials to run:** the two Secrets Manager bundles, a
certificate for the Postiz endpoint, and the platform channels connected by
hand. See [docs/DEPLOY.md](docs/DEPLOY.md).

**Open and outside our control:** the niche brief, without which trend research
refuses to start; YouTube's quota extension; and TikTok's app audit. Until the
last two clear, the pipeline publishes to Instagram and LinkedIn only, which is
encoded in `platform_targets` rather than in code.

### Constraints worth knowing before you plan around this

- **YouTube's default API quota allows 6 uploads/day, not 10.** A quota
  extension requires a compliance audit and takes weeks.
- **TikTok will not publish automatically without an audited app.** Without the
  audit, posts land in an inbox for someone to finish by hand.
- Trend research genuinely covers TikTok hashtag feeds and Google Trends.
  Instagram has no trend source at all and should not be assumed.
- **Postiz must be publicly reachable.** Connecting a channel is an OAuth flow
  completed in a browser, and the platforms require an HTTPS redirect URI on a
  registered domain. Its own login is the boundary.

Full detail in [docs/GAPS.md](docs/GAPS.md).

## Documents

| File | What it is |
|---|---|
| [STACK.md](docs/STACK.md) | The stack, every decision made, and why |
| [GAPS.md](docs/GAPS.md) | What the stack does not cover, and what it costs to close |
| [VERDICTS.md](docs/VERDICTS.md) | Repo evaluation evidence — measured, not claimed |
