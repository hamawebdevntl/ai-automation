import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { trendSearchRun } from '@/features/trends/test-fixtures';
import type { IdeaRow, TrendRunRow } from '@/lib/database.types';
import { makeIdea } from '@/test/factories';

vi.mock('@/features/auth/use-owner', () => ({
  useOwner: () => ({ isOwner: true, isLoading: false, role: 'owner', displayName: null }),
}));

vi.mock('@tanstack/react-router', () => ({
  // Enough of `Link` to assert on where the way back points.
  Link: ({ children, to, search }: { children: ReactNode; to: string; search?: Record<string, unknown> }) => {
    const query = new URLSearchParams(Object.entries(search ?? {}).map(([k, v]) => [k, String(v)])).toString();
    return <a href={query ? `${to}?${query}` : to}>{children}</a>;
  },
}));

let ideas: IdeaRow[] = [];
vi.mock('@/features/queue/api', () => ({
  searchIdeasQueryOptions: (runId: string) => ({
    queryKey: ['queue', 'search-ideas', runId],
    queryFn: async () => ideas,
  }),
  useRejectIdea: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

let selected: TrendRunRow | null = null;
let isRunPending = false;
vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return {
    ...actual,
    useSelectedTrendRun: () => ({ run: selected, isPending: isRunPending, error: null }),
  };
});

const { SearchResults } = await import('@/features/queue/components/search-results');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  ideas = [];
  selected = null;
  isRunPending = false;
});

describe('the results of one search', () => {
  it('names the search, says how it was understood, and counts what is left to decide', async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 8 });
    ideas = [makeIdea({ id: 'a', relevance: 90 }), makeIdea({ id: 'b', relevance: 70 }), makeIdea({ id: 'c' })];
    render(<SearchResults runId="search-1" />, { wrapper });

    expect(await screen.findByText(/ideas for “i want to start a small home fitness brand/i)).toBeInTheDocument();
    expect(screen.getByText(/understood as/i)).toBeInTheDocument();
    expect(await screen.findByText(/3 of 8 still to decide/i)).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /back to the full queue/i })[0]).toHaveAttribute(
      'href',
      '/queue?page=1',
    );
  });

  it('keeps the order the query returned and shows no pagination', async () => {
    // Best fit first is decided in SQL. The component's job is not to re-sort.
    selected = trendSearchRun({ status: 'succeeded', inserted: 3 });
    ideas = [
      makeIdea({ id: 'a', title: 'First', relevance: 90 }),
      makeIdea({ id: 'b', title: 'Second', relevance: 70 }),
      makeIdea({ id: 'c', title: 'Third', relevance: null }),
    ];
    render(<SearchResults runId="search-1" />, { wrapper });

    await screen.findByText('First');
    expect(screen.getAllByText(/^(First|Second|Third)$/).map((el) => el.textContent)).toEqual([
      'First',
      'Second',
      'Third',
    ]);
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('says it is still searching rather than that nothing is waiting', async () => {
    selected = trendSearchRun({ status: 'running', inserted: null });
    render(<SearchResults runId="search-1" />, { wrapper });

    expect(await screen.findByText(/^searching…$/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing waiting/i)).not.toBeInTheDocument();
  });

  it('says when every idea from the search has been decided', async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 8 });
    render(<SearchResults runId="search-1" />, { wrapper });

    expect(await screen.findByText(/all decided/i)).toBeInTheDocument();
    expect(screen.getByText(/all 8 ideas from this search/i)).toBeInTheDocument();
  });

  it('points at the panel when nothing came back', async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 0 });
    render(<SearchResults runId="search-1" />, { wrapper });

    expect(await screen.findByText(/nothing came back/i)).toBeInTheDocument();
  });

  it('says when the search is gone', async () => {
    selected = null;
    render(<SearchResults runId="search-1" />, { wrapper });

    expect(await screen.findByText(/not here any more/i)).toBeInTheDocument();
  });
});
