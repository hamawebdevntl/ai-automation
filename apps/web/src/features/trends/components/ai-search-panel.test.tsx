import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import {
  trendInterpretation,
  trendRejections,
  trendRun,
  trendSearchRun,
  trendSettings,
} from '@/features/trends/test-fixtures';
import type { TrendRunRow } from '@/lib/database.types';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const navigate = vi.fn(async () => {});
vi.mock('@tanstack/react-router', () => ({
  // Enough of `Link` to assert on where a row points and whether it is current.
  Link: ({
    children,
    to,
    search,
    ...rest
  }: { children: ReactNode; to: string; search?: Record<string, unknown> } & Record<string, unknown>) => {
    const query = new URLSearchParams(Object.entries(search ?? {}).map(([k, v]) => [k, String(v)])).toString();
    return (
      <a href={query ? `${to}?${query}` : to} {...rest}>
        {children}
      </a>
    );
  },
  useNavigate: () => navigate,
}));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

let latest: TrendRunRow | null = null;
let selected: TrendRunRow | null = null;
let recent: TrendRunRow[] = [];
const settings = trendSettings();
const request = vi.fn(async (): Promise<TrendRunRow> => trendSearchRun({ id: 'search-9', status: 'requested' }));
const cancel = vi.fn(async () => trendRun({ status: 'cancelled' }));

vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return {
    ...actual,
    latestTrendRunQueryOptions: () => ({ queryKey: ['trends', 'latest-run'], queryFn: async () => latest }),
    trendSettingsQueryOptions: () => ({ queryKey: ['trends', 'settings'], queryFn: async () => settings }),
    recentSearchesQueryOptions: () => ({ queryKey: ['trends', 'recent-searches'], queryFn: async () => recent }),
    // Mocked directly: the hook's own `useQuery` calls would not see the
    // mocked query options above (see run-refresh.test.tsx for why).
    useSelectedTrendRun: () => ({ run: selected, isPending: false, error: null }),
    useRequestTrendRun: () => ({ mutateAsync: request, isPending: false }),
    useCancelTrendRun: () => ({ mutateAsync: cancel, isPending: false }),
  };
});

const { AiSearchPanel } = await import('@/features/trends/components/ai-search-panel');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const PROMPT = 'I want to start a small home fitness brand for busy parents';

function box() {
  return screen.getByRole('textbox', { name: /what are you working on/i });
}

function searchButton() {
  return screen.getByRole('button', { name: /^search$/i });
}

beforeEach(() => {
  latest = null;
  selected = null;
  recent = [];
  request.mockClear();
  navigate.mockClear();
  request.mockImplementation(async () => trendSearchRun({ id: 'search-9', status: 'requested' }));
  for (const fn of Object.values(toasts)) fn.mockClear();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('before anyone has searched', () => {
  it('invites a description and offers examples that only fill the box', async () => {
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    expect(await screen.findByText(/no searches yet/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: PROMPT }));

    expect(box()).toHaveValue(PROMPT);
    expect(request).not.toHaveBeenCalled();
  });

  it('cannot be sent empty, and says nothing is spent until Search is pressed', () => {
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    expect(searchButton()).toBeDisabled();
    expect(screen.getByText(/nothing is spent until you press search/i)).toBeInTheDocument();
  });
});

describe('who may search', () => {
  it('shows a viewer the box shut and says why, but leaves recent searches open', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    recent = [trendSearchRun({ status: 'succeeded', inserted: 8 })];
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    expect(box()).toBeDisabled();
    expect(screen.getByText(/only an owner can start a search/i)).toBeInTheDocument();
    expect(await screen.findByText(/recent searches/i)).toBeInTheDocument();
  });
});

