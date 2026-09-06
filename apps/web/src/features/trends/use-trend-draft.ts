import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { describeConstraintViolation, trendSettingsQueryOptions, useSaveTrendSettings } from '@/features/trends/api';
import { describeOutOfBounds, outOfBounds } from '@/features/trends/controls';
import type { TrendSettingsRow } from '@/lib/database.types';
import { toError } from '@/lib/supabase-error';

/**
 * An editable slice of the trend settings.
 *
 * The settings are spread across several cards, and each card edits its own
 * fields and saves only those. That is not a layout convenience: a card that
 * sent the whole row would let a tab left open on the schedule silently revert
 * a filter someone changed afterwards, and there is no server tier to notice.
 *
 * So a draft is a named subset. `keys` must be a module-level constant -- a
 * fresh array each render would re-seed the draft on every render and discard
 * what was being typed.
 */
export function useTrendDraft<K extends keyof TrendSettingsRow>(keys: readonly K[]) {
  const { data, isPending, error } = useQuery(trendSettingsQueryOptions());
  const save = useSaveTrendSettings();
  const [draft, setDraft] = useState<Pick<TrendSettingsRow, K> | null>(null);

  const stored = useMemo(() => (data ? pick(data, keys) : null), [data, keys]);

  // Seeded when the row arrives, and re-seeded after a save so the fields show
  // what was stored rather than what was typed -- the two differ whenever the
  // save normalised something, which is exactly when the owner needs to see it.
  useEffect(() => {
    if (stored) setDraft(stored);
  }, [stored]);

  const set = useCallback((patch: Partial<Pick<TrendSettingsRow, K>>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const dirty = Boolean(draft && stored) && JSON.stringify(draft) !== JSON.stringify(stored);

  // Every bounded field in this card's slice, checked in one place so a card
  // cannot forget one. Without it an out-of-range value reaches the CHECK
  // constraint, and the owner is shown a Postgres error naming a constraint
  // rather than being stopped at the field they typed it into.
  const blockedReason = draft ? describeOutOfBounds(outOfBounds(draft)) : null;

  const onSave = useCallback(async () => {
    if (!draft) return;
    try {
      await save.mutateAsync(draft);
      toast.success('Saved. This applies to the next run.');
    } catch (e) {
      // A bound is enforced by Postgres, not by this bundle, so a refusal
      // arrives as a constraint name. Saying which value it was about is the
      // difference between a correctable mistake and a wall.
      const refused = describeConstraintViolation(e);
      toast.error(refused ?? 'Could not save', { description: refused ? undefined : toError(e).message });
    }
  }, [draft, save]);

  return { data, draft, set, dirty, isPending, error, isSaving: save.isPending, blockedReason, onSave };
}

function pick<T extends object, K extends keyof T>(source: T, keys: readonly K[]): Pick<T, K> {
  const out = {} as Pick<T, K>;
  for (const key of keys) out[key] = source[key];
  return out;
}
