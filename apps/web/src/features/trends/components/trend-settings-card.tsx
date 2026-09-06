import { X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { normaliseHashtag } from '@/features/trends/api';
import { SaveRow } from '@/features/trends/components/setting-field';
import { estimateRunMinutes, LONG_RUN_MINUTES, runsToCoverEveryTag, tagsPerRun } from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';
import { formatMinutes } from '@/lib/format';

/** Edited here; the rest of the settings live on their own cards. */
const FIELDS = ['niche_brief', 'hashtags'] as const;

export function TrendSettingsCard() {
  const { isOwner } = useOwner();
  const { data, draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);
  const [draftTag, setDraftTag] = useState('');

  function addTag() {
    const clean = normaliseHashtag(draftTag);
    if (!clean || !draft) return;
    if (draft.hashtags.includes(clean)) {
      toast.info(`#${clean} is already in the list`);
      setDraftTag('');
      return;
    }
    set({ hashtags: [...draft.hashtags, clean] });
    setDraftTag('');
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Trend research</CardTitle>
        <CardDescription>
          What the next trend run scouts, and what it judges relevance against. Changes apply to the next run — they do
          not re-open ideas already in the queue.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {data && draft && (
          <>
            <div className="space-y-2">
              <Label htmlFor="niche-brief">Niche brief</Label>
              <Textarea
                id="niche-brief"
                rows={8}
                value={draft.niche_brief}
                disabled={!isOwner || isSaving}
                onChange={(e) => set({ niche_brief: e.target.value })}
                placeholder="What you do, who you speak to, and what you must never claim."
              />
              <p className="text-muted-foreground text-xs">
                Every idea is judged against this. The “must not claim” part is what stops the model proposing a claim
                you would have to defend. Trend research refuses to run while this is empty.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="new-hashtag">Hashtags</Label>

              <div className="flex flex-wrap gap-2">
                {draft.hashtags.length === 0 && (
                  <p className="text-muted-foreground text-sm">None. Trend research has nothing to scout.</p>
                )}
                {draft.hashtags.map((tag) => (
                  <Badge key={tag} variant="secondary" className="gap-1 pr-1 font-normal">
                    #{tag}
                    {isOwner && (
                      <button
                        type="button"
                        aria-label={`Remove #${tag}`}
                        className="rounded-sm opacity-60 hover:opacity-100"
                        onClick={() => set({ hashtags: draft.hashtags.filter((t) => t !== tag) })}
                        disabled={isSaving}
                      >
                        <X className="size-3" />
                      </button>
                    )}
                  </Badge>
                ))}
              </div>

              {isOwner && (
                <div className="flex gap-2 pt-1">
                  <Input
                    id="new-hashtag"
                    value={draftTag}
                    disabled={isSaving}
                    placeholder="Add a hashtag"
                    onChange={(e) => setDraftTag(e.target.value)}
                    onKeyDown={(e) => {
                      // Enter must not submit anything: this card has no form,
                      // and a stray submit would reload the page.
                      if (e.key === 'Enter' || e.key === ',') {
                        e.preventDefault();
                        addTag();
                      }
                    }}
                  />
                  <Button type="button" variant="outline" onClick={addTag} disabled={isSaving}>
                    Add
                  </Button>
                </div>
              )}

              <ScoutingEstimate hashtags={draft.hashtags} settings={data} />
            </div>

            <SaveRow isOwner={isOwner} dirty={dirty} isSaving={isSaving} blocked={blockedReason} onSave={onSave} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * What this hashtag list costs in run length.
 *
 * Uses the saved pacing and per-hashtag settings with the *unsaved* tag list,
 * because the number is here to inform the edit being made right now. It is an
 * upper bound -- see `estimateRunMinutes` -- and says so, since a number used
 * to decide whether a session is too long should err towards too long.
 */
function ScoutingEstimate({
  hashtags,
  settings,
}: {
  hashtags: string[];
  settings: Parameters<typeof estimateRunMinutes>[0];
}) {
  const withDraft = { ...settings, hashtags };
  const scouted = tagsPerRun(withDraft);
  const minutes = estimateRunMinutes(withDraft);
  const rounds = runsToCoverEveryTag(withDraft);

  return (
    <p className="text-muted-foreground text-xs">
      {hashtags.length} tag{hashtags.length === 1 ? '' : 's'}
      {scouted < hashtags.length ? (
        <>
          , of which {scouted} per run — the rotation covers all {hashtags.length} every {rounds} runs
        </>
      ) : null}
      . At most {formatMinutes(minutes)} of scouting on the next run.
      {minutes > LONG_RUN_MINUTES &&
        ' That is a long session against a platform that fights scrapers — consider trimming the list, or scouting fewer tags per run under Run length.'}
    </p>
  );
}
