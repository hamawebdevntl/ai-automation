import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { trendRejections, trendRun, trendSettings } from '@/features/trends/test-fixtures';
import type { TrendRunRow } from '@/lib/database.types';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <span>{children}</span>,
}));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const run = trendRun;

let latest: TrendRunRow | null = null;
// Sixteen hashtags, the shape the seeded defaults produce, so the "4 of 16"
// arithmetic in the estimate is exercised rather than trivially satisfied.
let settings = trendSettings({ hashtags: Array.from({ length: 16 }, (_, i) => `tag${i}`) });
const request = vi.fn(async () => run({ status: 'requested' }));
const cancel = vi.fn(async () => run({ status: 'cancelled' }));
// Present so a test can assert it is *not* called: choosing a length for one
// run must never write to the saved settings.
const saveSettings = vi.fn(async () => settings);

vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return {
    ...actual,
    latestTrendRunQueryOptions: () => ({ queryKey: ['trends', 'latest-run'], queryFn: async () => latest }),
    trendSettingsQueryOptions: () => ({ queryKey: ['trends', 'settings'], queryFn: async () => settings }),
    useRequestTrendRun: () => ({ mutateAsync: request, isPending: false }),
    useCancelTrendRun: () => ({ mutateAsync: cancel, isPending: false }),
    useSaveTrendSettings: () => ({ mutateAsync: saveSettings, isPending: false }),
  };
});

const { GenerateIdeasButton, TrendRunBanner } = await import('@/features/trends/components/generate-ideas');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  latest = null;
  settings = trendSettings({ hashtags: Array.from({ length: 16 }, (_, i) => `tag${i}`) });
  request.mockClear();
  saveSettings.mockClear();
  request.mockImplementation(async () => run({ status: 'requested' }));
  for (const fn of Object.values(toasts)) fn.mockClear();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('asking for more ideas', () => {
  it('an owner can start a run', async () => {
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    expect(request).toHaveBeenCalledOnce();
    expect(toasts.success).toHaveBeenCalled();
  });

  it('says how long it will take, because nothing else will', async () => {
    // Quotes the estimate for the length actually chosen, rather than a fixed
    // hour. With 16 hashtags at 30 videos and 2-5s pacing that is ~56 minutes,
    // and it is an upper bound, so it is worded as one.
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /generate more ideas/i }));

    const [, options] = toasts.success.mock.calls[0] as [string, { description: string }];
    expect(options.description).toMatch(/up to ~56 min/);
  });

  it('quotes the shorter time when a shorter search was chosen', async () => {
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));
    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    const [, options] = toasts.success.mock.calls[0] as [string, { description: string }];
    expect(options.description).toMatch(/up to ~14 min/);
  });

  it('a viewer cannot', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<GenerateIdeasButton />, { wrapper });

    expect(screen.getByRole('button', { name: /generate more ideas/i })).toBeDisabled();
  });

  it('is refused while a run is already going', async () => {
    latest = run({ status: 'running' });
    render(<GenerateIdeasButton />, { wrapper });

    // One at a time: a second hour-long browser session against a platform
    // that is looking for exactly that is the thing to avoid.
    expect(await screen.findByRole('button', { name: /looking for ideas/i })).toBeDisabled();
  });

  it('treats a concurrent request as information, not failure', async () => {
    // A plain object, not an Error: this is the shape supabase-js actually
    // rejects with, and the reason the message used to be thrown away.
    request.mockRejectedValueOnce({ code: '55006', message: 'A trend run is already in progress' });
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    expect(toasts.info).toHaveBeenCalled();
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it('still reports a real failure', async () => {
    request.mockRejectedValueOnce(new Error('network is down'));
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    expect(toasts.error).toHaveBeenCalled();
  });

  it('says why, when what refused it said why', async () => {
    // The regression this guards: the button reported "Could not start a
    // trend run" and nothing else while PostgREST was explaining, in full,
    // that the migration adding `request_trend_run` had never been pushed.
    request.mockRejectedValueOnce({
      code: 'PGRST202',
      message: 'Could not find the function public.request_trend_run without parameters in the schema cache',
    });
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    const [title, options] = toasts.error.mock.calls[0] as [string, { description: string }];
    expect(title).toMatch(/could not start a trend run/i);
    expect(options.description).toMatch(/has not been applied/i);
    expect(options.description).toContain('request_trend_run');
  });
});