describe('sending a description', () => {
  it('starts a run with the words, says how long, and opens the search', async () => {
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await user.type(box(), `  ${PROMPT}  `);
    await user.click(searchButton());

    expect(request).toHaveBeenCalledWith({ prompt: PROMPT, budgetMinutes: null, hashtagsPerRun: null });
    expect(toasts.success).toHaveBeenCalled();
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: '/queue', search: { search: 'search-9' } }));
  });

  it('sends on Cmd+Enter and not on Enter', async () => {
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await user.type(box(), PROMPT);
    await user.keyboard('{Enter}');
    expect(request).not.toHaveBeenCalled();

    await user.keyboard('{Meta>}{Enter}{/Meta}');
    expect(request).toHaveBeenCalledOnce();
  });

  it('is shut while a run is already going, and keeps the words', async () => {
    latest = trendRun({ status: 'running' });
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await screen.findByText(/a run is already going/i);
    await user.type(box(), 'still typing');

    expect(box()).toHaveValue('still typing');
    expect(searchButton()).toBeDisabled();
  });

  it('treats a concurrent request as information, and keeps the words', async () => {
    // A plain object, not an Error: the shape supabase-js actually rejects with.
    request.mockRejectedValueOnce({ code: '55006', message: 'A trend run is already in progress' });
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await user.type(box(), PROMPT);
    await user.click(searchButton());

    expect(toasts.info).toHaveBeenCalled();
    expect(toasts.error).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    expect(box()).toHaveValue(PROMPT);
  });

  it('says why a real failure refused it, and keeps the words', async () => {
    request.mockRejectedValueOnce({
      code: 'PGRST202',
      message: 'Could not find the function public.request_trend_run(p_prompt) in the schema cache',
    });
    const user = userEvent.setup();
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await user.type(box(), PROMPT);
    await user.click(searchButton());

    const [title, options] = toasts.error.mock.calls[0] as [string, { description: string }];
    expect(title).toMatch(/could not start the search/i);
    expect(options.description).toMatch(/has not been applied/i);
    expect(box()).toHaveValue(PROMPT);
  });
});

describe('watching a selected search', () => {
  it('shows the words while the description is still being read', async () => {
    selected = trendSearchRun({ status: 'running', interpretation: null, interpreted_at: null });
    latest = selected;
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/searching for ideas/i)).toBeInTheDocument();
    expect(screen.getByText(/reading your description/i)).toBeInTheDocument();
    expect(screen.getByText(/for: “i want to start a small home fitness/i)).toBeInTheDocument();
  });

  it('says how the description was understood, with the terms', async () => {
    selected = trendSearchRun({ status: 'running' });
    latest = selected;
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/understood as/i)).toBeInTheDocument();
    expect(screen.getByText('home workout for parents')).toBeInTheDocument();
    expect(screen.getByText('quick workout at home')).toBeInTheDocument();
  });

  it('shows hashtags as hashtags', async () => {
    selected = trendSearchRun({
      status: 'running',
      interpretation: trendInterpretation({ vocabulary: 'hashtags', terms: ['busyparents'] }),
    });
    latest = selected;
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText('#busyparents')).toBeInTheDocument();
  });

  it('nudges when the description was thin, and only then', async () => {
    selected = trendSearchRun({
      status: 'running',
      interpretation: trendInterpretation({ vague: true, nudge: 'Say who it is for.' }),
    });
    latest = selected;
    const { unmount } = render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });
    expect(await screen.findByText(/worth adding next time: say who it is for/i)).toBeInTheDocument();
    unmount();

    selected = trendSearchRun({ status: 'running' });
    latest = selected;
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });
    await screen.findByText(/understood as/i);
    expect(screen.queryByText(/worth adding next time/i)).not.toBeInTheDocument();
  });

  it("puts the selected search's words back in the box", async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 8 });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    await waitFor(() => expect(box()).toHaveValue(PROMPT));
  });
});

describe('when a search comes back thin', () => {
  it('says nothing relevant came back, offers the rephrasings, and refines without spending', async () => {
    const user = userEvent.setup();
    selected = trendSearchRun({
      status: 'succeeded',
      inserted: 0,
      rejections: trendRejections({ seen: 12, surfaced: 3, inserted: 0 }),
    });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/nothing relevant came back/i)).toBeInTheDocument();
    expect(screen.getByText('Home fitness for parents of toddlers, in the US')).toBeInTheDocument();
    // The fix for a thin search is a better description, not a looser filter.
    expect(screen.queryByText(/loosen/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /try again with more detail/i }));

    expect(box()).toHaveFocus();
    expect(request).not.toHaveBeenCalled();
  });

  it('is soft about a few results', async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 2 });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/only 2 ideas came back/i)).toBeInTheDocument();
  });

  it('says nothing about a search that found plenty', async () => {
    selected = trendSearchRun({ status: 'succeeded', inserted: 8 });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    await waitFor(() => expect(box()).toHaveValue(PROMPT));
    expect(screen.queryByText(/came back/i)).not.toBeInTheDocument();
  });
});

