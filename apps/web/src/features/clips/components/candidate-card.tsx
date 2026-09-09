/**
 * One proposed clip, and the decision on it. This is the clip gate.
 *
 * Everything an owner needs is here without a render having happened: the
 * model's title and stated reason, the words actually spoken in the range, the
 * timecodes, and a scrub preview of the recording at the start point. That is
 * the argument for the gate — discarding a candidate is free in a way that
 * discarding a finished cut is not.
 *
 * Accepting is the only action here that spends anything, and it says so: it
 * creates a production, which will draft nothing but will reach the script gate
 * and then a render. Discarding writes a decision and nothing else.
 */

import { CheckIcon, Loader2Icon, QuoteIcon, XIcon } from 'lucide-react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useAcceptClipCandidate, useDiscardClipCandidate } from '@/features/clips/api';
import { ClipPreview } from '@/features/clips/components/clip-preview';
import { ClipTimecode } from '@/features/clips/components/clip-timecode';
import type { ClipCandidateRow } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import { toError } from '@/lib/supabase-error';

export function CandidateCard({
  candidate,
  storageKey,
  canDecide,
}: {
  candidate: ClipCandidateRow;
  storageKey: string;
  /** Presentation only. `accept_clip_candidate` raises 42501 for a viewer. */
  canDecide: boolean;
}) {
  const accept = useAcceptClipCandidate();
  const discard = useDiscardClipCandidate();

  const pending = candidate.decision === 'pending';
  const busy = accept.isPending || discard.isPending;

  // `toError` rather than `error instanceof Error`: supabase-js does not throw,
  // and the plain object it returns takes the fallback branch of that idiom --
  // throwing away the one sentence that would have explained the failure. See
  // the note at the top of `lib/supabase-error.ts`.
  const decide = (mutation: typeof accept, failed: string) => {
    mutation.mutate(
      { candidateId: candidate.id },
      { onError: (error) => toast.error(failed, { description: toError(error).message }) },
    );
  };

  return (
    <Card className={pending ? undefined : 'opacity-70'}>
      <CardHeader className="gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="secondary" className="font-normal tabular-nums">
            #{candidate.rank}
          </Badge>
          <ClipTimecode
            start={candidate.start_seconds}
            end={candidate.end_seconds}
            className="text-xs text-muted-foreground"
          />
          {candidate.decision === 'accepted' && (
            <Badge
              variant="outline"
              className="border-transparent bg-emerald-600/15 text-emerald-700 dark:text-emerald-400"
            >
              <CheckIcon className="size-3" />
              Accepted {formatRelative(candidate.decided_at)}
            </Badge>
          )}
          {candidate.decision === 'discarded' && (
            <Badge variant="outline" className="text-muted-foreground">
              Discarded {formatRelative(candidate.decided_at)}
            </Badge>
          )}
        </div>
        <CardTitle className="text-base">{candidate.title}</CardTitle>
        {candidate.hook && <p className="text-sm text-muted-foreground">{candidate.hook}</p>}
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Only for a clip still being decided. A preview on six settled cards
            would fetch six recordings to show nobody anything. */}
        {pending && <ClipPreview storageKey={storageKey} start={candidate.start_seconds} end={candidate.end_seconds} />}

        {candidate.reason && (
          <p className="text-sm">
            <span className="font-medium">Why it stands alone. </span>
            <span className="text-muted-foreground">{candidate.reason}</span>
          </p>
        )}

        {/* The words, verbatim. This becomes the production's script, so it is
            also what the owner will be asked to approve at the script gate --
            and what gets burned in as captions. Showing it here means the two
            gates show the same text rather than two versions of it. */}
        {candidate.transcript_excerpt && (
          <blockquote className="flex gap-2 rounded-md bg-muted/50 p-3 text-sm text-muted-foreground">
            <QuoteIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden />
            <span>{candidate.transcript_excerpt}</span>
          </blockquote>
        )}

        {pending && canDecide && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy} onClick={() => decide(accept, 'Could not make this clip')}>
              {accept.isPending ? (
                <Loader2Icon className="size-4 animate-spin" aria-hidden />
              ) : (
                <CheckIcon className="size-4" aria-hidden />
              )}
              Make this clip
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => decide(discard, 'Could not discard this clip')}
            >
              {discard.isPending ? (
                <Loader2Icon className="size-4 animate-spin" aria-hidden />
              ) : (
                <XIcon className="size-4" aria-hidden />
              )}
              Discard
            </Button>
            <span className="text-xs text-muted-foreground">
              Accepting queues a production. You still approve the words before anything renders.
            </span>
          </div>
        )}

        {pending && !canDecide && <p className="text-xs text-muted-foreground">Only an owner can decide on a clip.</p>}
      </CardContent>
    </Card>
  );
}
