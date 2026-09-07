# Approval app

The web app for the three human gates in the reels pipeline: **Gate 1**, where
an owner approves an idea and picks how it gets made; the **script gate**, where
an owner reads, edits and approves the narration before anything is rendered;
and **Gate 2**, where an owner signs off the finished cut before anything
publishes.

Everything between those decisions runs unattended. This app is deliberately
small — it is the only place a person is required.

The script gate is the one that sits before any money is spent. Approving an
idea is not the same as approving what it says, and until that gate existed the
words were written inside the render — so the first sight of them was at Gate 2,
with the video already paid for.

---

## Stack

| | |
|---|---|
| Build | Vite 8 |
| UI | React 19, Tailwind CSS 4, shadcn/ui |
| Routing | TanStack Router (file-based, typed routes, per-route code splitting) |
| Server state | TanStack Query |
| Forms | TanStack Form + Zod 4 |
| Data + auth | Supabase (browser client, row-level security) |
| Lint + format | Biome |
| Tests | Vitest + Testing Library |
| Package manager | Bun |

```bash
bun install
bun run dev               # http://localhost:3000

bun run check             # lint, typecheck and test
bun run build             # static bundle in dist/
```

`.env` is already populated for the live project. `.env.local` holds the
secrets and is gitignored; the app never reads it.

---

## There is no server

This is a **pure client-side SPA**. It builds to static files and holds no
secrets — every `VITE_*` variable is compiled into the bundle where anyone can
read it.

That has three consequences worth internalising before changing anything here:

1. **Row-level security is the access control.** Not the route guards. A guard
   that redirects an unauthenticated visitor to `/login` is a courtesy; the
   thing that actually refuses them is Postgres.
2. **Both gate decisions go through database functions**, not `UPDATE`
   statements — `approve_idea`, `reject_idea` and `decide_production`. There is
   no server tier to keep "record the decision" and "write the audit row"
   together in one transaction, so Postgres does it. Those functions also raise
   `42501` for anyone whose profile role is not `owner`, which is the real
   enforcement behind the read-only state the UI shows a viewer.
3. **The pipeline writes, this app mostly reads.** Trend research, production,
   the quality check and publishing all run in the pipeline worker and write to
   these tables with a service-role key, which bypasses RLS.

   Gate 2 used to hold a Step Functions task token, which is why `private` is
   not in the exposed schema list. That token is gone -- a decision is now just
   a status change that the worker's claim query is watching for -- and the
   `productions.run_state` the driver keeps in its place is deliberately
   readable here: it holds no credential, and `select('*')` would fail outright
   on a revoked column rather than returning a filtered row.

Three things from the previous Next.js app did not come across, because a
client-only bundle cannot hold them safely: Upstash rate limiting, the Resend
integration, and the FreeWrite CMS fetcher (its block *types* survive; see the
note at the foot of `src/lib/freewritecms.ts`). The server-side
`AUTH_ALLOW_GOOGLE_SIGNUP` flag is gone for the same reason — restricting who
may sign up belongs in Supabase, not in a switch the browser can flip.

---

## The project

Live and linked. `supabase/config.toml` is the source of truth for the
project's settings — edit it and run `supabase config push`.

| | |
|---|---|
| Name | `reels-approvals` |
| Ref | `uerpeuidrxjzxqfxqzic` |
| Region | eu-west-1 (Ireland) |
| Dashboard | https://supabase.com/dashboard/project/uerpeuidrxjzxqfxqzic |

```bash
bunx supabase db push                  # apply new migrations
bunx supabase db push --include-seed    # ... and re-apply the demo rows
bunx supabase config push               # apply auth/API settings from config.toml
bunx supabase migration new <name>      # start a new migration
```

### Schema

| File | What it does |
|---|---|
| `migrations/…_approval_queue.sql` | Tables, RLS policies, and the three gate functions |
| `migrations/…_style_presets.sql` | The three production lanes with their cost and ETA |
| `migrations/…_script_gate.sql` | The script gate: `awaiting_script`, the approval columns, and `save_script` / `approve_script` / `request_script_redraft`. Also the constraint that makes it real — a production cannot hold a `task_id` without an approved script, so no render can be paid for against words nobody read |
| `migrations/…_script_gate_whitespace.sql` | `btrim(text)` strips spaces and nothing else, so a newline passed the empty check. Both places now name the whitespace explicitly |
| `migrations/…_fix_role_guard_bootstrap.sql` | Lets a service-role caller set roles — without it no first owner could exist |
| `seed.sql` | Demo rows. Opt-in: only `--include-seed` applies them |

### Accounts

New accounts land as **viewers** — they can read the queue and the scripts but
pass no gate. Promote deliberately, with the secret key (a signed-in viewer cannot
promote themselves; the `guard_profile_role` trigger refuses):

```sql
update public.profiles set role = 'owner' where email = 'you@example.com';
```

**Signup is open.** Anyone who registers becomes a viewer, and a viewer can read
every idea and every cut — the read policies are `to authenticated using
(true)`. For an internal tool that is a data leak waiting to happen. Once the
people who need accounts have them, close it:

```toml
# supabase/config.toml
[auth]
enable_signup = false
```

```bash
bunx supabase config push
```

Email confirmation is on, so an unverified address cannot sign in. Note the
built-in mailer is rate-limited to a handful of messages an hour — configure
`[auth.email.smtp]` before relying on password resets.

The types in `src/lib/database.types.ts` are hand-written to match the
migrations. They can now be regenerated from the live project instead:

```bash
bunx supabase gen types typescript --linked > src/lib/database.types.ts
```

If you do, keep the note at the top of that file about `type` aliases versus
`interface` — the generator gets it right, but hand edits after the fact can
silently collapse every query result to `never`.

Keep them in step. They are the only thing standing between a typo in a column
name and a runtime error.

---

## Layout

```
src/
  routes/            file-based routes; the tree in routeTree.gen.ts is generated
    _app.tsx           authenticated layout + route guard
    _app/queue.*       Gate 1 — idea queue and the decision screen, and the
                       script gate, which lives on the same idea page so a
                       decision and the thing it set in motion stay together
    _app/review.*      Gate 2 — finished cuts and sign-off
  features/
    auth/              session context, profile/role, auth form pieces
    queue/             queries, gate mutations, and the gate-specific components
  components/
    ui/                shadcn/ui
    animate-ui/        \
    kokonutui/          }  vendored kits, carried over from the previous app
    mvpblocks/         /
  lib/                 supabase client, env, formatting, database types
supabase/migrations/   schema, seed, demo data
```

### About the vendored components

`components/animate-ui`, `components/kokonutui`, `components/mvpblocks` and
`components/ui` were installed from registries and moved across unchanged.
`biome.json` scopes the accessibility and security rules down to warnings for
those paths — the findings are real, but they are upstream's to fix, and
everything we write keeps the full rule set. Four files needed edits to compile
at all; each carries a comment saying why.

`components/ui/link.tsx` is a `next/link` stand-in so the ported components kept
working. New code should import `Link` from `@tanstack/react-router` directly
and get typed routes.

`components/ui/form.tsx` is shadcn's form primitive rebuilt on TanStack Form —
the usage comment at the top of that file is the reference.
