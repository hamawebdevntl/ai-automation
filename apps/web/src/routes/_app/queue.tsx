import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { ArrowRightIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { ideasQueryOptions } from '@/features/queue/api';
import { PageHeader } from '@/features/queue/components/page-header';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';
import { VelocityBadge } from '@/features/queue/components/velocity-badge';
import { formatRelative, titleCase } from '@/lib/format';

export const Route = createFileRoute('/_app/queue')({
  loader: ({ context }) => context.queryClient.ensureQueryData(ideasQueryOptions('pending')),
  component: IdeaQueuePage,
});

function IdeaQueuePage() {
  const { data: ideas, isPending, error } = useQuery(ideasQueryOptions('pending'));

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6">
      <PageHeader
        title="Gate 1 · Ideas"
        description="Approve an idea and choose how it gets made. Nothing is spent until you do — this is the decision that commits the budget."
        count={ideas?.length}
      />

      {error && <QueryError error={error} />}
      {isPending && <ListSkeleton />}

      {ideas && ideas.length === 0 && (
        <EmptyQueue
          title="Nothing waiting"
          description="Trend research has not proposed any new ideas since the last time you looked."
        />
      )}

      {ideas && ideas.length > 0 && (
        <ul className="space-y-3">
          {ideas.map((idea) => (
            <li key={idea.id}>
              <Card className="transition-colors hover:border-primary/40">
                <CardHeader>
                  <div className="flex flex-wrap items-center gap-2">
                    <VelocityBadge label={idea.velocity_label} ratio={idea.velocity_ratio} />
                    {idea.trend_keyword && (
                      <Badge variant="outline" className="font-normal text-muted-foreground">
                        {idea.trend_keyword}
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">{formatRelative(idea.created_at)}</span>
                  </div>
                  <CardTitle className="text-base">{idea.title}</CardTitle>
                  {idea.hook && <CardDescription>{idea.hook}</CardDescription>}
                </CardHeader>
                <CardContent className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap gap-1.5">
                    {idea.target_platforms.map((platform) => (
                      <Badge key={platform} variant="secondary" className="font-normal">
                        {titleCase(platform)}
                      </Badge>
                    ))}
                  </div>
                  <Button asChild size="sm">
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
    </div>
  );
}
