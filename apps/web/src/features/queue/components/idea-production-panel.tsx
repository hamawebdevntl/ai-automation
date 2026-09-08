import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { ArrowRightIcon, CircleAlertIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Spinner } from '@/components/ui/spinner';
import { formatRelative } from '@/lib/format';
import { productionEventsQueryOptions, productionForIdeaQueryOptions } from '../api';
import { describeProduction, hasStarted, isAtScriptGate, WORKER_GRACE_MS } from '../pipeline-steps';
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

export function IdeaProductionPanel({ ideaId, decidedAt }: { ideaId: string; decidedAt: string | null }) {
  const productionQuery = useQuery({
    ...productionForIdeaQueryOptions(ideaId),
    // Keep polling until the worker has actually touched the row, not merely
    // until the row exists. A row that is never claimed is the case this
    // panel most needs to notice, and the re-render is what lets "Starting"
    // become "Waiting for a worker" without anyone reloading.
    refetchInterval: (query) => (query.state.data && hasStarted(query.state.data) ? false : STARTING_POLL_MS),
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
    const waitedMs = decidedAt ? Date.now() - new Date(decidedAt).getTime() : 0;
    if (waitedMs > WORKER_GRACE_MS) return <NobodyPickedItUp decidedAt={decidedAt} />;

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

/**
 * The honest version of "Starting", once starting has clearly not happened.
 *
 * Same shape as the trend runner's `NobodyPickedItUp`, for the same reason: a
 * spinner that runs for hours is a lie, and the person looking at it has no
 * way to tell "slow" from "nothing is running". Nothing on this page can clear
 * the state — a dispatcher that is not running is not sweeping either — so
 * the only useful thing to say is what to go and start.
 */
function NobodyPickedItUp({ decidedAt }: { decidedAt: string | null }) {
  return (
    <Alert variant="destructive">
      <CircleAlertIcon className="size-4" />
      <AlertTitle>Nothing has picked this up</AlertTitle>
      <AlertDescription className="space-y-2">
        <p>
          This idea was approved {formatRelative(decidedAt)}. A worker normally opens its production within a few
          seconds; none has, which means the pipeline worker is not running or cannot reach the database.
        </p>
        <p>
          Start it and this page updates on its own — <code>python -m pipeline.driver.worker</code> in{' '}
          <code>apps/pipeline</code>, or <code>docker compose up -d worker</code> on the box. Its logs are where a crash
          loop or bad credentials show; from here they all look identical.
        </p>
      </AlertDescription>
    </Alert>
  );
}
