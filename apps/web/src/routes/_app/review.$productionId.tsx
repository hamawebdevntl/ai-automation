import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { ArrowLeftIcon, CheckIcon, XIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { ScrollArea } from '@/components/ui/scroll-area';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { productionQueryOptions, useDecideProduction } from '@/features/queue/api';
import { PlatformCopyPanel } from '@/features/queue/components/platform-copy-panel';
import { QcReportCard } from '@/features/queue/components/qc-report-card';
import { QueryError } from '@/features/queue/components/query-state';
import { formatDuration, formatRelative, formatUsd, parseQcReport } from '@/lib/format';

export const Route = createFileRoute('/_app/review/$productionId')({
  loader: ({ context, params }) => context.queryClient.ensureQueryData(productionQueryOptions(params.productionId)),
  component: ProductionReviewPage,
});

function ProductionReviewPage() {
  const { productionId } = Route.useParams();
  const navigate = useNavigate();
  const { isOwner, isLoading: isRoleLoading } = useOwner();
  const { data, error } = useQuery(productionQueryOptions(productionId));
  const decide = useDecideProduction();

  const [note, setNote] = useState('');

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
  const awaitingDecision = production.status === 'awaiting_review' || production.status === 'qc_failed';
  const rejectNeedsNote = note.trim().length === 0;

  const submit = async (decision: 'approved' | 'rejected') => {
    try {
      await decide.mutateAsync({ productionId, decision, note });
      toast.success(decision === 'approved' ? 'Signed off' : 'Sent back', {
        description: decision === 'approved' ? 'It goes to the publisher next.' : 'The cut will not be published.',
      });
      await navigate({ to: '/review' });
    } catch (mutationError) {
      toast.error(mutationError instanceof Error ? mutationError.message : 'Could not record that decision');
    }
  };

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
        <Link to="/review">
          <ArrowLeftIcon className="size-4" />
          Back to finished cuts
        </Link>
      </Button>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          {style && (
            <Badge variant="secondary" className="font-normal">
              {style.name}
            </Badge>
          )}
          <Badge variant="outline" className="font-normal text-muted-foreground">
            {production.status.replace(/_/g, ' ')}
          </Badge>
          <span className="text-xs text-muted-foreground">
            rendered {formatRelative(production.completed_at ?? production.created_at)}
          </span>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight">{idea?.title ?? 'Finished cut'}</h1>
        {idea?.hook && <p className="text-muted-foreground">{idea.hook}</p>}
      </div>

      {!awaitingDecision && (
        <Alert>
          <AlertTitle>Already decided</AlertTitle>
          <AlertDescription>
            This cut was {production.status} {formatRelative(production.decided_at)}
            {production.decision_note ? ` — “${production.decision_note}”` : '.'}
          </AlertDescription>
        </Alert>
      )}

      {production.error && (
        <Alert variant="destructive">
          <AlertTitle>The pipeline reported an error</AlertTitle>
          <AlertDescription>{production.error}</AlertDescription>
        </Alert>
      )}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,340px)_1fr]">
        <div className="space-y-4">
          <div className="overflow-hidden rounded-lg border bg-black">
            {production.video_url ? (
              // biome-ignore lint/a11y/useMediaCaption: captions are burned into the render itself
              <video
                key={production.video_url}
                src={production.video_url}
                poster={production.thumbnail_url ?? undefined}
                controls
                playsInline
                preload="metadata"
                className="aspect-[9/16] w-full bg-black"
              />
            ) : (
              <div className="flex aspect-[9/16] w-full items-center justify-center p-6 text-center text-sm text-white/60">
                No video file on this record yet.
              </div>
            )}
          </div>

          <Card>
            <CardContent className="grid grid-cols-2 gap-4 pt-6 text-sm">
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Duration</p>
                <p className="tabular-nums">{formatDuration(production.duration_seconds)}</p>
              </div>
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Estimated</p>
                <p className="tabular-nums">{formatUsd(production.cost_estimate_usd)}</p>
              </div>
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Actual</p>
                <p className="tabular-nums">{formatUsd(production.cost_actual_usd)}</p>
              </div>
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground">Task</p>
                <p className="truncate font-mono text-xs">{production.task_id ?? '—'}</p>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="space-y-6">
          <QcReportCard report={qc} />

          {production.script && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Script</CardTitle>
                <CardDescription>What the narration says, as it was written.</CardDescription>
              </CardHeader>
              <CardContent>
                <ScrollArea className="h-48 rounded-md border bg-muted/30 p-3">
                  <p className="whitespace-pre-wrap text-sm leading-relaxed">{production.script}</p>
                </ScrollArea>
              </CardContent>
            </Card>
          )}

          <PlatformCopyPanel value={production.platform_copy} targetPlatforms={idea?.target_platforms ?? []} />

          {awaitingDecision && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Sign-off</CardTitle>
                <CardDescription>
                  Approving hands the cut to the publisher. The cost of a slow post is small; the cost of a bad one
                  under our name is not.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="review-note">
                    Note {rejectNeedsNote && <span className="text-muted-foreground">(required to reject)</span>}
                  </Label>
                  <Textarea
                    id="review-note"
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="What needs to change, or why this is good to go."
                    rows={3}
                    disabled={!isOwner || decide.isPending}
                  />
                </div>

                {!isOwner && !isRoleLoading && (
                  <Alert>
                    <AlertTitle>Read-only</AlertTitle>
                    <AlertDescription>Your account is a viewer. An owner has to sign this off.</AlertDescription>
                  </Alert>
                )}

                <div className="flex flex-wrap gap-2">
                  <Button onClick={() => submit('approved')} disabled={!isOwner || decide.isPending}>
                    {decide.isPending ? <Spinner className="size-4" /> : <CheckIcon className="size-4" />}
                    Approve and publish
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => submit('rejected')}
                    disabled={!isOwner || decide.isPending || rejectNeedsNote}
                    title={rejectNeedsNote ? 'A rejection has to say why' : undefined}
                  >
                    <XIcon className="size-4" />
                    Reject
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
