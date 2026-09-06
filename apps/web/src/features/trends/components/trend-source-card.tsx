import { X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { useOwner } from '@/features/auth/use-owner';
import { normaliseHashtag } from '@/features/trends/api';
import { SaveRow } from '@/features/trends/components/setting-field';
import { GEO_OPTIONS, MAX_TREND_KEYWORDS, normaliseKeyword } from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';
import type { TrendSource } from '@/lib/database.types';

const FIELDS = ['trend_source', 'trend_keywords', 'trend_geo', 'hashtags'] as const;

/**
 * Where ideas come from.
 *
 * The two sources answer different questions and take different vocabularies,
 * which is why this is one card rather than a toggle buried in another. Google
 * Trends measures search demand and takes the words a buyer would type; TikTok
 * measured which video format was working and takes hashtags. Handing either
 * list to the other produces a run that completes, reports success and finds
 * nothing — the failure that looks most like a quiet week.
 */
export function TrendSourceCard() {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);
  const [term, setTerm] = useState('');

  const usingTrends = draft?.trend_source === 'google_trends';

  function addTerm() {
    if (!draft) return;
    if (usingTrends) {
      const clean = normaliseKeyword(term);
      if (!clean) return;
      if (draft.trend_keywords.includes(clean)) {
        toast.info(`“${clean}” is already on the list`);
        setTerm('');
        return;
      }
      if (draft.trend_keywords.length >= MAX_TREND_KEYWORDS) {
        toast.info(`That is the ${MAX_TREND_KEYWORDS}-term limit.`);
        return;
      }
      set({ trend_keywords: [...draft.trend_keywords, clean] });
    } else {
      const clean = normaliseHashtag(term);
      if (!clean) return;
      if (draft.hashtags.includes(clean)) {
        toast.info(`#${clean} is already in the list`);
        setTerm('');
        return;
      }
      set({ hashtags: [...draft.hashtags, clean] });
    }
    setTerm('');
  }

  const items = usingTrends ? (draft?.trend_keywords ?? []) : (draft?.hashtags ?? []);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Where ideas come from</CardTitle>
        <CardDescription>
          The scout grounds every idea in something it observed. This is what it observes.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {draft && (
          <>
            <div className="space-y-2">
              <Label htmlFor="trend-source">Source</Label>
              <NativeSelect
                id="trend-source"
                className="w-56"
                value={draft.trend_source}
                disabled={!isOwner || isSaving}
                onChange={(e) => set({ trend_source: e.target.value as TrendSource })}
              >
                <NativeSelectOption value="google_trends">Google Trends</NativeSelectOption>
                <NativeSelectOption value="tiktok">TikTok hashtags</NativeSelectOption>
              </NativeSelect>
              <p className="text-muted-foreground text-xs">
                {usingTrends
                  ? 'Measures what people are searching for, against each term’s own recent history. It says a subject is live; it cannot tell you which hook opens the video.'
                  : 'Measures which video formats are outperforming their own author’s median — the better signal for a hook.'}
              </p>
            </div>

            {!usingTrends && (
              <Alert variant="destructive">
                <AlertTitle>TikTok is not currently working</AlertTitle>
                <AlertDescription>
                  Every hashtag feed is refused — from a datacenter IP and a home one, headless and not, with a token
                  and without. The library is pinned at its own latest release, which is five months old. Kept as an
                  option in case that changes; runs will find nothing until it does.
                </AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="new-term">{usingTrends ? 'Search terms' : 'Hashtags'}</Label>

              <div className="flex flex-wrap gap-2">
                {items.length === 0 && (
                  <p className="text-muted-foreground text-sm">None. The scout has nothing to look at.</p>
                )}
                {items.map((item) => (
                  <Badge key={item} variant="secondary" className="gap-1 pr-1 font-normal">
                    {usingTrends ? item : `#${item}`}
                    {isOwner && (
                      <button
                        type="button"
                        aria-label={`Remove ${item}`}
                        className="rounded-sm opacity-60 hover:opacity-100"
                        disabled={isSaving}
                        onClick={() =>
                          usingTrends
                            ? set({ trend_keywords: draft.trend_keywords.filter((k) => k !== item) })
                            : set({ hashtags: draft.hashtags.filter((h) => h !== item) })
                        }
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
                    id="new-term"
                    value={term}
                    disabled={isSaving}
                    placeholder={usingTrends ? 'e.g. bookkeeping software' : 'Add a hashtag'}
                    onChange={(e) => setTerm(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || (e.key === ',' && !usingTrends)) {
                        e.preventDefault();
                        addTerm();
                      }
                    }}
                  />
                  <Button type="button" variant="outline" onClick={addTerm} disabled={isSaving}>
                    Add
                  </Button>
                </div>
              )}

              <p className="text-muted-foreground text-xs">
                {usingTrends
                  ? 'Write what a buyer would type, not what we would call it. “bookkeeping software”, not “workflow orchestration” — a term nobody searches returns a flat line and scores as no signal. Each term costs one request, so the list length is what decides run time.'
                  : 'Without the leading #. Each hashtag is roughly four minutes of scouting.'}
              </p>
            </div>

            {usingTrends && (
              <div className="space-y-2">
                <Label htmlFor="trend-geo">Region</Label>
                <NativeSelect
                  id="trend-geo"
                  className="w-56"
                  value={draft.trend_geo}
                  disabled={!isOwner || isSaving}
                  onChange={(e) => set({ trend_geo: e.target.value })}
                >
                  {GEO_OPTIONS.map((g) => (
                    <NativeSelectOption key={g.code} value={g.code}>
                      {g.label}
                    </NativeSelectOption>
                  ))}
                </NativeSelect>
                <p className="text-muted-foreground text-xs">
                  Search interest is regional. Worldwide mixes markets you do not sell to, which usually flattens the
                  terms that matter most.
                </p>
              </div>
            )}

            <SaveRow isOwner={isOwner} dirty={dirty} isSaving={isSaving} blocked={blockedReason} onSave={onSave} />
          </>
        )}
      </CardContent>
    </Card>
  );
}
