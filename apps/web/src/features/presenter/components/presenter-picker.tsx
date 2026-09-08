import { useQuery } from '@tanstack/react-query';
import { CircleAlertIcon, RefreshCwIcon, TriangleAlertIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import {
  catalogueQueryOptions,
  looksQueryOptions,
  useCatalogueSync,
  useRefreshCatalogue,
  useRequestVoice,
  voicesQueryOptions,
} from '@/features/presenter/api';
import { byUsability, engineFor, needsConfirmation, orientationWarning } from '@/features/presenter/presenter';
import { QueryError } from '@/features/queue/components/query-state';
import { type HeyGenLookRow, type HeyGenVoiceRow, isCatalogueRefreshing } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import { cn } from '@/lib/utils';

/** What the picker holds while it is being used. */
export interface PresenterDraft {
  avatarId: string | null;
  voiceId: string | null;
  /** Whether the owner has said out loud that they want a cropping look. */
  confirmed: boolean;
}

export const EMPTY_DRAFT: PresenterDraft = { avatarId: null, voiceId: null, confirmed: false };

/**
 * Choosing an avatar and a voice, against what the account can actually use.
 *
 * The same component serves both places a presenter is chosen — the preset
 * default in Settings and the one-production override at Gate 1 — because the
 * two differ in what is done with the answer, not in how it is reached.
 *
 * It lists a cache rather than the live account. There is no server tier here
 * and the browser holds no secrets, so the worker fills `heygen_looks` and
 * `heygen_voices` and this reads them; Refresh asks the worker to go and look
 * again. That indirection is visible on purpose — the header says when the
 * list was last filled, because a picker silently a week out of date is how
 * someone spends an afternoon wondering where their new avatar went.
 */
export function PresenterPicker({
  value,
  onChange,
  disabled,
}: {
  value: PresenterDraft;
  onChange: (next: PresenterDraft) => void;
  disabled?: boolean;
}) {
  const looksQuery = useQuery(looksQueryOptions());
  const voicesQuery = useQuery(voicesQueryOptions());
  const catalogueQuery = useQuery(catalogueQueryOptions());

  const looks = [...(looksQuery.data ?? [])].sort(byUsability);
  const voices = voicesQuery.data ?? [];
  const catalogue = catalogueQuery.data ?? null;
  const refreshing = isCatalogueRefreshing(catalogue);

  // The looks and voices do not poll; the one row that says when they last
  // changed does. This is what turns that into a refetch.
  useCatalogueSync(catalogue?.refreshed_at);

  const selectedLook = looks.find((look) => look.avatar_id === value.avatarId) ?? null;

  if (looksQuery.error) return <QueryError error={looksQuery.error} />;

  return (
    <div className="space-y-5">
      <CatalogueHeader
        refreshing={refreshing}
        lastFilled={catalogue?.refreshed_at ?? null}
        error={catalogue?.error ?? null}
        // A viewer may read the catalogue and may not refill it:
        // `request_heygen_catalogue_refresh` raises 42501 for one, and a button
        // that is enabled only to fail is worse than one that is not there.
        disabled={disabled}
      />

      {looksQuery.isPending ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <Skeleton className="aspect-[3/4] w-full" />
          <Skeleton className="aspect-[3/4] w-full" />
          <Skeleton className="aspect-[3/4] w-full" />
        </div>
      ) : looks.length === 0 ? (
        <NoLooks refreshing={refreshing} />
      ) : (
        <fieldset className="space-y-3" disabled={disabled}>
          <legend className="text-sm font-medium">Avatar</legend>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            {looks.map((look) => (
              <LookTile
                key={look.avatar_id}
                look={look}
                selected={look.avatar_id === value.avatarId}
                disabled={disabled}
                // Choosing again clears the confirmation: it was given about a
                // particular look, and carrying it over would let a landscape
                // look inherit consent that was granted to a portrait one.
                onSelect={() => onChange({ ...value, avatarId: look.avatar_id, confirmed: false })}
              />
            ))}
          </div>
        </fieldset>
      )}

      {selectedLook && <LookConsequences look={selectedLook} value={value} onChange={onChange} disabled={disabled} />}

      <VoiceField
        voices={voices}
        value={value}
        onChange={onChange}
        disabled={disabled}
        isPending={voicesQuery.isPending}
      />
    </div>
  );
}