describe('what the last run did', () => {
  it('says nothing when there has never been one', () => {
    const { container } = render(<TrendRunBanner />, { wrapper });
    expect(container).toBeEmptyDOMElement();
  });

  it('says nothing after a run that produced ideas', async () => {
    latest = run({ status: 'succeeded', inserted: 8 });
    const { container } = render(<TrendRunBanner />, { wrapper });

    // The ideas are the result. A bar above them announcing it is clutter.
    await Promise.resolve();
    expect(container.textContent).toBe('');
  });

  it('explains an hour of apparent silence', async () => {
    latest = run({ status: 'running' });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/scouting for ideas/i)).toBeInTheDocument();
  });

  it('reads a fresh request as a slow start, not a problem', async () => {
    latest = run({ status: 'requested', started_at: null, requested_at: new Date().toISOString() });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/scouting for ideas/i)).toBeInTheDocument();
    expect(screen.getByText(/usually under a minute/i)).toBeInTheDocument();
  });

  it('stops calling a request that nobody claimed a slow start', async () => {
    // The dispatcher runs every minute. A row still unclaimed after several of
    // them is the shape of an unapplied terraform, not of a busy queue -- and
    // saying "usually under a minute" for the twentieth minute running makes a
    // broken deployment look like a working one.
    const twentyMinutesAgo = new Date(Date.now() - 20 * 60_000).toISOString();
    latest = run({ status: 'requested', started_at: null, requested_at: twentyMinutesAgo });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/nothing has picked this run up/i)).toBeInTheDocument();
    // And names the one thing to check. The write-off is the *same* sweeper,
    // so promising automatic recovery here would be promising it from the
    // component that has just reported that sweeper missing.
    expect(screen.getByText(/dispatcher is also what writes an unclaimed request off/i)).toBeInTheDocument();
  });

  it('surfaces why a run failed', async () => {
    latest = run({ status: 'failed', error: 'ecs could not place the task', inserted: null });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/did not finish/i)).toBeInTheDocument();
    expect(screen.getByText(/could not place the task/i)).toBeInTheDocument();
  });

  it('says so plainly when a run has no breakdown to show', async () => {
    // A row from before the per-stage counts existed. The honest answer is
    // that we cannot tell why, not a guess dressed up as a diagnosis.
    latest = run({ status: 'succeeded', inserted: 0, drafted: 0, signals: 3, rejections: null });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/no breakdown was recorded/i)).toBeInTheDocument();
  });
});

/**
 * An empty queue has three unrelated causes, and before the breakdown existed
 * they all arrived as the same sentence. These are the three, told apart.
 */
