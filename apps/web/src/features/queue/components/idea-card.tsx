import { Link } from '@tanstack/react-router';
import { ArrowRightIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { DismissIdeaButton } from '@/features/queue/components/dismiss-idea-button';
import { RelevanceBadge } from '@/features/queue/components/relevance-badge';
import { VelocityBadge } from '@/features/queue/components/velocity-badge';
import type { IdeaRow } from '@/lib/database.types';
import { formatRelative, titleCase } from '@/lib/format';

export interface IdeaCardProps {
  idea: IdeaRow;
  /**
   * `queue` is the card as it has always been. `search` is the same card
   * answering the question the owner asked: why this is trending now, and how
   * it connects to what they described.
   */
  variant?: 'queue' | 'search';
}

/**
 * One idea in a list.
 *
 * Extracted from the queue route once it grew a second variant. The two share
 * everything that decides an idea -- velocity, keyword, title, hook, Review and
 * Dismiss -- and differ only in what a described search has to add.
 */
export function IdeaCard({ idea, variant = 'queue' }: IdeaCardProps) {
  const searching = variant === 'search';
  return (
    <Card className="transition-colors hover:border-primary/40">
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <VelocityBadge label={idea.velocity_label} ratio={idea.velocity_ratio} />
          {searching && <RelevanceBadge relevance={idea.relevance} />}
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
      <CardContent className="space-y-3">
        {searching && (idea.rationale || idea.connection) && (
          <div className="space-y-2">
            <Line label="Why now">{idea.rationale}</Line>
            <Line label="How it fits">{idea.connection}</Line>
          </div>
        )}
        {/* Stacked on a phone, one row from `sm` up: the platform badges and
            the button both need their full width below that, and a half-width
            button is a poor tap target. */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
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
        </div>
      </CardContent>
    </Card>
  );
}

/** A labelled line, in the style the Review page uses for an idea's details. */
function Line({ label, children }: { label: string; children: string | null }) {
  if (!children) return null;
  return (
    <div className="space-y-0.5">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="text-sm leading-relaxed break-words">{children}</p>
    </div>
  );
}
