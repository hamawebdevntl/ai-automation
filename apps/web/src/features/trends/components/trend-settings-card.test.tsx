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

const { TrendSettingsCard } = await import('@/features/trends/components/trend-settings-card');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mutate.mockClear();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('the brief', () => {
  it('shows what is stored', async () => {
    render(<TrendSettingsCard />, { wrapper });
    expect(await screen.findByLabelText(/what you do, who you speak to/i)).toHaveValue(row.niche_brief);
  });

  it('saves only the brief, leaving the source lists to their own card', async () => {
    // Two cards editing one field is how a tab left open on one silently
    // reverts the other, and there is no server tier to arbitrate.
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    await user.type(await screen.findByLabelText(/what you do, who you speak to/i), ' We also do integrations.');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith({
      niche_brief: `${row.niche_brief} We also do integrations.`,
    });
  });

  it('says why the brief matters more now', async () => {
    // Google Trends says a subject is live and nothing about how to open a
    // video, so the hook comes from here or from nowhere.
    render(<TrendSettingsCard />, { wrapper });
    expect(await screen.findByText(/the hook comes from what you write here/i)).toBeInTheDocument();
  });

  it('disables save until something changes', async () => {
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    expect(await screen.findByRole('button', { name: 'Save' })).toBeDisabled();
    await user.type(screen.getByLabelText(/what you do, who you speak to/i), 'x');
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });
});

describe('a viewer cannot edit', () => {
  beforeEach(() => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
  });

  it('shows the brief but offers no way to change it', async () => {
    render(<TrendSettingsCard />, { wrapper });

    expect(await screen.findByLabelText(/what you do, who you speak to/i)).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('says the rule is enforced in the database, not here', async () => {
    render(<TrendSettingsCard />, { wrapper });
    expect(await screen.findByText(/row-level security/i)).toBeInTheDocument();
  });
});
