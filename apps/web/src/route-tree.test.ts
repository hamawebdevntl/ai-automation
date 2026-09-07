import { createRouter } from '@tanstack/react-router';
import { describe, expect, it } from 'vitest';
import { routeTree } from '@/routeTree.gen';

function matchedRouteIds(pathname: string): string[] {
  const router = createRouter({ routeTree, context: { queryClient: {} as never, auth: {} as never } });
  const matches = router.matchRoutes({ pathname, search: {} } as never);
  return matches.map((match: { routeId: string }) => match.routeId);
}

/**
 * A detail page is a whole page, not a panel under its list. Naming the file
 * `queue.$ideaId` instead of `queue_.$ideaId` would nest it under `/_app/queue`,
 * and because the list route renders no `<Outlet />` the detail page would
 * silently never mount — a blank page rather than a build error.
 */
describe('route tree', () => {
  it('renders the idea decision page instead of the queue list, not inside it', () => {
    const ids = matchedRouteIds('/queue/e1f78031-e9a2-4e3b-8383-dc206c22834e');

    expect(ids.at(-1)).toBe('/_app/queue_/$ideaId');
    expect(ids).not.toContain('/_app/queue');
  });

  it('renders the cut review page instead of the review list, not inside it', () => {
    const ids = matchedRouteIds('/review/3f1b0c42-5d6e-4a7b-8c9d-0e1f2a3b4c5d');

    expect(ids.at(-1)).toBe('/_app/review_/$productionId');
    expect(ids).not.toContain('/_app/review');
  });

  it('still matches the list routes on their own paths', () => {
    expect(matchedRouteIds('/queue').at(-1)).toBe('/_app/queue');
    expect(matchedRouteIds('/review').at(-1)).toBe('/_app/review');
  });
});

/**
 * The queue's page lives in the URL. A pasted or stale link is the normal way
 * to arrive at a bad one, so the schema must land the reader on the queue
 * rather than on an error boundary.
 */
describe('queue paging', () => {
  function parsedSearch(url: string): Record<string, unknown> {
    const [pathname, search] = url.split('?');
    const router = createRouter({ routeTree, context: { queryClient: {} as never, auth: {} as never } });
    const matches = router.matchRoutes({
      pathname,
      search: Object.fromEntries(new URLSearchParams(search ?? '')),
      searchStr: search ? `?${search}` : '',
    } as never);
    return (matches.at(-1) as { search: Record<string, unknown> }).search;
  }

  it('reads the page from the url', () => {
    expect(parsedSearch('/queue?page=3')).toMatchObject({ page: 3 });
  });

  it('defaults to the first page', () => {
    expect(parsedSearch('/queue')).toMatchObject({ page: 1 });
  });

  it.each(['banana', '0', '-2', '1.5', ''])('falls back to the first page for ?page=%s', (value) => {
    expect(parsedSearch(`/queue?page=${value}`)).toMatchObject({ page: 1 });
  });
});
