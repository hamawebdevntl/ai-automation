import { queryOptions, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';
import { queueKeys } from '@/features/queue/api';
import { BLOCKLIST_MAX_ENTRIES, blocklistEntryError, normaliseBlockedWord } from '@/features/trends/controls';
import { isTrendRunInFlight, type TrendRunRow, type TrendSettingsRow } from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import { codeOf, toError } from '@/lib/supabase-error';

export const trendKeys = {
  all: ['trends'] as const,
  settings: () => [...trendKeys.all, 'settings'] as const,
  latestRun: () => [...trendKeys.all, 'latest-run'] as const,
};

/**
 * Everything the trend task reads before scouting.
 *
 * A single row, guaranteed by a check constraint rather than by convention, so
 * this reads the first row without needing an id to ask for.
 */
export function trendSettingsQueryOptions() {
  return queryOptions({
    queryKey: trendKeys.settings(),
    queryFn: async (): Promise<TrendSettingsRow | null> => {
      const { data, error } = await supabase.from('trend_settings').select('*').limit(1).maybeSingle();
      if (error) throw toError(error);
      return data;
    },
  });
}

/**
 * A settings save.
 *
 * `Partial`, and that is the point: the settings are edited on three separate
 * cards -- the brief and hashtags, the schedule, the filters -- and each saves
 * only what it owns. A card sending the whole row would let a stale tab
 * silently revert a change made from another card, or from another session.
 *
 * `hashtag_cursor` is excluded by the table's `Update` type. The pipeline owns
 * it, and writing it from here would also mark the settings as edited by the
 * owner.
 */
export type SaveTrendSettingsInput = Partial<Omit<TrendSettingsRow, 'id' | 'updated_at' | 'hashtag_cursor'>>;

/**
 * Save any subset of the trend settings.
 *
 * Only an owner may write: the update policy calls `is_owner()`, so a viewer
 * who gets here anyway is refused by Postgres rather than by this file. The
 * `eq('id', true)` is the singleton row -- an update without a filter would be
 * refused by PostgREST, and there is no second row it could match.
 *
 * Nothing here validates a number. The CHECK constraints do, and a value they
 * refuse arrives back as a `23514` with the constraint's own name in it, which
 * `describeConstraintViolation` turns into a sentence. Duplicating the bounds
 * as a second gate would mean two places to change and one of them to forget;
 * `features/trends/controls.ts` mirrors them for the form's own feedback,
 * which is a different job from deciding what is allowed.
 */
export function useSaveTrendSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SaveTrendSettingsInput): Promise<TrendSettingsRow> => {
      const { data: auth } = await supabase.auth.getUser();
      const patch: SaveTrendSettingsInput = { ...input, updated_by: auth.user?.id ?? null };
      if (input.niche_brief !== undefined) patch.niche_brief = input.niche_brief.trim();
      if (input.hashtags !== undefined) patch.hashtags = normaliseHashtags(input.hashtags);
      if (input.caption_blocklist !== undefined) {
        patch.caption_blocklist = normaliseBlocklist(input.caption_blocklist);
      }

      const { data, error } = await supabase.from('trend_settings').update(patch).eq('id', true).select().single();
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (row) => {
      queryClient.setQueryData(trendKeys.settings(), row);
    },
  });
}

/**
 * Clean the caption blocklist for storage.
 *
 * Lowercased because the scout matches case-insensitively, and de-duplicated
 * so one word cannot be counted twice in a run's breakdown. Entries too short
 * to match safely are dropped here as well as being refused by the constraint:
 * a one-character word is a word in a great many captions, and the result is a
 * run that rejects everything.
 */
