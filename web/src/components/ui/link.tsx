import { Link as RouterLink, useLocation } from '@tanstack/react-router';
import type * as React from 'react';

/**
 * Drop-in replacement for `next/link`, so components ported from the previous
 * Next.js app keep working unchanged.
 *
 * Internal hrefs are handed to TanStack Router (client-side navigation);
 * anything external, or a bare hash/mailto/tel, falls through to a plain
 * anchor. TanStack Router types `to` as a union of the app's real routes, so a
 * runtime string needs one cast — deliberately confined to this file. New code
 * should import `Link` from `@tanstack/react-router` directly and get the
 * full typed-route checking.
 */
export type LinkProps = Omit<React.ComponentPropsWithoutRef<'a'>, 'href'> & {
  href: string;
  replace?: boolean;
  prefetch?: boolean | null;
};

const isExternal = (href: string) =>
  /^([a-z][a-z0-9+.-]*:)?\/\//i.test(href) || /^(mailto:|tel:|sms:)/i.test(href) || href.startsWith('#');

export function Link({ href, replace, prefetch, children, ...props }: LinkProps) {
  if (isExternal(href)) {
    const external = /^([a-z][a-z0-9+.-]*:)?\/\//i.test(href);
    return (
      <a href={href} rel={external ? 'noopener noreferrer' : undefined} {...props}>
        {children}
      </a>
    );
  }

  return (
    <RouterLink to={href as never} replace={replace} preload={prefetch === false ? false : undefined} {...props}>
      {children}
    </RouterLink>
  );
}

/** `usePathname()` equivalent for ported components. */
export function usePathname(): string {
  return useLocation({ select: (location) => location.pathname });
}

export default Link;
