# Running it

Two ways, and they are the same system: on your own machine for development and
for the first end-to-end check, and on a VPS with Docker Compose for real.

There is no cloud provider, nothing to provision and no infrastructure state to
keep. This used to be seventeen Terraform files, four ECR repositories, an OIDC
role and a `terraform apply` with an image tag from CI.

---

## What you need before either

**Supabase.** The system of record and the render store. A hosted project is
fine; nothing here needs it to be self-hosted.

```sh
bunx supabase link --project-ref <your-ref>
bunx supabase db push
```

**Credentials.** Two env files, each with an example beside it:

```sh
cp apps/pipeline/.env.example apps/pipeline/.env
cp services/mpt/.env.example  services/mpt/.env
```

The minimum for a working stock-footage reel is four values: Supabase's URL and
service-role key, a shared `MPT_API_KEY` (any long random string — MoneyPrinter-
Turbo compares it in constant time), and a **free Pexels key** from
pexels.com/api, without which the stock lane has no footage to cut together.
Everything else is per-lane and can stay empty: `FAL_API_KEY` and
`HEYGEN_API_KEY` are only read when a preset that names them is approved, and
each trend source's credential is checked before scouting starts rather than at
the point of use — which matters most on Apify, where finding out afterwards
means having paid for a scrape whose results were then thrown away.

**A niche brief.** Set it under Settings in the app. A trend run with no brief
is a hard failure on purpose: without one it produces plausible, irrelevant
ideas, and each costs the owner a review and possibly a render.

---

## Locally, without Docker

Everything except publishing runs natively, which is the fastest way to watch a
production go through both gates.

**ffmpeg and ffprobe** are needed by the quality check and by the fal assembly
lane. If your package manager is awkward, the static build needs no root:

```sh
curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz \
  | tar -xJ --strip-components=1 --wildcards -C ~/.local/bin '*/ffmpeg' '*/ffprobe'
```

Then three terminals:

```sh
# 1. MoneyPrinterTurbo. It is a plain FastAPI app; the Docker image only exists
#    because upstream's own launches the Streamlit WebUI instead of the API.
cd services/mpt
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
set -a && . ./.env && set +a
MPT__APP__ENABLE_REDIS=false ./.venv/bin/python main.py     # :8080

# 2. The pipeline worker.
cd apps/pipeline
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
set -a && . ./.env && set +a
MPT_BASE_URL=http://127.0.0.1:8080 ./.venv/bin/python -m pipeline.driver.worker

# 3. The approval app.
cd apps/web && bun install && bun run dev                    # :5173
```

`enable_redis=false` is fine for one local run and not for the VPS: without
Redis, MoneyPrinterTurbo keeps task state in process memory and loses every
in-flight render when it restarts. `reconcile_renders` reports exactly that and
parks the rows.

### Watching one go through

```sql
select status, stage, run_state->>'step', run_state->>'due_at'
  from productions order by created_at desc limit 5;
```

Approve an idea at Gate 1 and the row should appear within five seconds, then
walk `submit_render → poll_render → fetch_and_qc → generate_copy → await_gate2`.
The app shows the same walk without the SQL: approving now stays on the idea's
page and the timeline there advances as the driver writes, over Realtime rather
than a poll. At Gate 2 it stops until you decide. With publishing off, approving leaves it at
`status='approved'` with `step = 'publishing_disabled'` — that is the backlog,
not a failure.

**Worth doing once:** kill the worker mid-render and start it again. It should
resume polling the same render rather than submitting a second one. That is the
property `run_state` exists for, and the one thing the old state machine
provided for free.

---

## On a VPS

Any box with Docker and Compose. Two CPUs and 4 GB is enough for the stock lane;
rendering is what wants the headroom, not the driver.

```sh
git clone --recurse-submodules <this repo> && cd <repo>
cp apps/pipeline/.env.example apps/pipeline/.env   # and fill it in
cp services/mpt/.env.example  services/mpt/.env    # and fill it in
./deploy.sh
```

`deploy.sh` pulls, builds the approval app, builds the images and restarts the
stack. Redeploying is the same command; Compose sends SIGTERM, and the worker
finishes the step it is on and releases its leases rather than abandoning them,
so a deploy costs seconds of latency instead of a lease timeout of stalled
queue.

The stack is four containers: `worker`, `mpt`, `mpt-redis`, and nginx serving
the built SPA on :8080. Put a reverse proxy in front of that if you want TLS;
nothing in the pipeline needs to be reachable from outside.

### Watching it

```sh
docker compose logs -f worker
```

A production that stopped is **parked**, which means it is waiting for a person
rather than broken. That distinction is deliberate throughout: a rejection and a
park are both ordinary outcomes, so anything that looks like a failure really is
one.

```sql
select id, error, run_state->>'step' from productions where status = 'parked';
```

In the app this is the **In production** panel on the queue page, which lists
every production that has not finished and puts the stopped ones first. Opening
one shows its whole step log and the controls to retry, pause or re-run it, so
parking no longer means reaching for psql. The step-by-step record is:

```sql
select step, outcome, detail, error, created_at
  from production_events where production_id = '<id>' order by created_at;
```

---

## If you are migrating from the AWS deployment

Three things do not happen by themselves.

1. **Delete the two Supabase Database Webhooks** (Database → Webhooks, on
   `ideas` and `productions`). They are configured in the Supabase dashboard,
   not in this repo, so nothing here removes them — and left in place they keep
   firing `pg_net` requests at a dead API Gateway on every idea approval and
   every Gate 2 decision. The driver does not need them: it polls.
2. **Apply both migrations.** `..._driver_run_state.sql` is additive and safe to
   apply while the old stack is still up; `..._retire_gate_tokens.sql` drops the
   callback machinery and should go last.
