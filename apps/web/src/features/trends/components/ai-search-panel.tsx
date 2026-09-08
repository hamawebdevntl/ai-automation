import { useQuery } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { SparklesIcon } from 'lucide-react';
import { type KeyboardEvent, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Kbd } from '@/components/ui/kbd';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import {
  isAlreadyRunningError,
  latestTrendRunQueryOptions,
  recentSearchesQueryOptions,
  TREND_RUN_MINUTES,
  trendSettingsQueryOptions,
  useRequestTrendRun,
  useSelectedTrendRun,
} from '@/features/trends/api';
import { RunBanner } from '@/features/trends/components/generate-ideas';
import { ExamplePrompts, RecentSearches } from '@/features/trends/components/recent-searches';
import { estimateSearchLength } from '@/features/trends/controls';
import { PROMPT_MAX_CHARS, promptError, SEARCH_TERMS_DEFAULT } from '@/features/trends/search';
import { isTrendRunInFlight } from '@/lib/database.types';
import { formatMinutes } from '@/lib/format';
import { toError } from '@/lib/supabase-error';

/**
 * Asking for ideas by describing what you are working on, and watching the answer.
 *
 * One component rather than three because the box, the selected search's state
 * and the recent list all revolve around the words in the box: "try again with
 * more detail" is only actionable next to the box it means, and selecting a
 * recent search is what puts its words back.
 *
 * Spends a run. The only things here that do are the Search button and its
 * keyboard shortcut. Examples and recent searches fill or select; they never
 * send. A search is a full scout, so it is one explicit press.
 */
export function AiSearchPanel({ selectedRunId }: { selectedRunId: string | null }) {
  const { isOwner, isLoading: isRoleLoading } = useOwner();
  const { data: latest } = useQuery(latestTrendRunQueryOptions());
  const { data: settings } = useQuery(trendSettingsQueryOptions());
  const { run: selected } = useSelectedTrendRun(selectedRunId);
  const { data: recent } = useQuery(recentSearchesQueryOptions());
  const request = useRequestTrendRun();
  const navigate = useNavigate();

  const [draft, setDraft] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Selecting a search puts its words back in the box. Keyed on the words
  // rather than the row, so a poll that refreshes the same run does not
  // overwrite what someone has started typing.
  const selectedPrompt = selected?.prompt ?? null;
  useEffect(() => {
    if (selectedPrompt) setDraft(selectedPrompt);
  }, [selectedPrompt]);

  const inFlight = isTrendRunInFlight(latest);
  const problem = promptError(draft);
  const disabled = !isOwner || inFlight || request.isPending || problem !== null;

  /** Put the cursor at the end of the box, optionally with new words in it. */
  function refine(prompt?: string) {
    if (prompt !== undefined) setDraft(prompt);
    const box = textareaRef.current;
    if (!box) return;
    box.focus();
    requestAnimationFrame(() => {
      const end = box.value.length;
      box.setSelectionRange(end, end);
    });
  }

  async function submit(prompt: string = draft) {
    try {
      const run = await request.mutateAsync({ prompt: prompt.trim(), budgetMinutes: null, hashtagsPerRun: null });
      // The estimate for a run of the default number of terms. An upper bound,
      // and worded as one, for the same reason the Generate button's is.
      const minutes = settings
        ? estimateSearchLength(settings, { hashtagsPerRun: SEARCH_TERMS_DEFAULT, budgetMinutes: null }).minutes
        : TREND_RUN_MINUTES;
      toast.success('Searching for ideas', {
        description: `This takes up to ${formatMinutes(minutes)}. The ideas appear here when it finishes, and you can leave this page.`,
      });
      await navigate({ to: '/queue', search: { search: run.id } });
    } catch (error) {
      // The words are only ever read here, never cleared: whatever refused the
      // search, the description is still in the box for the next attempt.
      if (isAlreadyRunningError(error)) {
        toast.info('A run is already going', {
          description: 'Only one can run at a time. Your description is still here for when it finishes.',
        });
        return;
      }
      toast.error('Could not start the search', { description: toError(error).message });
    }
  }

  // On the box itself, not on `document`: a page-wide shortcut would send a
  // search from the hashtag input further down the page.
  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (!disabled) void submit();
    }
  }

  // Said in text rather than in a tooltip, as the Generate button does: a
  // tooltip cannot be hovered on a phone. One reason at a time, the most
  // blocking first. An empty box is not nagged about.
  let hint: string | null = null;
  if (!isOwner && !isRoleLoading) {
    hint = 'Only an owner can start a search. You can still open any recent one.';
  } else if (inFlight) {
    hint =
      'A run is already going. Only one can run at a time — stop it from the notice on this page if you want this one instead.';
  } else if (problem && draft.trim()) {
    hint = problem;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Search by describing what you want to make</CardTitle>
        <CardDescription>
          Say what your business is and who it is for. The search works out what to look for, finds what is trending
          around it, and drafts ideas that connect the two. It is a full scout run, so it takes a while.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <form
          className="space-y-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!disabled) void submit();
          }}
        >
          <Label htmlFor="ai-search-prompt" className="sr-only">
            What are you working on?
          </Label>
          <Textarea
            id="ai-search-prompt"
            ref={textareaRef}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="I want to start a small home fitness brand for busy parents"
            maxLength={PROMPT_MAX_CHARS}
            disabled={!isOwner}
            aria-describedby="ai-search-hint"
            className="min-h-24 max-h-48 overflow-y-auto text-base"
          />
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
            <p id="ai-search-hint" className="text-xs text-muted-foreground">
              {hint ?? (
                <>
                  <Kbd>⌘↵</Kbd> or Ctrl+Enter to search. Nothing is spent until you press Search.
                </>
              )}
            </p>
            <Button type="submit" disabled={disabled} className="w-full sm:w-auto">
              {request.isPending ? <Spinner className="size-4" /> : <SparklesIcon className="size-4" />}
              {request.isPending ? 'Starting…' : 'Search'}
            </Button>
          </div>
        </form>

        {/* The selected search's state lives here, next to the box its
            advice is about. The page-wide banner skips this run so it is
            not said twice. */}
        {selected && <RunBanner run={selected} onRefine={() => refine()} onRetry={(prompt) => void submit(prompt)} />}

        {recent !== undefined &&
          (recent.length > 0 ? (
            <RecentSearches runs={recent} selectedRunId={selectedRunId} />
          ) : (
            <ExamplePrompts onPick={(prompt) => refine(prompt)} disabled={!isOwner} />
          ))}
      </CardContent>
    </Card>
  );
}