function CatalogueHeader({
  refreshing,
  lastFilled,
  error,
  disabled,
}: {
  refreshing: boolean;
  lastFilled: string | null;
  error: string | null;
  disabled?: boolean;
}) {
  const refresh = useRefreshCatalogue();

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          {refreshing
            ? 'Asking HeyGen what this account can use…'
            : lastFilled
              ? `Read from HeyGen ${formatRelative(lastFilled)}.`
              : 'Never read from HeyGen.'}
        </p>
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled || refreshing || refresh.isPending}
          onClick={async () => {
            try {
              await refresh.mutateAsync();
            } catch (err) {
              toast.error(err instanceof Error ? err.message : 'Could not ask for a refresh');
            }
          }}
        >
          {refreshing || refresh.isPending ? <Spinner className="size-4" /> : <RefreshCwIcon className="size-4" />}
          Refresh
        </Button>
      </div>
      {error && (
        <Alert variant="destructive">
          <CircleAlertIcon className="size-4" />
          <AlertTitle>The last refresh failed</AlertTitle>
          {/* Shown rather than logged: the person who pressed the button is
              looking at this page, and 'unauthorized' is something they can
              act on where a worker log is not. */}
          <AlertDescription className="break-words">{error}</AlertDescription>
        </Alert>
      )}
    </div>
  );
}

function NoLooks({ refreshing }: { refreshing: boolean }) {
  return (
    <Alert>
      <AlertTitle>{refreshing ? 'Reading the account…' : 'No looks cached yet'}</AlertTitle>
      <AlertDescription>
        {refreshing
          ? 'The worker is asking HeyGen which avatars this account owns. This page updates on its own.'
          : 'Press Refresh, and the worker will ask HeyGen which avatars this account owns. Nothing can be picked until it answers — an avatar this account cannot use fails the render outright, after the gate has already been passed.'}
      </AlertDescription>
    </Alert>
  );
}

