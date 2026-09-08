import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link } from '@tanstack/react-router';
import { ArrowLeftIcon, ExternalLinkIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { productionEventsQueryOptions, productionQueryOptions, stylePresetsQueryOptions } from '@/features/queue/api';
import { PlatformCopyPanel } from '@/features/queue/components/platform-copy-panel';
import { ProductionControls } from '@/features/queue/components/production-controls';
import { ProductionStatusBadge } from '@/features/queue/components/production-status';
import { ProductionTimeline } from '@/features/queue/components/production-timeline';
import { QcReportCard } from '@/features/queue/components/qc-report-card';
import { QueryError } from '@/features/queue/components/query-state';
import { ScriptEditor } from '@/features/queue/components/script-editor';
import { SourceFootageSection } from '@/features/queue/components/source-footage-panel';
import { describeProduction, hasStarted, isAtGate2, isAtScriptGate } from '@/features/queue/pipeline-steps';
import { useProductionStream } from '@/features/queue/use-production-stream';
import { RENDER_MODE_LABELS } from '@/lib/database.types';
import { formatDuration, formatRelative, formatUsd, parseQcReport } from '@/lib/format';

export const Route = createFileRoute('/_app/productions_/$productionId')({
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.ensureQueryData(productionQueryOptions(params.productionId)),
      context.queryClient.ensureQueryData(productionEventsQueryOptions(params.productionId)),
      context.queryClient.ensureQueryData(stylePresetsQueryOptions()),
    ]),
  component: ProductionProcessPage,
});

/**
 * Everything that happens to one production, and every way to intervene.
 *
 * This page exists because there was previously nowhere at all to look: the
 * queue listed only pending ideas and the review page only cuts already at
 * Gate 2, so a production that was rendering, retrying or stopped appeared in
 * no list and had no page. "It was approved an hour ago" was the entire
 * available account of it.
 */
function ProductionProcessPage() {
  const { productionId } = Route.useParams();

  const { data, error } = useQuery({
    ...productionQueryOptions(productionId),
    // Realtime carries every change the worker makes. The one thing it cannot
    // carry is the worker never arriving, so poll until the row has been
    // touched — that re-render is what turns "Starting" into an honest
    // "Waiting for a worker".
    refetchInterval: (query) => (query.state.data && hasStarted(query.state.data.production) ? false : 5_000),
  });
  const eventsQuery = useQuery(productionEventsQueryOptions(productionId));
  const presetsQuery = useQuery(stylePresetsQueryOptions());

  // Live from here on: the row and its log both push.
  useProductionStream(productionId);

  if (error) return <QueryError error={error} />;

  if (!data) {
    return (
      <div className="mx-auto w-full max-w-5xl space-y-4">
        <Skeleton className="h-8 w-1/2" />
        <Skeleton className="h-[420px] w-full" />
      </div>
    );
  }

  const { production, idea, style } = data;
  const qc = parseQcReport(production.qc);
  const verdict = describeProduction(production);

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
        <Link to="/queue">
          <ArrowLeftIcon className="size-4" />
          Back to the queue
        </Link>
      </Button>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <ProductionStatusBadge production={production} />
          {style && (
            <Badge variant="secondary" className="font-normal">
              {style.name}
            </Badge>
          )}
          {production.render_backend && (
            <Badge variant="outline" className="font-normal text-muted-foreground">
              {RENDER_MODE_LABELS[production.render_backend]}
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">opened {formatRelative(production.created_at)}</span>
        </div>

        <h1 className="text-2xl font-semibold tracking-tight break-words">{idea?.title ?? 'Production'}</h1>
        {verdict.detail && <p className="text-muted-foreground">{verdict.detail}</p>}
      </div>

      {production.superseded_by && (
        <Alert>
          <AlertTitle>Superseded</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-2">
            This production was re-run and a newer one replaced it. It is kept here in full.
            <Button asChild variant="outline" size="sm">
              <Link to="/productions/$productionId" params={{ productionId: production.superseded_by }}>
                Open the replacement
              </Link>
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {isAtGate2(production) && (
        <Alert>
          <AlertTitle>This cut is waiting for a decision</AlertTitle>
          <AlertDescription className="flex flex-wrap items-center gap-2">
            Signing it off is Gate 2, which is a separate screen.
            <Button asChild size="sm">
              <Link to="/review/$productionId" params={{ productionId: production.id }}>
                Review the cut
              </Link>
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {isAtScriptGate(production) && (
        <>
          <SourceFootageSection production={production} />
          <ScriptEditor production={production} />
        </>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Progress</CardTitle>
          <CardDescription>
            Every step between approving the idea and the finished output. Open a step to see exactly what happened at
            it, including anything that failed and was retried.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ProductionTimeline
            production={production}
            events={eventsQuery.data ?? []}
            isLoadingEvents={eventsQuery.isPending}
          />
          {eventsQuery.error && <p className="mt-2 text-sm text-destructive">The step log could not be loaded.</p>}
        </CardContent>
      </Card>

      <ProductionControls production={production} presets={presetsQuery.data ?? []} />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">The cut</CardTitle>
            <CardDescription>
              {production.video_url
                ? 'Stored privately. This link is signed and expires.'
                : 'Nothing has been rendered yet.'}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {production.video_url ? (
              <>
                {/* biome-ignore lint/a11y/useMediaCaption: captions are burned
                    into the render itself; there is no separate track. */}
                <video
                  src={production.video_url}
                  poster={production.thumbnail_url ?? undefined}
                  controls
                  className="w-full rounded-md border bg-black"
                />
                <div className="flex flex-wrap gap-3 text-sm text-muted-foreground">
                  <span>{formatDuration(production.duration_seconds)}</span>
                  <span>estimated {formatUsd(production.cost_estimate_usd)}</span>
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                Estimated cost for this style is {formatUsd(production.cost_estimate_usd)}.
              </p>
            )}
          </CardContent>
        </Card>

        <QcReportCard report={qc} />
      </div>

      <PlatformCopyPanel value={production.platform_copy} targetPlatforms={idea?.target_platforms ?? []} />

      {/* The editor replaces what was a read-only `ScrollArea` of the script.
          It renders locked once a render has been submitted, so this position
          still shows 'what the narration was written from' for a finished
          production -- and shows it as the same component the owner approved
          it in, rather than a second rendering of the same column. */}
      {!isAtScriptGate(production) && (
        <>
          {/* Renders nothing unless this style read an upload, in which case
              it is the record of what the reel was made from. */}
          <SourceFootageSection production={production} />
          {production.script && <ScriptEditor production={production} />}
        </>
      )}

      {idea && (
        <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
          <Link to="/queue/$ideaId" params={{ ideaId: idea.id }}>
            The idea this came from
            <ExternalLinkIcon className="size-3.5" />
          </Link>
        </Button>
      )}
    </div>
  );
}
