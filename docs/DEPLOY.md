# Deploying

Order matters in a couple of places, and those places are called out. Nothing
here needs Docker on your machine: images are built by the `images` workflow.

## 0. Decide whether publishing is on

`postiz_enabled` defaults to **false**, and the default path through this
document assumes it stays that way. Publishing is the only part of the system
that needs approved platform apps and connected channels, and it is also the
most expensive: a t3.large carrying Temporal and Elasticsearch, a public ALB, a
100GB volume and nightly snapshots.

With it off:

- No Postiz resource is created, so the `reels/postiz` secret and the ACM
  certificate are not needed and steps 4 and the Postiz half of step 1 do not
  apply.
- `POSTIZ_BASE_URL` is empty and `PUBLISHING_ENABLED=false` reaches the
  pipeline. `PostizClient` refuses to be constructed and the publish, poll and
  analytics activities return a skip instead of calling anything.
- `reconcile_publishes` and `collect_analytics` are not scheduled at all.
- Gate 2 approval ends the execution at `PublishingDisabled`, a `Succeed`. The
  row keeps `status='approved'` with its finished cut and per-platform copy.

Everything else is unchanged: trend research, Gate 1, render, quality check,
copy generation, Gate 2 and the five remaining reconcilers all run. Nothing
about how a video is *made* differs, which is the point — switching publishing
on later does not invalidate anything produced before.

## 1. Create the secrets

They are separate on purpose. The Postiz host has no business holding the
Supabase service-role key, and the pipeline has no business holding platform
OAuth secrets.

Only `reels/pipeline` is needed while publishing is off.

### `reels/pipeline`

Read at Lambda cold start and by the trend task. `NICHE_BRIEF` and
`TREND_HASHTAGS` are not secret, but this bundle is the only injection path
into the trend task, so they live here too.

```json
{
  "SUPABASE_SERVICE_ROLE_KEY": "…",
  "MPT_API_KEY":               "a long random string; MoneyPrinterTurbo compares it in constant time",
  "POSTIZ_API_KEY":            "from Postiz Settings → Public API; omit while publishing is off",
  "ANTHROPIC_API_KEY":         "idea generation, when IDEA_LLM_PROVIDER=claude (the default)",
  "GEMINI_API_KEY":            "idea generation, when IDEA_LLM_PROVIDER=gemini; free from aistudio.google.com",
  "FAL_API_KEY":               "only needed if a fal preset is active; sent as 'Authorization: Key …', not Bearer",
  "HEYGEN_API_KEY":            "for the presenter lane; sent raw in the 'X-Api-Key' header, not as a bearer token",
  "GATE_BRIDGE_SECRET":        "shared header for the webhook bridge",
  "NICHE_BRIEF":               "what we do, who we speak to, and what we must NOT claim",
  "TREND_HASHTAGS":            "comma,separated,hashtags",
  "TIKTOK_MS_TOKEN":           "from your tiktok.com cookies; scouting mostly fails without it"
}
```

`NICHE_BRIEF` and `TREND_HASHTAGS` are **fallbacks now**. Both live in the
`trend_settings` table, seeded by migration and edited under Settings in the
app -- they were the two values most likely to be tuned weekly, and routing
that through a secret and a cold start was the wrong shape. Since
20260906150000 that table also holds the schedule, the filters, the pacing and
the drafting model; see **When trend research runs** below. The row wins whenever it
has a value; the bundle answers only when it does not, which is what lets a
headless run work against a database that has not been migrated. Keeping them
here is therefore optional, and belt-and-braces rather than required.

Either way, a run with **no** brief from either source is a **hard failure**.
That is deliberate: a trend run without one produces plausible, irrelevant
ideas, and each one costs the owner a review and possibly a render.

Only the key for the selected provider is needed -- but if you intend to switch
provider from the app, both keys have to be in the bundle, because that switch
no longer involves a deploy. The provider is now `trend_settings.idea_provider`,
edited under **Run length and pacing** in Settings. `IDEA_LLM_PROVIDER`, set on
the trend task via the `idea_provider` Terraform variable, is the fallback for
when that column is blank or the table cannot be read.

The key is checked **before** scouting rather than at the point of drafting.
Scouting drives a real browser for several minutes, and a run that discovers a
missing key after that has thrown all of it away for a reason that was knowable
at the start.

Switching providers changes which model drafts and nothing else: the system
prompt, the output schema and the row mapping are shared, which is what keeps
the two lanes' ideas comparable. Gemini exists on this path because its free
tier makes a demo possible without a billing account.

