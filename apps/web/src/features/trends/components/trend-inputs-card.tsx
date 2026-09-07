import { ChevronRightIcon, X } from 'lucide-react';
import { type ReactNode, useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { useOwner } from '@/features/auth/use-owner';
import { normaliseHashtag } from '@/features/trends/api';
import { SaveRow } from '@/features/trends/components/setting-field';
import {
  APIFY_PLATFORM_OPTIONS,
  GEO_OPTIONS,
  MAX_TREND_KEYWORDS,
  normaliseKeyword,
  SOURCE_OPTIONS,
  usesHashtags,
} from '@/features/trends/controls';
import { useTrendDraft } from '@/features/trends/use-trend-draft';
import type { ApifyPlatform, TrendSource } from '@/lib/database.types';

const FIELDS = ['trend_source', 'trend_keywords', 'trend_geo', 'hashtags', 'apify_platforms'] as const;

/**
 * What the scout searches: both vocabularies, and the region.
 *
 * The two lists are shown together rather than one at a time, which is the
 * change from the card this replaces. Each source reads only one of them —
 * Google Trends and YouTube take the words a buyer types, Apify takes hashtags
 * — and swapping the visible list when the source changed made the other one
 * invisible rather than absent. That is the failure this card exists to
 * prevent: hand a source the wrong vocabulary and the run completes, reports
 * success and finds nothing, which looks exactly like a quiet week.
 *
 * So both are always on screen, and the one in use says so. Editing the list
 * the current source ignores is legitimate — it is how you prepare a switch —
 * and now it is visibly that rather than an accident.
 *
 * The region is Google Trends' alone: interest is normalised within a region,
 * so worldwide mixes markets we do not sell to. Apify's scrapers and the
 * YouTube search API take no geography from us at all, so it stays on screen
 * and says which source will act on it rather than vanishing.
 *
 * Rendered on both the queue and Settings from this one definition. `collapsible`
 * is the only difference between them: on the queue it opens from a summary
 * line so configuration sits beside the button that spends it without pushing
 * the ideas down the page.
 */
export function TrendInputsCard({ collapsible = false }: { collapsible?: boolean }) {
  const { isOwner } = useOwner();
  const { draft, set, dirty, isPending, error, isSaving, blockedReason, onSave } = useTrendDraft(FIELDS);
  const [newTerm, setNewTerm] = useState('');
  const [newTag, setNewTag] = useState('');
  const [open, setOpen] = useState(false);

  const source = draft?.trend_source;
  const hashtagMode = source ? usesHashtags(source) : false;
  const chosen = SOURCE_OPTIONS.find((option) => option.value === source);
  const isGoogleTrends = source === 'google_trends';
  // Apify with nothing selected is a source that can never scout, which looks
  // exactly like a runner that has stopped working. Blocked rather than saved.
  const noPlatforms = source === 'apify' && (draft?.apify_platforms ?? []).length === 0;

  function addTerm() {
    if (!draft) return;
    const clean = normaliseKeyword(newTerm);
    if (!clean) return;
    if (draft.trend_keywords.includes(clean)) {
      toast.info(`“${clean}” is already on the list`);
      setNewTerm('');
      return;
    }
    if (draft.trend_keywords.length >= MAX_TREND_KEYWORDS) {
      toast.info(`That is the ${MAX_TREND_KEYWORDS}-term limit.`);
      return;
    }
    set({ trend_keywords: [...draft.trend_keywords, clean] });
    setNewTerm('');
  }

  function addTag() {
    if (!draft) return;
    const clean = normaliseHashtag(newTag);
    if (!clean) return;
    if (draft.hashtags.includes(clean)) {
      toast.info(`#${clean} is already in the list`);
      setNewTag('');
      return;
    }
    set({ hashtags: [...draft.hashtags, clean] });
    setNewTag('');
  }

  const body = (
    <CardContent className="space-y-6">
      {error && <p className="text-destructive text-sm">Could not load settings: {error.message}</p>}
      {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

      {draft && (
        <>
          <div className="space-y-2">
            <Label htmlFor="trend-source">Source</Label>
            <NativeSelect
              id="trend-source"
              className="w-full sm:w-72"
              value={draft.trend_source}
              disabled={!isOwner || isSaving}
              onChange={(e) => set({ trend_source: e.target.value as TrendSource })}
            >
              {SOURCE_OPTIONS.map((option) => (
                <NativeSelectOption key={option.value} value={option.value}>
                  {option.label}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <p className="text-muted-foreground text-xs">{chosen?.help}</p>
          </div>

          {/* Shown for the two sources that need a credential the pipeline
              holds and this app cannot see — it is a static bundle talking to
              Postgres, with no way to ask whether a key is set. So the
              warning is unconditional rather than clever: better to tell
              someone a key is required when it already is than to let a
              scheduled run fail every hour with nobody watching. */}
          {chosen?.needsCredential && (
            <Alert>
              <AlertTitle>{chosen.label} needs a key before it will run</AlertTitle>
              <AlertDescription>
                {chosen.credentialHint} Until it is set, every run refuses in its first second rather than scouting —
                deliberately, because{' '}
                {chosen.value === 'apify'
                  ? 'an Apify scrape is billed per result and finding out afterwards would mean paying for one nobody could use'
                  : 'a run that scouts and then cannot score is a wasted quota'}
                . Google Trends needs no key.
              </AlertDescription>
            </Alert>
          )}

          {source === 'apify' && (
            <div className="space-y-2">
              <Label>Platforms</Label>
              <ToggleGroup
                type="multiple"
                variant="outline"
                className="flex-wrap"
                value={draft.apify_platforms}
                disabled={!isOwner || isSaving}
                onValueChange={(platforms) => set({ apify_platforms: platforms as ApifyPlatform[] })}
              >
                {APIFY_PLATFORM_OPTIONS.map((platform) => (
                  <ToggleGroupItem key={platform.value} value={platform.value} aria-label={platform.label}>
                    {platform.label}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              {noPlatforms ? (
                <p className="text-destructive text-xs">
                  Pick at least one. A source that is selected but can never scout looks exactly like a broken runner —
                  choose a different source instead.
                </p>
              ) : (
                <p className="text-muted-foreground text-xs">
                  Both are scraped with the same hashtag list. Each one is a separate metered scrape, so two platforms
                  is roughly twice the cost of one. Instagram publishes no view count on many posts and no share count
                  at all; those are reported in the run breakdown rather than guessed at.
                </p>
              )}
            </div>
          )}

          {/* Search terms first, because the default source reads them. */}
          <TermList
            id="new-term"
            label="Search terms"
            inUse={!hashtagMode}
            usedBy="Google Trends and YouTube"
            items={draft.trend_keywords}
            format={(term) => term}
            placeholder="e.g. bookkeeping software"
            value={newTerm}
            onValueChange={setNewTerm}
            onAdd={addTerm}
            onRemove={(term) => set({ trend_keywords: draft.trend_keywords.filter((k) => k !== term) })}
            canEdit={isOwner}
            disabled={isSaving}
            emptyText="None. Google Trends has nothing to measure."
            count={`${draft.trend_keywords.length} of ${MAX_TREND_KEYWORDS}`}
            help="Write what a buyer would type, not what we would call it. “bookkeeping software”, not “workflow orchestration” — a term nobody searches returns a flat line and scores as no signal. Each term costs one request, so the list length is what decides run time."
          />

          <TermList
            id="new-hashtag"
            label="Hashtags"
            inUse={hashtagMode}
            usedBy="Apify (TikTok and Instagram)"
            items={draft.hashtags}
            format={(tag) => `#${tag}`}
            placeholder="Add a hashtag"
            value={newTag}
            onValueChange={setNewTag}
            onAdd={addTag}
            onRemove={(tag) => set({ hashtags: draft.hashtags.filter((h) => h !== tag) })}
            canEdit={isOwner}
            disabled={isSaving}
            commaAdds
            emptyText="None. The Apify scrapers have nothing to look at."
            count={`${draft.hashtags.length}`}
            help="Without the leading #. Each hashtag is one metered scrape per platform, so the list length is what decides run cost. Google Trends cannot read these — it measures typed searches, and a hashtag is not one."
          />

          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor="trend-geo">Region</Label>
              {isGoogleTrends ? (
                <Badge variant="secondary" className="font-normal">
                  In use
                </Badge>
              ) : (
                <Badge variant="outline" className="text-muted-foreground font-normal">
                  Google Trends only
                </Badge>
              )}
            </div>
            <NativeSelect
              id="trend-geo"
              className="w-full sm:w-64"
              value={draft.trend_geo}
              disabled={!isOwner || isSaving}
              onChange={(e) => set({ trend_geo: e.target.value })}
            >
              {GEO_OPTIONS.map((geo) => (
                <NativeSelectOption key={geo.code} value={geo.code}>
                  {geo.label}
                </NativeSelectOption>
              ))}
            </NativeSelect>
            <p className="text-muted-foreground text-xs">
              {isGoogleTrends
                ? 'Search interest is regional. Worldwide mixes markets you do not sell to, which usually flattens the terms that matter most.'
                : 'Saved either way, and applied the moment the source is Google Trends. Neither the Apify scrapers nor the YouTube search take a region from here.'}
            </p>
          </div>

          <SaveRow
            isOwner={isOwner}
            dirty={dirty}
            isSaving={isSaving}
            blocked={
              noPlatforms ? 'Pick at least one platform for Apify, or choose a different source.' : blockedReason
            }
            onSave={onSave}
          />
        </>
      )}
    </CardContent>
  );

  const title = 'Trend search inputs';
  const description = 'What the scout searches for, and where. Changes apply to the next run.';

  if (!collapsible) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{title}</CardTitle>
          <CardDescription>{description}</CardDescription>
        </CardHeader>
        {body}
      </Card>
    );
  }

  // Closed to start with. This is a page for deciding on ideas; the inputs
  // that produced them belong within reach rather than in the way, and an open
  // panel would push the queue itself below the fold on a phone.
  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        {/* The whole header is the target, not just the chevron: it is the
            easiest thing to hit on a phone. No aria-label, deliberately — the
            summary is inside the button, so letting it into the accessible
            name is what tells a screen-reader user what the shut panel says. */}
        <CollapsibleTrigger className="w-full text-left">
          <CardHeader className="gap-1">
            <CardTitle className="flex items-center gap-1.5 text-base">
              <ChevronRightIcon
                className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {title}
            </CardTitle>
            <CardDescription className="pl-[1.375rem]">
              {draft ? <Summary draft={draft} /> : description}
            </CardDescription>
          </CardHeader>
        </CollapsibleTrigger>
        <CollapsibleContent>{body}</CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

/**
 * The one line that has to be true when the panel is shut.
 *
 * It names the source, the size of the list that source actually reads, and —
 * for Google Trends — the region, because those three are what decide whether
 * pressing Generate is worth anything. The list the source ignores is left out
 * deliberately: a summary that counted both would read as though both were
 * being searched.
 */
function Summary({
  draft,
}: {
  draft: { trend_source: TrendSource; trend_keywords: string[]; hashtags: string[]; trend_geo: string };
}) {
  const label = SOURCE_OPTIONS.find((option) => option.value === draft.trend_source)?.label ?? draft.trend_source;
  const hashtagMode = usesHashtags(draft.trend_source);
  const count = hashtagMode ? draft.hashtags.length : draft.trend_keywords.length;
  const noun = hashtagMode ? 'hashtag' : 'search term';
  const geo = GEO_OPTIONS.find((option) => option.code === draft.trend_geo)?.label ?? draft.trend_geo;

  return (
    <span>
      {label} · {count} {count === 1 ? noun : `${noun}s`}
      {draft.trend_source === 'google_trends' && ` · ${geo}`}
    </span>
  );
}

/**
 * One vocabulary: its chips, the box that adds to it, and whether it is live.
 *
 * Both lists are the same control because they are the same job, and writing
 * it twice would let the two drift — which is how a fix to the duplicate check
 * ends up applying to hashtags and not to terms. What differs between them is
 * data: how an item is displayed, what the empty list means, and whether a
 * comma ends an entry (it does for hashtags, which are single tokens, and must
 * not for terms, where a comma can be part of the phrase).
 */
function TermList({
  id,
  label,
  inUse,
  usedBy,
  items,
  format,
  placeholder,
  value,
  onValueChange,
  onAdd,
  onRemove,
  canEdit,
  disabled,
  commaAdds = false,
  emptyText,
  count,
  help,
}: {
  id: string;
  label: string;
  inUse: boolean;
  usedBy: string;
  items: string[];
  format: (item: string) => string;
  placeholder: string;
  value: string;
  onValueChange: (value: string) => void;
  onAdd: () => void;
  onRemove: (item: string) => void;
  canEdit: boolean;
  disabled: boolean;
  commaAdds?: boolean;
  emptyText: string;
  count: string;
  help: ReactNode;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <Label htmlFor={id}>{label}</Label>
        {inUse ? (
          <Badge variant="secondary" className="font-normal">
            In use
          </Badge>
        ) : (
          <Badge variant="outline" className="text-muted-foreground font-normal">
            Not searched by the current source
          </Badge>
        )}
        <span className="text-muted-foreground ml-auto text-xs tabular-nums">{count}</span>
      </div>

      <p className="text-muted-foreground text-xs">Read by {usedBy}.</p>

      <div className="flex flex-wrap gap-2">
        {items.length === 0 && <p className="text-muted-foreground text-sm">{emptyText}</p>}
        {items.map((item) => (
          <Badge key={item} variant="secondary" className="max-w-full gap-1 pr-1 font-normal">
            <span className="truncate">{format(item)}</span>
            {canEdit && (
              <button
                type="button"
                aria-label={`Remove ${item}`}
                className="shrink-0 rounded-sm opacity-60 hover:opacity-100"
                disabled={disabled}
                onClick={() => onRemove(item)}
              >
                <X className="size-3" />
              </button>
            )}
          </Badge>
        ))}
      </div>

      {canEdit && (
        <div className="flex gap-2 pt-1">
          <Input
            id={id}
            value={value}
            disabled={disabled}
            placeholder={placeholder}
            onChange={(e) => onValueChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || (e.key === ',' && commaAdds)) {
                e.preventDefault();
                onAdd();
              }
            }}
          />
          <Button
            type="button"
            variant="outline"
            aria-label={`Add ${label.toLowerCase().replace(/s$/, '')}`}
            onClick={onAdd}
            disabled={disabled}
          >
            Add
          </Button>
        </div>
      )}

      <p className="text-muted-foreground text-xs">{help}</p>
    </div>
  );
}
