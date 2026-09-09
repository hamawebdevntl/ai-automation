/**
 * One uploaded recording, and whatever it is currently asking of the owner.
 *
 * The four states this has to render are genuinely different questions:
 *
 *   working          Nothing to do. Say which phase and stop.
 *   awaiting_picks   The gate. Show the candidates.
 *   resolved         Show what was decided, collapsed.
 *   failed           Say why, and offer another go — which costs nothing,
 *                    because no render has happened at any point before the
 *                    gate is passed.
 */

import { useQuery } from '@tanstack/react-query';
import { CircleAlertIcon, FilmIcon, Loader2Icon, RotateCcwIcon } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { clipCandidatesQueryOptions, isClipSourceWorking, useRetryClipSource } from '@/features/clips/api';
import { CandidateCard } from '@/features/clips/components/candidate-card';
import { CLIP_SOURCE_STATUS_LABELS, type ClipSourceRow } from '@/lib/database.types';
import { formatDuration, formatRelative } from '@/lib/format';
import { toError } from '@/lib/supabase-error';

export function SourceCard({ source, canDecide }: { source: ClipSourceRow; canDecide: boolean }) {
  const working = isClipSourceWorking(source);
  const failed = source.status === 'failed';

  // Only fetched once there is something to fetch. A recording that is still
  // transcribing has no candidates, and asking for them every five seconds
  // while it does would be a query per poll for an empty list.
  const decidable = source.status === 'awaiting_picks' || source.status === 'resolved';
  const {
    data: candidates,
    isPending,
    error,
  } = useQuery({
    ...clipCandidatesQueryOptions(source.id),
    enabled: decidable,
  });

  const retry = useRetryClipSource();
  const undecided = (candidates ?? []).filter((c) => c.decision === 'pending');
  const accepted = (candidates ?? []).filter((c) => c.decision === 'accepted');

  return (
    <Card>
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={failed ? 'destructive' : working ? 'secondary' : 'outline'} className="font-normal">
            {working && <Loader2Icon className="size-3 animate-spin" aria-hidden />}
            {CLIP_SOURCE_STATUS_LABELS[source.status]}
          </Badge>
          {source.duration_seconds !== null && (
            <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground tabular-nums">
              <FilmIcon className="size-3.5" aria-hidden />
              {formatDuration(source.duration_seconds)}
            </span>
          )}
          <span className="text-xs text-muted-foreground">uploaded {formatRelative(source.created_at)}</span>
        </div>
        <CardTitle className="text-base break-all">{source.filename ?? 'Untitled recording'}</CardTitle>
        {source.status === 'awaiting_picks' && (
          <CardDescription>
            {undecided.length === 1 ? 'One clip to decide on.' : `${undecided.length} clips to decide on.`} Discarding
            costs nothing — none of these has been rendered.
          </CardDescription>
        )}
        {source.status === 'resolved' && (
          <CardDescription>
            {accepted.length === 0
              ? 'Nothing was made from this recording.'
              : accepted.length === 1
                ? 'One clip was made from this recording.'
                : `${accepted.length} clips were made from this recording.`}
          </CardDescription>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {working && (
          <p className="text-sm text-muted-foreground">
            {source.status === 'transcribing'
              ? 'Transcribing the recording. A long one takes a few minutes.'
              : source.status === 'proposing'
                ? 'Reading the transcript and choosing the moments worth cutting.'
                : 'Queued. A worker will pick this up shortly.'}
          </p>
        )}

        {failed && (
          <div className="space-y-3">
            <p className="flex gap-2 text-sm text-destructive">
              <CircleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>{source.error ?? 'This recording failed for a reason nobody recorded.'}</span>
            </p>
            {canDecide && (
              <Button
                size="sm"
                variant="outline"
                disabled={retry.isPending}
                onClick={() =>
                  retry.mutate(
                    { sourceId: source.id },
                    {
                      onError: (err) =>
                        toast.error('Could not retry that recording', {
                          description: toError(err).message,
                        }),
                    },
                  )
                }
              >
                {retry.isPending ? (
                  <Loader2Icon className="size-4 animate-spin" aria-hidden />
                ) : (
                  <RotateCcwIcon className="size-4" aria-hidden />
                )}
                Try again
              </Button>
            )}
            <p className="text-xs text-muted-foreground">
              Nothing was rendered, so this costs only the transcription — and a retry keeps the transcript if there
              already is one.
            </p>
          </div>
        )}

        {decidable && isPending && <Skeleton className="h-24 w-full rounded-md" />}
        {decidable && error && (
          <p className="text-sm text-destructive">Could not load the clips: {toError(error).message}</p>
        )}

        {decidable && candidates && candidates.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Nothing in this recording stands alone as a short. That is an answer, not a failure — and it cost one
            transcription to find out.
          </p>
        )}

        {decidable && candidates && candidates.length > 0 && (
          <ul className="space-y-3">
            {candidates.map((candidate) => (
              <li key={candidate.id}>
                <CandidateCard candidate={candidate} storageKey={source.storage_key} canDecide={canDecide} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
