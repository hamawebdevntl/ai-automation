import { UserRoundIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import type { PresenterRecord } from '@/lib/database.types';

/**
 * Who presented a finished reel.
 *
 * Read from the production rather than from the preset it was made under. The
 * preset is editable and the Gate 1 override is deleted with its idea, so
 * anything reconstructed after the fact would be a guess — which is why
 * `render_backend` is recorded the same way and read the same way here.
 */
export function PresenterRecordBadge({ presenter }: { presenter: PresenterRecord | null }) {
  if (!presenter?.avatar_id) return null;

  const avatar = presenter.avatar_name ?? presenter.avatar_id;
  const voice = presenter.voice_name ?? presenter.voice_id;

  return (
    <Badge variant="outline" className="font-normal text-muted-foreground">
      <UserRoundIcon className="size-3.5" aria-hidden />
      {avatar}
      {voice && ` · ${voice}`}
      {/* Said out loud, because 'the preset' and 'this one production' are the
          two different answers to "why this presenter?" and only one of them
          is still visible anywhere else. */}
      {presenter.source === 'override' && ' · chosen at Gate 1'}
    </Badge>
  );
}
