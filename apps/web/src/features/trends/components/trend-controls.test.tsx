import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { trendSettings } from '@/features/trends/test-fixtures';
import type { TrendSettingsRow } from '@/lib/database.types';

/**
 * The scout settings, as a form.
 *
 * Two properties are worth pinning beyond "the input renders".
 *
 * Each card saves only the fields it owns. The settings are spread over four
 * cards and there is no server tier to arbitrate, so a card that sent the whole
 * row would let a tab left open on one card silently revert a change made on
 * another.
 *
 * And a value the database would refuse must be refused here first, with the
 * reason. The bounds are CHECK constraints, which means the alternative to
 * saying so is a Postgres error message naming a constraint.
 */

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

let row: TrendSettingsRow = trendSettings();
const mutate = vi.fn(async () => row);

vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return {
    ...actual,
    trendSettingsQueryOptions: () => ({ queryKey: ['trends', 'settings'], queryFn: async () => row }),
    useSaveTrendSettings: () => ({ mutateAsync: mutate, isPending: false }),
  };
});

const { TrendScheduleCard } = await import('@/features/trends/components/trend-schedule-card');
const { TrendFiltersCard } = await import('@/features/trends/components/trend-filters-card');
const { TrendRunCostCard } = await import('@/features/trends/components/trend-run-cost-card');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mutate.mockClear();
  toasts.info.mockClear();
  row = trendSettings();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('the schedule', () => {
  it('shows the daily 06:00 UTC run the cron used to do', async () => {
    render(<TrendScheduleCard />, { wrapper });
    expect(await screen.findByText(/every day at 06:00 UTC/i)).toBeInTheDocument();
  });

  it('saves a new time without touching any other setting', async () => {
    const user = userEvent.setup();
    render(<TrendScheduleCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Hour, UTC'), '21');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith({
      schedule_enabled: true,
      schedule_hour_utc: 21,
      schedule_minute_utc: 0,
      schedule_days: [0, 1, 2, 3, 4, 5, 6],
      max_video_age_days: 30,
    });
  });

  it('refuses to save a schedule that is on but can never fire', async () => {
    // An empty day list is indistinguishable from a broken dispatcher once it
    // is running. Pausing is how "never" is meant to be said.
    const user = userEvent.setup();
    row = trendSettings({ schedule_days: [1] });
    render(<TrendScheduleCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: 'Mon' }));

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText(/switch the schedule off/i)).toBeInTheDocument();
  });

  it('says pausing leaves the manual run alone', async () => {
    render(<TrendScheduleCard />, { wrapper });
    expect(await screen.findByText(/still start one yourself/i)).toBeInTheDocument();
  });

  it('shows the recency limit that has no previous behaviour to match', async () => {
    render(<TrendScheduleCard />, { wrapper });
    expect(await screen.findByLabelText(/only count videos posted in the last/i)).toHaveValue(30);
  });
});

describe('the filters', () => {
  it('saves only the filter fields', async () => {
    const user = userEvent.setup();
    render(<TrendFiltersCard />, { wrapper });

    const views = await screen.findByLabelText('Minimum views');
    await user.clear(views);
    await user.type(views, '50000');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith({
      min_plays: 50000,
      min_engagement_rate: 0,
      min_outlier_ratio: 1.5,
      caption_blocklist: [],
      videos_per_hashtag: 30,
      ideas_per_run: 10,
    });
  });

  it('shows the engagement rate as a percentage and stores it as a fraction', async () => {
    const user = userEvent.setup();
    row = trendSettings({ min_engagement_rate: 0.04 });
    render(<TrendFiltersCard />, { wrapper });

    const field = await screen.findByLabelText('Minimum engagement rate');
    expect(field).toHaveValue(4);

    await user.clear(field);
    await user.type(field, '6');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ min_engagement_rate: 0.06 }));
  });

  it('lowercases a blocked word, because matching is case-insensitive', async () => {
    const user = userEvent.setup();
    render(<TrendFiltersCard />, { wrapper });

    await user.type(await screen.findByLabelText('Blocked caption words'), 'Course');
    await user.click(screen.getByRole('button', { name: 'Block' }));

    expect(screen.getByText('course')).toBeInTheDocument();
  });

  it('refuses a one-character blocked word and says why', async () => {
    // Whole-word matching does not save you from "a": it is a word in almost
    // every caption, and the result is a run that rejects everything.
    const user = userEvent.setup();
    render(<TrendFiltersCard />, { wrapper });

    await user.type(await screen.findByLabelText('Blocked caption words'), 'a');
    await user.click(screen.getByRole('button', { name: 'Block' }));

    expect(toasts.info).toHaveBeenCalledWith(expect.stringMatching(/at least two/i));
    expect(mutate).not.toHaveBeenCalled();
  });

  it('marks a value the database would refuse', async () => {
    const user = userEvent.setup();
    render(<TrendFiltersCard />, { wrapper });

    const ratio = await screen.findByLabelText('Minimum outlier ratio');
    await user.clear(ratio);
    await user.type(ratio, '0.5');

    expect(ratio).toHaveAttribute('aria-invalid', 'true');
  });
});

describe('run length and pacing', () => {
  it('has rotation off, which is what the pipeline did before it existed', async () => {
    render(<TrendRunCostCard />, { wrapper });
    expect(await screen.findByText(/every run scouts the whole list/i)).toBeInTheDocument();
  });

  it('turns rotation on with a usable number rather than zero', async () => {
    // Zero tags a run means "never scout anything", which is not what
    // switching a feature on should produce.
    const user = userEvent.setup();
    render(<TrendRunCostCard />, { wrapper });

    await user.click(await screen.findByRole('switch', { name: 'Rotate hashtags' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ hashtags_per_run: 6 }));
  });

  it('blocks a pacing range that is the wrong way round', async () => {
    // random.uniform(5, 2) does not raise; it quietly draws from a range
    // nobody asked for.
    const user = userEvent.setup();
    render(<TrendRunCostCard />, { wrapper });

    const shortest = await screen.findByLabelText('At least');
    await user.clear(shortest);
    await user.type(shortest, '9');

    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(screen.getByText(/has to be at least the shortest/i)).toBeInTheDocument();
  });

  it('will not offer a pacing delay below the floor that protects the account', async () => {
    const user = userEvent.setup();
    render(<TrendRunCostCard />, { wrapper });

    const shortest = await screen.findByLabelText('At least');
    await user.clear(shortest);
    await user.type(shortest, '0.2');

    expect(shortest).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });
});

describe('a viewer', () => {
  it('can read the settings but not change them', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<TrendFiltersCard />, { wrapper });

    expect(await screen.findByLabelText('Minimum views')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
    expect(screen.getByText(/only an owner can change them/i)).toBeInTheDocument();
  });
});