`GEMINI_MODEL` defaults to `gemini-3.5-flash`. Two things decided that: the pro
models have no free tier, and `gemini-3.8-flash` -- which does have one --
returned 503 "high demand" on every schema-constrained request tried against a
free key, while answering a one-word prompt fine. Free-tier traffic is shed
first and the newest model is where the queue is. `gemini-2.5-flash` is not an
option at all any more: it 404s with "no longer available to new users".

### When trend research runs

The schedule is a **setting, not a deployment**. It used to be
`cron(0 6 * * ? *)` in `infra/schedules.tf`, pointed straight at the ECS
cluster; it is now `schedule_hour_utc`, `schedule_minute_utc`, `schedule_days`
and `schedule_enabled` on `trend_settings`, edited under **When runs happen**
in the app. Times are UTC, matching the run rows and the logs.

`dispatch_trend_runs` -- already running every minute for the button -- is what
reads them. When a slot is due it opens a `trend_runs` row and starts it
through exactly the path a manual run takes, which is why a scheduled run now
has somewhere to report its progress, its results and its rejection breakdown.
Under the old cron it had none of that: a scheduled run's outcome existed only
in CloudWatch.

A slot that is missed -- because a run was still going, or the dispatcher was
down -- starts late if it can, up to an hour afterwards, and is abandoned past
that rather than fired at an unrelated time of day. Pausing stops the schedule
only; the button still works, which is how to test a settings change without
waiting for tomorrow.

Two consequences worth knowing before you apply this:

- **The apply deletes `aws_scheduler_schedule.trends`.** Until the migration is
  applied *and* the new activities image is deployed, nothing starts a
  scheduled run. The button is unaffected throughout.
- **The dispatcher reads `trend_settings` every minute.** If that table is
  missing or unreadable it falls back to the defaults, which are the old daily
  06:00 UTC run -- so a broken read degrades to the previous behaviour rather
  than to silence.

### Stopping a run

**Stop this run** appears on the banner whenever a run is in flight, for an
owner. It calls `cancel_trend_run`, and that function is the whole mechanism:
it moves the row to `cancelled`, which is not one of the statuses
`trend_runs_single_in_flight` indexes, so the lock frees itself.

No Lambda, no sweep and no AWS call is involved in that taking effect, and that
is deliberate rather than incidental. At most one run may be in flight, so a
row nothing will ever finish does not hold up one run -- it holds up every
future run. The only thing that could previously clear such a row was
`dispatch_trend_runs`, which is useless in the case that produces stuck rows
most often: **the dispatcher not running**. An unapplied `terraform apply`, a
Lambda that will not start, a broken image -- each leaves a `requested` row
that nothing claims and nothing expires, and the app correctly reports that
nothing is coming to clear it. Stop is the way out of that without a console.

If the run had already started, its Fargate task is stopped two ways, because
neither alone covers the case the other does:

- `dispatch_trend_runs` calls `ecs:StopTask` on its next sweep, within a
  minute. Works against a task that has stopped responding; needs the
  dispatcher.
- the scout checks its own row between hashtags and exits. Works when the
  dispatcher is the broken part; needs the task to still be running its loop.

Worst case both miss and the session is orphaned until the three-hour
write-off. It holds nothing up in the meantime -- the lock was freed the
instant the row was cancelled.

A stopped run **drafts and inserts nothing**, even if it had already found
signals worth drafting. Stop means stop: filling the queue a minute after being
told the run was cancelled is a surprise, and it spends an LLM call on a batch
nobody asked to finish.

`ecs:StopTask` is a new permission on the activities Lambda, scoped to tasks in
the pipeline cluster. Cancelling works without it; only the task kill does not.

### Trend runs on demand

The **Generate more ideas** button on the Gate 1 queue starts a run
immediately. The app cannot call AWS --
there is no server tier -- so the chain is the same one every other write in
this system uses:

1. `request_trend_run()` inserts a row in `trend_runs`. Owner-only, and a
   partial unique index permits **one** in-flight run at a time.
2. `dispatch_trend_runs`, a sweeper running every minute, claims that row and
   calls `ecs:RunTask` on the same task definition, subnets and security group
   the nightly run uses.
3. The task reports back into the row: counts on success, the error on failure.
   It knows which row from `TREND_RUN_ID`, set as a container override -- which
   is also how it tells an on-demand run from a scheduled one, since the
   scheduled run has no row and needs none.

This needs a `terraform apply` as well as the migration, and **both**: the
sweeper's schedule, the Lambda's `TRENDS_*` environment, and `ecs:RunTask` plus
a scoped `iam:PassRole` on the Lambda role are all new.

Apply the two together, and in that order if they must be separate. The failure
modes on either side of the pair are not symmetrical:

