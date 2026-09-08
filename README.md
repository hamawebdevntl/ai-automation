# Reels Automation

An end-to-end system that researches what is working in our niche, proposes
Reel ideas for a human to approve, produces the finished videos, and publishes
them to Instagram, TikTok, YouTube Shorts and LinkedIn — at roughly **10 Reels
per day**, with a person in control at three points and never more.

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
       |
  5  ==== SCRIPT GATE: HUMAN ==== owner reads the draft, edits it, approves it.
       |                          Nothing is rendered until they do.
  6  Voice                 TTS narration
  7  Visuals               stock b-roll, generative clips, or an AI presenter
  8  Compose               assembly, transitions, music
  9  Captions              burned-in, styled to brand
 10  Quality check         automated: file integrity, audio levels, caption
       |                    presence, slideshow-risk score
 11  ==== GATE 2: HUMAN ====   owner approves the finished cut
       |
 12  Publish               Instagram / TikTok / YouTube Shorts / LinkedIn
 13  Analytics             per-platform performance pulled back in
       |
       +--> feeds back into (1) so idea scoring learns from our own results
```

Stages 3, 5 and 11 are the only places a person is required. Everything else
runs unattended on a schedule.

Stage 5 is the newest of the three and the cheapest to have added. Until it
existed, the words a Reel said were decided by whichever backend happened to
render it — MoneyPrinterTurbo wrote its own script *inside* the render on the
two default styles — and the first time anyone read them was at Gate 2, with the
video already paid for. Approving an idea is not the same as approving what it
says, and the gap between the two was the one place this system decided
something a person is supposed to decide. The gate costs one LLM call and no
video generation, so rejecting a script is free in a way that rejecting a cut is
not.

Stages 12 and 13 are **switched off by default.** They are the only part of the
system that needs approved platform apps and connected channels, and they carry
the stack's largest running cost — Postiz brings its own Postgres, Redis,
Temporal and Elasticsearch — so `PUBLISHING_ENABLED=false` leaves them out until
they are wanted. Everything through Gate 2 still runs; an approved production
waits at `status='approved'` with its finished cut and its per-platform copy,
and that queue is the backlog to publish once the switch is on. See
[Turning publishing on](docs/DEPLOY.md#turning-publishing-on).

---

## Architecture

**Orchestration** is a worker loop over Postgres. A production is a row; the
driver claims one, advances it a single step, writes down where it got to, and
releases it. Both human gates are simply states the claim query skips — a job
waits, for minutes or for days, because nothing is holding it.

This was AWS Step Functions, with the gates as callback task tokens. Removing it
removed rather more than the state machine: the tokens, the private table that
held them, the six database functions that guarded it, an SQS pair, an API
Gateway, two Supabase webhooks and the sweeper that existed because those
webhooks are at-most-once and would sometimes drop a decision outright. What
replaced all of that is one `jsonb` column and a lease.

**Production** runs as a forked service driven over its REST API. It is
deterministic Python and FFmpeg, which means a given input produces a
predictable output and failures are debuggable.

**Storage** is Supabase: Postgres for the system of record, and a private
Storage bucket for the finished cuts. The pipeline mints short-lived signed URLs
rather than making the bucket public, so an unreviewed cut is never reachable by
guessing a path.

**Publishing** is a self-hosted Postiz deployment, called over its REST API and
never modified. It owns the four platform integrations, OAuth token refresh,
scheduling and analytics — the part of this system most likely to break when a
platform changes its rules, deliberately isolated behind a boundary. That same
boundary is what lets it be left undeployed.

**Approval** is a small internal web app (`apps/web/`): a queue of pending ideas
with cost per style at Gate 1, an editor for the drafted script at the script
gate, and a preview with approve/reject at Gate 2. It is a static Vite +
TanStack Router SPA talking to Supabase directly, with no server of its own —
which means row-level security and a handful of Postgres functions, not the
browser, are what decide who may pass a gate.

Between those two gates the app shows the production itself: every step from
approval to finished output, live, with what each step did, what failed and was
retried, and why anything stopped. That record is `production_events`, an
append-only log the driver writes as it goes — `run_state` is a rolling
snapshot that each step overwrites, so it can say where a production is but
never how it got there. The same screen is where a production is driven by
hand: pause, resume, retry, cancel, redo a step, or make the idea again as a
fresh production. Each of those is an owner-gated Postgres function that
records itself, for the same reason the gate decisions are.

**Deployment** is `docker compose up` on one VPS: the worker, MoneyPrinterTurbo,
a Redis for its task state, and nginx serving the approval app. There is no
cloud provider in the loop and nothing to provision — see
[docs/DEPLOY.md](docs/DEPLOY.md).

**Generation** is where the money goes, and the only thing reached for off the
box. Voice, footage and the presenter lane go to the best available providers,
because output quality is the whole point.

### Style lanes

The owner picks a production style per idea at Gate 1. The styles differ mainly
in cost, which is why the cost estimate is shown at the moment of choosing:

| Style | Roughly | Best for |
|---|---|---|
| Stock b-roll | $0.10–0.40 / reel | The default. Volume. |
| Generative | billed per request | Visual novelty, concept-led ideas. |
| AI presenter | $1–2 / reel | Authority content, LinkedIn especially. |

Which engine produces a lane is a separate axis from the lane itself, set per
preset by `render_mode`: MoneyPrinterTurbo end to end, fal for the footage or
for footage and narration, or **HeyGen for the presenter lane** — which returns
a finished, voiced, captioned 9:16 reel that MoneyPrinterTurbo never touches.
Only the script generator is shared, which is what keeps a presenter reel and a
stock reel on the same idea recognisably the same script in the same voice.

The presenter lane is the one that bills per render against a pay-as-you-go
wallet rather than a monthly allowance, which is why the cost range sits next
to the choice at Gate 1 rather than behind it.

There is a fifth mode, `fal_video`, whose input is **a video the owner uploads
plus an instruction they write** rather than text. It is the only lane that
does not generate its footage, and it needs three things on the row before a
cent is spent: the file, the instruction, and a record of consent for the
people who appear in it. The instruction is the model's prompt verbatim, so it
is approved by the script gate alongside the script rather than saved and
forgotten. Its preset (`fal-restyle`) ships **inactive**: video-to-video is
priced above text-to-video, its cost depends on how long a file you upload, and
there is no spend cap yet — so nobody should be able to pick it by accident
before those two are answered.

---

## Repositories used

| Repo | License | Role |
|---|---|---|
| `MoneyPrinterTurbo` | MIT | Production core. Forked and owned. |
| `postiz-app` | AGPL-3.0 | Publishing + analytics. Deployed as-is, never forked. |
| `TikTok-Api` | MIT | Retired trend source. Dormant in the tree; refused on every feed. |
| Apify actors | Commercial API | TikTok + Instagram trend data. Rented, billed per result. |
| YouTube Data API v3 | Commercial API | YouTube trend data. Free, capped by a daily search quota. |
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

**Built:** the three-gate approval app, the full pipeline (trend research, render,
quality check, per-platform copy, publish, analytics), the worker loop that
drives it and its reconcilers, and a Compose stack that runs the lot on one
machine.

**Deployed by default:** everything up to and including Gate 2. Publishing and
analytics are built and reviewed but gated behind `PUBLISHING_ENABLED=false`, so
the default stack runs no Postiz and needs no public endpoint at all.

**Needs your credentials to run:** `apps/pipeline/.env` and
`services/mpt/.env` — copy the `.env.example` beside each. Turning publishing on
additionally needs a domain, a Postiz deployment and the platform channels
connected by hand. See [docs/DEPLOY.md](docs/DEPLOY.md).

**Open and outside our control:** the niche brief, without which trend research
refuses to start; YouTube's quota extension; and TikTok's app audit. Until the
last two clear, the pipeline publishes to Instagram and LinkedIn only, which is
encoded in `platform_targets` rather than in code.

**Not yet judged:** the presenter lane is wired and active, but no reel has
been rendered through it and compared with the stock lane. Its $1–2 estimate
comes from the stack review, not from a bill.

### Constraints worth knowing before you plan around this

- **YouTube's default API quota allows 6 uploads/day, not 10.** A quota
  extension requires a compliance audit and takes weeks.
- **TikTok will not publish automatically without an audited app.** Without the
  audit, posts land in an inbox for someone to finish by hand.
- Trend research covers Google Trends (search demand, free), Apify's hosted
  TikTok and Instagram scrapers (video formats, **billed per result**), and the
  YouTube Data API (video formats, capped at roughly a hundred searches a day).
  One is selected in Settings; they are not combined.
- **The scout no longer drives TikTok directly.** `TikTok-Api` was refused on
  every feed from every kind of address, so that source is retired — the module
  is still in the tree, but it is not selectable. Apify is how TikTok is read
  now, which converts an arms race into a line item.
- **Postiz must be publicly reachable.** Connecting a channel is an OAuth flow
  completed in a browser, and the platforms require an HTTPS redirect URI on a
  registered domain. Its own login is the boundary. This is a large part of why
  publishing is opt-in: until channels are worth connecting, the stack is
  better off without a public endpoint at all.

Full detail in [docs/GAPS.md](docs/GAPS.md).

## Documents

| File | What it is |
|---|---|
| [STACK.md](docs/STACK.md) | The stack, every decision made, and why |
| [GAPS.md](docs/GAPS.md) | What the stack does not cover, and what it costs to close |
| [VERDICTS.md](docs/VERDICTS.md) | Repo evaluation evidence — measured, not claimed |
