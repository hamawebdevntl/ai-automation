import { Badge } from '@/components/ui/badge';
import type { VelocityLabel } from '@/lib/database.types';
import { cn } from '@/lib/utils';

const LABEL_STYLES: Record<VelocityLabel, string> = {
  breakout: 'border-transparent bg-emerald-600/15 text-emerald-700 dark:text-emerald-400',
  rising: 'border-transparent bg-sky-600/15 text-sky-700 dark:text-sky-400',
  steady: 'border-transparent bg-muted text-muted-foreground',
  declining: 'border-transparent bg-amber-600/15 text-amber-700 dark:text-amber-400',
};

/**
 * Trend velocity is scored against the channel's own baseline rather than
 * absolute view counts, so the ratio is the informative half — 3.4x on a small
 * baseline is a better signal than a large flat number.
 */
export function VelocityBadge({
  label,
  ratio,
  className,
}: {
  label: VelocityLabel | null;
  ratio: number | null;
  className?: string;
}) {
  if (!label) return null;
  return (
    <Badge variant="outline" className={cn(LABEL_STYLES[label], className)}>
      {label}
      {ratio !== null && <span className="ml-1 font-mono text-[0.7rem] opacity-80">{ratio.toFixed(1)}x</span>}
    </Badge>
  );
}
