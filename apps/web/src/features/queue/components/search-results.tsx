import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { Button } from '@/components/ui/button';
import { Card, CardAction, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { searchIdeasQueryOptions } from '@/features/queue/api';
import { IdeaCard } from '@/features/queue/components/idea-card';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';
import { TREND_RUN_MINUTES, useSelectedTrendRun } from '@/features/trends/api';
import { SearchInterpretation } from '@/features/trends/components/search-states';
import { isSearchRun, truncatePrompt } from '@/features/trends/search';
import { isTrendRunInFlight } from '@/lib/database.types';
import { formatMinutes } from '@/lib/format';

/**
 * The queue narrowed to one described search, best fit first.
 *
 * Pending ideas only, so Review and Dismiss mean what they mean everywhere
 * else; the header quotes the run's own count for the total. Unpaged, because
 * a run inserts at most `ideas_per_run`. The empty states are about *this*
 * search -- "Nothing waiting" would be the wrong sentence for a list that is
 * empty because the search is still going.
 */
export function SearchResults({ runId }: { runId: string }) {
  const { run, isPending: isRunPending, error: runError } = useSelectedTrendRun(runId);
  const { data: ideas, isPending, error } = useQuery(searchIdeasQueryOptions(runId));

  const back = (
    <Button asChild variant="outline" size="sm">
      <Link to="/queue" search={{ page: 1 }}>
        Back to the full queue
      </Link>
    </Button>
  );

  if (runError) return <QueryError error={runError} />;
  if (!run && isRunPending) return <ListSkeleton />;
  if (!run || !isSearchRun(run)) {
    return (
      <EmptyQueue
        title="That search is not here any more"
        description="It may have been removed, or the link is from somewhere else. The full queue is still here."
        action={back}
      />
    );
  }

  const pending = ideas ?? [];
  const inFlight = isTrendRunInFlight(run);
  const finished = run.status === 'succeeded';
  const inserted = run.inserted ?? 0;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base break-words">Ideas for “{truncatePrompt(run.prompt, 140)}”</CardTitle>
          <CardDescription className="space-y-2">
            <SearchInterpretation interpretation={run.interpretation} />
            <span className="block">
              {run.inserted !== null
                ? `${pending.length} of ${run.inserted} still to decide`
                : inFlight
                  ? 'Still searching'
                  : `${pending.length} idea${pending.length === 1 ? '' : 's'}`}
            </span>
          </CardDescription>
          <CardAction>{back}</CardAction>
        </CardHeader>
      </Card>

      {error && <QueryError error={error} />}
      {isPending && <ListSkeleton />}

      {!isPending && pending.length === 0 && inFlight && (
        <EmptyQueue
          title="Searching…"
          description={`Ideas appear here when the search finishes. That takes up to ${formatMinutes(TREND_RUN_MINUTES)}, and you can leave this page.`}
        />
      )}
      {!isPending && pending.length === 0 && finished && inserted > 0 && (
        <EmptyQueue
          title="All decided"
          description={`You have decided on all ${inserted} idea${inserted === 1 ? '' : 's'} from this search.`}
          action={back}
        />
      )}
      {!isPending && pending.length === 0 && finished && inserted === 0 && (
        <EmptyQueue
          title="Nothing came back"
          description="See the note above the search box for what to try."
          action={back}
        />
      )}
      {!isPending && pending.length === 0 && (run.status === 'failed' || run.status === 'cancelled') && (
        <EmptyQueue title="Nothing was added" description="See the note above the search box." action={back} />
      )}

      {pending.length > 0 && (
        <ul className="space-y-3">
          {pending.map((idea) => (
            <li key={idea.id}>
              <IdeaCard idea={idea} variant="search" />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
