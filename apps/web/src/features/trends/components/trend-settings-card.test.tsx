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

describe('defaults are editable, not fixed', () => {
  it('shows the seeded hashtags as removable', async () => {
    render(<TrendSettingsCard />, { wrapper });
    expect(await screen.findByText('#aiautomation')).toBeInTheDocument();
    expect(screen.getByLabelText('Remove #webdesign')).toBeInTheDocument();
  });

  it('removes a tag and saves the list without it', async () => {
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    await user.click(await screen.findByLabelText('Remove #webdesign'));
    expect(screen.queryByText('#webdesign')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(mutate).toHaveBeenCalledWith({
      niche_brief: row.niche_brief,
      hashtags: ['aiautomation', 'devops'],
    });
  });

  it('adds a typed tag, normalising it on the way in', async () => {
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), '#No Code{enter}');
    expect(screen.getByText('#nocode')).toBeInTheDocument();
  });

  it('refuses a duplicate rather than scouting the same feed twice', async () => {
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), 'AIAutomation{enter}');
    expect(screen.getAllByText('#aiautomation')).toHaveLength(1);
  });
});

describe('save is only offered when there is something to save', () => {
  it('disables save until something changes', async () => {
    const user = userEvent.setup();
    render(<TrendSettingsCard />, { wrapper });

    expect(await screen.findByRole('button', { name: 'Save' })).toBeDisabled();
    await user.click(screen.getByLabelText('Remove #devops'));
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled();
  });
});

describe('a viewer cannot edit', () => {
  beforeEach(() => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
  });

  it('shows the tags but offers no way to change them', async () => {
    render(<TrendSettingsCard />, { wrapper });

    expect(await screen.findByText('#aiautomation')).toBeInTheDocument();
    expect(screen.queryByLabelText('Remove #aiautomation')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Add a hashtag')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('says the rule is enforced in the database, not here', async () => {
    render(<TrendSettingsCard />, { wrapper });
    expect(await screen.findByText(/row-level security/i)).toBeInTheDocument();
  });
});