export function normaliseBlocklist(words: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of words) {
    const clean = normaliseBlockedWord(raw);
    if (!clean || seen.has(clean)) continue;
    if (blocklistEntryError(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out.slice(0, BLOCKLIST_MAX_ENTRIES);
}

/**
 * A CHECK constraint refusing a value, said as a sentence.
 *
 * The bounds live in Postgres because this app ships as a static bundle and a
 * guard that ships in JavaScript is a guard an old tab can skip. The cost of
 * that is a `23514` whose message names a constraint rather than a field, so
 * this is the translation -- without it the owner is shown
 * "new row violates check constraint trend_settings_pacing_min", which names
 * the right thing in the wrong language.
 */
export function describeConstraintViolation(error: unknown): string | null {
  if (codeOf(error) !== '23514') return null;
  const message = toError(error).message;
  const named = Object.entries(CONSTRAINT_SENTENCES).find(([constraint]) => message.includes(constraint));
  return named ? named[1] : 'One of these values is outside the range the pipeline will accept.';
}

const CONSTRAINT_SENTENCES: Record<string, string> = {
  trend_settings_pacing_order: 'The longest delay has to be at least the shortest one.',
  trend_settings_pacing_min: 'Delays below a second are what gets an account flagged, so they are not allowed.',
  trend_settings_pacing_max: 'That delay is longer than the pipeline allows.',
  trend_settings_schedule_days: 'Pick at least one day, or pause the schedule instead.',
  trend_settings_blocklist: 'A blocked word must be between 2 and 60 characters.',
  trend_settings_min_ratio: 'The outlier ratio has to be between 1× and 50×.',
  trend_settings_min_engagement: 'The engagement rate has to be between 0% and 50%.',
};

/**
 * Clean a hashtag for storage.
 *
 * The scout passes these straight to TikTok's hashtag feed, which wants the
 * bare word: no `#`, no spaces, no case. Doing it here rather than at read
 * time means what is stored is what was scouted, so a surprising queue can be
 * traced back to an exact tag.
 */
export function normaliseHashtag(raw: string): string {
  return raw.trim().replace(/^#+/, '').replace(/\s+/g, '').toLowerCase();
}

/** Normalise, drop blanks, and de-duplicate while keeping the given order. */
export function normaliseHashtags(raw: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const tag of raw) {
    const clean = normaliseHashtag(tag);
    if (clean && !seen.has(clean)) {
      seen.add(clean);
      out.push(clean);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------
// On-demand trend runs
// ---------------------------------------------------------------------------

/** Roughly how long a run takes, for setting expectations before one starts. */
export const TREND_RUN_MINUTES = 60;

/** How often to re-read a run that is still going. */
const IN_FLIGHT_POLL_MS = 15_000;

/**
 * When the dispatcher gives up on a request it never claimed.
 *
 * Mirrors `REQUEST_STALE_MINUTES` in `activities/reconcile.py`, which is the
 * side that acts on it. Duplicated rather than derived because there is no
 * server tier to ask, and it is quoted to the owner as a promise -- so a change
 * on that side that is not made here turns a true sentence into a false one.
 */
export const REQUEST_UNCLAIMED_WRITE_OFF_MINUTES = 10;

/**
 * How long a request may sit unclaimed before that is worth saying out loud.
 *
 * Comfortably more than the dispatcher's one-minute cadence -- two missed
 * sweeps is a hiccup, not news -- and comfortably less than the write-off
 * above, so the owner learns something is wrong while the row is still there
 * to explain it.
 */
const UNCLAIMED_AFTER_MS = 3 * 60_000;

/**
 * A request that has been sitting in the queue too long to still read as new.
 *
 * `requested` means "inserted, and waiting for the dispatcher". That is the one
 * in-flight state that can mean nothing is listening rather than something is
 * happening: an unapplied `terraform apply` leaves the sweeper unscheduled, and
 * the row then waits for something that does not exist.
 */
export function isUnclaimed(run: TrendRunRow, now: number = Date.now()): boolean {
  if (run.status !== 'requested') return false;
  return now - new Date(run.requested_at).getTime() > UNCLAIMED_AFTER_MS;
}

/**
 * The most recent trend run, whatever became of it.
 *
 * There is no server tier and no socket, so progress is a row that the trend
 * task writes into and this polls. Only while something is actually happening:
 * a finished run is a fact, and re-reading it every fifteen seconds for the
 * rest of the session would be pure noise against the database.
 */
export function latestTrendRunQueryOptions() {
  return queryOptions({
    queryKey: trendKeys.latestRun(),
    queryFn: async (): Promise<TrendRunRow | null> => {
      const { data, error } = await supabase
        .from('trend_runs')
        .select('*')
        .order('requested_at', { ascending: false })
        .limit(1)
        .maybeSingle();
      // The bundle and the database are deployed separately, so a build can
      // reach a database that has not had this migration applied yet. `42P01`
      // is the missing table, and the honest answer to "what was the last
      // run?" against a schema that cannot hold one is "there hasn't been" --
      // not an error banner over a queue that is otherwise working. The same
      // fallback the trend runner makes for `trend_settings`.
      if (error) {
        if (error.code === '42P01') return null;
        throw toError(error);
      }
      return data;
    },
    refetchInterval: (query) => (isTrendRunInFlight(query.state.data) ? IN_FLIGHT_POLL_MS : false),
    // A run started in another tab, or by someone else, should show up here.
    refetchOnWindowFocus: true,
  });
}

/**
 * Ask for a trend run, optionally saying how long it should take.
 *
 * The length rides on the row and applies to this run only -- the saved
 * settings, and therefore the scheduled run, are untouched. Sending nothing is
 * the same request this made before the length control existed.
 *
 * Inserting the row is the whole client-side story: `dispatch_trend_runs` picks
 * it up within the minute and starts the Fargate task. The function refuses a
 * second concurrent run with `55006`, which is a sentence for the owner rather
 * than a failure -- someone has already pressed it.
 */
export interface RequestTrendRunInput {
  /** Time ceiling for this run only. Null uses the saved setting. */
  budgetMinutes?: number | null;
  /** Hashtags to scout for this run only. Null uses the saved setting. */
  hashtagsPerRun?: number | null;
}

export function useRequestTrendRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: RequestTrendRunInput = {}): Promise<TrendRunRow> => {
      const { data, error } = await supabase.rpc('request_trend_run', {
        p_budget_minutes: input.budgetMinutes ?? null,
        p_hashtags_per_run: input.hashtagsPerRun ?? null,
      });
      // `toError` and not a bare `throw`: supabase-js hands back a plain object
      // here, and every display path in this app narrows on `instanceof Error`.
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (run) => {
      queryClient.setQueryData(trendKeys.latestRun(), run);
    },
    onError: () => {
      // Whatever refused it, the row this page is showing is now out of date:
      // the usual cause is a run someone else started a moment ago.
      void queryClient.invalidateQueries({ queryKey: trendKeys.latestRun() });
    },
  });
}

/**
 * Pull the new ideas in when the run that was making them stops.
 *
 * Without this the queue is the one thing the run does not update. The run row
 * is polled, so the spinner and the banner are right within fifteen seconds of
 * a run finishing -- and then the list underneath them still shows what it
 * showed an hour ago, because nothing invalidated it. The owner is told the
 * scouting succeeded while being shown no new ideas, which is precisely the
 * "did the button do anything?" question the banner exists to answer.
 *
 * A window-focus refetch covers the owner who went away and came back, which is
 * the expected way to spend an hour-long run. It does nothing for the one who
 * sat and watched, and that is the person owed the answer.
 *
 * The edge is the trigger, not the state: invalidating on every poll of a
 * finished run would re-fetch the queue every fifteen seconds forever, so this
 * fires once, on the transition out of flight.
 */
export function useRefreshQueueWhenRunEnds() {
  const queryClient = useQueryClient();
  const { data: run } = useQuery(latestTrendRunQueryOptions());
  const inFlight = isTrendRunInFlight(run);

  // Starts false on a fresh mount by design. Landing on the page after a run
  // has already finished is a mount, and a mount fetches the queue anyway.
  const wasInFlight = useRef(false);

  useEffect(() => {
    if (wasInFlight.current && !inFlight) {
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    }
    wasInFlight.current = inFlight;
  }, [inFlight, queryClient]);
}

/** `55006` is object_in_use: a run is already going. Anything else is real. */
export function isAlreadyRunningError(error: unknown): boolean {
  return codeOf(error) === '55006';
}

/**
 * Stop the run that is going.
 *
 * The whole value of this is that it is a single database call and nothing
 * else. At most one run may be in flight, so a run nothing will ever finish
 * does not hold up one run -- it holds up every future run, and the button
 * stays shut for as long as the row sits there. Until now the only thing that
 * could clear such a row was the dispatcher, which is useless in the case that
 * produces stuck rows most often: the dispatcher not running.
 *
 * `cancel_trend_run` moves the row out of the in-flight set, and the partial
 * unique index frees itself. No Lambda, no sweep, no AWS call is involved in
 * that taking effect. Stopping the Fargate task, where one exists, is a
 * separate and best-effort concern handled by the pipeline.
 */
export function useCancelTrendRun() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (runId: string): Promise<TrendRunRow> => {
      const { data, error } = await supabase.rpc('cancel_trend_run', { p_run_id: runId });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (run) => {
      queryClient.setQueryData(trendKeys.latestRun(), run);
    },
    onError: () => {
      // Whatever refused it, the row on screen is out of date -- most often
      // because the run finished on its own a moment ago.
      void queryClient.invalidateQueries({ queryKey: trendKeys.latestRun() });
    },
  });
}

/**
 * `P0002` is no_data_found: there was no in-flight run to stop.
 *
 * Not a failure. The page was showing a run that has since ended, which is the
 * outcome the owner wanted described badly if it arrives as an error.
 */
export function isNothingToStopError(error: unknown): boolean {
  return codeOf(error) === 'P0002';
}
