import { useEffect } from 'react';

/**
 * Tidies the OAuth query string out of the address bar.
 *
 * The Next.js version of this component forwarded `?code=` to an
 * `/api/callback` route handler. There is no server here to run one:
 * supabase-js is configured with `detectSessionInUrl`, so it performs the PKCE
 * exchange itself as soon as it loads. All that is left worth doing is
 * removing the spent `code` / `error` parameters, so a reload or a shared link
 * does not carry a consumed authorization code around.
 *
 * Rendering it is optional — the sign-in works without it.
 */
export function OAuthCallbackRedirect() {
  useEffect(() => {
    const url = new URL(window.location.href);
    const spent = ['code', 'error', 'error_code', 'error_description', 'provider_token', 'state'];
    const present = spent.filter((key) => url.searchParams.has(key));
    if (present.length === 0) return;

    for (const key of present) url.searchParams.delete(key);
    window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`);
  }, []);

  return null;
}
