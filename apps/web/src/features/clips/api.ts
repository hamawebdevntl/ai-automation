/**
 * Intelligent clipping: upload a recording, review the clips it proposes.
 *
 * The shape here is the script gate's, and for the same reasons. Every decision
 * is an owner-gated Postgres function rather than a table write, because there
 * is no server tier and a `security definer` function is the only thing that can
 * keep "record the decision" and "write the audit row" in one transaction — and
 * that refuses a viewer rather than trusting a disabled button.
 *
 * The upload is the one thing that is not an RPC. There is no server to proxy a
 * multi-gigabyte file through, so the bytes go straight to Storage under the
 * policy the footage lane added — owners writing under `sources/`, and nothing
 * else — and `create_clip_source` is the row change that makes the object
 * something the pipeline will pick up. Two steps, row written second: a row
 * pointing at an upload that failed is worse than an object no row points at.
 */

import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';
import { queueKeys } from '@/features/queue/api';
import {
  CLIP_WORKING_STATUSES,
  type ClipCandidateRow,
  type ClipSourceRow,
  DEFAULT_CANDIDATE_CAP,
  MAX_CANDIDATE_CAP,
  MIN_CANDIDATE_CAP,
} from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import { toError } from '@/lib/supabase-error';

export const clipKeys = {
  all: ['clips'] as const,
  sources: () => [...clipKeys.all, 'sources'] as const,
  source: (id: string) => [...clipKeys.sources(), id] as const,
  candidates: (sourceId: string) => [...clipKeys.all, 'candidates', sourceId] as const,
  preview: (key: string) => [...clipKeys.all, 'preview', key] as const,
};

/** The private bucket the pipeline already reads and writes. */
const RENDERS_BUCKET = 'renders';

/**
 * What the bucket's `allowed_mime_types` accepts.
 *
 * Mirrored here so a rejected file says why. Storage answers an unlisted type
 * with a 400 whose body is not something to show anyone, and QuickTime — what a
 * Mac screen recording and an iPhone both produce — is exactly the file most
 * likely to hit it.
 */
export const CLIP_SOURCE_TYPES = ['video/mp4', 'video/quicktime', 'video/webm'] as const;

/**
 * The bucket's own `file_size_limit`, mirrored for a message rather than a 413.
 *
 * Raised to 5 GiB by the clipping migration, from the 500 MB the footage lane
 * set: this feature's entire premise is a long recording, and an hour of 1080p
 * is routinely over a gigabyte. The failure that number would cause is the
 * worst-shaped one available — a 413 at the end of a long upload.
 */
export const CLIP_SOURCE_MAX_BYTES = 5 * 1024 * 1024 * 1024;

/**
 * A storage key for one recording: `sources/clips/<upload id>/<name>`.
 *
 * The id is generated here rather than being the source row's own, and it has to
 * be: the bytes are uploaded *before* `create_clip_source` runs, so the row does
 * not exist yet to be named. Uniqueness is what the key needs and
 * `crypto.randomUUID` gives it; `clip_sources.storage_key` is unique in Postgres
 * as the backstop.
 *
 * `clips/` separates these from the footage lane's `sources/<production id>/`
 * folders. A production id is a uuid and can never be the literal `clips`, so
 * the two namespaces cannot collide.
 */
export function clipSourceKey(fileName: string): string {
  const base = fileName.split(/[\\/]/).pop() ?? 'recording.mp4';
  const safe =
    base
      .replace(/[^A-Za-z0-9._-]+/g, '-')
      .replace(/^-+/, '')
      .slice(-80) || 'recording.mp4';
  return `sources/clips/${crypto.randomUUID()}/${safe}`;
}

/** Why this file cannot be uploaded, or null. Exported so the panel can say so before the click. */
export function clipSourceFileError(file: File): string | null {
  if (!(CLIP_SOURCE_TYPES as readonly string[]).includes(file.type)) {
    return `${file.type || 'That file'} is not a video this pipeline accepts. Upload an MP4, a QuickTime .mov or a WebM.`;
  }
  if (file.size > CLIP_SOURCE_MAX_BYTES) {
    return `That file is ${Math.round(file.size / 1024 / 1024 / 1024)} GB. The limit is ${CLIP_SOURCE_MAX_BYTES / 1024 / 1024 / 1024} GB.`;
  }
  if (file.size === 0) {
    return 'That file is empty.';
  }
  return null;
}

