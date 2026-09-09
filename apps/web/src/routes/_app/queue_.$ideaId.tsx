import { useQuery } from '@tanstack/react-query';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { ArrowLeftIcon, CheckIcon, ExternalLinkIcon, XIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { ideaQueryOptions, stylePresetsQueryOptions, useApproveIdea, useRejectIdea } from '@/features/queue/api';
import { IdeaProductionPanel } from '@/features/queue/components/idea-production-panel';
import { QueryError } from '@/features/queue/components/query-state';
import { StylePicker } from '@/features/queue/components/style-picker';
import { VelocityBadge } from '@/features/queue/components/velocity-badge';
import { needsClipRange } from '@/lib/database.types';
import { formatCostRange, formatMinutes, formatRelative, titleCase } from '@/lib/format';

export const Route = createFileRoute('/_app/queue_/$ideaId')({
  loader: ({ context, params }) =>
    Promise.all([
      context.queryClient.ensureQueryData(ideaQueryOptions(params.ideaId)),
      context.queryClient.ensureQueryData(stylePresetsQueryOptions()),
    ]),
  component: IdeaDecisionPage,
});

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  if (!children) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="text-sm leading-relaxed">{children}</div>
    </div>
  );
}

function IdeaDecisionPage() {
  const { ideaId } = Route.useParams();
  const navigate = useNavigate();
  const { isOwner, isLoading: isRoleLoading } = useOwner();

  const ideaQuery = useQuery(ideaQueryOptions(ideaId));
  const presetsQuery = useQuery(stylePresetsQueryOptions());

  const [styleId, setStyleId] = useState<string | null>(null);
  const [note, setNote] = useState('');

  const approve = useApproveIdea();
  const reject = useRejectIdea();
  const isDeciding = approve.isPending || reject.isPending;

  const idea = ideaQuery.data;
  // A clip style is not a Gate 1 choice. It is active because every accepted
  // clip is attached to it, but an ordinary idea has no range for it to cut and
  // `approve_idea` refuses the combination -- so offering it here would be
  // offering a choice the database rejects. The refusal in SQL is what holds;
  // this is what stops anyone meeting it.
  const presets = (presetsQuery.data ?? []).filter((preset) => !needsClipRange(preset.render_mode));
  const selectedPreset = presets.find((preset) => preset.id === styleId) ?? null;

  if (ideaQuery.error) return <QueryError error={ideaQuery.error} />;

  if (!idea) {
    return (
      <div className="mx-auto w-full max-w-3xl space-y-4">
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-48 w-full" />
      </div>
    );
  }

  const isPending = idea.status === 'pending';

  const handleApprove = async () => {
    if (!styleId) return;
    try {
      await approve.mutateAsync({ ideaId, styleId, note });
      toast.success('Approved', {
        description: `Queued as ${selectedPreset?.name ?? 'the chosen style'}. Production starts in a few seconds.`,
      });
      // Deliberately no navigation. This page becomes the live view of the
      // production the approval just opened; bouncing back to the list of
      // *pending* ideas is what made the whole post-approval pipeline
      // invisible in the first place. The production panel renders in this
      // card's place, below, so the replacement appears where the eye already is.
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not approve this idea');
    }
  };

  const handleReject = async () => {
    try {
      await reject.mutateAsync({ ideaId, note });
      toast.success('Rejected', { description: 'Nothing was spent on it.' });
      await navigate({ to: '/queue' });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not reject this idea');
    }
  };

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6">
      <Button asChild variant="ghost" size="sm" className="-ml-2 w-fit">
        <Link to="/queue">
          <ArrowLeftIcon className="size-4" />
          Back to the queue
        </Link>
      </Button>

      <div className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <VelocityBadge label={idea.velocity_label} ratio={idea.velocity_ratio} />
          {idea.trend_keyword && (
            <Badge variant="outline" className="font-normal text-muted-foreground">
              {idea.trend_keyword}
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">proposed {formatRelative(idea.created_at)}</span>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight break-words">{idea.title}</h1>
        {idea.hook && <p className="text-lg text-muted-foreground">{idea.hook}</p>}
      </div>

      {!isPending && (
        <Alert>
          <AlertTitle>Already decided</AlertTitle>
          <AlertDescription>
            This idea was {idea.status} {formatRelative(idea.decided_at)}
            {idea.decision_note ? ` — “${idea.decision_note}”` : '.'}
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">The idea</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <Detail label="Angle">{idea.angle}</Detail>
          <Detail label="Why now">{idea.rationale}</Detail>
          {/* Only an idea from a described search has one: how it connects to
              what the owner said they were working on. */}
          <Detail label="How it fits">{idea.connection}</Detail>
          <Detail label="Platforms">
            <div className="flex flex-wrap gap-1.5">
              {idea.target_platforms.map((platform) => (
                <Badge key={platform} variant="secondary" className="font-normal">
                  {titleCase(platform)}
                </Badge>
              ))}
            </div>
          </Detail>
          {idea.source_url && (
            <Detail label="Source">
              <a
                href={idea.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 underline underline-offset-4"
              >
                {idea.source ?? idea.source_url}
                <ExternalLinkIcon className="size-3.5" />
              </a>
            </Detail>
          )}
        </CardContent>
      </Card>

      {idea.status === 'approved' && <IdeaProductionPanel ideaId={idea.id} decidedAt={idea.decided_at} />}

      {isPending && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">How should it be made?</CardTitle>
            <CardDescription>
              The cost and time below are estimates for this style. Approving commits to them.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            {presetsQuery.error && <QueryError error={presetsQuery.error} />}
            {presetsQuery.isPending && <Skeleton className="h-40 w-full" />}
            {presets.length > 0 && (
              <StylePicker presets={presets} value={styleId} onChange={setStyleId} disabled={!isOwner || isDeciding} />
            )}

            <Separator />

            <div className="space-y-2">
              <Label htmlFor="decision-note">Note (optional)</Label>
              <Textarea
                id="decision-note"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="Anything the next person should know about this decision."
                rows={3}
                disabled={!isOwner || isDeciding}
              />
            </div>

            {!isOwner && !isRoleLoading && (
              <Alert>
                <AlertTitle>Read-only</AlertTitle>
                <AlertDescription>
                  Your account is a viewer, so you can read the queue but not pass a gate. An owner can promote you.
                </AlertDescription>
              </Alert>
            )}

            {selectedPreset && (
              <p className="text-sm text-muted-foreground">
                Approving queues this as <strong className="text-foreground">{selectedPreset.name}</strong> —{' '}
                {formatCostRange(selectedPreset)}, {formatMinutes(selectedPreset.est_minutes)}.
              </p>
            )}

            <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              <Button
                onClick={handleApprove}
                disabled={!isOwner || !styleId || isDeciding}
                className="w-full sm:w-auto"
              >
                {approve.isPending ? <Spinner className="size-4" /> : <CheckIcon className="size-4" />}
                Approve and produce
              </Button>
              <Button
                variant="outline"
                onClick={handleReject}
                disabled={!isOwner || isDeciding}
                className="w-full sm:w-auto"
              >
                {reject.isPending ? <Spinner className="size-4" /> : <XIcon className="size-4" />}
                Reject
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
