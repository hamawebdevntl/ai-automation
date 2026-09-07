import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { ChevronRightIcon } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { liveProductionsQueryOptions } from '../api';
import { describeProduction } from '../pipeline-steps';
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
export function InProductionPanel() {
  const { data, isPending, error } = useQuery(liveProductionsQueryOptions());
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

  if (productions.length === 0) return null;

  const needingYou = productions.filter((item) => {
    const tone = describeProduction(item.production).tone;
    return tone === 'bad' || tone === 'waiting';
  }).length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          In production
          <span className="ml-2 text-sm font-normal text-muted-foreground">{productions.length}</span>
        </CardTitle>
        <CardDescription>
          Ideas you have already approved, and where each one has got to.
          {needingYou > 0 && ` ${needingYou === 1 ? 'One is' : `${needingYou} are`} waiting on you.`}
        </CardDescription>
      </CardHeader>

      <CardContent>
        <ul className="space-y-2">
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