describe('why a run added nothing', () => {
  it('names the filter that rejected the most', async () => {
    latest = run({
      status: 'succeeded',
      inserted: 0,
      drafted: 0,
      signals: 0,
      rejections: trendRejections({ seen: 480, surfaced: 0, inserted: 0 }),
    });
    render(<TrendRunBanner />, { wrapper });

    // 310 of the 480 went to the recency limit, which is the one to loosen.
    // Named twice on purpose: once in the verdict, once as the funnel row.
    expect(await screen.findAllByText(/older than your recency limit/i)).toHaveLength(2);
    expect(screen.getByText(/loosen the one responsible/i)).toBeInTheDocument();
  });

  it('separates a refused scrape from a strict filter', async () => {
    // The distinction the whole report exists for. Filters can only reduce
    // `seen`, so zero is never a filter.
    latest = run({
      status: 'succeeded',
      inserted: 0,
      rejections: trendRejections({
        seen: 0,
        surfaced: 0,
        inserted: 0,
        stages: trendRejections().stages.map((s) => ({ ...s, dropped: 0 })),
        failed_hashtags: [{ hashtag: 'crm', error: 'EmptyResponseException' }],
      }),
    });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/the scrape was refused/i)).toBeInTheDocument();
    expect(screen.getByText(/not your filters/i)).toBeInTheDocument();
  });

  it('says when the ideas were all repeats rather than all weak', async () => {
    latest = run({
      status: 'succeeded',
      inserted: 0,
      drafted: 4,
      suppressed: 4,
      rejections: trendRejections({
        seen: 300,
        surfaced: 12,
        drafted: 4,
        inserted: 0,
        stages: trendRejections().stages.map((s) =>
          s.key === 'duplicate' ? { ...s, dropped: 4 } : { ...s, dropped: 0 },
        ),
      }),
    });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/everything drafted was a repeat/i)).toBeInTheDocument();
  });

  it('shows every stage, including the ones that rejected nothing', async () => {
    // A report listing only what fired invites loosening whichever filter is
    // named. The zeroes are how the owner sees it was not that one.
    latest = run({ status: 'succeeded', inserted: 0, rejections: trendRejections({ surfaced: 0 }) });
    render(<TrendRunBanner />, { wrapper });

    await screen.findAllByText(/older than your recency limit/i);
    expect(screen.getByText(/caption contained a blocked word/i)).toBeInTheDocument();
    expect(screen.getByText(/under your minimum engagement rate/i)).toBeInTheDocument();
    // Its count reads as a plain zero rather than a "−0".
    expect(screen.getByText(/author's own history could not be read/i)).toBeInTheDocument();
  });
});

/**
 * Stopping a run.
 *
 * The state this exists for is the one where everything else has failed: at
 * most one run may be in flight, so a row nothing will ever claim does not
 * hold up one run -- it holds up every future run, and the only thing that
 * could previously clear it was the dispatcher that is not running. So the
 * button has to be offered in that state above all, and it has to work with a
 * single database call.
 */
describe('stopping a run', () => {
  it('is offered while a run is going', async () => {
    latest = run({ status: 'running' });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByRole('button', { name: /stop this run/i })).toBeInTheDocument();
  });

  it('is offered on the stuck request nothing has claimed', async () => {
    // The case that motivated the button. Without it the queue is wedged
    // until somebody opens the AWS console.
    latest = run({
      status: 'requested',
      requested_at: new Date(Date.now() - 60 * 60_000).toISOString(),
      started_at: null,
    });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/nothing has picked this run up/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /stop this run/i })).toBeInTheDocument();
  });

  it('cancels by id, so a run that started since does not get stopped instead', async () => {
    const user = userEvent.setup();
    latest = run({ id: 'run-42', status: 'running' });
    render(<TrendRunBanner />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /stop this run/i }));

    expect(cancel).toHaveBeenCalledWith('run-42');
  });

  it('treats an already-finished run as good news, not an error', async () => {
    // The page was showing a run that ended by itself between load and click.
    // That is the outcome the owner wanted, described badly if it arrives red.
    const user = userEvent.setup();
    cancel.mockRejectedValueOnce(Object.assign(new Error('That run is no longer in flight'), { code: 'P0002' }));
    latest = run({ status: 'running' });
    render(<TrendRunBanner />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /stop this run/i }));

    expect(toasts.info).toHaveBeenCalledWith('That run had already finished', expect.anything());
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it('says what the run had already found before it was stopped', async () => {
    // Written back by the runner into the diagnostic columns only -- the
    // status stays the owner's. It is what separates "I stopped it too early"
    // from "it was getting nowhere anyway".
    latest = run({ status: 'cancelled', inserted: 0, rejections: trendRejections({ seen: 312 }) });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/looked at 312 videos/i)).toBeInTheDocument();
  });

  it('reports a stopped run quietly rather than as a failure', async () => {
    // A red "did not finish" every time the button is used is how the banner
    // that reports real breakage stops being read.
    latest = run({ status: 'cancelled', inserted: 0, rejections: null });
    render(<TrendRunBanner />, { wrapper });

    expect(await screen.findByText(/you stopped that run/i)).toBeInTheDocument();
    expect(screen.queryByText(/did not finish/i)).not.toBeInTheDocument();
  });

  it('does not offer the button to a viewer', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    latest = run({ status: 'running' });
    render(<TrendRunBanner />, { wrapper });

    await screen.findByText(/scouting for ideas/i);
    expect(screen.queryByRole('button', { name: /stop this run/i })).not.toBeInTheDocument();
  });
});

