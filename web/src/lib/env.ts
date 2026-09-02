import { z } from 'zod';

/**
 * Environment for a pure client-side SPA.
 *
 * Everything here is inlined into the JavaScript bundle at build time and is
 * therefore public. No secret may ever be read through this module — access
 * control is enforced by Postgres row-level security in Supabase, not here.
 */

const flag = z
  .string()
  .optional()
  .transform((value) => value === '1' || value === 'true');

const schema = z.object({
  VITE_SUPABASE_URL: z.url({ error: 'VITE_SUPABASE_URL must be your project URL, e.g. https://abc.supabase.co' }),
  VITE_SUPABASE_PUBLISHABLE_KEY: z
    .string()
    .min(1, { error: 'VITE_SUPABASE_PUBLISHABLE_KEY is required (Project Settings -> API)' }),
  VITE_SITE_URL: z.url().optional(),

  VITE_AUTH_ENABLED: flag,
  VITE_LOGIN_EMAIL_AUTH_ENABLED: flag,
  VITE_REGISTER_EMAIL_AUTH_ENABLED: flag,
  VITE_FORGOT_PASSWORD_ENABLED: flag,
  VITE_RESET_PASSWORD_ENABLED: flag,
  VITE_GOOGLE_AUTH_ENABLED: flag,
});

export type Env = z.infer<typeof schema>;

const parsed = schema.safeParse(import.meta.env);

/** Parsed environment, or `null` when the app is misconfigured. */
export const env: Env | null = parsed.success ? parsed.data : null;

/** Human-readable reasons the environment failed to parse. Empty when valid. */
export const envIssues: string[] = parsed.success
  ? []
  : parsed.error.issues.map((issue) => `${issue.path.join('.') || '(root)'}: ${issue.message}`);

/** Throws with a useful message rather than failing deep inside a library. */
export function requireEnv(): Env {
  if (!env) {
    throw new Error(`Missing or invalid environment:\n  - ${envIssues.join('\n  - ')}\n\nSee .env.example.`);
  }
  return env;
}

/** Origin Supabase should redirect back to after an OAuth round-trip. */
export function siteUrl(): string {
  return env?.VITE_SITE_URL ?? window.location.origin;
}
