/**
 * A clip's position in the recording it came from.
 *
 * Split out because it is the one piece of this feature that appears in three
 * places — the candidate card, the accepted list, and the production panel — and
 * because "1:40 → 1:52 · 12s" is a format worth having exactly one of.
 */

import { MAX_CLIP_SECONDS, MIN_CLIP_SECONDS } from '@/lib/database.types';
import { cn } from '@/lib/utils';

/** `m:ss`, or `h:mm:ss` past an hour. Seconds are dropped to whole numbers:
 *  a timecode shown to two decimals invites precision the model does not have. */
export function timecode(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = whole % 60;
  const mm = h > 0 ? String(m).padStart(2, '0') : String(m);
  return h > 0 ? `${h}:${mm}:${String(s).padStart(2, '0')}` : `${mm}:${String(s).padStart(2, '0')}`;
}

/** How long the clip runs, to the nearest second. */
export function clipLength(start: number, end: number): string {
  return `${Math.round(Math.max(0, end - start))}s`;
}

export function ClipTimecode({ start, end, className }: { start: number; end: number; className?: string }) {
  const length = end - start;
  // The database refuses anything outside these bounds, so this should never
  // show. It is here because the alternative to noticing is a clip that looks
  // ordinary in a list and fails on insert.
  const impossible = length < MIN_CLIP_SECONDS || length > MAX_CLIP_SECONDS;

  return (
    <span className={cn('inline-flex items-center gap-1.5 tabular-nums', className)}>
      <span>
        {timecode(start)} → {timecode(end)}
      </span>
      <span aria-hidden>·</span>
      <span className={impossible ? 'text-destructive' : undefined}>{clipLength(start, end)}</span>
    </span>
  );
}