function LookTile({
  look,
  selected,
  disabled,
  onSelect,
}: {
  look: HeyGenLookRow;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  const warning = orientationWarning(look);
  return (
    <button
      type="button"
      onClick={onSelect}
      disabled={disabled}
      aria-pressed={selected}
      className={cn(
        'group overflow-hidden rounded-lg border text-left transition-colors',
        'hover:bg-accent/50 disabled:cursor-not-allowed disabled:opacity-60',
        selected && 'border-primary bg-accent/40 ring-1 ring-primary/30',
      )}
    >
      {look.preview_image_url ? (
        <img src={look.preview_image_url} alt="" loading="lazy" className="aspect-[3/4] w-full bg-muted object-cover" />
      ) : (
        <div className="flex aspect-[3/4] w-full items-center justify-center bg-muted text-xs text-muted-foreground">
          No preview
        </div>
      )}
      <div className="space-y-1 p-2">
        <p className="truncate text-sm font-medium">{look.name ?? look.avatar_id}</p>
        <div className="flex flex-wrap items-center gap-1">
          <Badge variant={warning ? 'outline' : 'secondary'} className="text-[0.7rem] font-normal">
            {look.orientation}
          </Badge>
          {/* Named on the tile because it is what will be sent, and because a
              look advertising avatar_iii only is the case that fails
              terminally when nobody says so. */}
          {engineFor(look) && (
            <Badge variant="outline" className="text-[0.7rem] font-normal">
              {engineFor(look)}
            </Badge>
          )}
        </div>
      </div>
    </button>
  );
}

function LookConsequences({
  look,
  value,
  onChange,
  disabled,
}: {
  look: HeyGenLookRow;
  value: PresenterDraft;
  onChange: (next: PresenterDraft) => void;
  disabled?: boolean;
}) {
  const warning = orientationWarning(look);
  if (!warning) return null;

  return (
    <Alert>
      <TriangleAlertIcon className="size-4" />
      <AlertTitle>{look.name ?? look.avatar_id}</AlertTitle>
      <AlertDescription className="space-y-3">
        <p>{warning}</p>
        {/* A refusal would be wrong: the crop is a picture, not a failure,
            and a head-and-shoulders look can crop to a usable 9:16. Choosing
            it without knowing is what must not happen. */}
        {needsConfirmation(look) && (
          <div className="flex items-start gap-2">
            <Checkbox
              id="presenter-confirm-crop"
              checked={value.confirmed}
              disabled={disabled}
              onCheckedChange={(checked) => onChange({ ...value, confirmed: checked === true })}
            />
            <Label htmlFor="presenter-confirm-crop" className="text-sm font-normal leading-snug">
              Use it anyway — I want the crop.
            </Label>
          </div>
        )}
      </AlertDescription>
    </Alert>
  );
}

function VoiceField({
  voices,
  value,
  onChange,
  disabled,
  isPending,
}: {
  voices: HeyGenVoiceRow[];
  value: PresenterDraft;
  onChange: (next: PresenterDraft) => void;
  disabled?: boolean;
  isPending: boolean;
}) {
  const [draft, setDraft] = useState('');
  const request = useRequestVoice();

  async function add() {
    const id = draft.trim();
    if (!id) return;
    try {
      const row = await request.mutateAsync(id);
      setDraft('');
      onChange({ ...value, voiceId: row.voice_id });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not add that voice');
    }
  }

  return (
    <fieldset className="space-y-3" disabled={disabled}>
      <legend className="text-sm font-medium">Voice</legend>
      <p className="text-xs text-muted-foreground">
        Voices are checked one id at a time rather than listed: this account's own catalogue runs to 3,089 entries and
        does not contain its cloned voices, which only answer when asked for directly.
      </p>

      {isPending ? (
        <Skeleton className="h-16 w-full" />
      ) : (
        <div className="space-y-2">
          {voices.map((voice) => (
            <VoiceRow
              key={voice.voice_id}
              voice={voice}
              selected={voice.voice_id === value.voiceId}
              disabled={disabled}
              onSelect={() => onChange({ ...value, voiceId: voice.voice_id })}
            />
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-48 flex-1 space-y-1">
          <Label htmlFor="presenter-voice-id" className="text-xs font-normal text-muted-foreground">
            Add a voice by id
          </Label>
          <Input
            id="presenter-voice-id"
            value={draft}
            placeholder="506420c8af914cb6a3cc3c350ccb411d"
            disabled={disabled || request.isPending}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void add();
              }
            }}
          />
        </div>
        <Button type="button" variant="outline" onClick={add} disabled={disabled || !draft.trim() || request.isPending}>
          {request.isPending ? <Spinner className="size-4" /> : null}
          Check it
        </Button>
      </div>
    </fieldset>
  );
}

function VoiceRow({
  voice,
  selected,
  disabled,
  onSelect,
}: {
  voice: HeyGenVoiceRow;
  selected: boolean;
  disabled?: boolean;
  onSelect: () => void;
}) {
  const usable = voice.status === 'ok';
  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-3 rounded-lg border p-3',
        selected && 'border-primary bg-accent/40 ring-1 ring-primary/30',
      )}
    >
      <button
        type="button"
        onClick={onSelect}
        // A voice HeyGen has refused cannot be chosen. It stays on the list
        // saying so, because deleting the row would only invite the same id
        // to be typed again.
        disabled={disabled || !usable}
        aria-pressed={selected}
        className="flex-1 text-left disabled:cursor-not-allowed disabled:opacity-60"
      >
        <span className="block text-sm font-medium">{voice.name ?? voice.voice_id}</span>
        <span className="block text-xs text-muted-foreground">
          {voice.status === 'pending' && 'Checking with HeyGen…'}
          {voice.status === 'unknown' && (voice.error ?? 'HeyGen does not recognise this id')}
          {usable && [voice.voice_id, voice.language, voice.gender].filter(Boolean).join(' · ')}
        </span>
      </button>
      {/* HeyGen's own reference recording. Hearing a narrator before a reel is
          committed to it is the only check available from here. */}
      {voice.preview_audio_url && (
        // biome-ignore lint/a11y/useMediaCaption: a voice sample has no transcript to caption.
        <audio controls preload="none" src={voice.preview_audio_url} className="h-8 max-w-56" />
      )}
    </div>
  );
}
