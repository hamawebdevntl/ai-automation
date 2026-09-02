# Approval app

The web app for the two human gates in the reels pipeline: **Gate 1**, where an
owner approves an idea and picks how it gets made, and **Gate 2**, where an
owner signs off the finished cut before anything publishes.

Everything between those two decisions runs unattended. This app is deliberately
small — it is the only place a person is required.

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
cp .env.example .env      # fill in your Supabase project
bun run dev               # http://localhost:3000

bun run check             # lint, typecheck and test
bun run build             # static bundle in dist/
```

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
   the quality check and publishing all run on AWS and write to these tables
   with a service-role key, which bypasses RLS. Step Functions task tokens never
   reach the browser.

Three things from the previous Next.js app did not come across, because a
client-only bundle cannot hold them safely: Upstash rate limiting, the Resend
integration, and the FreeWrite CMS fetcher (its block *types* survive; see the
note at the foot of `src/lib/freewritecms.ts`). The server-side
`AUTH_ALLOW_GOOGLE_SIGNUP` flag is gone for the same reason — restricting who
may sign up belongs in Supabase, not in a switch the browser can flip.

---

## Database

`supabase/migrations/` holds the schema. Apply it through the Supabase dashboard
SQL editor or `supabase db push`, in order:

| File | What it does |
|---|---|
| `0001_approval_queue.sql` | Tables, RLS policies, and the three gate functions |
| `0002_style_presets_seed.sql` | The three production lanes with their cost and ETA |
| `0003_demo_data.sql` | Optional sample rows, enough to exercise both gates |

New accounts land as **viewers** — they can read the queue but pass neither
gate. Promote deliberately:

```sql
update public.profiles set role = 'owner' where email = 'you@example.com';
```

The types in `src/lib/database.types.ts` are hand-written to match the
migration. Once a project exists they can be regenerated instead:

```bash
bunx supabase gen types typescript --project-id <ref> > src/lib/database.types.ts
```

Keep them in step. They are the only thing standing between a typo in a column
name and a runtime error.

---

## Layout

```
src/
  routes/            file-based routes; the tree in routeTree.gen.ts is generated
    _app.tsx           authenticated layout + route guard
    _app/queue.*       Gate 1 — idea queue and the decision screen
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
