/**
 * Row factories for tests.
 *
 * `ProductionRow` was duplicated verbatim in two test files, so adding a column
 * broke both of them in a way that said nothing about either test. One factory
 * means a new column is one edit — and it is `ProductionRow` rather than a
 * loose object, so a column that is added to the type and forgotten here is a
 * compile error rather than an `undefined` that quietly reaches a component.
 */

import type { ClipCandidateRow, ClipSourceRow, IdeaRow, ProductionRow } from '@/lib/database.types';

export function makeProduction(overrides: Partial<ProductionRow> = {}): ProductionRow {
  return {
    id: 'p1',
    idea_id: 'i1',
    style_preset_id: 's1',
    status: 'running',
    stage: null,
    task_id: null,
    run_state: {},
    leased_by: null,
    lease_expires_at: null,
    render_backend: null,
    script: null,
    script_approved_at: null,
    script_approved_by: null,
    script_updated_at: null,
    script_updated_by: null,
    source_video_key: null,
    source_video_name: null,
    source_video_bytes: null,
    source_video_uploaded_at: null,
    source_video_uploaded_by: null,
    render_instruction: null,
    render_instruction_updated_at: null,
    render_instruction_updated_by: null,
    source_consent_at: null,
    source_consent_by: null,
    source_consent_note: null,
    video_url: null,
    thumbnail_url: null,
    duration_seconds: null,
    qc: {},
    platform_copy: {},
    cost_estimate_usd: 0.4,
    cost_actual_usd: null,
    error: null,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    created_at: '2026-09-07T10:00:00Z',
    completed_at: null,
    updated_at: '2026-09-07T10:00:00Z',
    postiz_media_id: null,
    postiz_media_path: null,
    paused_at: null,
    superseded_by: null,
    ...overrides,
  };
}

/** A pending idea from an ordinary run. The described-search columns are null unless asked for. */
export function makeIdea(overrides: Partial<IdeaRow> = {}): IdeaRow {
  return {
    id: 'idea-1',
    title: 'Why your quotes lose the job',
    hook: 'You sent the quote. They went quiet.',
    angle: 'Follow-up timing is the whole game.',
    rationale: 'quoting software is rising 3.4x against its own recent history.',
    source: 'google_trends',
    source_url: 'https://trends.google.com/trends/explore?q=quoting%20software',
    trend_keyword: 'quoting software',
    velocity_ratio: 3.4,
    velocity_label: 'rising',
    target_platforms: ['instagram', 'tiktok'],
    status: 'pending',
    approved_style_id: null,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    created_at: '2026-09-07T10:00:00Z',
    trend_run_id: null,
    relevance: null,
    connection: null,
    // Null on every idea that did not come from a clip. `makeClipIdea` below is
    // the one that sets them, all four together -- the database refuses any
    // other combination.
    clip_source_id: null,
    clip_candidate_id: null,
    clip_start_seconds: null,
    clip_end_seconds: null,
    ...overrides,
  };
}

/** An idea created by accepting a clip candidate: already approved, with a range. */
export function makeClipIdea(overrides: Partial<IdeaRow> = {}): IdeaRow {
  return makeIdea({
    id: 'idea-clip-1',
    title: 'The quote that went quiet',
    source: 'clip',
    source_url: null,
    trend_keyword: null,
    velocity_ratio: null,
    velocity_label: null,
    status: 'approved',
    approved_style_id: 'style-clip',
    decided_by: 'owner-1',
    decided_at: '2026-09-08T12:00:00Z',
    clip_source_id: 'src-1',
    clip_candidate_id: 'cand-1',
    clip_start_seconds: 100,
    clip_end_seconds: 112,
    ...overrides,
  });
}

/** An uploaded recording sitting at the clip gate. */
export function makeClipSource(overrides: Partial<ClipSourceRow> = {}): ClipSourceRow {
  return {
    id: 'src-1',
    storage_key: 'sources/clips/9f2b/quoting-webinar.mp4',
    filename: 'quoting-webinar.mp4',
    content_type: 'video/mp4',
    size_bytes: 1_200_000_000,
    duration_seconds: 2400,
    style_preset_id: 'style-clip',
    status: 'awaiting_picks',
    transcript: {
      text: 'Your quote went out on Friday.',
      language: 'en',
      model: 'fal-ai/whisper',
      segments: [{ start: 100, end: 104, text: 'Your quote went out on Friday.' }],
    },
    candidate_cap: 6,
    error: null,
    leased_by: null,
    lease_expires_at: null,
    uploaded_by: 'owner-1',
    created_at: '2026-09-08T11:00:00Z',
    updated_at: '2026-09-08T11:30:00Z',
    ...overrides,
  };
}

/** One proposed clip, still undecided. */
export function makeClipCandidate(overrides: Partial<ClipCandidateRow> = {}): ClipCandidateRow {
  return {
    id: 'cand-1',
    source_id: 'src-1',
    rank: 1,
    start_seconds: 100,
    end_seconds: 112,
    title: 'The quote that went quiet',
    hook: 'You sent the quote. They went silent.',
    reason: 'It states the problem and the fix without needing the rest of the talk.',
    transcript_excerpt: 'Your quote went out on Friday. By Monday they had stopped replying.',
    decision: 'pending',
    decided_by: null,
    decided_at: null,
    decision_note: null,
    idea_id: null,
    created_at: '2026-09-08T11:30:00Z',
    ...overrides,
  };
}
