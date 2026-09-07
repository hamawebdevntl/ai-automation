import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { ArrowRightIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';
import { productionEventsQueryOptions, productionForIdeaQueryOptions } from '../api';
import { describeProduction, isAtScriptGate } from '../pipeline-steps';
import { useProductionStream } from '../use-production-stream';
import { ProductionStatusBadge } from './production-status';
import { ProductionTimeline } from './production-timeline';
import { ScriptEditor } from './script-editor';

/**
 * What happened after this idea was approved, on the idea's own page.
 *
 * Approving used to navigate straight back to the queue, so the moment a
 * decision was made the thing it set in motion left the screen. Staying here
 * and watching it start is the point of this panel.
 *
 * It is also where the script is read and approved. That is deliberate rather
 * than incidental: the script gate is the second thing an owner does to an
 * idea, seconds after the first, and sending them to another page to do it
 * would recreate exactly the disappearing-act this panel was built to fix.
 */

/**
 * How often to look for the production while it does not exist yet.
 *
 * The dispatcher opens one within `driver_poll_seconds` — five by default — so
 * this window is real but short. It is the one place in this feature that
 * polls: there is no row yet, so there is nothing for a row-filtered
 * subscription to attach to.
 */
const STARTING_POLL_MS = 2000;

export function IdeaProductionPanel({ ideaId }: { ideaId: string }) {
  const productionQuery = useQuery({
    ...productionForIdeaQueryOptions(ideaId),
    refetchInterval: (query) => (query.state.data ? false : STARTING_POLL_MS),
  });

  const production = productionQuery.data ?? null;

  useProductionStream(production?.id);

  const eventsQuery = useQuery({
    ...productionEventsQueryOptions(production?.id ?? ''),
    enabled: production !== null,
  });

  if (productionQuery.isPending) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">In production</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Looking for this idea's production…</p>
        </CardContent>
      </Card>
    );
  }

  if (!production) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <Spinner className="size-4" />
            Starting
          </CardTitle>
          <CardDescription>
            Approved. A worker opens the production within a few seconds — this updates on its own.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const verdict = describeProduction(production);

  // At the gate the script is the whole of what this panel is for, so it goes
  // above the timeline. Afterwards the timeline is the story and the script is
  // reference, so it goes below -- locked, because the render has been paid for
  // against it.
  const atGate = isAtScriptGate(production);
  const script = <ScriptEditor production={production} />;

  const record = (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">In production</CardTitle>
          <ProductionStatusBadge production={production} />
        </div>
        <CardDescription>{verdict.detail ?? 'Where this idea has got to since you approved it.'}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <ProductionTimeline
          production={production}
          events={eventsQuery.data ?? []}
          isLoadingEvents={eventsQuery.isPending}
        />

        <Button asChild variant="outline" size="sm">
          <Link to="/productions/$productionId" params={{ productionId: production.id }}>
            Open the full record and controls
            <ArrowRightIcon className="size-4" />
          </Link>
        </Button>
      </CardContent>
    </Card>
  );

  return (
    <div className="space-y-6">
      {atGate && script}
      {record}
      {!atGate && production.script && script}
    </div>
  );
}
