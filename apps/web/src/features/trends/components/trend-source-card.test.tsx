import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { trendSettings } from '@/features/trends/test-fixtures';
import type { TrendSettingsRow } from '@/lib/database.types';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const row: TrendSettingsRow = trendSettings();

const mutate = vi.fn(async () => row);

// The card is tested against the data layer's shape, not against Supabase.
vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return {
    ...actual,
    trendSettingsQueryOptions: () => ({ queryKey: ['trends', 'settings'], queryFn: async () => row }),
    useSaveTrendSettings: () => ({ mutateAsync: mutate, isPending: false }),
  };
});

const { TrendSourceCard } = await import('@/features/trends/components/trend-source-card');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mutate.mockClear();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('google trends, the default', () => {
  it('edits search terms, not hashtags', async () => {
    render(<TrendSourceCard />, { wrapper });
    expect(await screen.findByText('invoice software')).toBeInTheDocument();
    expect(screen.queryByText('#aiautomation')).not.toBeInTheDocument();
  });

  it('keeps a term as a phrase rather than mangling it into a hashtag', async () => {
    // "excel alternative" is two words on purpose. Stripping the space would
    // change what is being measured.
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'Excel  Alternative{enter}');
    expect(screen.getByText('excel alternative')).toBeInTheDocument();
  });

  it('refuses a duplicate rather than paying for the same request twice', async () => {
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'Invoice Software{enter}');
    expect(screen.getAllByText('invoice software')).toHaveLength(1);
  });

  it('saves the terms without touching the hashtags', async () => {
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.click(await screen.findByLabelText('Remove crm software'));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        trend_source: 'google_trends',
        trend_keywords: ['invoice software', 'bookkeeping software'],
        hashtags: row.hashtags,
      }),
    );
  });

  it('offers a region, because interest is normalised within one', async () => {
    render(<TrendSourceCard />, { wrapper });
    expect(await screen.findByLabelText('Region')).toBeInTheDocument();
  });

  it('warns against writing in our own vocabulary', async () => {
    render(<TrendSourceCard />, { wrapper });
    expect(await screen.findByText(/what a buyer would type/i)).toBeInTheDocument();
  });
});

describe('switching to tiktok', () => {
  it('swaps the list to hashtags', async () => {
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'tiktok');

    expect(screen.getByText('#aiautomation')).toBeInTheDocument();
    expect(screen.queryByText('invoice software')).not.toBeInTheDocument();
  });

  it('normalises a typed hashtag on the way in', async () => {
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'tiktok');
    await user.type(screen.getByPlaceholderText('Add a hashtag'), '#No Code{enter}');

    expect(screen.getByText('#nocode')).toBeInTheDocument();
  });

  it('says plainly that it does not currently work', async () => {
    // Offering a broken source without saying so would send someone hunting
    // through filters for an empty queue the filters did not cause.
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'tiktok');

    expect(screen.getByText(/not currently working/i)).toBeInTheDocument();
  });

  it('hides the region, which means nothing to a video source', async () => {
    const user = userEvent.setup();
    render(<TrendSourceCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'tiktok');

    expect(screen.queryByLabelText('Region')).not.toBeInTheDocument();
  });
});

describe('a viewer cannot edit', () => {
  beforeEach(() => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
  });

  it('shows the terms but offers no way to change them', async () => {
    render(<TrendSourceCard />, { wrapper });

    expect(await screen.findByText('invoice software')).toBeInTheDocument();
    expect(screen.queryByLabelText('Remove invoice software')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });
});
