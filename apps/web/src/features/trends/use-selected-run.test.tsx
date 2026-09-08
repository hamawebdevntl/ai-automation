import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { trendKeys, useSelectedTrendRun } from '@/features/trends/api';
import { trendRun, trendSearchRun } from '@/features/trends/test-fixtures';

/**
 * Seeded into the cache rather than mocked at the module boundary, for the
 * reason run-refresh.test.tsx gives: the hook calls its own module's query
 * options, so a mocked export would not be the one it reads.
 */
function Probe({ runId }: { runId: string | null }) {
  const { run, isPending } = useSelectedTrendRun(runId);
  return <output>{isPending ? 'pending' : (run?.id ?? 'none')}</output>;
}

let client: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
});

describe('the selected run', () => {
  it('is nothing when nothing is selected', () => {
    render(<Probe runId={null} />, { wrapper });
    expect(screen.getByRole('status')).toHaveTextContent('none');
  });

  it('is the polled latest row when the ids match, without a second fetch', () => {
    // At most one run is in flight, so a selected run that is still going is
    // always the latest one -- reading it from the poll is what keeps the
    // banner and the list moving together.
    client.setQueryData(trendKeys.latestRun(), trendSearchRun({ id: 'search-1', status: 'running' }));
    render(<Probe runId="search-1" />, { wrapper });

    expect(screen.getByRole('status')).toHaveTextContent('search-1');
    expect(client.getQueryState(trendKeys.run('search-1'))?.fetchStatus ?? 'idle').toBe('idle');
  });

  it('is the row held by id otherwise', async () => {
    client.setQueryData(trendKeys.latestRun(), trendRun({ id: 'run-9', status: 'succeeded' }));
    client.setQueryData(trendKeys.run('search-1'), trendSearchRun({ id: 'search-1', status: 'succeeded' }));
    render(<Probe runId="search-1" />, { wrapper });

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('search-1'));
  });
});