3. **Tear down the AWS stack by hand.** `infra/` is gone from this repo, so
   there is no `terraform destroy` to run — delete the state machine, the two
   Lambdas, the ECS cluster, the SQS queues, the API Gateway, the EventBridge
   schedules, the ECR repositories and the secrets from the console, and check
   the NAT gateway is gone, because it is the line item that keeps billing.

In-flight productions carry over. A row mid-render has `task_id` and its status,
which is all `reconcile_renders` needs; a row at `awaiting_review` is picked up
by the new claim the moment it is decided. Rows whose Step Functions execution
died are parked by `reconcile_leases` within a minute of the worker starting.

---

## Turning publishing on

Publishing is off by default and not in `docker-compose.yml` at all. It is the
only part of the system that needs approved platform apps, connected channels
and a public HTTPS domain, and it brings its own Postgres, Redis, Temporal and
Elasticsearch — most of the box's memory, for a capability that is worth nothing
until the channels exist.

Nothing produced while it was off is wasted. Every approved production is
sitting at `status='approved'` with `run_state.step = 'publishing_disabled'`,
holding its finished render and its per-platform copy.

1. Bring up Postiz from `docker/postiz-compose.yaml`. It needs a `.env` with
   `JWT_SECRET`, its two database passwords, and `MAIN_URL` / `FRONTEND_URL` /
   `NEXT_PUBLIC_BACKEND_URL` all set to the domain the platform apps will
   redirect to. Those three must match the certificate or OAuth callbacks land
   nowhere.
2. Put a reverse proxy with TLS in front of it — Caddy will get a certificate on
   its own. Postiz has to be publicly reachable because connecting a channel is
   an OAuth flow completed in a browser and the platforms require an HTTPS
   redirect URI on a registered domain.
3. Create the single owner account, then connect each platform. Registration is
   disabled in the compose file, so that first account is the only one. Connect
   a LinkedIn **Page**, not a personal profile — the personal provider reports
   no analytics at all, so the feedback loop would be silently blind there.
4. Copy the key from Settings → Public API into `POSTIZ_API_KEY`, set
   `PUBLISHING_ENABLED=true` and `POSTIZ_BASE_URL`, and restart the worker.
5. The backlog releases itself: `flush_publishing_backlog` sends rested
   productions back through the gate. Watch the first few — a month of backlog
   released at once is a month of posts released at once, and `daily_cap` in
   `platform_targets` is not what will stop it.

### As platforms clear

`platform_targets` ships with Instagram and LinkedIn enabled and YouTube and
TikTok disabled, because YouTube's default quota allows six uploads a day
against a target of ten, and TikTok will not publish automatically without an
audited app. Both are multi-week external processes.

```sql
update platform_targets set enabled = true, daily_cap = <new cap> where platform = 'youtube';
update platform_targets set enabled = true where platform = 'tiktok';
```

**Back up the Postiz volume.** It holds OAuth tokens for four platforms, and
re-obtaining them means re-running TikTok's audit and YouTube's review. On AWS
this was an EBS volume with `prevent_destroy` and nightly snapshots; a Docker
volume has neither, so a `pg_dump` of `postiz-postgres` plus a tar of
`/uploads`, on a timer, is doing real work here rather than being tidy.

---

## The presenter lane

`ai-presenter` is approvable at Gate 1 and spends real money against a
pay-as-you-go wallet.

**It does not need MoneyPrinterTurbo.** HeyGen returns a finished, voiced,
captioned 9:16 reel, and the two pieces of text the lane needs — the script an
owner approves at the script gate, and the per-platform copy — are written by
the same provider that drafts ideas (`IDEA_LLM_PROVIDER` with `GEMINI_API_KEY`
or `ANTHROPIC_API_KEY`; see `pipeline/llm.py`). A deployment that only renders
with HeyGen therefore needs Supabase, that one LLM key, `HEYGEN_API_KEY`, and
`ffmpeg`/`ffprobe` for the quality check. `MPT_BASE_URL` can stay unset; it is
read only when a preset with `render_mode = 'mpt'` or a fal lane is approved,
or when no LLM key is configured at all, in which case scripts fall back to MPT
and the worker log says so.

Two checks before the first approval:

```sh
# Valid key, and money behind it. Costs nothing.
curl -s https://api.heygen.com/v3/users/me -H "X-Api-Key: $HEYGEN_API_KEY"

# The looks this account owns. Prefer preferred_orientation = portrait: a 9:16
# render from a landscape source crops the speaker to fill the frame.
curl -s "https://api.heygen.com/v3/avatars/looks?limit=50&ownership=private" \
  -H "X-Api-Key: $HEYGEN_API_KEY"
```

An empty wallet fails the render *after* Gate 1 has been passed, which wastes a
review rather than preventing one. A stale `avatar_id` fails with
`avatar_not_found`, which the pipeline treats as terminal and parks — correctly,
since it would fail identically on retry. Changing the avatar or voice is a
preset update, not a deploy:

```sql
update style_presets
set params = jsonb_set(params, '{heygen,avatar_id}', '"<look id>"')
where slug = 'ai-presenter';
```

There is a live test that submits one real render. It is off unless asked for:

```sh
cd apps/pipeline
set -a && . ./.env && set +a
HEYGEN_LIVE=1 ./.venv/bin/pytest tests/test_heygen_live.py -v
```

**Cost, measured on 2026-09-07: $0.40** for the 10.5-second render it submits —
about $2.29 a rendered minute, which is where the $1–2 estimate for a full reel
comes from. A rerun spends nothing: the submit carries a stable
`Idempotency-Key`, so a resubmit within 24 hours replays the original response,
and the file is cached in `apps/pipeline/.heygen-live/` after that.
