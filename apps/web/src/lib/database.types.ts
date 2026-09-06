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
 *  - `heygen`      HeyGen returns a finished presenter reel -- voiced,
 *                  captioned and already 9:16. MoneyPrinterTurbo never touches
 *                  the output; only the script generator is shared.
 */
export type RenderMode = 'mpt' | 'fal_visuals' | 'fal_full' | 'heygen';

export const RENDER_MODE_LABELS: Record<RenderMode, string> = {
  mpt: 'Standard render',
  fal_visuals: 'fal footage, standard assembly',
  fal_full: 'fal end to end',
  heygen: 'HeyGen presenter',
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

/**
 * Everything the trend scout is told to do.
 *
 * The brief and the hashtags were the first two out of the environment; the
 * rest arrived in 20260906150000, having previously been constants in the
 * pipeline or a cron expression in Terraform. Every default is the value the
 * code used before it was tunable, with one exception -- `max_video_age_days`,
 * which had no previous value at all because there was no age filter.
 *
 * Every numeric field is bounded by a CHECK constraint in Postgres. The bounds
 * are mirrored in `features/trends/controls.ts` for the form's own feedback,
 * but this app ships as a static bundle: the constraint is what actually holds.
 */
export type TrendSettingsRow = {
  /** Always `true`. The table is a singleton by constraint, not by convention. */
  id: boolean;
  niche_brief: string;
  /** Without the leading `#`. */
  hashtags: string[];
  updated_at: string;
  updated_by: string | null;

  // --- Timing. UTC throughout; there is deliberately no timezone setting. ---
  /** Whether the daily run happens on its own. Pausing does not touch the button. */
  schedule_enabled: boolean;
  schedule_hour_utc: number;
  schedule_minute_utc: number;
  /** 0 = Sunday .. 6 = Saturday, as Postgres `extract(dow)` counts. */
  schedule_days: number[];

  // --- Filters, in the order the scout applies them. ---
  /** Reject a video older than this before it is scored. */
  max_video_age_days: number;
  min_plays: number;
  /** Interactions per view, as a fraction: 0.04 is 4%. */
  min_engagement_rate: number;
  /** How far above its own author's median a video must perform. */
  min_outlier_ratio: number;
  /** Matched as whole words, case-insensitively. Stored lowercase. */
  caption_blocklist: string[];
  videos_per_hashtag: number;
  ideas_per_run: number;

  // --- Run cost. ---
  /** Tags to scout per run, rotating through the list. Null scouts all of them. */
  hashtags_per_run: number | null;
  /** Rotation position. Written by the pipeline, never by this app. */
  hashtag_cursor: number;
  /** Stop scouting after this long and draft from what was found. Null = no ceiling. */
  run_budget_minutes: number | null;
  baseline_sample_size: number;
  pacing_min_seconds: number;
  pacing_max_seconds: number;
  dedup_window_days: number;
  idea_expiry_days: number;
  idea_provider: IdeaProvider;
};

/** Which model drafts the queue. Both are wired; the key must be in the bundle. */
export type IdeaProvider = 'claude' | 'gemini';

/**
 * One stage of a run's rejection breakdown.
 *
 * `level` is what stops the app adding these together: video-level stages
 * count videos and sum to `seen`, while `duplicate` counts drafted ideas and
 * cannot be added to them.
 */
export type TrendRejectionStage = {
  key: string;
  /** Travels with the payload so an older build can still render a new stage. */
  label: string;
  level: 'video' | 'idea';
  dropped: number;
  /** The setting responsible, or null where no setting could change it. */
  setting: string | null;
  /** Its value at the time of the run, not now. */
  value: number | string[] | null;
};

/**
 * Why a run produced what it produced.
 *
 * The point of the whole document is `seen`: filters can only ever reduce it,
 * so `seen === 0` is never a filter and always the scrape. That is the line
 * between an empty queue and a broken scraper.
 */
export type TrendRejections = {
  seen: number;
  stages: TrendRejectionStage[];
  surfaced: number;
  drafted: number;
  inserted: number;
  failed_hashtags: Array<{ hashtag: string; error: string }>;
  budget_exhausted: boolean;
  /** The scout noticed it had been stopped and gave up the remaining hashtags. */
  cancelled: boolean;
  hashtags_skipped: number;
  hashtags_scouted: string[];
  hashtags_configured: number | null;
};

/**
 * A trend run is in flight while it is one of these. At most one row can be.
 *
 * `cancelled` is its own status rather than a flavour of `failed` because the
 * two need different banners: a run you stopped on purpose showing as a red
 * failure teaches you to ignore the alarm that reports a real breakage.
 */
export type TrendRunStatus = 'requested' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export const TREND_RUN_IN_FLIGHT = ['requested', 'running'] as const satisfies readonly TrendRunStatus[];

export function isTrendRunInFlight(run: TrendRunRow | null | undefined): boolean {
  return run !== null && run !== undefined && (TREND_RUN_IN_FLIGHT as readonly string[]).includes(run.status);
}

export type TrendRunRow = {
  id: string;
  status: TrendRunStatus;
  requested_by: string | null;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  /** The ECS task, for finding a misbehaving run in the console. */
  task_arn: string | null;
  /** Null until the run ends. Mirrors what `runner.run` returns. */
  signals: number | null;
  drafted: number | null;
  inserted: number | null;
  suppressed: number | null;
  error: string | null;

  /** Videos the feeds returned before any filter. Zero means the scrape found nothing. */
  scouted: number | null;
  /** Which tags this run looked at — a subset of the list when rotation is on. */
  hashtags_scouted: string[] | null;
  /** The per-stage breakdown. Null on a run that predates it, or one that failed early. */
  rejections: TrendRejections | null;
  /** How the run came to exist. */
  trigger: TrendRunTrigger;
  /** The schedule slot this run is for. Null on a manual run. */
  scheduled_for: string | null;

  /** When an owner stopped this run. The lock is freed at the same instant. */
  cancelled_at: string | null;
  cancelled_by: string | null;
  /** When the dispatcher last tried `ecs:StopTask`. An attempt, not a confirmation. */
  task_stopped_at: string | null;

  /**
   * The length asked for when this run was started, for this run only.
   *
   * Null uses the saved setting, which is what every scheduled run inserts.
   * Only the two costs are overridable — neither changes what qualifies as a
   * signal, so runs of different lengths stay comparable.
   */
  override_run_budget_minutes: number | null;
  override_hashtags_per_run: number | null;
};

/** The button, or the dispatcher acting on the owner's schedule. */
export type TrendRunTrigger = 'manual' | 'schedule';

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
      trend_settings: {
        Row: TrendSettingsRow;
        Insert: never;
        // `hashtag_cursor` is deliberately absent: the pipeline advances it,
        // and an update from here would also be an update the touch trigger
        // counts as the owner having edited their settings.
        Update: Partial<Omit<TrendSettingsRow, 'id' | 'updated_at' | 'hashtag_cursor'>>;
        Relationships: [];
      };
      trend_runs: {
        Row: TrendRunRow;
        // Requested through `request_trend_run`, written by the pipeline with
        // the service role. Neither path goes through this client.
        Insert: never;
        Update: never;
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
      request_trend_run: {
        // Both optional. Null, or omitted, uses the saved settings.
        Args: { p_budget_minutes?: number | null; p_hashtags_per_run?: number | null };
        Returns: TrendRunRow;
      };
      cancel_trend_run: {
        Args: { p_run_id: string };
        Returns: TrendRunRow;
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