/**
 * Choosing how long the next run should take, at the moment of starting it.
 *
 * The settings page decides how the *scheduled* run behaves, every day,
 * unattended. This decides one run somebody is about to press a button for,
 * and their reason has nothing to do with tomorrow morning -- so the choice
 * must ride on the request and leave the saved settings alone.
 */
describe('search length', () => {
  it('offers the length above the button before a run starts', async () => {
    render(<GenerateIdeasButton />, { wrapper });

    expect(await screen.findByRole('radiogroup', { name: /search length/i })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Quick' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Deep' })).toBeInTheDocument();
  });

  it('states the exact hashtags and videos, and the idea figure as a ceiling', async () => {
    // Hashtags and videos are arithmetic on the settings, so they are exact.
    // The idea number is the cap and must never read as a forecast.
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));

    expect(screen.getByText(/4 of 16 hashtags/i)).toBeInTheDocument();
    expect(screen.getByText(/~120 videos/i)).toBeInTheDocument(); // 4 tags x 30 videos
    expect(screen.getByText(/up to 10 ideas/i)).toBeInTheDocument();
    expect(screen.getByText(/not a forecast/i)).toBeInTheDocument();
  });

  it('changes the estimate when a longer search is chosen', async () => {
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));
    expect(screen.getByText(/~120 videos/i)).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: 'Deep' }));
    expect(screen.getByText(/all 16 hashtags/i)).toBeInTheDocument();
    expect(screen.getByText(/~480 videos/i)).toBeInTheDocument();
  });

  it('says how long the rotation takes to cover the whole list', async () => {
    // A shorter run narrows which rooms are looked at, not just the clock.
    // Saying so is what stops "Quick" reading as a free win.
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));

    expect(screen.getByText(/covers the whole list every 4 runs/i)).toBeInTheDocument();
  });

  it('sends the chosen length with the request', async () => {
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));
    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    expect(request).toHaveBeenCalledWith(
      expect.objectContaining({ hashtagsPerRun: 4, budgetMinutes: expect.any(Number) }),
    );
  });

  it('enforces exactly the ceiling it displayed', async () => {
    // The control said "stops by ~14 min", so the row must carry 14. An
    // earlier version padded this by 50% for headroom, which meant choosing a
    // 14-minute search and getting a 21-minute cap -- the kind of small
    // dishonesty that makes every other number on this control suspect.
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));
    expect(screen.getByText(/stops by ~14 min/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    const [input] = request.mock.calls[0] as unknown as [{ budgetMinutes: number }];
    expect(input.budgetMinutes).toBe(14); // 4 tags x 30 videos x 2 x 3.5s
  });

  it('does not touch the saved settings', async () => {
    // The whole point of the per-run override: a quick look now must not make
    // tomorrow's 06:00 scheduled run quick too.
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('radio', { name: 'Quick' }));
    await user.click(screen.getByRole('button', { name: /generate more ideas/i }));

    expect(saveSettings).not.toHaveBeenCalled();
  });

  it('sends nothing when no length is chosen, so the saved settings apply', async () => {
    const user = userEvent.setup();
    render(<GenerateIdeasButton />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /generate more ideas/i }));

    expect(request).toHaveBeenCalledWith({ hashtagsPerRun: null, budgetMinutes: null });
  });

  it('is hidden while a run is already going', async () => {
    // There is nothing to configure about a run that has already begun.
    latest = run({ status: 'running' });
    render(<GenerateIdeasButton />, { wrapper });

    await screen.findByRole('button', { name: /looking for ideas/i });
    expect(screen.queryByRole('radiogroup', { name: /search length/i })).not.toBeInTheDocument();
  });

  it('is hidden from a viewer, who cannot start one', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<GenerateIdeasButton />, { wrapper });

    await screen.findByText(/only an owner can start/i);
    expect(screen.queryByRole('radiogroup', { name: /search length/i })).not.toBeInTheDocument();
  });
});
