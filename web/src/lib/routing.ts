import type { LinkProps } from '@tanstack/react-router';

/**
 * TanStack Router types `to` as the union of the app's real routes, which is
 * most of the point of using it. A path that arrives at runtime — a `?redirect=`
 * search param, a nav item read from config — has no such guarantee, so it is
 * checked here and handed back in the shape the router wants.
 *
 * Rejecting anything that does not start with a single `/` is also what stops
 * `?redirect=//evil.example` from turning the login page into an open redirect.
 */
export function toRoutePath(path: string | undefined | null, fallback: LinkProps['to']): LinkProps['to'] {
  if (!path?.startsWith('/') || path.startsWith('//')) return fallback;
  return path as LinkProps['to'];
}
