import { describe, expect, it } from 'vitest';
import {
  describeRunStatus,
  FEW_RESULTS_MAX,
  formatTerm,
  isSearchRun,
  PROMPT_MAX_CHARS,
  promptError,
  searchOutcome,
  truncatePrompt,
} from '@/features/trends/search';
import { trendRun, trendSearchRun } from '@/features/trends/test-fixtures';

describe('what counts as a described search', () => {
  it('is a run with words on it', () => {
    expect(isSearchRun(trendSearchRun())).toBe(true);
    expect(isSearchRun(trendRun())).toBe(false);
    expect(isSearchRun(trendRun({ prompt: '   ' }))).toBe(false);
    expect(isSearchRun(null)).toBe(false);
  });
});

describe('whether a draft can be sent', () => {
  it('refuses an empty box', () => {
    expect(promptError('')).toMatch(/describe/i);
    expect(promptError('   \n ')).toMatch(/describe/i);
  });

  it('has no minimum: a short description is still a question', () => {
    // The worker flags a thin one as vague and says what would help, which is
    // more use than a form refusing to send it.
    expect(promptError('fitness')).toBeNull();
  });

  it('mirrors the database bound', () => {
    expect(promptError('x'.repeat(PROMPT_MAX_CHARS))).toBeNull();
    expect(promptError('x'.repeat(PROMPT_MAX_CHARS + 1))).toMatch(/limit is 1,000/);
  });
});

describe('how a search went', () => {
  it('is unknown until it has succeeded', () => {
    expect(searchOutcome({ status: 'running', inserted: null })).toBeNull();
    expect(searchOutcome({ status: 'failed', inserted: 0 })).toBeNull();
    expect(searchOutcome({ status: 'cancelled', inserted: 0 })).toBeNull();
  });

  it.each([
    [0, 'none'],
    [1, 'few'],
    [FEW_RESULTS_MAX, 'few'],
    [FEW_RESULTS_MAX + 1, 'plenty'],
  ])('%i inserted is %s', (inserted, outcome) => {
    expect(searchOutcome({ status: 'succeeded', inserted })).toBe(outcome);
  });

  it('reads a missing count as nothing', () => {
    expect(searchOutcome({ status: 'succeeded', inserted: null })).toBe('none');
  });
});

describe('truncating a prompt', () => {
  it('leaves a short one alone, tidied', () => {
    expect(truncatePrompt('  a  short   one ')).toBe('a short one');
  });

  it('cuts on a word boundary with an ellipsis', () => {
    const long = 'I want to start a small home fitness brand for busy parents who never have an hour';
    expect(truncatePrompt(long, 40)).toBe('I want to start a small home fitness…');
  });
});

describe('describing where a search got to', () => {
  it.each([
    ['requested', 'Queued'],
    ['running', 'Searching…'],
    ['cancelled', 'Stopped'],
    ['failed', 'Did not finish'],
  ] as const)('%s reads as %s', (status, words) => {
    expect(describeRunStatus(trendSearchRun({ status, inserted: null }))).toBe(words);
  });

  it('counts what a finished one found, singular included', () => {
    expect(describeRunStatus(trendSearchRun({ status: 'succeeded', inserted: 8 }))).toBe('8 ideas');
    expect(describeRunStatus(trendSearchRun({ status: 'succeeded', inserted: 1 }))).toBe('1 idea');
    expect(describeRunStatus(trendSearchRun({ status: 'succeeded', inserted: 0 }))).toBe('Nothing relevant');
  });
});

describe('showing a term', () => {
  it('gives a hashtag its sign back and leaves a search term alone', () => {
    expect(formatTerm('busyparents', 'hashtags')).toBe('#busyparents');
    expect(formatTerm('home workout', 'keywords')).toBe('home workout');
  });
});
