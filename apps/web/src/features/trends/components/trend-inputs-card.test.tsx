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

const { TrendInputsCard } = await import('@/features/trends/components/trend-inputs-card');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  mutate.mockClear();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('both vocabularies, on screen together', () => {
  // The reason this card replaced one that swapped the visible list: a list
  // that is hidden looks like a list that is empty, and handing a source the
  // wrong vocabulary produces a run that succeeds and finds nothing.
  it('shows the search terms and the hashtags at once', async () => {
    render(<TrendInputsCard />, { wrapper });

    expect(await screen.findByText('invoice software')).toBeInTheDocument();
    expect(screen.getByText('#aiautomation')).toBeInTheDocument();
  });

  it('says which list the chosen source actually reads', async () => {
    render(<TrendInputsCard />, { wrapper });

    // Google Trends is the default, so the terms are live and the hashtags
    // are not. Said next to each list rather than only in prose.
    const terms = (await screen.findByText('Search terms')).closest('div')?.parentElement;
    const tags = screen.getByText('Hashtags').closest('div')?.parentElement;

    expect(terms).toHaveTextContent('In use');
    expect(terms).toHaveTextContent('Read by Google Trends and YouTube.');
    expect(tags).toHaveTextContent('Not searched by the current source');
    expect(tags).toHaveTextContent('Read by Apify (TikTok and Instagram).');
  });

  it('moves the "in use" mark to the hashtags when Apify is chosen', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');

    const tags = screen.getByText('Hashtags').closest('div')?.parentElement;
    const terms = screen.getByText('Search terms').closest('div')?.parentElement;

    expect(tags).toHaveTextContent('In use');
    expect(terms).toHaveTextContent('Not searched by the current source');
    // And both lists are still editable: preparing a switch is legitimate.
    expect(screen.getByText('invoice software')).toBeInTheDocument();
    expect(screen.getByText('#aiautomation')).toBeInTheDocument();
  });

  it('warns that Google Trends cannot read a hashtag', async () => {
    // The single most expensive misunderstanding this card can prevent.
    render(<TrendInputsCard />, { wrapper });
    expect(await screen.findByText(/Google Trends cannot read these/i)).toBeInTheDocument();
  });
});

describe('search terms', () => {
  it('keeps a term as a phrase rather than mangling it into a hashtag', async () => {
    // "excel alternative" is two words on purpose. Stripping the space would
    // change what is being measured.
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'Excel  Alternative{enter}');
    expect(screen.getByText('excel alternative')).toBeInTheDocument();
  });

  it('refuses a duplicate rather than paying for the same request twice', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'Invoice Software{enter}');
    expect(screen.getAllByText('invoice software')).toHaveLength(1);
  });

  it('does not treat a comma as the end of a term', async () => {
    // A comma ends a hashtag, which is one token. A search phrase may contain
    // one, and splitting on it would silently change the query.
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'crm, invoicing');
    expect(screen.queryByText('crm')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText(/bookkeeping software/i)).toHaveValue('crm, invoicing');
  });

  it('adds from the button as well as the keyboard', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText(/bookkeeping software/i), 'quoting software');
    await user.click(screen.getByRole('button', { name: 'Add search term' }));

    expect(screen.getByText('quoting software')).toBeInTheDocument();
  });

  it('shows how much of the 50-term limit is used', async () => {
    // The limit is a CHECK constraint, so an owner who learns it by being
    // refused has been handed a puzzle instead of a form.
    render(<TrendInputsCard />, { wrapper });
    expect(await screen.findByText('3 of 50')).toBeInTheDocument();
  });

  it('removes a term and saves without touching the hashtags', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

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
});

