import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { ArrowRightIcon } from 'lucide-react';
import { z } from 'zod';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ideaPageQueryOptions } from '@/features/queue/api';
import { DismissIdeaButton } from '@/features/queue/components/dismiss-idea-button';
import { InProductionPanel } from '@/features/queue/components/in-production-panel';
import { PageHeader } from '@/features/queue/components/page-header';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';
import { QueuePagination } from '@/features/queue/components/queue-pagination';
import { VelocityBadge } from '@/features/queue/components/velocity-badge';
import { useRefreshQueueWhenRunEnds } from '@/features/trends/api';
import { GenerateIdeasButton, TrendRunBanner } from '@/features/trends/components/generate-ideas';
import { TrendInputsCard } from '@/features/trends/components/trend-inputs-card';
import { formatRelative, titleCase } from '@/lib/format';

/**
 * The page is a search param rather than component state so it survives the
 * round trip through an idea's detail page, which is the most common way to
 * leave this list and come back to it.
 *
 * `catch` rather than a validation error: `?page=banana` in a pasted link
 * should land on the queue, not on an error boundary.
 */
const searchSchema = z.object({
  page: z.coerce.number().int().min(1).catch(1).default(1),
});

export const Route = createFileRoute('/_app/queue')({
  validateSearch: searchSchema,
  loaderDeps: ({ search: { page } }) => ({ page }),
  loader: ({ context, deps }) => context.queryClient.ensureQueryData(ideaPageQueryOptions('pending', deps.page)),
  component: IdeaQueuePage,
});

function IdeaQueuePage() {
  const { page } = Route.useSearch();
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

      <TrendRunBanner />

      {/* Directly under the button that spends it. These are the inputs
          that decide what the next run finds, and until now they lived on
          another page — so the one question this page raises ("why is
          nothing coming through?") was answered somewhere else. Shut by
          default: the ideas are what this page is for. */}
      <TrendInputsCard collapsible />

      {/* Above the pending list on purpose: work already authorised, and
          possibly stuck, matters more than the next decision to make. */}
      <InProductionPanel />

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
          description="Trend research has not proposed any new ideas since the last time you looked. Generating more searches whatever is set under Trend search inputs above."
        />
      )}

      {ideas.length > 0 && (
        <ul className="space-y-3">
          {ideas.map((idea) => (
            <li key={idea.id}>
              <Card className="transition-colors hover:border-primary/40">
                <CardHeader>
                  <div className="flex flex-wrap items-center gap-2">
                    <VelocityBadge label={idea.velocity_label} ratio={idea.velocity_ratio} />
                    {idea.trend_keyword && (
                      <Badge variant="outline" className="max-w-full truncate font-normal text-muted-foreground">
                        {idea.trend_keyword}
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">{formatRelative(idea.created_at)}</span>
                  </div>
                  <CardTitle className="text-base break-words">{idea.title}</CardTitle>
                  {idea.hook && <CardDescription className="break-words">{idea.hook}</CardDescription>}
                  {/* Top right rather than beside Review: the two are opposite
                      decisions, and on a phone Review takes the full width. */}
                  <CardAction>
                    <DismissIdeaButton ideaId={idea.id} title={idea.title} />
                  </CardAction>
                </CardHeader>
                {/* Stacked on a phone, one row from `sm` up: the platform
                    badges and the button both need their full width below
                    that, and a half-width button is a poor tap target. */}
                <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div className="flex flex-wrap gap-1.5">
                    {idea.target_platforms.map((platform) => (
                      <Badge key={platform} variant="secondary" className="font-normal">
                        {titleCase(platform)}
                      </Badge>
                    ))}
                  </div>
                  <Button asChild size="sm" className="w-full sm:w-auto">
                    <Link to="/queue/$ideaId" params={{ ideaId: idea.id }}>
                      Review
                      <ArrowRightIcon className="size-4" />
                    </Link>
                  </Button>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}

      <QueuePagination page={page} pageCount={pageCount} />
    </div>
  );
}
