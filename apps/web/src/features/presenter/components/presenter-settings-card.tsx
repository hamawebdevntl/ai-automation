import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { useOwner } from '@/features/auth/use-owner';
import { looksQueryOptions, useSetPresetPresenter, voicesQueryOptions } from '@/features/presenter/api';
import { EMPTY_DRAFT, type PresenterDraft, PresenterPicker } from '@/features/presenter/components/presenter-picker';
import { blockedReason, engineFor } from '@/features/presenter/presenter';
import { presenterOf } from '@/features/presenter/preset';
import { stylePresetsQueryOptions } from '@/features/queue/api';

/**
 * Who fronts the presenter lane by default.
 *
 * This card replaces a migration. The pair used to be two literals in
 * `20260903120000_heygen_presenter_lane.sql`, so changing the presenter meant
 * writing SQL and pushing it, and nobody could see what the account could even
 * use. DEPLOY.md's answer was a curl and a `jsonb_set`.
 *
 * The save goes through `set_preset_presenter` rather than an UPDATE. `params`
 * is one jsonb column holding the whole lane configuration — aspect ratio,
 * resolution, captions — and a browser writing a replacement object built from
 * a stale read would silently drop the rest of it.
 */
export function PresenterSettingsCard() {
  const { isOwner } = useOwner();
  const presetsQuery = useQuery(stylePresetsQueryOptions());
  const looksQuery = useQuery(looksQueryOptions());
  const voicesQuery = useQuery(voicesQueryOptions());
  const save = useSetPresetPresenter();

  const preset = (presetsQuery.data ?? []).find((row) => row.render_mode === 'heygen') ?? null;
  const saved = presenterOf(preset);
  // The two ids rather than the object `presenterOf` builds: it narrows a
  // `Json` column and returns a fresh object every render, so an effect
  // depending on it would re-seed the draft on every pass and never settle.
  const savedAvatarId = saved?.avatar_id ?? null;
  const savedVoiceId = saved?.voice_id ?? null;

  const [draft, setDraft] = useState<PresenterDraft>(EMPTY_DRAFT);

  // Seeded from the preset once it arrives, so the card opens showing what is
  // in force rather than an empty picker that reads like "nothing is set".
  useEffect(() => {
    if (!savedAvatarId) return;
    setDraft({ avatarId: savedAvatarId, voiceId: savedVoiceId, confirmed: false });
  }, [savedAvatarId, savedVoiceId]);

  if (!preset) {
    // Not an error: a deployment can legitimately run the stock lanes only,
    // and the presenter preset ships inactive until the lane is wanted.
    return null;
  }

  const look = (looksQuery.data ?? []).find((row) => row.avatar_id === draft.avatarId) ?? null;
  const voice = (voicesQuery.data ?? []).find((row) => row.voice_id === draft.voiceId) ?? null;
  const blocked = blockedReason(look, voice, draft.confirmed);
  const dirty = draft.avatarId !== savedAvatarId || draft.voiceId !== savedVoiceId;

  async function onSave() {
    if (!preset || !look || !voice) return;
    try {
      await save.mutateAsync({
        presetId: preset.id,
        avatarId: look.avatar_id,
        voiceId: voice.voice_id,
        engine: engineFor(look),
      });
      toast.success('Presenter saved', {
        description: `${look.name ?? look.avatar_id} will front every reel on this lane from now on.`,
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not save the presenter');
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Presenter</CardTitle>
        <CardDescription>
          Who fronts a <strong>{preset.name}</strong> reel, and who narrates it. An owner can override this for one
          production at Gate 1.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <PresenterPicker value={draft} onChange={setDraft} disabled={!isOwner || save.isPending} />

        <Separator />

        {!isOwner ? (
          <p className="text-xs text-muted-foreground">
            Viewers can read this. Only an owner can change it — enforced by row-level security, not by this page.
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <Button type="button" onClick={onSave} disabled={!dirty || save.isPending || Boolean(blocked)}>
              {save.isPending ? 'Saving…' : 'Save as the default'}
            </Button>
            {blocked ? (
              <span className="text-xs text-destructive">{blocked}</span>
            ) : (
              dirty && <span className="text-xs text-muted-foreground">Unsaved changes</span>
            )}
          </div>
        )}

        {!saved && (
          <Alert>
            <AlertTitle>This preset names no presenter</AlertTitle>
            <AlertDescription>
              A production on this lane cannot render until one is chosen — the render step refuses outright rather than
              letting HeyGen pick.
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
