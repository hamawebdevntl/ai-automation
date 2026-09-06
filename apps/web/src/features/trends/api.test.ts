import { describe, expect, it } from 'vitest';
import { normaliseHashtag, normaliseHashtags } from '@/features/trends/api';

/**
 * These rules exist because the stored value is handed straight to TikTok's
 * hashtag feed, which wants a bare word. Normalising on save rather than on
 * read means what is stored is exactly what gets scouted, so a surprising
 * queue can be traced back to a tag someone can see.
 */
describe('normaliseHashtag', () => {
  it('strips a leading hash, however many were typed', () => {
    expect(normaliseHashtag('#aiautomation')).toBe('aiautomation');
    expect(normaliseHashtag('##aiautomation')).toBe('aiautomation');
  });

  it('lowercases, because the feed is case-insensitive but the queue is not', () => {
    expect(normaliseHashtag('AIAutomation')).toBe('aiautomation');
  });

  it('removes inner whitespace rather than accepting a tag that cannot exist', () => {
    expect(normaliseHashtag('  ai automation ')).toBe('aiautomation');
  });

  it('reduces a tag with no content to empty, so it can be rejected', () => {
    expect(normaliseHashtag('   ')).toBe('');
    expect(normaliseHashtag('#')).toBe('');
  });
});

describe('normaliseHashtags', () => {
  it('drops blanks instead of storing them', () => {
    expect(normaliseHashtags(['saas', '', '   ', '#'])).toEqual(['saas']);
  });

  it('de-duplicates after normalising, not before', () => {
    // '#SaaS' and 'saas' are the same feed; storing both would scout it twice
    // and double that tag's share of the run.
    expect(normaliseHashtags(['#SaaS', 'saas', 'nocode'])).toEqual(['saas', 'nocode']);
  });

  it('keeps the order the owner arranged them in', () => {
    expect(normaliseHashtags(['devops', 'aiagents', 'webdesign'])).toEqual(['devops', 'aiagents', 'webdesign']);
  });

  it('returns an empty list unchanged, which the runner treats as a hard stop', () => {
    expect(normaliseHashtags([])).toEqual([]);
  });
});