describe('hashtags', () => {
  it('normalises a typed hashtag on the way in', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), '#No Code{enter}');
    expect(screen.getByText('#nocode')).toBeInTheDocument();
  });

  it('ends an entry on a comma, because a hashtag is one token', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), 'exceltips,');
    expect(screen.getByText('#exceltips')).toBeInTheDocument();
  });

  it('refuses a duplicate', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), 'aiautomation{enter}');
    expect(screen.getAllByText('#aiautomation')).toHaveLength(1);
  });

  it('adds from the button as well as the keyboard', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.type(await screen.findByPlaceholderText('Add a hashtag'), 'propertymanagement');
    await user.click(screen.getByRole('button', { name: 'Add hashtag' }));

    expect(screen.getByText('#propertymanagement')).toBeInTheDocument();
  });

  it('removes a hashtag and saves without touching the terms', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.click(await screen.findByLabelText('Remove webdesign'));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        hashtags: ['aiautomation', 'devops'],
        trend_keywords: row.trend_keywords,
      }),
    );
  });
});

describe('the region', () => {
  it('offers the regions, because interest is normalised within one', async () => {
    render(<TrendInputsCard />, { wrapper });

    const select = await screen.findByLabelText('Region');
    const values = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'));
    // Worldwide first: nothing in the pipeline knows where this business sells.
    expect(values[0]).toBe('');
    expect(values).toContain('GB');
    expect(values).toContain('US');
  });

  it('saves the chosen region', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Region'), 'GB');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(expect.objectContaining({ trend_geo: 'GB' }));
  });

  it('stays on screen for a source that ignores it, and says so', async () => {
    // It used to vanish. That made a saved region look unset, and hid the
    // fact that switching back to Google Trends would restore it.
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');

    expect(screen.getByLabelText('Region')).toBeInTheDocument();
    expect(screen.getByText('Google Trends only')).toBeInTheDocument();
    expect(screen.getByText(/Neither the Apify scrapers nor the YouTube search/i)).toBeInTheDocument();
  });

  it('explains why worldwide is usually the wrong answer', async () => {
    render(<TrendInputsCard />, { wrapper });
    expect(await screen.findByText(/mixes markets you do not sell to/i)).toBeInTheDocument();
  });
});

describe('the sources on offer', () => {
  it('offers exactly the three the pipeline can run', async () => {
    // A dropdown entry the pipeline has no scout for is an option that cannot
    // work; a scout with no entry is one nobody can select. The pipeline side
    // asserts the same list against its own registry.
    render(<TrendInputsCard />, { wrapper });

    const select = await screen.findByLabelText('Source');
    const values = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'));
    expect(values).toEqual(['google_trends', 'apify', 'youtube']);
  });

  it('no longer offers the source that stopped working', async () => {
    render(<TrendInputsCard />, { wrapper });

    const select = await screen.findByLabelText('Source');
    const values = Array.from(select.querySelectorAll('option')).map((o) => o.getAttribute('value'));
    expect(values).not.toContain('tiktok');
  });

  it('defaults to Google Trends, which needs no key', async () => {
    render(<TrendInputsCard />, { wrapper });

    expect(await screen.findByLabelText('Source')).toHaveValue('google_trends');
    expect(screen.getByLabelText('Source').querySelector('option')).toHaveValue('google_trends');
    expect(screen.getByText(/needs no key/i)).toBeInTheDocument();
    expect(screen.queryByText(/needs a key before it will run/i)).not.toBeInTheDocument();
  });

  it.each([
    ['apify', 'APIFY_TOKEN'],
    ['youtube', 'YOUTUBE_API_KEY'],
  ])('warns that %s needs %s', async (source, variable) => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), source);

    expect(screen.getByText(/needs a key before it will run/i)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(variable))).toBeInTheDocument();
  });

  it('explains why Apify refuses up front rather than scouting first', async () => {
    // The reason is money, not tidiness: the scrape is billed per result.
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');

    expect(screen.getByText(/paying for one nobody could use/i)).toBeInTheDocument();
  });
});

