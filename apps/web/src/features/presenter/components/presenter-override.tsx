import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { Switch } from '@/components/ui/switch';
import { looksQueryOptions, voicesQueryOptions } from '@/features/presenter/api';
import { EMPTY_DRAFT, type PresenterDraft, PresenterPicker } from '@/features/presenter/components/presenter-picker';
import { blockedReason, toChoice } from '@/features/presenter/presenter';
import { presenterOf } from '@/features/presenter/preset';
import type { PresenterChoice, StylePresetRow } from '@/lib/database.types';

/** What the approving page needs to know: what to send, and why it may not. */
export interface PresenterOverrideState {
  /** Null means "use the preset", which is the default and the common case. */
  choice: PresenterChoice | null;
  /** A half-made override. Named so the Approve button is never mutely dead. */
  blocked: string | null;
}

export const NO_OVERRIDE: PresenterOverrideState = { choice: null, blocked: null };

/**
 * Swapping the presenter for one production, at Gate 1.
 *
 * Off by default, and the default is the preset's pair — the point of the
 * preset is that most reels do not need this decision made again. Turning it
 * on is what makes the picker appear.
 *
 * The override is stored on the idea and read by the render step; the pair
 * that actually ran is recorded on the production afterwards. It is validated
 * in `approve_idea` against the same cached catalogue this picker lists,
 * because an avatar this account cannot use fails *terminally* — which would
 * happen after this gate, having already spent the review that reached it.
 */
export function PresenterOverride({
  preset,
  onChange,
  disabled,
}: {
  preset: StylePresetRow;
  onChange: (state: PresenterOverrideState) => void;
  disabled?: boolean;
}) {
  const looksQuery = useQuery(looksQueryOptions());
  const voicesQuery = useQuery(voicesQueryOptions());

  const [on, setOn] = useState(false);
  const [draft, setDraft] = useState<PresenterDraft>(EMPTY_DRAFT);

  const saved = presenterOf(preset);
  const look = (looksQuery.data ?? []).find((row) => row.avatar_id === draft.avatarId) ?? null;
  const voice = (voicesQuery.data ?? []).find((row) => row.voice_id === draft.voiceId) ?? null;

  const blocked = on ? blockedReason(look, voice, draft.confirmed) : null;
  const choice = on && look && voice && !blocked ? toChoice(look, voice) : null;

  // Reported through an effect rather than from the handlers because the state
  // can change without anyone touching the form: a voice sits at "checking…"
  // until the worker answers, and that answer is what unblocks Approve.
  const signature = `${on}|${choice?.avatar_id ?? ''}|${choice?.voice_id ?? ''}|${blocked ?? ''}`;
  // biome-ignore lint/correctness/useExhaustiveDependencies: `signature` is the value identity of choice+blocked; the objects are rebuilt every render.
  useEffect(() => {
    onChange({ choice, blocked });
  }, [signature]);

  return (
    <div className="space-y-4">
      <Separator />
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <Label htmlFor="presenter-override-toggle">Use a different presenter for this one</Label>
          <p className="text-xs text-muted-foreground">
            {saved
              ? `Otherwise this reel is fronted by the preset's avatar (${saved.avatar_id}).`
              : 'This preset names no presenter, so one must be chosen in Settings before it can render.'}
          </p>
        </div>
        <Switch
          id="presenter-override-toggle"
          checked={on}
          disabled={disabled}
          onCheckedChange={(next) => {
            setOn(next);
            // Cleared on the way off, so turning it back on cannot silently
            // reinstate a pair the owner had abandoned.
            if (!next) setDraft(EMPTY_DRAFT);
          }}
        />
      </div>

      {on && <PresenterPicker value={draft} onChange={setDraft} disabled={disabled} />}
    </div>
  );
}