/** Whether the pipeline is still working on this recording and the owner just waits. */
export function isClipSourceWorking(source: ClipSourceRow): boolean {
  return (CLIP_WORKING_STATUSES as readonly string[]).includes(source.status);
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

/**
 * Every uploaded recording, newest first.
 *
 * Polled while anything is still working, because the three pre-gate phases are
 * driven by a worker thread rather than by this browser: an owner who has just
 * uploaded is watching for a status change nothing here will push. The interval
 * stops as soon as everything has settled, so an idle page makes no requests.
 */
export function clipSourcesQueryOptions() {
  return queryOptions({
    queryKey: clipKeys.sources(),
    queryFn: async (): Promise<ClipSourceRow[]> => {
      const { data, error } = await supabase.from('clip_sources').select('*').order('created_at', { ascending: false });
      if (error) throw toError(error);
      return data ?? [];
    },
    refetchInterval: (query) => ((query.state.data ?? []).some(isClipSourceWorking) ? 5_000 : false),
  });
}

/** The candidates for one recording, in the model's own ranking. */
export function clipCandidatesQueryOptions(sourceId: string) {
  return queryOptions({
    queryKey: clipKeys.candidates(sourceId),
    queryFn: async (): Promise<ClipCandidateRow[]> => {
      const { data, error } = await supabase
        .from('clip_candidates')
        .select('*')
        .eq('source_id', sourceId)
        .order('rank', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * A signed URL for scrubbing the recording at the gate.
 *
 * This is what makes the gate reviewable without rendering: the owner seeks to a
 * candidate's start point in the original file and sees the moment, rather than
 * reading two numbers and guessing. The bucket is private, so a signed URL is
 * the only way to hand it to a `<video>` element.
 *
 * An hour, and cached for slightly less so a long review session re-signs before
 * the link dies under it. `staleTime` below `expiresIn` is the whole trick.
 */
const PREVIEW_TTL_SECONDS = 3600;

export function clipPreviewUrlQueryOptions(storageKey: string) {
  return queryOptions({
    queryKey: clipKeys.preview(storageKey),
    staleTime: (PREVIEW_TTL_SECONDS - 300) * 1000,
    gcTime: PREVIEW_TTL_SECONDS * 1000,
    queryFn: async (): Promise<string> => {
      const { data, error } = await supabase.storage
        .from(RENDERS_BUCKET)
        .createSignedUrl(storageKey, PREVIEW_TTL_SECONDS);
      if (error) throw toError(error);
      if (!data?.signedUrl) throw new Error('Storage returned no URL for that recording.');
      return data.signedUrl;
    },
  });
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

/**
 * One place that invalidates what a clip decision changes.
 *
 * `queueKeys.all` as well as the clip keys, and that is not belt-and-braces:
 * accepting a candidate creates an idea *and* a production in the same
 * transaction, so the queue and the in-progress list are both immediately
 * wrong. Getting this wrong would show an owner four accepted clips and an empty
 * queue.
 */
function useClipMutation<TInput, TResult>(fn: (input: TInput) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: clipKeys.all });
      queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

export interface UploadClipSourceInput {
  file: File;
  styleId: string;
  candidateCap?: number;
}

/**
 * Put the recording in Storage, then register it.
 *
 * `upsert: false` so a key collision is an error rather than a silent
 * overwrite. With a uuid in the key it should never happen, and if it does the
 * right answer is to hear about it.
 */
export function useUploadClipSource() {
  return useClipMutation(async ({ file, styleId, candidateCap }: UploadClipSourceInput) => {
    const problem = clipSourceFileError(file);
    if (problem) throw new Error(problem);

    const cap = candidateCap ?? DEFAULT_CANDIDATE_CAP;
    if (cap < MIN_CANDIDATE_CAP || cap > MAX_CANDIDATE_CAP) {
      throw new Error(`Ask for between ${MIN_CANDIDATE_CAP} and ${MAX_CANDIDATE_CAP} clips, not ${cap}.`);
    }

    const key = clipSourceKey(file.name);
    const { error: uploadError } = await supabase.storage
      .from(RENDERS_BUCKET)
      .upload(key, file, { contentType: file.type, upsert: false });
    if (uploadError) throw toError(uploadError);

    const { data, error } = await supabase.rpc('create_clip_source', {
      p_key: key,
      p_style_id: styleId,
      p_filename: file.name,
      p_content_type: file.type,
      p_bytes: file.size,
      p_candidate_cap: cap,
    });
    if (error) throw toError(error);
    return data;
  });
}

export interface ClipDecisionInput {
  candidateId: string;
  note?: string | null;
}

/**
 * Accept a candidate: it becomes an idea, and the idea becomes a production.
 *
 * Both in one transaction, in SQL. That is deliberately not left to the
 * `start_approved_productions` sweep, which would also have worked: an owner who
 * accepts four candidates should see four productions when the page settles, not
 * up to five seconds later depending on which worker thread asks first.
 */
export function useAcceptClipCandidate() {
  return useClipMutation(async ({ candidateId, note }: ClipDecisionInput) => {
    const { data, error } = await supabase.rpc('accept_clip_candidate', {
      p_candidate_id: candidateId,
      p_note: note ?? null,
    });
    if (error) throw toError(error);
    return data;
  });
}

/** Discard a candidate. Nothing was rendered, so nothing is wasted. */
export function useDiscardClipCandidate() {
  return useClipMutation(async ({ candidateId, note }: ClipDecisionInput) => {
    const { data, error } = await supabase.rpc('discard_clip_candidate', {
      p_candidate_id: candidateId,
      p_note: note ?? null,
    });
    if (error) throw toError(error);
    return data;
  });
}

/** Another go at a recording that failed to transcribe or propose. */
export function useRetryClipSource() {
  return useClipMutation(async ({ sourceId }: { sourceId: string }) => {
    const { data, error } = await supabase.rpc('retry_clip_source', { p_source_id: sourceId });
    if (error) throw toError(error);
    return data;
  });
}
