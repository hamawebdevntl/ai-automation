import { CircleStopIcon, PauseIcon, PlayIcon, RefreshCwIcon, RotateCcwIcon, UndoDotIcon } from 'lucide-react';
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
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { useOwner } from '@/features/auth/use-owner';
import type { ProductionRow, RewindStep, StylePresetRow } from '@/lib/database.types';
import { cn } from '@/lib/utils';
import {
  useCancelProduction,
  usePauseProduction,
  useRerunProduction,
  useResumeProduction,
  useRetryProduction,
  useRewindProduction,
} from '../api';
import {
  availableControls,
  type ControlAction,
  isRenderInFlight,
  REWIND_DESCRIPTIONS,
  REWIND_LABELS,
  rewindableSteps,
} from '../pipeline-steps';

/**
 * Driving a production by hand.
 *
 * Every button here maps to one owner-gated Postgres function. The disabled
 * states mirror those functions' guards so that a control the database would
 * refuse is never offered as if it would work — and, more usefully, so the
 * owner is told *why*. A viewer sees the same panel, entirely disabled, which
 * is how the gate buttons already behave.
 *
 * The two that can cost money — retry and re-run — confirm first. Cancel
 * confirms too, because it is the only one that cannot be undone.
 */

interface ConfirmSpec {
  action: Exclude<ControlAction, 'rewind'> | 'rewind';
  title: string;
  body: string;
  confirmLabel: string;
  destructive?: boolean;
  step?: RewindStep;
}

export function ProductionControls({ production, presets }: { production: ProductionRow; presets: StylePresetRow[] }) {
  const { isOwner, isLoading: isRoleLoading } = useOwner();
  const [note, setNote] = useState('');
  const [confirm, setConfirm] = useState<ConfirmSpec | null>(null);

  const pause = usePauseProduction();
  const resume = useResumeProduction();
  const retry = useRetryProduction();
  const cancel = useCancelProduction();
  const rerun = useRerunProduction();
  const rewind = useRewindProduction();

  const busy =
    pause.isPending || resume.isPending || retry.isPending || cancel.isPending || rerun.isPending || rewind.isPending;

  const controls = availableControls(production);
  const rewinds = rewindableSteps(production);
  const style = presets.find((preset) => preset.id === production.style_preset_id) ?? null;

  const run = async (spec: ConfirmSpec) => {
    const input = { productionId: production.id, note };
    try {
      switch (spec.action) {
        case 'pause':
          await pause.mutateAsync(input);
          toast.success('Paused', {
            description: isRenderInFlight(production)
              ? 'The current step finishes first. A render already running at the provider carries on.'
              : 'The current step finishes first, then nothing else runs.',
          });
          break;
        case 'resume':
          await resume.mutateAsync(input);
          toast.success('Resumed', { description: 'The worker picks it up within a few seconds.' });
          break;
        case 'retry':
          await retry.mutateAsync(input);
          toast.success('Retrying', {
            description: 'Sent back in at the step it stopped on. Any render it already paid for is reused.',
          });
          break;
        case 'cancel':
          await cancel.mutateAsync(input);
          toast.success('Cancelled', { description: 'Nothing more will happen to this production.' });
          break;
        case 'rerun': {
          const created = await rerun.mutateAsync(input);
          toast.success('Re-running', {
            description: `A new production was opened. This one is kept as ${created.id.slice(0, 8)}'s predecessor.`,
          });
          break;
        }
        case 'rewind':
          if (!spec.step) return;
          await rewind.mutateAsync({ ...input, step: spec.step });
          toast.success('Rewound', { description: REWIND_DESCRIPTIONS[spec.step] });
          break;
      }
      setNote('');
    } catch (error) {
      // `toError` in the api layer makes this branch true for a real database
      // refusal, so the reason the function gave is what gets shown.
      toast.error(error instanceof Error ? error.message : 'That did not work');
    } finally {
      setConfirm(null);
    }
  };

  return (
    <TooltipProvider>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Controls</CardTitle>
          <CardDescription>
            Nothing here happens on its own. Each of these is recorded against this production with your name on it.
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-4">
          {!isOwner && !isRoleLoading && (
            <Alert>
              <AlertTitle>Read-only</AlertTitle>
              <AlertDescription>
                Your account is a viewer, so you can follow this production but not drive it. An owner can promote you.
              </AlertDescription>
            </Alert>
          )}

          <div className="space-y-2">
            <Label htmlFor="control-note">Note (optional)</Label>
            <Textarea
              id="control-note"
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="Why you are doing this. Kept with the action."
              rows={2}
              disabled={!isOwner || busy}
            />
          </div>

          <div className="flex flex-wrap gap-2">
            <ControlButton
              control={controls.pause}
              disabled={!isOwner || busy}
              icon={PauseIcon}
              label="Pause"
              onClick={() =>
                setConfirm({
                  action: 'pause',
                  title: 'Pause this production?',
                  body: isRenderInFlight(production)
                    ? 'The step running now finishes first. Note that a render already running at the provider carries on regardless — pausing does not stop it or save the money.'
                    : 'The step running now finishes first, then nothing else runs until you resume it.',
                  confirmLabel: 'Pause',
                })
              }
            />

            <ControlButton
              control={controls.resume}
              disabled={!isOwner || busy}
              icon={PlayIcon}
              label="Resume"
              onClick={() => void run({ action: 'resume', title: '', body: '', confirmLabel: '' })}
            />

            <ControlButton
              control={controls.retry}
              disabled={!isOwner || busy}
              icon={RotateCcwIcon}
              label="Retry"
              onClick={() =>
                setConfirm({
                  action: 'retry',
                  title: 'Retry this production?',
                  body: 'It goes back in at the step it stopped on. A render it has already paid for is reused rather than submitted again, so this does not spend anything by itself.',
                  confirmLabel: 'Retry',
                })
              }
            />

            <ControlButton
              control={controls.cancel}
              disabled={!isOwner || busy}
              icon={CircleStopIcon}
              label="Cancel"
              destructive
              onClick={() =>
                setConfirm({
                  action: 'cancel',
                  title: 'Cancel this production?',
                  body: 'It stops for good and nothing further happens to it. The idea stays as it is — you can re-run this production afterwards if you change your mind.',
                  confirmLabel: 'Cancel it',
                  destructive: true,
                })
              }
            />

            <ControlButton
              control={controls.rerun}
              disabled={!isOwner || busy}
              icon={RefreshCwIcon}
              label="Make it again"
              onClick={() =>
                setConfirm({
                  action: 'rerun',
                  title: 'Make this idea again?',
                  body: `This opens a new production and renders from scratch${
                    style ? ` as ${style.name}` : ''
                  }. It spends money. This production is kept exactly as it is, with its cut and its record.`,
                  confirmLabel: 'Re-run',
                })
              }
            />
          </div>

          {rewinds.length > 0 && (
            <div className="space-y-2 border-t pt-4">
              <p className="text-sm font-medium">Redo a step</p>
              <p className="text-sm text-muted-foreground">
                These reuse the render this production already has, so they cost nothing.
              </p>
              <div className="flex flex-wrap gap-2">
                {rewinds.map((step) => (
                  <Button
                    key={step}
                    variant="outline"
                    size="sm"
                    disabled={!isOwner || busy || !controls.rewind.enabled}
                    onClick={() =>
                      setConfirm({
                        action: 'rewind',
                        step,
                        title: REWIND_LABELS[step],
                        body: REWIND_DESCRIPTIONS[step],
                        confirmLabel: 'Do it',
                      })
                    }
                  >
                    <UndoDotIcon className="size-4" />
                    {REWIND_LABELS[step]}
                  </Button>
                ))}
              </div>
            </div>
          )}
        </CardContent>

        <AlertDialog open={confirm !== null} onOpenChange={(open) => !open && setConfirm(null)}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{confirm?.title}</AlertDialogTitle>
              <AlertDialogDescription>{confirm?.body}</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel disabled={busy}>Back</AlertDialogCancel>
              <AlertDialogAction
                disabled={busy}
                onClick={(event) => {
                  event.preventDefault();
                  if (confirm) void run(confirm);
                }}
              >
                {busy && <Spinner className="size-4" />}
                {confirm?.confirmLabel}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </Card>
    </TooltipProvider>
  );
}