- **Migration without the apply.** `request_trend_run` exists, so the button
  inserts a row -- and nothing is scheduled to claim it. Note what that costs:
  `dispatch_trend_runs` is also what *expires* an unclaimed request, so the row
  is not written off either, and the one-in-flight index then holds the button
  shut until the apply lands. The queue says so rather than spinning: a request
  unclaimed for more than three minutes swaps the "scouting" banner for one that
  names the dispatcher as the thing to check. The `the trend task is not
  configured for on-demand runs` error is the *next* state along -- it means the
  sweeper is running and its `TRENDS_*` environment is unset, which is a
  different fault from the sweeper not running at all.
- **Apply without the migration.** The sweeper queries a `trend_runs` table that
  does not exist and finds nothing to do, every minute, harmlessly. The button
  is the only thing that suffers, and it says why: PostgREST reports the missing
  function and the toast passes that on.

The `ecs:RunTask` grant names the task-definition **family** with a revision
wildcard rather than the current ARN. Every image build registers a new
revision, and a policy pinned to one would start denying the button the moment
the task definition changed.

**A run that never finishes.** At most one may be in flight, so a task killed by
Fargate would otherwise hold the button shut permanently. `dispatch_trend_runs`
writes off a run left `running` for more than 3 hours, or `requested` for more
than 10 minutes, before it claims anything -- expiry first, so the one state
that needs recovering is not the one state that never recovers. Nothing has to
be done by hand; the button comes back within the minute.

### `reels/postiz`

Only read when `postiz_enabled = true`, so it does not have to exist yet.

Written to `/opt/postiz/.env` at boot. Every key becomes an environment
variable for the compose stack.

```json
{
  "JWT_SECRET":              "long random string, unique to this install",
  "POSTIZ_DB_PASSWORD":      "…",
  "TEMPORAL_DB_PASSWORD":    "…",
  "MAIN_URL":                "https://social.example.org",
  "FRONTEND_URL":            "https://social.example.org",
  "NEXT_PUBLIC_BACKEND_URL": "https://social.example.org/api",

  "INSTAGRAM_APP_ID":     "…", "INSTAGRAM_APP_SECRET":     "…",
  "LINKEDIN_CLIENT_ID":   "…", "LINKEDIN_CLIENT_SECRET":   "…",
  "YOUTUBE_CLIENT_ID":    "…", "YOUTUBE_CLIENT_SECRET":    "…",
  "TIKTOK_CLIENT_ID":     "…", "TIKTOK_CLIENT_SECRET":     "…"
}
```

The three URL values must match the certificate's domain, or OAuth callbacks
land nowhere.

## 2. Build images

Push to `main`, or run the `images` workflow by hand. It needs
`AWS_DEPLOY_ROLE_ARN` (an OIDC role, so no long-lived keys in GitHub) and an
`AWS_REGION` variable. The job summary prints the tag to deploy.

## 3. Apply

```sh
cd infra
terraform init
terraform apply \
  -var supabase_url=https://uerpeuidrxjzxqfxqzic.supabase.co \
  -var image_tag=<sha from the workflow>
```

The `next_steps` output prints the remaining checklist for whichever mode you
applied in, and `publishing_enabled` reports which one that was.

To include publishing, add `-var postiz_enabled=true` and
`-var postiz_certificate_arn=<acm arn>`. Without the certificate everything
comes up and the Postiz UI loads, but **no channel can be connected** —
Instagram, TikTok, YouTube and LinkedIn all require an HTTPS redirect URI on a
registered domain.

## 4. Connect the channels

Skip this while publishing is off; there is nothing to connect to.

Open `postiz_public_url`, create the **single** owner account, then connect
each platform. Registration is disabled in the compose file, so that first
account is the only one.

Two things to get right here:

- Connect a LinkedIn **Page**, not a personal profile. The personal provider
  reports no analytics at all, so the feedback loop would be silently blind on
  that platform.
- Copy the API key from Settings → Public API into `POSTIZ_API_KEY` and
  re-apply, so the pipeline can authenticate.

## 5. Wire the webhooks

Two Database Webhooks in the Supabase dashboard, pointed at the `gate1` and
`gate2` outputs. **Set the `WHEN` clauses**, or the pipeline's own writes to
`productions` will re-invoke the Gate 2 bridge dozens of times per production:

| Gate | Table | Condition | Timeout |
|---|---|---|---|
| 1 | `ideas` | `old.status <> 'approved' AND new.status = 'approved'` | 5000 ms |
| 2 | `productions` | `old.status IS DISTINCT FROM new.status AND new.status IN ('approved','rejected')` | 5000 ms |

Add the `GATE_BRIDGE_SECRET` as a static header on both.

