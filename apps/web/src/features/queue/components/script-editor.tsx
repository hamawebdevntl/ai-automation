import { CheckIcon, LockIcon, RefreshCwIcon, SaveIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { MAX_SCRIPT_CHARS, type ProductionRow } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import { useApproveScript, useRedraftScript, useSaveScript } from '../api';
import { currentGraphStep, isLeased, isScriptEditable, scriptLockReason, sourceGapReason } from '../pipeline-steps';
import { useSourceLane } from '../use-source-lane';

/**
 * Reading, editing and approving the narration, before a cent is spent on it.
 *
 * This screen is the reason the script gate exists. Before it, the words a reel
 * said were decided by whichever backend happened to render it — MoneyPrinter-
 * Turbo wrote its own inside the render on the two default styles — and the
 * first time a person saw them was at Gate 2, with the video already paid for.
 * The README says a human is here to "decide *what we say*"; this is where that
 * actually happens.
 *
 * Two properties matter more than anything visual here:
 *
 *   1. **Approving is explicit.** Saving persists the words and leaves the gate
 *      shut. Only the approve button hands the row back to the driver, and it
 *      confirms first, because that is the click that spends money.
 *   2. **A live update never eats an edit.** The pipeline writes to
 *      `productions.script` (a draft landing, a redraft finishing) and those
 *      arrive over Realtime while somebody may be typing. Incoming text is
 *      adopted only when the box is untouched; otherwise it is offered, and the
 *      owner's words stay on screen.
 *
 * Everything the buttons here can do, Postgres refuses independently for a
 * viewer or for a production whose render has already been submitted. The
 * disabled states mirror those guards so the reason is visible rather than
 * arriving as a failed request.
 */

/** Below this a "script" is almost certainly a stub. A warning, never a block. */
const SHORT_SCRIPT_CHARS = 80;

/** Roughly what a reel narration runs to, for the length hint. */
const WORDS_PER_MINUTE = 150;

export interface ScriptValidity {
  ok: boolean;
  /** Blocks saving and approving. */
  error: string | null;
  /** Worth saying, but not worth refusing. */
  warning: string | null;
}

/**
 * The same rules `clean_script` enforces in Postgres, for feedback only.
 *
 * Exported because it is the part of this file worth testing directly: the
 * boundaries are exact numbers that have to agree with a check constraint, and
 * agreeing with it is not something you can see by looking at the page.
 */
export function validateScript(script: string): ScriptValidity {
  const trimmed = script.trim();
  if (!trimmed) {
    return { ok: false, error: 'A script cannot be empty.', warning: null };
  }
  if (trimmed.length > MAX_SCRIPT_CHARS) {
    return {
      ok: false,
      error: `That is ${trimmed.length.toLocaleString()} characters. The limit is ${MAX_SCRIPT_CHARS.toLocaleString()} — what HeyGen accepts, and what the database enforces.`,
      warning: null,
    };
  }
  if (trimmed.length < SHORT_SCRIPT_CHARS) {
    return {
      ok: true,
      error: null,
      warning: 'That is very short for a reel. Check it is the whole script and not just the hook.',
    };
  }
  return { ok: true, error: null, warning: null };
}

/** Read time, so the length means something without counting characters. */
export function estimateSeconds(script: string): number {
  const words = script.trim().split(/\s+/).filter(Boolean).length;
  return Math.round((words / WORDS_PER_MINUTE) * 60);
}

type Pending = 'redraft' | 'approve' | null;

export function ScriptEditor({ production }: { production: ProductionRow }) {
  const { isOwner, isLoading: isRoleLoading } = useOwner();

  const serverScript = production.script ?? '';

  // Seeded from the row, and re-seeded only when the row's own text changes.
  // `seed` is what the box was last filled from, which is the only way to tell
  // "the pipeline wrote a new draft" from "this component re-rendered".
  const [seed, setSeed] = useState(serverScript);
  const [draft, setDraft] = useState(serverScript);
  const [incoming, setIncoming] = useState<string | null>(null);

  if (serverScript !== seed) {
    // Adjusting state during render rather than in an effect: this must settle
    // before the textarea paints, or the owner sees one frame of stale text.
    const untouched = draft === seed;
    setSeed(serverScript);
    if (untouched) {
      setDraft(serverScript);
      setIncoming(null);
    } else {
      // Their edits win the screen. The new draft is offered, not applied —
      // silently replacing what somebody is halfway through writing is the one
      // unrecoverable thing this component could do.
      setIncoming(serverScript);
    }
  }

  const [confirm, setConfirm] = useState<Pending>(null);

  const save = useSaveScript();
  const approve = useApproveScript();
  const redraft = useRedraftScript();
  const busy = save.isPending || approve.isPending || redraft.isPending;

  const editable = isScriptEditable(production);
  const lockReason = scriptLockReason(production);
  const leased = isLeased(production);
  const step = currentGraphStep(production);
  const approved = production.script_approved_at !== null;
  const isDirty = draft !== serverScript;
  const validity = validateScript(draft);

  // The draft has not arrived yet. Not an error and not an empty state: the
  // worker is writing it, and this resolves on its own over Realtime.
  const isDrafting =
    !serverScript && (step === 'write_script' || step === 'open_script_gate') && !isStoppedRow(production);

  const canWrite = isOwner && editable && !busy;

  // On the footage lane, approving is a statement about the upload and the
  // instruction as well as the words -- so it is refused here for the same
  // reason `approve_script` refuses it in Postgres. `isUnknown` blocks too: the
  // style is what says whether this lane applies at all, and enabling the one
  // button that spends money before that answer arrives -- or after the read
  // for it failed -- would be the wrong way to be wrong.
  const lane = useSourceLane(production);
  const canApprove = canWrite && !leased && validity.ok && !lane.isUnknown && lane.gap === null;

  const runSave = async () => {
    try {
      await save.mutateAsync({ productionId: production.id, script: draft });
      toast.success('Saved', {
        description: 'The words are stored. Nothing renders until you approve them.',
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not save the script');
    }
  };

  const runApprove = async () => {
    try {
      await approve.mutateAsync({ productionId: production.id, script: draft });
      toast.success('Script approved', {
        description: 'The render is submitted against exactly these words, within a few seconds.',
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not approve the script');
    } finally {
      setConfirm(null);
    }
  };

  const runRedraft = async () => {
    try {
      await redraft.mutateAsync({ productionId: production.id });
      toast.success('Writing another draft', {
        description: 'One model call, no video. The new text appears here on its own.',
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not ask for another draft');
    } finally {
      setConfirm(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">The script</CardTitle>
          <ScriptBadge approved={approved} isDirty={isDirty} editable={editable} />
        </div>
        <CardDescription>
          {editable
            ? 'This is what the video will say, word for word. Nothing is rendered until you approve it.'
            : 'What this video was rendered from.'}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {isDrafting && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Spinner className="size-4" />
            Writing the first draft. This appears here on its own.
          </p>
        )}

        {!serverScript && !isDrafting && editable && (
          <Alert>
            <AlertTitle>No draft was written</AlertTitle>
            <AlertDescription>
              {production.error
                ? `The pipeline stopped before it could write one: ${production.error}`
                : 'The pipeline has not written a draft for this production.'}{' '}
              You can write the script yourself below and approve it — the render does not need the drafting service.
            </AlertDescription>
          </Alert>
        )}

        {incoming !== null && (
          <Alert>
            <AlertTitle>A new draft arrived while you were editing</AlertTitle>
            <AlertDescription className="space-y-2">
              <p>Your version is still on screen and has not been touched.</p>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setDraft(incoming);
                    setIncoming(null);
                  }}
                >
                  Use the new draft
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setIncoming(null)}>
                  Keep mine
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}

        <div className="space-y-2">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <Label htmlFor="script-body">Narration</Label>
            <ScriptMeter script={draft} />
          </div>
          <Textarea
            id="script-body"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="What the video says, start to finish."
            rows={12}
            className="font-normal leading-relaxed"
            disabled={!canWrite}
            aria-invalid={validity.error !== null}
            aria-describedby="script-validity"
          />
          <p id="script-validity" className="text-sm">
            {validity.error ? (
              <span className="text-destructive">{validity.error}</span>
            ) : validity.warning ? (
              <span className="text-muted-foreground">{validity.warning}</span>
            ) : (
              <span className="text-muted-foreground">
                {approved && !isDirty
                  ? `Approved ${formatRelative(production.script_approved_at)}. The render uses exactly this text.`
                  : isDirty
                    ? 'Unsaved changes. Save keeps them; approve saves and starts the render.'
                    : 'Saved, and not yet approved. No render can start until you approve it.'}
              </span>
            )}
          </p>
        </div>

        {!editable && lockReason && (
          <Alert>
            <LockIcon className="size-4" />
            <AlertTitle>Locked</AlertTitle>
            <AlertDescription>{lockReason}</AlertDescription>
          </Alert>
        )}

        {!isOwner && !isRoleLoading && (
          <Alert>
            <AlertTitle>Read-only</AlertTitle>
            <AlertDescription>
              Your account is a viewer, so you can read the script but not change or approve it. An owner can promote
              you.
            </AlertDescription>
          </Alert>
        )}

        {editable && leased && (
          <p className="text-sm text-muted-foreground">
            The worker is running a step on this production right now. Approving is available again in a moment.
          </p>
        )}

        {editable && lane.gap !== null && <p className="text-sm text-muted-foreground">{sourceGapReason(lane.gap)}</p>}

        {editable && (
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
            <Button onClick={() => setConfirm('approve')} disabled={!canApprove}>
              {approve.isPending ? <Spinner className="size-4" /> : <CheckIcon className="size-4" />}
              Approve and start the render
            </Button>
            <Button variant="outline" onClick={() => void runSave()} disabled={!canWrite || !validity.ok || !isDirty}>
              {save.isPending ? <Spinner className="size-4" /> : <SaveIcon className="size-4" />}
              Save without approving
            </Button>
            <Button variant="ghost" onClick={() => setConfirm('redraft')} disabled={!canWrite || leased}>
              {redraft.isPending ? <Spinner className="size-4" /> : <RefreshCwIcon className="size-4" />}
              Write another draft
            </Button>
          </div>
        )}

        {production.script_updated_at && (
          <p className="text-xs text-muted-foreground">
            Last changed {formatRelative(production.script_updated_at)}
            {production.script_updated_by ? ' by hand' : ' by the pipeline'}.
          </p>
        )}
      </CardContent>

      <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {confirm === 'approve' ? 'Approve this script and start the render?' : 'Write another draft?'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {confirm === 'approve'
                ? 'The video will be generated from exactly these words, and generating it costs money. You cannot change the script afterwards — you would have to make the idea again.'
                : 'The model writes a fresh script and it replaces what is in the box. This costs one model call and generates no video. Your current text is not kept.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Back</AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={(event) => {
                event.preventDefault();
                if (confirm === 'approve') void runApprove();
                if (confirm === 'redraft') void runRedraft();
              }}
            >
              {busy && <Spinner className="size-4" />}
              {confirm === 'approve' ? 'Approve and render' : 'Write another'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}

function isStoppedRow(production: ProductionRow): boolean {
  return production.status === 'parked' || production.status === 'failed';
}

function ScriptBadge({ approved, isDirty, editable }: { approved: boolean; isDirty: boolean; editable: boolean }) {
  if (!editable) return <Badge variant="secondary">Locked</Badge>;
  if (isDirty) return <Badge variant="outline">Unsaved changes</Badge>;
  if (approved) return <Badge variant="secondary">Approved</Badge>;
  return <Badge variant="outline">Waiting for your approval</Badge>;
}

/** Length in the two units that mean something: characters, and seconds. */
function ScriptMeter({ script }: { script: string }) {
  const chars = script.trim().length;
  const seconds = estimateSeconds(script);
  const over = chars > MAX_SCRIPT_CHARS;
  return (
    <span className={over ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
      {chars.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters
      {seconds > 0 && ` · about ${seconds}s spoken`}
    </span>
  );
}
