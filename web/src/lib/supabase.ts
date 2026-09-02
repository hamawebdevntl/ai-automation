import { createClient as createSupabaseClient, type SupabaseClient } from '@supabase/supabase-js';
import type { Database } from '@/lib/database.types';
import { requireEnv } from './env';

/**
 * Single browser Supabase client for the whole app.
 *
 * There is no server half to this application, so there is no service-role
 * client and no cookie-based session handling: the session lives in
 * localStorage and every query is subject to row-level security.
 *
 * `detectSessionInUrl` (on by default) completes the OAuth PKCE exchange when
 * the provider redirects back, which is why this SPA needs no `/api/callback`
 * route the way the Next.js app did.
 */
export type AppSupabaseClient = SupabaseClient<Database>;

let client: AppSupabaseClient | null = null;

export function createClient(): AppSupabaseClient {
  if (!client) {
    const env = requireEnv();
    client = createSupabaseClient<Database>(env.VITE_SUPABASE_URL, env.VITE_SUPABASE_PUBLISHABLE_KEY, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
        flowType: 'pkce',
      },
    });
  }
  return client;
}

/**
 * Convenience accessor for call sites that just want to run a query.
 *
 * The indirection buys lazy construction: `createClient()` throws when the
 * environment is missing, and that has to happen after React is up so the
 * configuration screen can render instead of a blank page.
 *
 * Methods are bound to the real client rather than handed back loose, so `this`
 * is never the proxy. supabase-js uses no `#private` fields today, but a
 * release that started to would break every call through here in a way that is
 * miserable to debug.
 */
export const supabase = new Proxy({} as AppSupabaseClient, {
  get(_target, property) {
    const client = createClient();
    const value = Reflect.get(client, property, client);
    return typeof value === 'function' ? value.bind(client) : value;
  },
});
