import { describe, expect, it } from 'vitest';
import { codeOf, toError } from '@/lib/supabase-error';

/**
 * The case every one of these covers is the one the app was getting wrong:
 * supabase-js returns its failures as plain objects, so `instanceof Error` is
 * false for every real database refusal and the message is discarded.
 */
describe('toError', () => {
  it('keeps the message from a PostgREST error object', () => {
    const error = {
      code: '42501',
      message: 'Only an owner may start a trend run',
      details: null,
      hint: null,
    };

    expect(toError(error)).toBeInstanceOf(Error);
    expect(toError(error).message).toBe('Only an owner may start a trend run');
  });

  it('carries the code across, so a caller can still branch on it', () => {
    // 55006 is the concurrent run, which the UI says as information rather
    // than as a failure. It has to survive the wrapping to stay sayable.
    const wrapped = toError({ code: '55006', message: 'A trend run is already in progress' });

    expect(codeOf(wrapped)).toBe('55006');
  });

  it('says a missing function is an unapplied migration', () => {
    // The actual body PostgREST returned for the button, verbatim. On its own
    // it reads as a bug in the app; it is a database that has not been pushed.
    const message = toError({
      code: 'PGRST202',
      message: 'Could not find the function public.request_trend_run without parameters in the schema cache',
      details: 'Searched for the function public.request_trend_run without parameters',
      hint: 'Perhaps you meant to call the function public.reject_idea',
    }).message;

    expect(message).toMatch(/database change that has not been applied/i);
    // Still names the object: whoever can fix this needs to know which one.
    expect(message).toContain('request_trend_run');
  });

  it('treats a missing relation the same way', () => {
    expect(toError({ code: '42P01', message: 'relation "public.trend_runs" does not exist' }).message).toMatch(
      /has not been applied/i,
    );
  });

  it('returns a real Error untouched, stack and all', () => {
    const original = new TypeError('fetch failed');
    expect(toError(original)).toBe(original);
  });

  it('says something rather than "[object Object]" when there is nothing to say', () => {
    expect(toError({}).message).toBe('The request failed.');
    expect(toError(null).message).toBe('The request failed.');
    expect(toError({ code: '08006' }).message).toContain('08006');
  });
});

describe('codeOf', () => {
  it('reads a code off a plain object, an Error, and neither', () => {
    expect(codeOf({ code: '55006' })).toBe('55006');
    expect(codeOf(Object.assign(new Error('in use'), { code: '55006' }))).toBe('55006');
    expect(codeOf(new Error('in use'))).toBeUndefined();
    expect(codeOf(null)).toBeUndefined();
  });
});
