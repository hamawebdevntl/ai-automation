/**
 * Row factories for tests.
 *
 * `ProductionRow` was duplicated verbatim in two test files, so adding a column
 * broke both of them in a way that said nothing about either test. One factory
 * means a new column is one edit — and it is `ProductionRow` rather than a
 * loose object, so a column that is added to the type and forgotten here is a
 * compile error rather than an `undefined` that quietly reaches a component.
 */

import type { ProductionRow } from '@/lib/database.types';

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
