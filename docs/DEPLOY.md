# Deploying

Order matters in a couple of places, and those places are called out. Nothing
here needs Docker on your machine: images are built by the `images` workflow.

## 1. Create the two secrets

They are separate on purpose. The Postiz host has no business holding the
Supabase service-role key, and the pipeline has no business holding platform
OAuth secrets.

### `reels/pipeline`

Read at Lambda cold start and by the trend task. `NICHE_BRIEF` and
`TREND_HASHTAGS` are not secret, but this bundle is the only injection path
into the trend task, so they live here too.

```json
{
  "SUPABASE_SERVICE_ROLE_KEY": "…",
  "MPT_API_KEY":               "a long random string; MoneyPrinterTurbo compares it in constant time",
  "POSTIZ_API_KEY":            "from Postiz Settings → Public API",
  "ANTHROPIC_API_KEY":         "for idea generation",
  "GATE_BRIDGE_SECRET":        "shared header for the webhook bridge",
  "NICHE_BRIEF":               "what we do, who we speak to, and what we must NOT claim",
  "TREND_HASHTAGS":            "comma,separated,hashtags",
  "TIKTOK_MS_TOKEN":           "from your tiktok.com cookies; scouting mostly fails without it"
}
```

`NICHE_BRIEF` is a **hard startup failure** if absent. That is deliberate: a
trend run without it produces plausible, irrelevant ideas, and each one costs
the owner a review and possibly a render.

### `reels/postiz`

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
  -var image_tag=<sha from the workflow> \
  -var postiz_certificate_arn=<acm arn>
```

Without `postiz_certificate_arn` everything comes up and the Postiz UI loads,
but **no channel can be connected** — Instagram, TikTok, YouTube and LinkedIn
all require an HTTPS redirect URI on a registered domain.

## 4. Connect the channels

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

## Operational notes

- **Shell access to Postiz:** the `postiz_ssh` output prints a Session Manager
  command. There is no SSH key and no open port 22.
- **A rejection or a park is a `Succeed`**, so a `FAILED` execution always
  means something is genuinely broken. That is why the two CloudWatch alarms
  are worth paging on.
- **MPT deploys are disruptive by design.** One replica, stop-before-start.
  In-flight renders are lost, `reconcile_renders` parks them, and a human
  re-runs. Deploy it when the queue is empty.
- **The `renders` bucket is private.** Postiz gets a short-lived signed URL at
  publish time; a public bucket would expose every unreviewed cut.
