import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { SaveRow } from '@/features/trends/components/setting-field';
import { useTrendDraft } from '@/features/trends/use-trend-draft';

/**
 * The brief.
 *
 * On its own card, and it carries more weight than it used to. While the scout
 * read TikTok it could show the model which format was working, and the brief
 * only had to supply relevance. Google Trends says a subject is live and
 * nothing about how to open a video, so the hook now comes from here or from
 * nowhere.
 *
 * The hashtags moved to "Where ideas come from", next to the search terms they
 * are an alternative to — each source reads its own vocabulary, and keeping
 * both lists in one place is what makes that visible.
 */
const FIELDS = ['niche_brief'] as const;

export function TrendSettingsCard() {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Niche brief</CardTitle>
        <CardDescription>
          What every idea is judged against. Changes apply to the next run — they do not re-open ideas already in the
          queue.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {draft && (
          <>
            <div className="space-y-2">
              <Label htmlFor="niche-brief">What you do, who you speak to, what you must not claim</Label>
              <Textarea
                id="niche-brief"
                rows={10}
                value={draft.niche_brief}
                disabled={!isOwner || isSaving}
                onChange={(e) => set({ niche_brief: e.target.value })}
                placeholder="What you do, who you speak to, and what you must never claim."
              />
              <p className="text-muted-foreground text-xs">
                The “must not claim” part is what stops the model proposing a claim you would have to defend. Trend
                research refuses to run while this is empty.
              </p>
              <p className="text-muted-foreground text-xs">
                This matters more than it used to. The scout now measures search demand, which says a subject is live
                but nothing about how to open a video — so the hook comes from what you write here. The more precisely
                it names the buyer’s week, the better the ideas.
              </p>
            </div>

            <SaveRow isOwner={isOwner} dirty={dirty} isSaving={isSaving} blocked={blockedReason} onSave={onSave} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