describe('apify platforms', () => {
  it('offers the platforms to scrape, both on by default', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');

    expect(screen.getByRole('button', { name: 'TikTok' })).toHaveAttribute('data-state', 'on');
    expect(screen.getByRole('button', { name: 'Instagram' })).toHaveAttribute('data-state', 'on');
  });

  it('refuses to save with no platform selected', async () => {
    // A source that is selected and can never scout looks exactly like a
    // runner that has stopped working, which is the confusion worth refusing.
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');
    await user.click(screen.getByRole('button', { name: 'TikTok' }));
    await user.click(screen.getByRole('button', { name: 'Instagram' }));

    expect(screen.getByText(/looks exactly like a broken runner/i)).toBeInTheDocument();
    expect(screen.getByText(/Pick at least one platform for Apify/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    expect(mutate).not.toHaveBeenCalled();
  });

  it('saves the platforms that are left selected', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'apify');
    await user.click(screen.getByRole('button', { name: 'Instagram' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ trend_source: 'apify', apify_platforms: ['tiktok'] }),
    );
  });

  it('offers no platform picker for a source that is not apify', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard />, { wrapper });

    await user.selectOptions(await screen.findByLabelText('Source'), 'youtube');

    expect(screen.queryByRole('button', { name: 'Instagram' })).not.toBeInTheDocument();
  });
});

describe('on the queue page, where it opens from a summary', () => {
  it('is shut to start with, so the ideas stay at the top', async () => {
    render(<TrendInputsCard collapsible />, { wrapper });

    // Awaited on the summary rather than the button: the button is there from
    // the first paint, and only says anything once the settings have arrived.
    await screen.findByText(/Google Trends · 3 search terms · Worldwide/);
    // The shut panel still says what the next run will search — the summary is
    // part of the button's name rather than hidden behind it.
    expect(screen.getByRole('button', { name: /Trend search inputs/ })).toHaveAccessibleName(
      /Google Trends · 3 search terms · Worldwide/,
    );
    expect(screen.queryByLabelText('Source')).not.toBeInTheDocument();
  });

  it('says what the next run will search without being opened', async () => {
    // The whole point of the shut state: the answer to "why is nothing coming
    // through?" should not require a click.
    render(<TrendInputsCard collapsible />, { wrapper });

    expect(await screen.findByText(/Google Trends · 3 search terms · Worldwide/)).toBeInTheDocument();
  });

  it('counts the hashtags instead when a hashtag source is chosen', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard collapsible />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /Trend search inputs/ }));
    await user.selectOptions(screen.getByLabelText('Source'), 'apify');

    // Three hashtags in the fixture, and the summary drops the region — it
    // decides nothing on this source, so counting it would mislead.
    const summary = screen.getByText(/Apify \(TikTok and Instagram\) · 3 hashtags/);
    expect(summary).toBeInTheDocument();
    expect(summary).not.toHaveTextContent('Worldwide');
  });

  it('opens onto the same controls', async () => {
    const user = userEvent.setup();
    render(<TrendInputsCard collapsible />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /Trend search inputs/ }));

    expect(screen.getByLabelText('Source')).toBeInTheDocument();
    expect(screen.getByLabelText('Region')).toBeInTheDocument();
    expect(screen.getByText('invoice software')).toBeInTheDocument();
    expect(screen.getByText('#aiautomation')).toBeInTheDocument();
  });
});

describe('a viewer cannot edit', () => {
  beforeEach(() => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
  });

  it('shows both lists but offers no way to change them', async () => {
    render(<TrendInputsCard />, { wrapper });

    expect(await screen.findByText('invoice software')).toBeInTheDocument();
    expect(screen.getByText('#aiautomation')).toBeInTheDocument();
    expect(screen.queryByLabelText('Remove invoice software')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Remove aiautomation')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('Add a hashtag')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument();
  });

  it('leaves the region readable but not settable', async () => {
    render(<TrendInputsCard />, { wrapper });
    expect(await screen.findByLabelText('Region')).toBeDisabled();
  });
});
