/**
 * Row factories for tests.
 *
 * `ProductionRow` was duplicated verbatim in two test files, so adding a column
 * broke both of them in a way that said nothing about either test. One factory
 * means a new column is one edit — and it is `ProductionRow` rather than a
 * loose object, so a column that is added to the type and forgotten here is a
 * compile error rather than an `undefined` that quietly reaches a component.
 */

import type { IdeaRow, ProductionRow } from '@/lib/database.types';

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
    ...overrides,
  };
}
