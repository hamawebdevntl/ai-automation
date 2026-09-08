import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { ChevronRightIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import type { IdeaRow } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import { liveProductionsQueryOptions, undispatchedIdeasQueryOptions } from '../api';
import { describeProduction, hasStarted, WORKER_GRACE_MS } from '../pipeline-steps';
import { useLiveProductionsStream } from '../use-production-stream';
import { ProductionProgress } from './production-status';

/**
 * What the pipeline is doing with the ideas already approved.
 *
 * This panel is the answer to the question the queue page could not previously
 * answer at all: an owner approved something, the page navigated back to a list
 * of *pending* ideas, and the work they had just authorised became invisible
 * until it surfaced at Gate 2 — or never, if it stopped on the way.
 *
 * Ordered so the things wanting a person come first. A backlog is read top
 * down, and a stopped production is the only kind that will not move on its own.
 */
/** Re-read while anything is still waiting on a worker, so staleness shows. */
const UNSTARTED_POLL_MS = 10_000;

export function InProductionPanel() {
  const { data, isPending, error } = useQuery({
    ...liveProductionsQueryOptions(),
    refetchInterval: (query) =>
      query.state.data?.some((item) => !hasStarted(item.production)) ? UNSTARTED_POLL_MS : false,
  });
  const undispatched = useQuery({
    ...undispatchedIdeasQueryOptions(),
    refetchInterval: (query) => (query.state.data && query.state.data.length > 0 ? UNSTARTED_POLL_MS : false),
  });
  useLiveProductionsStream();

  if (error) return null;

  if (isPending) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">In production</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <Skeleton className="h-16 w-full" />
        </CardContent>
      </Card>
    );
  }

  const productions = [...(data ?? [])].sort(
    (a, b) => urgency(describeProduction(a.production).tone) - urgency(describeProduction(b.production).tone),
  );

  const orphans = undispatched.data ?? [];
  if (productions.length === 0 && orphans.length === 0) return null;

  const needingYou =
    productions.filter((item) => {
      const tone = describeProduction(item.production).tone;
      return tone === 'bad' || tone === 'waiting';
    }).length + orphans.filter((idea) => isOverdue(idea)).length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          In production
          <span className="ml-2 text-sm font-normal text-muted-foreground">{productions.length + orphans.length}</span>
        </CardTitle>
        <CardDescription>
          Ideas you have already approved, and where each one has got to.
          {needingYou > 0 && ` ${needingYou === 1 ? 'One is' : `${needingYou} are`} waiting on you.`}
        </CardDescription>
      </CardHeader>

      <CardContent>
        <ul className="space-y-2">
          {/* Approved and not yet dispatched. These used to be in no list at
              all: gone from the pending queue, not yet in this one. */}
          {orphans.map((idea) => (
            <li key={idea.id}>
              <Link
                to="/queue/$ideaId"
                params={{ ideaId: idea.id }}
                className="flex items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-accent/50"
              >
                <div className="min-w-0 flex-1 space-y-1.5">
                  <p className="text-sm font-medium break-words">{idea.title}</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant="outline"
                      className={
                        isOverdue(idea)
                          ? 'gap-1.5 font-normal border-amber-500/40 text-amber-700 dark:text-amber-300'
                          : 'gap-1.5 font-normal border-blue-500/40 text-blue-700 dark:text-blue-300'
                      }
                    >
                      {isOverdue(idea) ? 'Waiting for a worker' : 'Starting'}
                    </Badge>
                    <span className="text-xs text-muted-foreground">approved {formatRelative(idea.decided_at)}</span>
                  </div>
                  {isOverdue(idea) && (
                    <p className="text-xs text-muted-foreground">
                      No worker has opened a production for this. The pipeline worker is probably not running.
                    </p>
                  )}
                </div>
                <ChevronRightIcon className="mt-1 size-4 shrink-0 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
          {productions.map(({ production, idea }) => (
            <li key={production.id}>
              <Link
                to="/productions/$productionId"
                params={{ productionId: production.id }}
                className="flex items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-accent/50"
              >
                <div className="min-w-0 flex-1 space-y-2">
                  <p className="text-sm font-medium break-words">{idea?.title ?? 'Untitled idea'}</p>
                  <ProductionProgress production={production} />
                </div>
                <ChevronRightIcon className="mt-1 size-4 shrink-0 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

/** Stopped first, then waiting on a person, then whatever is simply running. */
function urgency(tone: ReturnType<typeof describeProduction>['tone']): number {
  switch (tone) {
    case 'bad':
      return 0;
    case 'waiting':
      return 1;
    case 'working':
      return 2;
    case 'held':
      return 3;
    default:
      return 4;
  }
}

/** Approved longer ago than a worker should ever take to notice. */
function isOverdue(idea: IdeaRow, now: number = Date.now()): boolean {
  if (!idea.decided_at) return false;
  return now - new Date(idea.decided_at).getTime() > WORKER_GRACE_MS;
}
