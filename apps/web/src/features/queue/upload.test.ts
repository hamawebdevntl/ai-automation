import { describe, expect, it } from 'vitest';
import {
  formatBytes,
  isUploadReady,
  MAX_BRIEF_CHARS,
  MAX_TITLE_CHARS,
  MAX_UPLOAD_BYTES,
  renderKeyFor,
  UPLOAD_CONTENT_TYPE,
  type UploadDraft,
  validateUpload,
} from './upload';

/**
 * Every rule here is also enforced by the bucket or by
 * `create_upload_production`. These tests are about the copy of them that runs
 * before a 500 MB upload is spent — which is the only copy anyone experiences,
 * because the enforcing one only ever speaks after the file has gone.
 */

function file(overrides: { size?: number; type?: string; name?: string } = {}): File {
  const { size = 4 * 1024 * 1024, type = UPLOAD_CONTENT_TYPE, name = 'cut.mp4' } = overrides;
  const blob = new File([new Uint8Array(0)], name, { type });
  // jsdom builds `size` from the parts, and allocating 500 MB to test a limit
  // would be an odd way to spend a test run.
  Object.defineProperty(blob, 'size', { value: size });
  return blob;
}

function draft(overrides: Partial<UploadDraft> = {}): UploadDraft {
  return {
    file: file(),
    title: 'Three quoting mistakes',
    brief: 'A walk-through of the three things that lose the job after you quote.',
    isAigc: false,
    ...overrides,
  };
}

describe('validateUpload', () => {
  it('passes a complete draft', () => {
    expect(validateUpload(draft())).toEqual({});
    expect(isUploadReady(draft())).toBe(true);
  });

  it('asks for a file', () => {
    expect(validateUpload(draft({ file: null })).file).toBeDefined();
  });

  it('refuses anything but MP4, because the bucket does', () => {
    // `allowed_mime_types` rejects it server-side with a message about MIME
    // types. Saying so here saves the upload, not just the message.
    expect(validateUpload(draft({ file: file({ type: 'video/quicktime' }) })).file).toMatch(/MP4/);
  });

  it('accepts a file the browser could not type', () => {
    // Some browsers hand over an empty `type` for a file dragged in. The
    // bucket still checks; refusing here would block a valid upload on a
    // browser quirk.
    expect(validateUpload(draft({ file: file({ type: '' }) })).file).toBeUndefined();
  });

  it('refuses a file past the bucket limit, and says how far past', () => {
    const problem = validateUpload(draft({ file: file({ size: MAX_UPLOAD_BYTES + 1 }) })).file;
    expect(problem).toContain('500 MB');
  });

  it('refuses an empty file', () => {
    expect(validateUpload(draft({ file: file({ size: 0 }) })).file).toBeDefined();
  });

  it('asks for a title, and keeps it inside what YouTube will show', () => {
    expect(validateUpload(draft({ title: '   ' })).title).toBeDefined();
    expect(validateUpload(draft({ title: 'x'.repeat(MAX_TITLE_CHARS + 1) })).title).toBeDefined();
    expect(validateUpload(draft({ title: 'x'.repeat(MAX_TITLE_CHARS) })).title).toBeUndefined();
  });

  it('asks for a description, because nothing else can write the copy', () => {
    expect(validateUpload(draft({ brief: '' })).brief).toBeDefined();
    expect(validateUpload(draft({ brief: 'x'.repeat(MAX_BRIEF_CHARS + 1) })).brief).toBeDefined();
  });

  it('will not let the disclosure go unanswered', () => {
    // Not defaulted to either value. "No" would under-disclose a video that is
    // AI-generated, and "yes" would put a false label on one that is not; the
    // only honest default is no default.
    expect(validateUpload(draft({ isAigc: null })).isAigc).toBeDefined();
    expect(validateUpload(draft({ isAigc: true })).isAigc).toBeUndefined();
    expect(validateUpload(draft({ isAigc: false })).isAigc).toBeUndefined();
  });

  it('reports every problem at once rather than one at a time', () => {
    const problems = validateUpload({ file: null, title: '', brief: '', isAigc: null });
    expect(Object.keys(problems).sort()).toEqual(['brief', 'file', 'isAigc', 'title']);
  });
});

describe('renderKeyFor', () => {
  it('is the path the pipeline already writes and reads', () => {
    // `upload_render` writes `<id>/final.mp4`, `publish` falls back to it, and
    // `check_upload` defaults to it. Four places, one string.
    expect(renderKeyFor('abc-123')).toBe('abc-123/final.mp4');
  });
});

describe('formatBytes', () => {
  it.each([
    [512, '512 B'],
    [2048, '2 KB'],
    [4 * 1024 * 1024, '4.0 MB'],
    [MAX_UPLOAD_BYTES, '500 MB'],
    [2 * 1024 * 1024 * 1024, '2.0 GB'],
  ])('reads %i as %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });
});
