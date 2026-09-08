import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { z } from 'zod';
import { Button } from '@/components/ui/button';
import { ideaPageQueryOptions, searchIdeasQueryOptions } from '@/features/queue/api';
import { IdeaCard } from '@/features/queue/components/idea-card';
import { InProductionPanel } from '@/features/queue/components/in-production-panel';
import { PageHeader } from '@/features/queue/components/page-header';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';
import { QueuePagination } from '@/features/queue/components/queue-pagination';
import { SearchResults } from '@/features/queue/components/search-results';
import { trendRunQueryOptions, useRefreshQueueWhenRunEnds } from '@/features/trends/api';
import { AiSearchPanel } from '@/features/trends/components/ai-search-panel';
import { GenerateIdeasButton, TrendRunBanner } from '@/features/trends/components/generate-ideas';
import { TrendInputsCard } from '@/features/trends/components/trend-inputs-card';

/**
 * The page is a search param rather than component state so it survives the
 * round trip through an idea's detail page, which is the most common way to
 * leave this list and come back to it.
 *
 * `catch` rather than a validation error: `?page=banana` in a pasted link
 * should land on the queue, not on an error boundary.
 *
 * `search` is a trend run id and narrows the list to the ideas that run
 * drafted. In the URL for the same reason as the page: a search someone
 * started should survive Review-and-back and be linkable. Same `catch`
 * convention -- a mangled id shows the whole queue rather than an error.
 */
const searchSchema = z.object({
  page: z.coerce.number().int().min(1).catch(1).default(1),
  search: z.uuid().optional().catch(undefined),
});

export const Route = createFileRoute('/_app/queue')({
  validateSearch: searchSchema,
  loaderDeps: ({ search: { page, search } }) => ({ page, search }),
  loader: ({ context, deps }) =>
    deps.search
      ? Promise.all([
          context.queryClient.ensureQueryData(trendRunQueryOptions(deps.search)),
          context.queryClient.ensureQueryData(searchIdeasQueryOptions(deps.search)),
        ])
      : context.queryClient.ensureQueryData(ideaPageQueryOptions('pending', deps.page)),
  component: IdeaQueuePage,
});

function IdeaQueuePage() {
  const { page, search: selectedRunId = null } = Route.useSearch();
  // Read in both modes, so the header count stays "the whole queue" while a
  // search is selected: it is the size of the decision, not of the filter.
  const { data, isPending, error } = useQuery(ideaPageQueryOptions('pending', page));

  // A run finishing is the one thing that fills this list without anybody
  // touching it, so it is the one thing that has to invalidate it.
  useRefreshQueueWhenRunEnds();

  const ideas = data?.ideas ?? [];
  const pageCount = data?.pageCount ?? 1;
  // Approving the last idea on the last page leaves you standing on a page
  // that no longer exists. That is not an error and should not read as one.
  const pastTheEnd = Boolean(data) && ideas.length === 0 && page > 1;

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6">
      <PageHeader
        title="Gate 1 · Ideas"
        description="Approve an idea and choose how it gets made. Nothing is spent until you do — this is the decision that commits the budget."
        count={data?.total}
        actions={<GenerateIdeasButton />}
      />

      {/* The page's first call to action. Describing what you are working on
          is the way in for someone who does not know which keywords to set;
          the saved inputs below are the standing brief. */}
      <AiSearchPanel selectedRunId={selectedRunId} />

      {/* Cause above effect: the banner reports on whichever run the header
          button or the panel started, so it sits under both. It skips the
          selected search, which the panel is already describing. */}
      <TrendRunBanner exceptRunId={selectedRunId} />

      {/* Directly under the controls that spend it. These are the inputs
          that decide what the next ordinary run finds, and until now they
          lived on another page — so the one question this page raises ("why
          is nothing coming through?") was answered somewhere else. Shut by
          default: the ideas are what this page is for. */}
      <TrendInputsCard collapsible />

      {/* Above the pending list on purpose: work already authorised, and
          possibly stuck, matters more than the next decision to make. */}
      <InProductionPanel />

      {selectedRunId ? (
        <SearchResults runId={selectedRunId} />
      ) : (
        <>
          {error && <QueryError error={error} />}
          {isPending && <ListSkeleton />}

          {pastTheEnd && (
            <EmptyQueue
              title="Nothing on this page"
              description="The queue is shorter than it was — ideas here have since been decided."
              action={
                <Button asChild variant="outline" size="sm">
                  <Link to="/queue" search={{ page: 1 }}>
                    Back to the first page
                  </Link>
                </Button>
              }
            />
          )}

          {data && data.total === 0 && (
            <EmptyQueue
              title="Nothing waiting"
              description="Trend research has not proposed any new ideas since the last time you looked. Describe what you want above, or generate more with whatever is set under Trend search inputs."
            />
          )}

          {ideas.length > 0 && (
            <ul className="space-y-3">
              {ideas.map((idea) => (
                <li key={idea.id}>
                  <IdeaCard idea={idea} />
                </li>
              ))}
            </ul>
          )}

          <QueuePagination page={page} pageCount={pageCount} />
        </>
      )}
    </div>
  );
}
