import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { queueKeys } from '@/features/queue/api';
import { trendKeys, useRefreshQueueWhenRunEnds } from '@/features/trends/api';
import { trendRun } from '@/features/trends/test-fixtures';
import type { TrendRunRow } from '@/lib/database.types';

/**
 * A run finishing is the only thing that adds to the queue with nobody
 * touching the page, and for an hour-long run the owner may well be watching
 * it happen. Being told the scouting succeeded while looking at an unchanged
 * list is the same ambiguity the banner exists to remove.
 *
 * The polled row is seeded into the cache rather than mocked at the module
 * boundary: the hook reads the query options from inside its own module, so a
 * mocked export would not be the one it calls. `staleTime: Infinity` keeps the
 * seeded value from being refetched out from under the test.
 */

function row(over: Partial<TrendRunRow> = {}): TrendRunRow {
  return trendRun({
    status: 'running',
    finished_at: null,
    signals: null,
    drafted: null,
    inserted: null,
    suppressed: null,
    scouted: null,
    hashtags_scouted: null,
    ...over,
  });
}

function Probe() {
  useRefreshQueueWhenRunEnds();
  return null;
}

let client: QueryClient;
let invalidate: ReturnType<typeof vi.spyOn>;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** What the fifteen-second poll does when the row has changed. */
async function poll(next: TrendRunRow) {
  await act(async () => {
    client.setQueryData(trendKeys.latestRun(), next);
  });
}

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Number.POSITIVE_INFINITY } },
  });
  invalidate = vi.spyOn(client, 'invalidateQueries');
});

describe('refreshing the queue when a run ends', () => {
  it('invalidates the queue when a watched run finishes', async () => {
    client.setQueryData(trendKeys.latestRun(), row({ status: 'running' }));
    render(<Probe />, { wrapper });
    expect(invalidate).not.toHaveBeenCalled();

    await poll(row({ status: 'succeeded', finished_at: '2026-09-06T11:00:00Z', inserted: 8 }));

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queueKeys.all }));
  });

  it('does it for a failed run too, since expiry writes the row off', async () => {
    client.setQueryData(trendKeys.latestRun(), row({ status: 'requested', started_at: null }));
    render(<Probe />, { wrapper });

    await poll(row({ status: 'failed', error: 'ecs could not place the task' }));

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queueKeys.all }));
  });

  it('does not refetch the queue on every poll of an already-finished run', async () => {
    // The trigger is the edge, not the state. Invalidating on the state would
    // put the queue on a fifteen-second refetch for the rest of the session.
    client.setQueryData(trendKeys.latestRun(), row({ status: 'succeeded', inserted: 8 }));
    render(<Probe />, { wrapper });

    await poll(row({ status: 'succeeded', inserted: 8 }));
    await poll(row({ status: 'succeeded', inserted: 8 }));

    expect(invalidate).not.toHaveBeenCalled();
  });

  it('stays quiet while a run is still going', async () => {
    client.setQueryData(trendKeys.latestRun(), row({ status: 'requested', started_at: null }));
    render(<Probe />, { wrapper });

    await poll(row({ status: 'running' }));

    // Nothing has been inserted yet: the ideas land at the end of the run.
    expect(invalidate).not.toHaveBeenCalled();
  });
});
