import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { ArrowRightIcon, CircleAlertIcon, FilmIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { reviewQueueQueryOptions } from '@/features/queue/api';
import { PageHeader } from '@/features/queue/components/page-header';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';
import { formatDuration, formatRelative, formatUsd, parseQcReport } from '@/lib/format';

export const Route = createFileRoute('/_app/review')({
  loader: ({ context }) => context.queryClient.ensureQueryData(reviewQueueQueryOptions()),
  component: ReviewQueuePage,
});

function ReviewQueuePage() {
  const { data: items, isPending, error } = useQuery(reviewQueueQueryOptions());

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6">
      <PageHeader
        title="Gate 2 · Finished cuts"
        description="Nothing publishes without a sign-off here. The quality check has already looked at the file — your job is whether the message is right to go out under our name."
        count={items?.length}
      />

      {error && <QueryError error={error} />}
      {isPending && <ListSkeleton />}

      {items && items.length === 0 && (
        <EmptyQueue title="Nothing to review" description="No finished cut is waiting on a decision right now." />
      )}

      {items && items.length > 0 && (
        <ul className="space-y-3">
          {items.map(({ production, idea, style }) => {
            const qc = parseQcReport(production.qc);
            const qcFailed = production.status === 'qc_failed' || (qc !== null && !qc.passed);

            return (
              <li key={production.id}>
                <Card className="transition-colors hover:border-primary/40">
                  <CardHeader>
                    <div className="flex flex-wrap items-center gap-2">
                      {qcFailed ? (
                        <Badge variant="outline" className="border-transparent bg-destructive/10 text-destructive">
                          <CircleAlertIcon className="size-3" />
                          QC failed
                        </Badge>
                      ) : (
                        <Badge
                          variant="outline"
                          className="border-transparent bg-emerald-600/15 text-emerald-700 dark:text-emerald-400"
                        >
                          QC passed
                        </Badge>
                      )}
                      {style && (
                        <Badge variant="secondary" className="font-normal">
                          {style.name}
                        </Badge>
                      )}
                      <span className="text-xs text-muted-foreground">
                        rendered {formatRelative(production.completed_at ?? production.created_at)}
                      </span>
                    </div>
                    <CardTitle className="text-base">{idea?.title ?? 'Untitled cut'}</CardTitle>
                    {idea?.hook && <CardDescription>{idea.hook}</CardDescription>}
                  </CardHeader>
                  <CardContent className="flex flex-wrap items-center justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-4 text-sm text-muted-foreground tabular-nums">
                      <span className="inline-flex items-center gap-1.5">
                        <FilmIcon className="size-3.5" aria-hidden />
                        {formatDuration(production.duration_seconds)}
                      </span>
                      <span>{formatUsd(production.cost_actual_usd ?? production.cost_estimate_usd)}</span>
                    </div>
                    <Button asChild size="sm">
                      <Link to="/review/$productionId" params={{ productionId: production.id }}>
                        Review
                        <ArrowRightIcon className="size-4" />
                      </Link>
                    </Button>
                  </CardContent>
                </Card>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
