import { X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useOwner } from '@/features/auth/use-owner';
import { NumberSetting, SaveRow } from '@/features/trends/components/setting-field';
import {
  BLOCKLIST_MAX_ENTRIES,
  blocklistEntryError,
  fromPercent,
  isVideoSource,
  normaliseBlockedWord,
  toPercent,
} from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';

const FIELDS = [
  'trend_source',
  'min_plays',
  'min_interest',
  'min_engagement_rate',
  'min_outlier_ratio',
  'caption_blocklist',
  'videos_per_hashtag',
  'ideas_per_run',
] as const;

/**
 * The bars a video has to clear before it becomes an idea.
 *
 * Presented in the order the scout applies them, because that order is useful
 * rather than incidental: the first three cost nothing and the fourth costs a
 * request per author, so tightening the cheap ones makes a run shorter as well
 * as stricter. The run report on the queue uses the same order, so a
 * breakdown can be read straight down this card.
 */
export function TrendFiltersCard() {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);
  const [word, setWord] = useState('');
  // Both video sources measure views, engagement and an author's own median;
  // Google Trends measures none of those. Which filters are shown follows from
  // that one distinction rather than from naming a source here.
  const videoSource = draft ? isVideoSource(draft.trend_source) : false;

  function addWord() {
    if (!draft) return;
    const clean = normaliseBlockedWord(word);
    const problem = blocklistEntryError(clean);
    if (problem) {
      toast.info(problem);
      return;
    }
    if (draft.caption_blocklist.includes(clean)) {
      toast.info(`“${clean}” is already blocked`);
      setWord('');
      return;
    }
    if (draft.caption_blocklist.length >= BLOCKLIST_MAX_ENTRIES) {
      toast.info(`That is the ${BLOCKLIST_MAX_ENTRIES}-word limit.`);
      return;
    }
    set({ caption_blocklist: [...draft.caption_blocklist, clean] });
    setWord('');
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Filters</CardTitle>
        <CardDescription>
          What a signal has to clear to reach your queue, in the order the scout checks it. Only the filters the chosen
          source can actually measure are shown — the rest would look like they were applying when they were not.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {draft && (
          <>
            {/* Two floors on incompatible scales, so only the one that
                applies is shown. A view count is unbounded; Google Trends
                interest is 0-100 against a term's own peak, and offering both
                at once is how a sensible video floor of 198,000 came to reject
                every search term that will ever exist. */}
            {!videoSource ? (
              <NumberSetting
                field="min_interest"
                label="Minimum search interest"
                value={draft.min_interest}
                disabled={!isOwner || isSaving}
                onChange={(v) => set({ min_interest: v })}
                help="How close a term must currently be to its own three-month peak. Zero means no floor — the rise is what this source measures, and the outlier ratio below already checks that."
              />
            ) : (
              <NumberSetting
                field="min_plays"
                label="Minimum views"
                value={draft.min_plays}
                disabled={!isOwner || isSaving}
                onChange={(v) => set({ min_plays: v })}
                help="Reject anything below this outright. Zero means no floor, which is how it has always run."
              />
            )}

            {videoSource && (
              <NumberSetting
                field="min_engagement_rate"
                label="Minimum engagement rate"
                suffix="%"
                value={toPercent(draft.min_engagement_rate)}
                disabled={!isOwner || isSaving}
                onChange={(v) => set({ min_engagement_rate: fromPercent(v) })}
                help="Likes, comments and shares as a share of views. A high view count with almost no interaction usually means paid promotion or an inflated count, and is not something to imitate."
              />
            )}

            <NumberSetting
              field="min_outlier_ratio"
              label="Minimum outlier ratio"
              value={draft.min_outlier_ratio}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ min_outlier_ratio: v })}
              help={
                !videoSource
                  ? 'How far a term must be rising against its own recent history. The strongest quality lever this source has — it is measured per term, so a big topic that is merely steady does not read as a trend.'
                  : 'How far a video must beat the median of its own author or channel — not an absolute view count, so a big account posting a normal video does not read as a trend. Raising it is the strongest quality lever here.'
              }
            />

            <div className="space-y-2">
              <Label htmlFor="blocked-word">Blocked caption words</Label>
              <div className="flex flex-wrap gap-2">
                {draft.caption_blocklist.length === 0 && (
                  <p className="text-muted-foreground text-sm">Nothing blocked.</p>
                )}
                {draft.caption_blocklist.map((blocked) => (
                  <Badge key={blocked} variant="secondary" className="gap-1 pr-1 font-normal">
                    {blocked}
                    {isOwner && (
                      <button
                        type="button"
                        aria-label={`Unblock ${blocked}`}
                        className="rounded-sm opacity-60 hover:opacity-100"
                        onClick={() => set({ caption_blocklist: draft.caption_blocklist.filter((w) => w !== blocked) })}
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
                    id="blocked-word"
                    value={word}
                    disabled={isSaving}
                    placeholder="Block a word"
                    onChange={(e) => setWord(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ',') {
                        e.preventDefault();
                        addWord();
                      }
                    }}
                  />
                  <Button type="button" variant="outline" onClick={addWord} disabled={isSaving}>
                    Block
                  </Button>
                </div>
              )}
              <p className="text-muted-foreground text-xs">
                Matched as whole words, ignoring case, so “course” rejects “my free course” but not “racecourse”.
                Hashtags are searched as one run of text, so it also rejects “#freecourse”. Two characters minimum — a
                single letter is a word in almost every caption. On YouTube this is matched against the title only:
                descriptions are mostly affiliate links and chapter lists, and matching all of that would reject almost
                everything.
              </p>
            </div>

            {videoSource && (
              <NumberSetting
                field="videos_per_hashtag"
                label="Videos to look at per hashtag"
                value={draft.videos_per_hashtag}
                disabled={!isOwner || isSaving}
                onChange={(v) => set({ videos_per_hashtag: v })}
                help="How deep into each feed to go. This is the main thing deciding how long a run takes, alongside the number of hashtags."
              />
            )}

            <NumberSetting
              field="ideas_per_run"
              label="Ideas per run"
              value={draft.ideas_per_run}
              disabled={!isOwner || isSaving}
              onChange={(v) => set({ ideas_per_run: v })}
              help="A ceiling, not a target. The model is told to prefer fewer strong ideas, so a quiet week produces fewer than this and that is working as intended."
            />

            <SaveRow isOwner={isOwner} dirty={dirty} isSaving={isSaving} blocked={blockedReason} onSave={onSave} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
