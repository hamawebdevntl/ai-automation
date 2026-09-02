/**
 * Database types for the approval queue.
 *
 * Hand-written to match `supabase/migrations/0001_approval_queue.sql`. Once a
 * Supabase project exists these can be regenerated instead:
 *
 *   bunx supabase gen types typescript --project-id <ref> > src/lib/database.types.ts
 *
 * Keep the two in step — this file is the only thing standing between a typo in
 * a column name and a runtime error, since there is no server tier to catch it.
 *
 * The row shapes are `type` aliases rather than `interface` declarations on
 * purpose. postgrest-js constrains them to `Record<string, unknown>`, and only
 * type aliases of object literals get an implicit index signature — an
 * interface silently fails the constraint, which collapses every query result
 * to `never`.
 */

export type Json = string | number | boolean | null | { [key: string]: Json | undefined } | Json[];

export type ProfileRole = 'owner' | 'viewer';

export type IdeaStatus = 'pending' | 'approved' | 'rejected' | 'expired';

export type VelocityLabel = 'breakout' | 'rising' | 'steady' | 'declining';

export type StyleLane = 'stock' | 'generative' | 'presenter';

/**
 * Which backend renders a preset.
 *
 * Separate from `lane` on purpose: a lane is what the video looks like, which
 * is what the owner is choosing between at Gate 1, while this is which engine
 * produces that look. Two presets can share a lane and differ here.
 *
 *  - `mpt`         MoneyPrinterTurbo does everything.
 *  - `fal_visuals` fal generates the footage; MoneyPrinterTurbo voices,
 *                  captions and assembles it as usual.
 *  - `fal_full`    fal generates footage and narration; the pipeline assembles
 *                  and captions. A separate output path, so its results are
 *                  not directly comparable with the other two.
 */
export type RenderMode = 'mpt' | 'fal_visuals' | 'fal_full';

export const RENDER_MODE_LABELS: Record<RenderMode, string> = {
  mpt: 'Standard render',
  fal_visuals: 'fal footage, standard assembly',
  fal_full: 'fal end to end',
};

export type ProductionStatus =
  | 'queued'
  | 'running'
  | 'qc_failed'
  | 'awaiting_review'
  | 'approved'
  | 'rejected'
  | 'publishing'
  | 'published'
  | 'failed';

export type ApprovalDecision = 'approved' | 'rejected';

export type ProfileRow = {
  id: string;
  email: string;
  display_name: string | null;
  avatar_url: string | null;
  role: ProfileRole;
  created_at: string;
};

export type StylePresetRow = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  lane: StyleLane;
  video_source: string;
  render_mode: RenderMode;
  est_cost_min_usd: number;
  est_cost_max_usd: number;
  est_minutes: number;
  params: Json;
  is_active: boolean;
  sort_order: number;
  created_at: string;
};

export type IdeaRow = {
  id: string;
  title: string;
  hook: string | null;
  angle: string | null;
  rationale: string | null;
  source: string | null;
  source_url: string | null;
  trend_keyword: string | null;
  velocity_ratio: number | null;
  velocity_label: VelocityLabel | null;
  target_platforms: string[];
  status: IdeaStatus;
  approved_style_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
};

export type ProductionRow = {
  id: string;
  idea_id: string;
  style_preset_id: string;
  status: ProductionStatus;
  stage: string | null;
  task_id: string | null;
  /** The render_mode in force when this ran, kept even if the preset later changes. */
  render_backend: RenderMode | null;
  execution_arn: string | null;
  script: string | null;
  video_url: string | null;
  thumbnail_url: string | null;
  duration_seconds: number | null;
  qc: Json;
  platform_copy: Json;
  cost_estimate_usd: number | null;
  cost_actual_usd: number | null;
  error: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
  completed_at: string | null;
};

export type ApprovalRow = {
  id: string;
  gate: 1 | 2;
  subject_type: 'idea' | 'production';
  subject_id: string;
  decision: ApprovalDecision;
  note: string | null;
  style_preset_id: string | null;
  actor_id: string;
  created_at: string;
};

type Writable<T> = Partial<T>;

export interface Database {
  public: {
    Tables: {
      profiles: {
        Row: ProfileRow;
        Insert: Omit<ProfileRow, 'created_at' | 'role'> & { role?: ProfileRole; created_at?: string };
        Update: Writable<ProfileRow>;
        Relationships: [];
      };
      style_presets: {
        Row: StylePresetRow;
        Insert: Omit<StylePresetRow, 'id' | 'created_at'> & { id?: string; created_at?: string };
        Update: Writable<StylePresetRow>;
        Relationships: [];
      };
      ideas: {
        Row: IdeaRow;
        Insert: Omit<IdeaRow, 'id' | 'created_at' | 'target_platforms' | 'status'> & {
          id?: string;
          created_at?: string;
          target_platforms?: string[];
          status?: IdeaStatus;
        };
        Update: Writable<IdeaRow>;
        Relationships: [];
      };
      productions: {
        Row: ProductionRow;
        Insert: Omit<ProductionRow, 'id' | 'created_at' | 'status' | 'qc' | 'platform_copy'> & {
          id?: string;
          created_at?: string;
          status?: ProductionStatus;
          qc?: Json;
          platform_copy?: Json;
        };
        Update: Writable<ProductionRow>;
        Relationships: [];
      };
      approvals: {
        Row: ApprovalRow;
        Insert: Omit<ApprovalRow, 'id' | 'created_at'> & { id?: string; created_at?: string };
        Update: Writable<ApprovalRow>;
        Relationships: [];
      };
    };
    Views: Record<never, never>;
    Functions: {
      is_owner: {
        Args: Record<never, never>;
        Returns: boolean;
      };
      approve_idea: {
        Args: { p_idea_id: string; p_style_id: string; p_note?: string | null };
        Returns: IdeaRow;
      };
      reject_idea: {
        Args: { p_idea_id: string; p_note?: string | null };
        Returns: IdeaRow;
      };
      decide_production: {
        Args: { p_production_id: string; p_decision: ApprovalDecision; p_note?: string | null };
        Returns: ProductionRow;
      };
    };
    Enums: Record<never, never>;
    CompositeTypes: Record<never, never>;
  };
}

// ---------------------------------------------------------------------------
// Shapes stored as jsonb, narrowed for the UI
// ---------------------------------------------------------------------------

export type QcStatus = 'pass' | 'warn' | 'fail';

export interface QcCheck {
  key: string;
  label: string;
  status: QcStatus;
  detail?: string;
}

export interface QcReport {
  passed: boolean;
  slideshow_risk?: number;
  checks: QcCheck[];
}

export interface PlatformCopy {
  caption?: string;
  title?: string;
  description?: string;
}

export type PlatformCopyMap = Partial<Record<Platform, PlatformCopy>>;

export const PLATFORMS = ['instagram', 'tiktok', 'youtube', 'linkedin'] as const;

export type Platform = (typeof PLATFORMS)[number];
