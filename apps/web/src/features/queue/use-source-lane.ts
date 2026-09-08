import { useQuery } from '@tanstack/react-query';
import type { ProductionRow, StylePresetRow } from '@/lib/database.types';
import { stylePresetQueryOptions } from './api';
import { missingSourceInput, needsSourceFootage, type SourceGap } from './pipeline-steps';

/**
 * Whether this production renders from an uploaded file, and what it still needs.
 *
 * A hook rather than a prop because the two places the script gate is drawn
 * have different amounts of context: the production page already holds the
 * style, the idea page holds only a `ProductionRow`. Reading the preset here
 * means both call sites are the same one line, and react-query serves the
 * second reader from cache.
 *
 * The preset is read by id rather than out of the active list. A preset can be
 * deactivated after a production chose it — which is exactly what will happen
 * to this lane's own preset while its cost is being measured — and a production
 * already under way must still know what it is rendering.
 */
export interface SourceLane {
  style: StylePresetRow | null;
  /** True only once the preset has actually been read. */
  needsSource: boolean;
  /** The next input the owner has to supply, or null when there is none left. */
  gap: SourceGap | null;
  isLoading: boolean;
}

export function useSourceLane(production: ProductionRow): SourceLane {
  const query = useQuery(stylePresetQueryOptions(production.style_preset_id));
  const style = query.data ?? null;
  const mode = style?.render_mode ?? null;

  return {
    style,
    // Deliberately false while the preset loads: this drives "show the upload
    // panel", and flashing one onto a production that does not need footage
    // would be worse than showing it a moment late.
    needsSource: needsSourceFootage(mode),
    gap: missingSourceInput(production, mode),
    isLoading: query.isPending,
  };
}