The default webhook timeout is **1000 ms**, which an API Gateway hop routinely
exceeds. Raise it to 5000. Even then the webhook is only a latency
optimisation — it is `pg_net`, so at-most-once with no retry and no
dead-letter queue. The `reconcile_gates` sweeper, running every minute, is the
actual guarantee.

## 5a. Check the presenter lane before anyone approves one

The `ai-presenter` preset is active, so it is approvable at Gate 1 and it
spends real money on a pay-as-you-go wallet rather than a monthly allowance.
Two things are worth confirming before the first approval:

```sh
# The key is valid, and there is money behind it. Costs nothing.
curl -s https://api.heygen.com/v3/users/me -H "X-Api-Key: $HEYGEN_API_KEY"
```

- `wallet.remaining_balance` is the ceiling on presenter renders. An empty
  wallet fails the render *after* Gate 1 has been passed, which wastes a
  review rather than preventing one.
- The preset's `params.heygen.avatar_id` must be a look this account can use.
  A stale id fails with `avatar_not_found`, which the pipeline treats as
  terminal and parks — correctly, since it would fail identically on retry.

```sh
# The looks this account owns, with their ids and portrait/landscape hint.
curl -s "https://api.heygen.com/v3/avatars/looks?limit=50&ownership=private" \
  -H "X-Api-Key: $HEYGEN_API_KEY"
```

Prefer a look whose `preferred_orientation` is `portrait`: a 9:16 render from a
landscape source crops the speaker to fill the frame. To change the avatar or
the voice, update the preset — no deploy:

```sql
update style_presets
set params = jsonb_set(params, '{heygen,avatar_id}', '"<look id>"')
where slug = 'ai-presenter';
```

## 6. Enable platforms as they clear

`platform_targets` ships with Instagram and LinkedIn enabled, and **YouTube and
TikTok disabled**:

```sql
-- once the YouTube quota extension is granted
update platform_targets set enabled = true, daily_cap = <new cap> where platform = 'youtube';
-- once the TikTok app audit clears and DIRECT_POST is available
update platform_targets set enabled = true where platform = 'tiktok';
```

No deploy needed. Both are multi-week external processes and neither has
started; until they do, the pipeline publishes to two platforms.

## Turning publishing on

Nothing has to be undone first, and no earlier work is wasted.

1. Create the `reels/postiz` secret (above) and add `POSTIZ_API_KEY` to
   `reels/pipeline` — the key comes from Postiz itself, so on the very first
   run it is a second apply: bring Postiz up, read the key from Settings →
   Public API, put it in the secret, apply again.
2. Request an ACM certificate for the domain the platform apps will redirect
   to, and point that domain at the `postiz_public_url` load balancer.
3. `terraform apply -var postiz_enabled=true -var postiz_certificate_arn=<arn>`
   plus the usual `supabase_url` and `image_tag`. This creates the host, the
   volume, the ALB, the two schedules, and re-templates the state machine so
   Gate 2 approval routes to `Publish` again.
4. Connect the channels (step 4) and enable platforms (step 6).
5. Flush the backlog. Everything approved while publishing was off is sitting
   at `status='approved'`:

   ```sql
   select id, created_at from productions where status = 'approved' order by created_at;
   ```

   Each one has its render in the `renders` bucket and its `platform_copy`
   written, so it publishes by starting a fresh execution for that
   `production_id`. Do this deliberately rather than in bulk: a month of
   backlog released at once is a month of posts released at once, and the
   `daily_cap` in `platform_targets` is not what will stop it.

Turning it back **off** is not symmetrical. The data volume is
`prevent_destroy`, because it holds the OAuth tokens for four platforms, so a
plan that would delete it fails instead. That is deliberate: re-obtaining those
tokens means re-running TikTok's audit and YouTube's review. Removing the volume
has to be an explicit `terraform state rm` or an edit to `postiz.tf`.

## Operational notes

- **Shell access to Postiz:** the `postiz_ssh` output prints a Session Manager
  command. There is no SSH key and no open port 22.
- **A rejection or a park is a `Succeed`**, so a `FAILED` execution always
  means something is genuinely broken. That is why the two CloudWatch alarms
  are worth paging on.
- **The presenter lane bills per render and cannot be undone.** A HeyGen
  submit carries the production id as an `Idempotency-Key`, so a retry inside
  24 hours replays rather than re-renders. Past 24 hours the same production
  re-run is a new charge — which matters if a backlog is ever replayed.
- **MPT deploys are disruptive by design.** One replica, stop-before-start.
  In-flight renders are lost, `reconcile_renders` parks them, and a human
  re-runs. Deploy it when the queue is empty.
- **The `renders` bucket is private.** Postiz gets a short-lived signed URL at
  publish time; a public bucket would expose every unreviewed cut.
