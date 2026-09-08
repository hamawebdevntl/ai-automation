import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';

export type Fit = 'strong' | 'good' | 'loose';

const FIT_LABELS: Record<Fit, string> = { strong: 'Strong fit', good: 'Good fit', loose: 'Loose fit' };

const FIT_STYLES: Record<Fit, string> = {
  strong: 'border-transparent bg-emerald-600/15 text-emerald-700 dark:text-emerald-400',
  good: 'border-transparent bg-sky-600/15 text-sky-700 dark:text-sky-400',
  loose: 'border-transparent bg-muted text-muted-foreground',
};

/** 0–100 into three words. The bands are for reading at a glance; the list is sorted on the number. */
export function fitOf(relevance: number): Fit {
  if (relevance >= 75) return 'strong';
  if (relevance >= 50) return 'good';
  return 'loose';
}

/**
 * How well an idea fits the description its run was given.
 *
 * Null renders nothing: only a described search scores fit, and an ordinary
 * run's ideas should not carry an empty badge implying they were judged.
 */
export function RelevanceBadge({ relevance, className }: { relevance: number | null; className?: string }) {
  if (relevance === null) return null;
  const fit = fitOf(relevance);
  return (
    <Badge variant="outline" className={cn(FIT_STYLES[fit], className)}>
      {FIT_LABELS[fit]}
      <span className="ml-1 font-mono text-[0.7rem] opacity-80">{relevance}</span>
    </Badge>
  );
}