describe('when a search did not finish', () => {
  it('says so, keeps the manual route in view, and can send the same words again', async () => {
    const user = userEvent.setup();
    selected = trendSearchRun({ status: 'failed', error: 'gemini returned no content', inserted: null });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/the search did not finish/i)).toBeInTheDocument();
    expect(screen.getByText(/gemini returned no content/i)).toBeInTheDocument();
    expect(screen.getByText(/trend search inputs below/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /try this search again/i }));

    expect(request).toHaveBeenCalledWith(expect.objectContaining({ prompt: PROMPT }));
  });

  it('shows the nudge when the description could not be read into a search', async () => {
    selected = trendSearchRun({
      status: 'failed',
      error: 'Could not turn that description into anything to search for.',
      inserted: null,
      interpretation: trendInterpretation({ terms: [], vague: true, nudge: 'Say who it is for.' }),
    });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/worth adding: say who it is for/i)).toBeInTheDocument();
  });

  it('reports a stopped search quietly', async () => {
    selected = trendSearchRun({ status: 'cancelled', inserted: 0, rejections: null });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/you stopped that run/i)).toBeInTheDocument();
  });

  it('flags a search an older worker ran as the saved list, and offers the new run as a button', async () => {
    // A finished run never changes, so this notice outlives the redeploy that
    // fixes it. The button is the way to find out the worker is new now.
    const user = userEvent.setup();
    selected = trendSearchRun({ status: 'succeeded', inserted: 5, interpretation: null, interpreted_at: null });
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/was not read as a search/i)).toBeInTheDocument();
    expect(screen.getByText(/stays until the next one/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /search this again/i }));

    expect(request).toHaveBeenCalledWith(expect.objectContaining({ prompt: PROMPT }));
  });
});

describe('recent searches', () => {
  it('lists them with when, how it went and how many, newest first', async () => {
    recent = [
      trendSearchRun({ id: 'search-2', prompt: 'Bookkeeping for tradespeople', status: 'running', inserted: null }),
      trendSearchRun({ id: 'search-1', status: 'succeeded', inserted: 8 }),
    ];
    render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });

    expect(await screen.findByText(/recent searches/i)).toBeInTheDocument();
    const rows = screen.getAllByRole('link', { name: /bookkeeping|home fitness/i });
    expect(rows[0]).toHaveAttribute('href', '/queue?search=search-2');
    expect(rows[0]).toHaveTextContent(/searching…/i);
    expect(rows[0]).not.toHaveAttribute('aria-current');
    expect(rows[1]).toHaveAttribute('href', '/queue?search=search-1');
    expect(rows[1]).toHaveTextContent(/8 ideas/i);
    expect(rows[1]).toHaveAttribute('aria-current', 'page');
  });

  it('offers the way back to the whole queue only while one is selected', async () => {
    recent = [trendSearchRun({ status: 'succeeded', inserted: 8 })];
    const { unmount } = render(<AiSearchPanel selectedRunId="search-1" />, { wrapper });
    expect(await screen.findByRole('link', { name: /show the whole queue/i })).toHaveAttribute('href', '/queue?page=1');
    unmount();

    render(<AiSearchPanel selectedRunId={null} />, { wrapper });
    await screen.findByText(/recent searches/i);
    expect(screen.queryByRole('link', { name: /show the whole queue/i })).not.toBeInTheDocument();
  });

  it('hides the examples once there is history', async () => {
    recent = [trendSearchRun()];
    render(<AiSearchPanel selectedRunId={null} />, { wrapper });

    await screen.findByText(/recent searches/i);
    expect(screen.queryByText(/no searches yet/i)).not.toBeInTheDocument();
  });
});
