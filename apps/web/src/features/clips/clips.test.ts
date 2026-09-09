import { describe, expect, it } from 'vitest';
import { CLIP_SOURCE_MAX_BYTES, clipSourceFileError, clipSourceKey, isClipSourceWorking } from '@/features/clips/api';
import { clipLength, timecode } from '@/features/clips/components/clip-timecode';
import { makeClipSource } from '@/test/factories';

/**
 * The pure parts of the clipping feature.
 *
 * `clipSourceKey` is the one worth the most care: `create_clip_source` refuses
 * a key outside `sources/clips/`, and the Storage policy refuses a write
 * outside `sources/`. A key this function builds wrongly is a failure the owner
 * sees only after their gigabyte has finished uploading.
 */

describe('clipSourceKey', () => {
  it('puts the recording under the prefix the policy and the function both require', () => {
    expect(clipSourceKey('webinar.mp4')).toMatch(/^sources\/clips\/[0-9a-f-]{36}\/webinar\.mp4$/);
  });

  it('gives every upload its own folder, so a re-upload never overwrites one', () => {
    expect(clipSourceKey('a.mp4')).not.toBe(clipSourceKey('a.mp4'));
  });

  it('strips a path, so a browser that sends one cannot escape the prefix', () => {
    const key = clipSourceKey('../../etc/passwd.mp4');
    expect(key.startsWith('sources/clips/')).toBe(true);
    expect(key).not.toContain('..');
    expect(key.split('/')).toHaveLength(4);
  });

  it('replaces runs of characters a storage key should not carry', () => {
    // A run collapses to one dash, so `) #` becomes a single separator rather
    // than three.
    expect(clipSourceKey('My Talk (final) #2.mov')).toMatch(/\/My-Talk-final-2\.mov$/);
  });

  it('falls back to a name rather than producing a key ending in a slash', () => {
    expect(clipSourceKey('')).toMatch(/\/recording\.mp4$/);
    expect(clipSourceKey('!!!')).toMatch(/\/recording\.mp4$/);
  });
});

describe('clipSourceFileError', () => {
  const file = (type: string, size: number) => ({ type, size, name: 'x.mp4' }) as unknown as File;

  it('accepts the three containers the bucket allows', () => {
    for (const type of ['video/mp4', 'video/quicktime', 'video/webm']) {
      expect(clipSourceFileError(file(type, 1024))).toBeNull();
    }
  });

  it('refuses a type Storage would reject with an unreadable 400', () => {
    expect(clipSourceFileError(file('audio/mpeg', 1024))).toMatch(/not a video/i);
  });

  it('refuses a file over the bucket limit rather than letting it 413 at the end', () => {
    expect(clipSourceFileError(file('video/mp4', CLIP_SOURCE_MAX_BYTES + 1))).toMatch(/limit is/i);
  });

  it('refuses an empty file', () => {
    expect(clipSourceFileError(file('video/mp4', 0))).toMatch(/empty/i);
  });
});

describe('isClipSourceWorking', () => {
  it('is true only while the pipeline still has something to do', () => {
    expect(isClipSourceWorking(makeClipSource({ status: 'uploaded' }))).toBe(true);
    expect(isClipSourceWorking(makeClipSource({ status: 'transcribing' }))).toBe(true);
    expect(isClipSourceWorking(makeClipSource({ status: 'proposing' }))).toBe(true);
  });

  it('is false at the gate, so the page stops polling once a person is the blocker', () => {
    expect(isClipSourceWorking(makeClipSource({ status: 'awaiting_picks' }))).toBe(false);
    expect(isClipSourceWorking(makeClipSource({ status: 'resolved' }))).toBe(false);
    expect(isClipSourceWorking(makeClipSource({ status: 'failed' }))).toBe(false);
  });
});

describe('timecode', () => {
  it('reads as a position in a recording, not as a number of seconds', () => {
    expect(timecode(0)).toBe('0:00');
    expect(timecode(9)).toBe('0:09');
    expect(timecode(100)).toBe('1:40');
    expect(timecode(599)).toBe('9:59');
  });

  it('grows an hours field only when there is one', () => {
    expect(timecode(3600)).toBe('1:00:00');
    expect(timecode(3661)).toBe('1:01:01');
    expect(timecode(3599)).toBe('59:59');
  });

  it('does not render a negative position', () => {
    expect(timecode(-5)).toBe('0:00');
  });
});

describe('clipLength', () => {
  it('is the whole seconds a viewer would count', () => {
    expect(clipLength(100, 112)).toBe('12s');
    expect(clipLength(100, 112.4)).toBe('12s');
    expect(clipLength(100, 112.6)).toBe('13s');
  });
});
