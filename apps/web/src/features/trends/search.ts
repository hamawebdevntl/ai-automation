import type { TrendRunInterpretation, TrendRunRow } from '@/lib/database.types';

/**
 * Bounds and wording for searching by description.
 *
 * The same job `controls.ts` does for the scout's settings: numbers and
 * sentences that are easy to get wrong and hard to see from the page. Postgres
 * is what actually holds the bound on the prompt; this is the form's own
 * feedback.
 */

/** Mirrors the CHECK constraint on `trend_runs.prompt`. */
export const PROMPT_MAX_CHARS = 1000;

/**
 * How many terms a described run asks for when no length was chosen.
 *
 * Mirrors `AI_TERMS_DEFAULT` in `pipeline/trends/interpret.py`. Used only to
 * estimate how long the run will take before it starts.
 */
export const SEARCH_TERMS_DEFAULT = 6;

/** At or below this many ideas, a finished search gets advice rather than silence. */
export const FEW_RESULTS_MAX = 2;

export const RECENT_SEARCHES_LIMIT = 5;

/**
 * Two ways in for someone who has not tried this yet. They fill the box and
 * nothing else: spending a run is one explicit press of Search.
 */
export const EXAMPLE_PROMPTS: readonly string[] = [
  'I want to start a small home fitness brand for busy parents',
  'We do the bookkeeping for tradespeople who hate paperwork, and want more of them as clients',
];

export type SearchRun = TrendRunRow & { prompt: string };

/** A run that was started from a description rather than from the saved list. */
export function isSearchRun(run: TrendRunRow | null | undefined): run is SearchRun {
  return Boolean(run && run.prompt !== null && run.prompt.trim() !== '');
}

/**
 * Why this draft cannot be sent yet, or null.
 *
 * No minimum length, on purpose. A very short description is still a question,
 * and the worker answers it -- flagging it as vague and saying what would
 * sharpen it -- which is more use than a form refusing to send it.
 */
export function promptError(draft: string): string | null {
  const trimmed = draft.trim();
  if (!trimmed) return 'Describe what you are working on first.';
  if (trimmed.length > PROMPT_MAX_CHARS) {
    return `That is ${trimmed.length.toLocaleString()} characters; the limit is ${PROMPT_MAX_CHARS.toLocaleString()}.`;
  }
  return null;
}

export type SearchOutcome = 'none' | 'few' | 'plenty';

/** How a finished search went, or null while it is still going or did not finish. */
export function searchOutcome(run: Pick<TrendRunRow, 'status' | 'inserted'>): SearchOutcome | null {
  if (run.status !== 'succeeded') return null;
  const inserted = run.inserted ?? 0;
  if (inserted === 0) return 'none';
  if (inserted <= FEW_RESULTS_MAX) return 'few';
  return 'plenty';
}

/** The first `max` characters, cut on a word boundary, with an ellipsis. */
export function truncatePrompt(prompt: string, max = 90): string {
  const clean = prompt.trim().replace(/\s+/g, ' ');
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  const kept = lastSpace > max / 2 ? cut.slice(0, lastSpace) : cut;
  return `${kept.trimEnd()}…`;
}

/** A word or two on where a search got to, for the recent list. */
export function describeRunStatus(run: TrendRunRow): string {
  if (run.status === 'requested') return 'Queued';
  if (run.status === 'running') return 'Searching…';
  if (run.status === 'cancelled') return 'Stopped';
  if (run.status === 'failed') return 'Did not finish';
  const inserted = run.inserted ?? 0;
  if (inserted === 0) return 'Nothing relevant';
  return `${inserted} idea${inserted === 1 ? '' : 's'}`;
}

/** A term as its source reads it: a hashtag gets its sign back for display. */
export function formatTerm(term: string, vocabulary: TrendRunInterpretation['vocabulary']): string {
  return vocabulary === 'hashtags' ? `#${term}` : term;
}
