import { Link } from '@tanstack/react-router';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Spinner } from '@/components/ui/spinner';
import type { ProductionRow } from '@/lib/database.types';
import { cn } from '@/lib/utils';
import { buildTimeline, describeProduction, isPaused, renderProgress } from '../pipeline-steps';

const TONE_CLASS: Record<ReturnType<typeof describeProduction>['tone'], string> = {
  working: 'border-blue-500/40 text-blue-700 dark:text-blue-300',
  waiting: 'border-amber-500/40 text-amber-700 dark:text-amber-300',
  good: 'border-emerald-500/40 text-emerald-700 dark:text-emerald-300',
  bad: 'border-destructive/40 text-destructive',
  held: 'border-muted-foreground/30 text-muted-foreground',
};

/**
 * Where a production is, in one badge.
 *
 * The wording comes from `describeProduction` rather than from the status
 * column, because several statuses do not mean what they say on their own:
 * `approved` with a `publishing_disabled` marker is a finished cut held back
 * on purpose, not a production waiting to publish.
 */
export function ProductionStatusBadge({ production }: { production: ProductionRow }) {
  const verdict = describeProduction(production);

  return (
    <Badge variant="outline" className={cn('gap-1.5 font-normal', TONE_CLASS[verdict.tone])}>
      {verdict.tone === 'working' && !isPaused(production) && <Spinner className="size-3" />}
      {verdict.headline}
    </Badge>
  );
}

/**
 * The compact form for a list row: how far along, and what is happening.
 *
 * Enough to scan a backlog and see which productions need attention without
 * opening any of them.
 */
export function ProductionProgress({ production }: { production: ProductionRow }) {
  const steps = buildTimeline(production);
  const settled = steps.filter((step) => step.state === 'done' || step.state === 'skipped').length;
  const verdict = describeProduction(production);
  const renderPct = renderProgress(production);

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <ProductionStatusBadge production={production} />
        <span className="text-xs text-muted-foreground">
          {settled} of {steps.length} steps
        </span>
        {renderPct !== null && (
          <span className="text-xs tabular-nums text-muted-foreground">· rendering {Math.round(renderPct)}%</span>
        )}
      </div>

      <Progress value={(settled / steps.length) * 100} className="h-1" />

      {verdict.detail && <p className="text-xs text-muted-foreground line-clamp-2">{verdict.detail}</p>}
    </div>
  );
}

/** A list row: the production, its progress, and a way into the full record. */
export function ProductionSummaryLink({ production, title }: { production: ProductionRow; title: string }) {
  return (
    <Link
      to="/productions/$productionId"
      params={{ productionId: production.id }}
      className="block rounded-lg border p-3 transition-colors hover:bg-accent/50"
    >
      <p className="mb-2 text-sm font-medium break-words">{title}</p>
      <ProductionProgress production={production} />
    </Link>
  );
}
