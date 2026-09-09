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

  // The clip gate is a signed-in page like the other two: under `_app`, so it
  // inherits the auth guard rather than needing one of its own.
  it('puts the clip gate behind the app layout', () => {
    const ids = matchedRouteIds('/clips');

    expect(ids.at(-1)).toBe('/_app/clips');
    expect(ids).toContain('/_app');
  });
});

/**
 * The queue's page lives in the URL. A pasted or stale link is the normal way
 * to arrive at a bad one, so the schema must land the reader on the queue
 * rather than on an error boundary.
 */
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

describe('queue paging', () => {
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

/**
 * A selected search is a trend run id in the URL, for the same reasons the
 * page is. A mangled one shows the whole queue rather than an error boundary.
 */
describe('queue search param', () => {
  const id = 'e1f78031-e9a2-4e3b-8383-dc206c22834e';

  it('reads a run id from the url, alongside the page', () => {
    expect(parsedSearch(`/queue?search=${id}`)).toMatchObject({ search: id, page: 1 });
    expect(parsedSearch(`/queue?page=2&search=${id}`)).toMatchObject({ search: id, page: 2 });
  });

  it('is absent by default', () => {
    expect(parsedSearch('/queue').search).toBeUndefined();
  });

  it.each(['banana', '', '123'])('drops a mangled ?search=%s and keeps the queue', (value) => {
    const search = parsedSearch(`/queue?search=${value}`);
    expect(search.search).toBeUndefined();
    expect(search).toMatchObject({ page: 1 });
  });
});