/**
 * A control, with its refusal attached.
 *
 * A disabled button that does not say why is the thing this panel most needs to
 * avoid: "cancel is greyed out" and "cancel is greyed out because a render has
 * already been paid for" are very different pieces of information.
 */
function ControlButton({
  control,
  disabled,
  icon: Icon,
  label,
  destructive,
  onClick,
}: {
  control: { enabled: boolean; reason: string | null };
  disabled: boolean;
  icon: typeof PauseIcon;
  label: string;
  destructive?: boolean;
  onClick: () => void;
}) {
  const inert = disabled || !control.enabled;

  // `aria-disabled` rather than `disabled`, so the button keeps its place in
  // the focus order and the tooltip saying *why* it is unavailable can be
  // reached from the keyboard. A truly disabled button fires no pointer or
  // focus events, which would hide the explanation from exactly the people
  // most likely to need it. The click is guarded instead.
  const button = (
    <Button
      variant={destructive ? 'destructive' : 'outline'}
      size="sm"
      aria-disabled={inert}
      className={cn(inert && 'opacity-50')}
      onClick={() => {
        if (!inert) onClick();
      }}
    >
      <Icon className="size-4" />
      {label}
    </Button>
  );

  if (control.enabled || !control.reason) return button;

  return (
    <Tooltip>
      <TooltipTrigger asChild>{button}</TooltipTrigger>
      <TooltipContent className="max-w-xs">{control.reason}</TooltipContent>
    </Tooltip>
  );
}
