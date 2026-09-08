import { useNavigate } from '@tanstack/react-router';
import { XIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { useOwner } from '@/features/auth/use-owner';
import { isAlreadyRemovedError, useDismissTrendRun } from '@/features/trends/api';
import { type SearchRun, truncatePrompt } from '@/features/trends/search';
import { isTrendRunInFlight } from '@/lib/database.types';
import { toError } from '@/lib/supabase-error';

/**
 * Taking a search off the list.
 *
 * A finished run's notice never changes, so a search made by mistake, or one
 * a pre-feature worker scouted as the saved list, sits at the top of the page
 * until a newer run replaces it. This moves it out of the way. The row is kept
 * and its ideas stay in the queue: removing the search is not a decision about
 * them, and each has its own Dismiss.
 *
 * On a search that is still going, removing also stops it. "Remove" pressed on
 * something running means "and stop spending on it", and the dialog says so.
 *
 * Hidden for a viewer, as the idea Dismiss is, and it asks first: the button
 * sits beside a link, and a thumb finds it by accident on a phone.
 */
export function RemoveSearchButton({ run, selected }: { run: SearchRun; selected: boolean }) {
  const { isOwner } = useOwner();
  const [open, setOpen] = useState(false);
  const dismiss = useDismissTrendRun();
  const navigate = useNavigate();

  if (!isOwner) return null;

  const inFlight = isTrendRunInFlight(run);

  async function onConfirm() {
    try {
      await dismiss.mutateAsync(run.id);
      setOpen(false);
      toast.success(inFlight ? 'Search stopped and removed' : 'Search removed', {
        description: 'Any ideas it added are still in the queue until you decide on them.',
      });
      // The list this page was filtered to has just been removed from under it.
      if (selected) await navigate({ to: '/queue', search: { page: 1 } });
    } catch (error) {
      if (isAlreadyRemovedError(error)) {
        // Two tabs, or a list that moved on. The outcome wanted is the one
        // that happened; it should not arrive red.
        setOpen(false);
        toast.info('That search had already been removed');
        return;
      }
      // The dialog stays open on a failure so the button is still there to
      // press again -- a closed dialog plus a toast reads as "it half worked".
      toast.error('Could not remove the search', { description: toError(error).message });
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          aria-label={`Remove “${truncatePrompt(run.prompt, 60)}”`}
        >
          <XIcon className="size-4" />
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{inFlight ? 'Stop and remove this search?' : 'Remove this search?'}</AlertDialogTitle>
          <AlertDialogDescription>
            “{truncatePrompt(run.prompt, 140)}” leaves the recent searches and the notice above the box.
            {inFlight && ' It is still running, so it is stopped first, and nothing it had found is drafted.'} Any ideas
            it already added stay in the queue until you decide on them.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={dismiss.isPending}>Keep it</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={dismiss.isPending}
            onClick={(event) => {
              // Radix closes on click; the close has to wait for the call so a
              // failure can be shown against the button that caused it.
              event.preventDefault();
              void onConfirm();
            }}
          >
            {dismiss.isPending && <Spinner className="size-4" />}
            {inFlight ? 'Stop and remove' : 'Remove'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
