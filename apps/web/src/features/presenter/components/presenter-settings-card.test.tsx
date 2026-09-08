import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { look, presenterPreset, voice } from '@/features/presenter/test-fixtures';
import type { HeyGenCatalogueRow, HeyGenLookRow, HeyGenVoiceRow, StylePresetRow } from '@/lib/database.types';

/**
 * The acceptance criterion this file exists for: a landscape or
 * engine-incompatible look cannot be saved by accident.
 *
 * Postgres refuses the same cases and is what actually holds. What is tested
 * here is the half Postgres cannot do — saying so before the button is
 * pressed, rather than after a review has been spent reaching a render that
 * parks with `avatar_not_found`.
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

let looks: HeyGenLookRow[] = [];
let voices: HeyGenVoiceRow[] = [];
let catalogue: HeyGenCatalogueRow | null = null;
let presets: StylePresetRow[] = [];

const save = vi.fn(async () => presenterPreset());
const refresh = vi.fn(async () => catalogue as HeyGenCatalogueRow);
const requestVoice = vi.fn(async (id: string) => voice({ voice_id: id, status: 'pending' }));

// Replaced outright rather than spread over the real modules: both of them
// reach `@/lib/supabase` at import time, and both are imported by each other,
// which is a cycle `importOriginal` resolves by hanging.
vi.mock('@/features/presenter/api', () => ({
  presenterKeys: { all: ['presenter'] },
  looksQueryOptions: () => ({ queryKey: ['presenter', 'looks'], queryFn: async () => looks }),
  voicesQueryOptions: () => ({ queryKey: ['presenter', 'voices'], queryFn: async () => voices }),
  catalogueQueryOptions: () => ({ queryKey: ['presenter', 'catalogue'], queryFn: async () => catalogue }),
  // A no-op here: it exists to refetch the caches when a refresh lands, and
  // these fixtures are already whatever the refetch would return.
  useCatalogueSync: () => {},
  useSetPresetPresenter: () => ({ mutateAsync: save, isPending: false }),
  useRefreshCatalogue: () => ({ mutateAsync: refresh, isPending: false }),
  useRequestVoice: () => ({ mutateAsync: requestVoice, isPending: false }),
}));

vi.mock('@/features/queue/api', () => ({
  queueKeys: { all: ['queue'], stylePresets: () => ['queue', 'style-presets'] },
  stylePresetsQueryOptions: () => ({ queryKey: ['queue', 'style-presets'], queryFn: async () => presets }),
}));

vi.mock('@/features/queue/components/query-state', () => ({
  QueryError: ({ error }: { error: Error }) => <p>{error.message}</p>,
}));

const { PresenterSettingsCard } = await import('@/features/presenter/components/presenter-settings-card');

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const PORTRAIT = look({ avatar_id: 'portrait-1', name: 'Dara', orientation: 'portrait' });
const LANDSCAPE = look({ avatar_id: 'landscape-1', name: 'Marcus', orientation: 'landscape' });
const STUDIO = look({ avatar_id: 'studio-1', name: 'Ines', orientation: 'landscape', engines: ['avatar_iii'] });
const VOICE = voice({ voice_id: 'v-ok' });

beforeEach(() => {
  vi.clearAllMocks();
  // Restated rather than left to `clearAllMocks`, which clears calls and not
  // implementations: without this the viewer test below leaks a disabled
  // picker into every test that runs after it.
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
  looks = [PORTRAIT, LANDSCAPE, STUDIO];
  voices = [VOICE];
  catalogue = {
    id: true,
    status: 'idle',
    requested_at: null,
    requested_by: null,
    started_at: null,
    refreshed_at: '2026-09-08T09:00:00Z',
    looks: 3,
    voices: 1,
    error: null,
  };
  // The preset as shipped: an avatar and voice nothing in the cache matches,
  // which is exactly the state this card was built to get out of.
  presets = [presenterPreset()];
});

describe('PresenterSettingsCard', () => {
  it('lists the looks the account owns, with how each is framed', async () => {
    render(<PresenterSettingsCard />, { wrapper });

    expect(await screen.findByRole('button', { name: /^Dara portrait$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Marcus/ })).toBeInTheDocument();
    // Two landscape looks, and the word is on both tiles rather than buried.
    expect(screen.getAllByText('landscape')).toHaveLength(2);
  });

  it('names the engine a studio avatar advertises, because omitting it selects Avatar IV', async () => {
    render(<PresenterSettingsCard />, { wrapper });

    const tile = await screen.findByRole('button', { name: /Ines/ });
    // The documented terminal failure: rendering a look that advertises
    // avatar_iii only on the engine it never claimed to support.
    expect(tile).toHaveTextContent('avatar_iii');
  });

  it('warns about a landscape look and holds the save until the owner says they meant it', async () => {
    const user = userEvent.setup();
    render(<PresenterSettingsCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /Marcus/ }));
    await user.click(screen.getByRole('button', { name: /Nadia \(calm\)/ }));

    expect(screen.getByText(/crops the speaker/)).toBeInTheDocument();
    const saveButton = screen.getByRole('button', { name: /Save as the default/ });
    expect(saveButton).toBeDisabled();
    expect(screen.getByText(/Confirm you want a landscape look/)).toBeInTheDocument();

    // A refusal would be wrong -- the crop is a picture, not a failure -- so
    // the consent is what unlocks it, not a second opinion about the look.
    await user.click(screen.getByRole('checkbox', { name: /Use it anyway/ }));
    expect(saveButton).toBeEnabled();
  });

  it('needs no confirmation for a portrait look', async () => {
    const user = userEvent.setup();
    render(<PresenterSettingsCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /^Dara portrait$/ }));
    await user.click(screen.getByRole('button', { name: /Nadia \(calm\)/ }));

    expect(screen.queryByText(/crops the speaker/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Save as the default/ }));

    expect(save).toHaveBeenCalledWith({
      presetId: 'preset-presenter',
      avatarId: 'portrait-1',
      voiceId: 'v-ok',
      // Null rather than 'avatar_iv': omitting the key is what selects it, and
      // writing the default into the preset would read like a decision.
      engine: null,
    });
  });

  it('sends the engine a studio avatar advertises', async () => {
    const user = userEvent.setup();
    render(<PresenterSettingsCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /Ines/ }));
    await user.click(screen.getByRole('button', { name: /Nadia \(calm\)/ }));
    await user.click(screen.getByRole('checkbox', { name: /Use it anyway/ }));
    await user.click(screen.getByRole('button', { name: /Save as the default/ }));

    expect(save).toHaveBeenCalledWith(expect.objectContaining({ avatarId: 'studio-1', engine: 'avatar_iii' }));
  });

  it('will not save a voice HeyGen has not confirmed', async () => {
    voices = [voice({ voice_id: 'v-new', name: null, status: 'pending', preview_audio_url: null })];
    const user = userEvent.setup();
    render(<PresenterSettingsCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /^Dara portrait$/ }));
    // The row is on screen and says what it is waiting for, but is not
    // selectable: `voice_not_found` is terminal, so guessing costs a render.
    expect(screen.getByText(/Checking with HeyGen/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /v-new/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Save as the default/ })).toBeDisabled();
  });

  it('repeats what HeyGen said about a voice it does not recognise', async () => {
    voices = [voice({ voice_id: 'v-gone', name: null, status: 'unknown', error: 'voice_not_found' })];
    render(<PresenterSettingsCard />, { wrapper });

    expect(await screen.findByText('voice_not_found')).toBeInTheDocument();
  });

  it('says the account has never been read, rather than showing an empty list', async () => {
    looks = [];
    render(<PresenterSettingsCard />, { wrapper });

    expect(await screen.findByText(/No looks cached yet/)).toBeInTheDocument();
  });

  it('surfaces a failed refresh, because the worker log is not where the owner is looking', async () => {
    catalogue = { ...(catalogue as HeyGenCatalogueRow), status: 'failed', error: '401 unauthorized' };
    render(<PresenterSettingsCard />, { wrapper });

    expect(await screen.findByText(/401 unauthorized/)).toBeInTheDocument();
  });

  it('lets a viewer read it but not change it', async () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<PresenterSettingsCard />, { wrapper });

    expect(await screen.findByText(/Only an owner can change it/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Save as the default/ })).not.toBeInTheDocument();
    // Including the one button that is not a save. Refreshing the catalogue is
    // owner-gated in SQL too, and a button enabled only to come back with
    // 42501 is worse than one that is plainly unavailable.
    expect(screen.getByRole('button', { name: /Refresh/ })).toBeDisabled();
  });

  it('stops saying "confirm this first" once the save has happened', async () => {
    const user = userEvent.setup();
    render(<PresenterSettingsCard />, { wrapper });

    await user.click(await screen.findByRole('button', { name: /Marcus/ }));
    await user.click(screen.getByRole('button', { name: /Nadia \(calm\)/ }));
    await user.click(screen.getByRole('checkbox', { name: /Use it anyway/ }));
    expect(screen.getByRole('button', { name: /Save as the default/ })).toBeEnabled();

    // The preset now names what was just saved, which re-seeds the draft and
    // resets the consent. Red text under a disabled button, seconds after the
    // save it is describing, reads as a failure rather than a finished job.
    presets = [presenterPreset({ params: { heygen: { avatar_id: 'landscape-1', voice_id: 'v-ok' } } })];
    await user.click(screen.getByRole('button', { name: /Save as the default/ }));

    expect(save).toHaveBeenCalled();
    expect(screen.queryByText(/Confirm you want a landscape look/)).not.toBeInTheDocument();
  });
});
