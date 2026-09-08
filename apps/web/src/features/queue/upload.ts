/**
 * What the browser checks before it spends a 500 MB upload.
 *
 * Every rule here is a copy of one Postgres or Supabase Storage already
 * enforces — the bucket's `allowed_mime_types` and `file_size_limit`, and
 * `create_upload_production`'s insistence on a title and a description. The
 * copy is for feedback only, exactly as `features/trends/controls.ts` and the
 * control predicates in `pipeline-steps.ts` are: the point is to say what is
 * wrong before the file goes over the wire, not to be the thing that holds.
 *
 * Pure functions, and in their own module, because the alternative is a
 * validity rule buried in a component that can only be exercised by rendering
 * it and typing.
 */

/** The bucket the pipeline already writes finished cuts to. */
export const RENDERS_BUCKET = 'renders';

/**
 * The only container the bucket accepts.
 *
 * `allowed_mime_types` on the bucket is `['video/mp4', 'image/jpeg']`, and
 * storage rejects anything else with a message about MIME types that nobody
 * should have to read after a five-minute upload.
 */
export const UPLOAD_CONTENT_TYPE = 'video/mp4';

/** `file_size_limit` on the renders bucket: 500 MB, in bytes. */
export const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;

/**
 * Long enough to be a title, short enough to be one.
 *
 * YouTube's own limit is 100 characters and `copy.py` trims to it, so a longer
 * title here would silently lose its tail at publish time.
 */
export const MAX_TITLE_CHARS = 100;

/**
 * The description the four captions are written from.
 *
 * Bounded because it is an LLM input, not a script: past a couple of
 * paragraphs it stops being a brief and starts being the thing the copy
 * paraphrases.
 */
export const MAX_BRIEF_CHARS = 1000;

export interface UploadDraft {
  file: File | null;
  title: string;
  brief: string;
  /** Null until the uploader answers. Not defaulted — see `upload-cut-panel`. */
  isAigc: boolean | null;
}

export type UploadField = 'file' | 'title' | 'brief' | 'isAigc';

/** What is wrong with the draft, by field. Empty means it can be sent. */
export type UploadProblems = Partial<Record<UploadField, string>>;

export function validateUpload(draft: UploadDraft): UploadProblems {
  const problems: UploadProblems = {};

  if (!draft.file) {
    problems.file = 'Choose the finished cut to upload.';
  } else if (draft.file.type && draft.file.type !== UPLOAD_CONTENT_TYPE) {
    problems.file = 'The renders bucket takes MP4 only. Export the cut as an MP4 and try again.';
  } else if (draft.file.size > MAX_UPLOAD_BYTES) {
    problems.file = `That file is ${formatBytes(draft.file.size)}. The limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`;
  } else if (draft.file.size === 0) {
    problems.file = 'That file is empty.';
  }

  const title = draft.title.trim();
  if (!title) {
    problems.title = 'A title is what the platform copy is written about.';
  } else if (title.length > MAX_TITLE_CHARS) {
    problems.title = `Keep it under ${MAX_TITLE_CHARS} characters — YouTube cuts titles there.`;
  }

  const brief = draft.brief.trim();
  if (!brief) {
    problems.brief = 'There is no script to write the captions from, so this is what they are written from instead.';
  } else if (brief.length > MAX_BRIEF_CHARS) {
    problems.brief = `Keep it under ${MAX_BRIEF_CHARS} characters. This is a brief, not the script.`;
  }

  if (draft.isAigc === null) {
    problems.isAigc = 'Say whether this video is AI-generated. The platforms require the disclosure.';
  }

  return problems;
}

export function isUploadReady(draft: UploadDraft): boolean {
  return Object.keys(validateUpload(draft)).length === 0;
}

/** Sizes as a person reads them, for the one message that quotes two of them. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${Math.round(bytes / 1024)} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

/**
 * Where the file goes.
 *
 * The same layout `upload_render` writes on the rendered path, so every step
 * downstream — the quality check, `publish`'s signed URL — finds it where it
 * already looks. The id is minted in the browser precisely so this path can be
 * known before the production row exists; `create_upload_production` then
 * refuses to open the row unless an object is actually sitting here.
 */
export function renderKeyFor(productionId: string): string {
  return `${productionId}/final.mp4`;
}
