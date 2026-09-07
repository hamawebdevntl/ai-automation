import { Trash2Icon } from 'lucide-react';
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
import { useRejectIdea } from '@/features/queue/api';

/**
 * The note written on a dismissal made from the list.
 *
 * A rejection from the detail page usually carries a reason typed by hand.
 * This one cannot, so it says where the decision came from instead — otherwise
 * the audit row is a bare "rejected" and nobody can tell a considered no from
 * a two-second triage pass.
 */
export const DISMISS_NOTE = 'Dismissed from the queue without opening it.';

export interface DismissIdeaButtonProps {
  ideaId: string;
  /** Shown in the confirmation so you can tell which card you hit. */
  title: string;
}

/**
 * Throwing an idea out without opening it.
 *
 * Gate 1 has no delete: rejecting is what removing an idea means here, and the
 * `reject_idea` function keeps the audit row and the status change together.
 * So this is the same decision the detail page makes, minus the reading.
 *
 * It asks first, briefly. Nothing restores a rejected idea — there is no
 * un-reject — and a trash can sitting next to a link you came to click is
 * exactly the kind of button a thumb finds by accident on a phone.
 *
 * Hidden rather than disabled for a viewer: a row of dead trash cans down a
 * list of ten cards is noise, and the detail page is where read-only gets
 * explained. The database refuses the call regardless of what is rendered.
 */
export function DismissIdeaButton({ ideaId, title }: DismissIdeaButtonProps) {
  const { isOwner } = useOwner();
  const [open, setOpen] = useState(false);
  const reject = useRejectIdea();

  if (!isOwner) return null;

  async function onConfirm() {
    try {
      await reject.mutateAsync({ ideaId, note: DISMISS_NOTE });
      setOpen(false);
      toast.success('Dismissed', { description: 'It is out of the queue. Nothing was spent on it.' });
    } catch (error) {
      // The dialog stays open on a failure so the button is still there to
      // press again -- a closed dialog plus a toast reads as "it half worked".
      toast.error(error instanceof Error ? error.message : 'Could not dismiss this idea');
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <AlertDialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          aria-label={`Dismiss “${title}”`}
        >
          <Trash2Icon className="size-4" />
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Dismiss this idea?</AlertDialogTitle>
          <AlertDialogDescription>
            “{title}” leaves the queue for good — there is no way to put it back. Nothing has been spent on it, and
            trend research can always propose it again.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={reject.isPending}>Keep it</AlertDialogCancel>
          <AlertDialogAction
            variant="destructive"
            disabled={reject.isPending}
            onClick={(event) => {
              // Radix closes on click; the close has to wait for the call so a
              // failure can be shown against the button that caused it.
              event.preventDefault();
              void onConfirm();
            }}
          >
            {reject.isPending && <Spinner className="size-4" />}
            Dismiss
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
