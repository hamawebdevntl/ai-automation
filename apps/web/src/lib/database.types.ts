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
  /** Stopped at the script gate: the narration is drafted and waiting for an
   *  owner to read it. Nothing is rendered from this state, which is the point
   *  of it — the spend happens after the words are approved, not before. */
  | 'awaiting_script'
  | 'qc_failed'
  | 'awaiting_review'
  | 'approved'
  | 'rejected'
  | 'publishing'
  | 'published'
  | 'failed'
  /** The pipeline stopped deliberately and wants a person. The DB has allowed
   *  this since the pipeline-integration migration; this file had drifted. */
  | 'parked'
  /** A person stopped it before it finished — `cancel_production`. */
  | 'cancelled';

/** Statuses that mean the pipeline is still working on this production. */
export const LIVE_PRODUCTION_STATUSES = [
  'queued',
  'running',
  'publishing',
] as const satisfies readonly ProductionStatus[];

/** Statuses that mean nothing more will happen without a person. */
export const STOPPED_PRODUCTION_STATUSES = ['parked', 'failed'] as const satisfies readonly ProductionStatus[];

/** The longest script any lane accepts. HeyGen's `POST /v3/videos` rejects more
 *  outright rather than truncating, and `productions_script_length` in Postgres
 *  enforces the same number. Mirrored here for the editor's own counter; the
 *  constraint is what actually holds. */
export const MAX_SCRIPT_CHARS = 5000;

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
  /** Video sources only. Absolute view count — unbounded, not comparable to min_interest. */
  min_plays: number;
  /** Google Trends only. Interest as a percentage of the term's own 3-month peak, 0-100. */
  min_interest: number;
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

  /** Where signals come from. */
  trend_source: TrendSource;
  /** Search terms in buyer language, for Google Trends and YouTube. Not hashtags. */
  trend_keywords: string[];
  /** ISO-3166 region for Google Trends, or empty for worldwide. */
  trend_geo: string;
  /** Which platforms the Apify source scrapes. At least one. Ignored by the others. */
  apify_platforms: ApifyPlatform[];
};

/**
 * Where the scout looks.
 *
 * `google_trends` measures search demand — what people type when a manual
 * process has finally cost them an afternoon. It says a subject is live and
 * nothing about how to open a video about it.
 *
 * `apify` and `youtube` measure video formats, with engagement as proof, which
 * is the better half for a hook. They differ in what they cost: Apify rents
 * hosted scrapers and bills per result, YouTube is the official API and spends
 * a daily quota instead.
 *
 * `tiktok` was here until TikTok-Api began refusing every feed. Apify is how
 * TikTok is read now.
 */
export type TrendSource = 'apify' | 'google_trends' | 'youtube';

/** The platforms the Apify source can be pointed at. */
export type ApifyPlatform = 'tiktok' | 'instagram';

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
  /**
   * The source ran out of allowance rather than time.
   *
   * Separate from `budget_exhausted` because the remedy is the opposite:
   * YouTube's search quota does not reset until midnight US/Pacific, so a
   * longer budget changes nothing and a shorter term list is the fix. Absent
   * on runs recorded before this existed.
   */
  quota_exhausted?: boolean;
  /** The scout noticed it had been stopped and gave up the remaining hashtags. */
  cancelled: boolean;
  hashtags_skipped: number;
  hashtags_scouted: string[];
  hashtags_configured: number | null;
  /**
   * Which source produced this run.
   *
   * Written by the pipeline and needed for reading the rest: the stage labels
   * are already relabelled per source, but `hashtags_scouted` holds hashtags
   * on one source and search terms on the others, and only this says which.
   * Absent on runs recorded before it was stored.
   */
  source?: string;
};

/**
 * How the worker read a described search.
 *
 * Written shortly after the run is claimed, so it can arrive up to a minute
 * after the row does. While the run is in flight, null means "not read yet";
 * once it has finished, null on a run that has a prompt means the worker that
 * ran it predates described searches.
 */
export type TrendRunInterpretation = {
  /** The request in the app's own words, so a misreading is visible at once. */
  restatement: string;
  /** What the scout was told to look for. Hashtags or search terms, by `vocabulary`. */
  terms: string[];
  vocabulary: 'keywords' | 'hashtags';
  source: string;
  /** Too thin to derive good terms from. `nudge` says what would help. */
  vague: boolean;
  nudge: string | null;
  /** Rephrasings worth trying when this one comes back thin. Empty, never null. */
  suggestions: string[];
  provider: string;
  model: string | null;
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

  /** What the owner said they were working on, verbatim. Null on a manual or scheduled run. */
  prompt: string | null;
  /** Null until the worker has read the prompt. Always null on a run without one. */
  interpretation: TrendRunInterpretation | null;
  interpreted_at: string | null;

  /** When an owner removed this run from the app's lists. The row stays; the app stops showing it. */
  dismissed_at: string | null;
  dismissed_by: string | null;
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
  /** The run that drafted this idea. Null on rows from before it was recorded. */
  trend_run_id: string | null;
  /** How well it fits the description its run was given, 0–100. Null unless the run had a prompt. */
  relevance: number | null;
  /** One sentence on how the idea connects to that description. Null unless the run had a prompt. */
  connection: string | null;
  /** The avatar and voice chosen for this one production at Gate 1, overriding
   *  the preset. Null uses the preset's pair.
   *
   *  It rides on the idea rather than on the production because at the moment
   *  the owner picks, the production does not exist — `start_approved_productions`
   *  opens it seconds later. Where it is *recorded* is `productions.presenter`. */
  presenter_override: PresenterChoice | null;
};

export type ProductionRow = {
  id: string;
  idea_id: string;
  style_preset_id: string;
  status: ProductionStatus;
  stage: string | null;
  task_id: string | null;
  /** Where the driver has got to with this production, plus the payload its
   *  steps pass between each other. Replaced the Step Functions execution. */
  run_state: Record<string, unknown> | null;
  /** Which worker currently holds this row. Advisory; `lease_expires_at` is
   *  what actually excludes. */
  leased_by: string | null;
  lease_expires_at: string | null;
  /** The render_mode in force when this ran, kept even if the preset later changes. */
  render_backend: RenderMode | null;
  /** Who presented this reel, on the presenter lane. Recorded for the same
   *  reason `render_backend` is: the preset is editable and the override is
   *  deleted with its idea, so nothing about it is reconstructable afterwards.
   *  Null on every other lane, and on presenter reels that ran before this
   *  was recorded. */
  presenter: PresenterRecord | null;
  /** The narration, and the source of truth for it. Drafted by `write_script`,
   *  edited and approved by an owner, then handed to whichever backend renders:
   *  `video_script` for MoneyPrinterTurbo, the script body for HeyGen, the TTS
   *  input for the fal end-to-end lane. No lane writes its own any more. */
  script: string | null;
  /** When an owner approved the script. Null is the gate: `claim_production`
   *  will not return this row at `await_script` while it is null, and
   *  `submit_render` refuses outright. Any later edit clears it again. */
  script_approved_at: string | null;
  script_approved_by: string | null;
  /** When the text last changed, by draft or by hand. */
  script_updated_at: string | null;
  /** Who last changed it. Null when the pipeline wrote the draft. */
  script_updated_by: string | null;
  video_url: string | null;
  thumbnail_url: string | null;
  duration_seconds: number | null;
  qc: Json;
  platform_copy: Json;
  cost_estimate_usd: number | null;
  /** Never written by the pipeline today. Rendered in the review UI, so it is
   *  reliably null — real spend is not tracked anywhere yet. */
  cost_actual_usd: number | null;
  error: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string;
  completed_at: string | null;
  /** Maintained by the `productions_touch_updated_at` trigger. */
  updated_at: string;
  postiz_media_id: string | null;
  postiz_media_path: string | null;
  /** Set by `pause_production`. A paused row is one `claim_production` does not
   *  return, so the driver simply never picks it up again. */
  paused_at: string | null;
  /** The production that replaced this one, set by `rerun_production`. */
  superseded_by: string | null;
};

/**
 * One entry in a production's step log.
 *
 * Append-only, and the only record of how a production actually ran:
 * `run_state` is a rolling snapshot that each step overwrites.
 */
export type ProductionEventRow = {
  id: string;
  production_id: string;
  /** A driver step name, or `control` for a human action. */
  step: string;
  outcome: ProductionEventOutcome;
  detail: string | null;
  error: string | null;
  attempt: number | null;
  payload: Json;
  /** Set only on `control` events — a driver event has no actor. */
  actor_id: string | null;
  created_at: string;
};

export type ProductionEventOutcome =
  | 'started'
  | 'progress'
  | 'succeeded'
  | 'retrying'
  | 'infra_retry'
  | 'waiting'
  | 'failed'
  | 'parked'
  | 'terminal'
  | 'control';

export type ApprovalRow = {
  id: string;
  gate: 1 | 2;
  subject_type: 'idea' | 'production';
  subject_id: string;
  decision: ApprovalDecision;
  note: string | null;
  style_preset_id: string | null;
  /** Nullable since the pipeline-integration migration, so that an automated
   *  transition can be recorded; a check constraint still requires an actor
   *  whenever `source` is `human`. */
  actor_id: string | null;
  source: 'human' | 'system';
  created_at: string;
};

/**
 * The steps a production can be sent back to on its own row.
 *
 * `submit_render` is deliberately absent: a re-render costs money and opens a
 * fresh production instead, via `rerun_production`.
 */
export const REWIND_STEPS = ['fetch_and_qc', 'generate_copy', 'open_gate2'] as const;
export type RewindStep = (typeof REWIND_STEPS)[number];

// ---------------------------------------------------------------------------
// The HeyGen catalogue
// ---------------------------------------------------------------------------
//
// This app is a static bundle holding no secrets, which is what makes shipping
// it safe and what makes it unable to ask HeyGen anything. So the worker asks
// on its behalf and writes the answers here; the picker reads these three
// tables and never sees an API key.

/** How a look is framed. `unknown` is honest rather than a guess — the warning
 *  it suppresses matters most on exactly the looks we know least about. */
export type LookOrientation = 'portrait' | 'landscape' | 'square' | 'unknown';

export type HeyGenLookRow = {
  avatar_id: string;
  name: string | null;
  preview_image_url: string | null;
  preview_video_url: string | null;
  orientation: LookOrientation;
  /** The engines this look advertises, lower-cased. Empty means the response
   *  said nothing, which is treated as "no opinion" rather than "supports
   *  none": a field HeyGen renames must cost a check, not the whole picker. */
  engines: string[];
  default_voice_id: string | null;
  gender: string | null;
  ownership: string;
  seen_at: string;
};

/**
 * Where a voice id has got to.
 *
 * Voices are resolved one at a time rather than listed: `GET /v3/voices` is
 * 3,089 entries over 62 pages on this account and does not contain the cloned
 * voice the preset names, while `GET /v3/voices/{id}` answers directly.
 */
export type VoiceStatus = 'pending' | 'ok' | 'unknown';

export type HeyGenVoiceRow = {
  voice_id: string;
  status: VoiceStatus;
  name: string | null;
  language: string | null;
  gender: string | null;
  preview_audio_url: string | null;
  error: string | null;
  requested_at: string;
  resolved_at: string | null;
};

export type CatalogueStatus = 'idle' | 'requested' | 'running' | 'failed';

/** The single row that asks the worker to refill the caches and records how
 *  the refill went. Same shape of thing as `trend_runs`, and for the same
 *  reason: the app cannot reach an external API itself. */
export type HeyGenCatalogueRow = {
  id: boolean;
  status: CatalogueStatus;
  requested_at: string | null;
  requested_by: string | null;
  started_at: string | null;
  refreshed_at: string | null;
  looks: number | null;
  voices: number | null;
  error: string | null;
};

/** True while the worker owes an answer, which is what the Refresh button
 *  disables itself on. */
export function isCatalogueRefreshing(row: HeyGenCatalogueRow | null): boolean {
  return row?.status === 'requested' || row?.status === 'running';
}

/**
 * A validated avatar/voice pair, as `presenter_choice` in Postgres returns it.
 *
 * Names are carried alongside the ids so a stored override reads as words
 * rather than as two hex strings — the look it came from may since have been
 * deleted from the account.
 */
export interface PresenterChoice {
  avatar_id: string;
  avatar_name?: string | null;
  orientation?: LookOrientation;
  voice_id: string;
  voice_name?: string | null;
  /** Absent means Avatar IV, which is what omitting it at submit time selects. */
  engine?: string;
}

/** What actually rendered, written to the production by the render step. */
export interface PresenterRecord {
  avatar_id: string | null;
  voice_id: string | null;
  avatar_name?: string;
  voice_name?: string;
  engine?: string;
  /** Not derivable afterwards: the override is deleted with its idea and the
   *  preset can be edited, so without this a reel made by an override and one
   *  made by a preset that has since changed are indistinguishable. */
  source: 'preset' | 'override';
}

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
        Insert: Omit<
          IdeaRow,
          'id' | 'created_at' | 'target_platforms' | 'status' | 'trend_run_id' | 'relevance' | 'connection'
        > & {
          id?: string;
          created_at?: string;
          target_platforms?: string[];
          status?: IdeaStatus;
          // Pipeline-owned. The browser never inserts an idea, but the type
          // should not demand columns only the worker knows.
          trend_run_id?: string | null;
          relevance?: number | null;
          connection?: string | null;
        };
        Update: Writable<IdeaRow>;
        Relationships: [];
      };
      productions: {
        Row: ProductionRow;
        Insert: Omit<
          ProductionRow,
          | 'id'
          | 'created_at'
          | 'updated_at'
          | 'status'
          | 'qc'
          | 'platform_copy'
          | 'paused_at'
          | 'superseded_by'
          | 'script_approved_at'
          | 'script_approved_by'
          | 'script_updated_at'
          | 'script_updated_by'
        > & {
          id?: string;
          created_at?: string;
          updated_at?: string;
          status?: ProductionStatus;
          qc?: Json;
          platform_copy?: Json;
          paused_at?: string | null;
          superseded_by?: string | null;
        };
        Update: Writable<ProductionRow>;
        Relationships: [];
      };
      production_events: {
        Row: ProductionEventRow;
        // Written by the service-role worker and by the control functions.
        // There is no insert policy, so the browser structurally cannot append.
        Insert: never;
        Update: never;
        Relationships: [];
      };
      approvals: {
        Row: ApprovalRow;
        Insert: Omit<ApprovalRow, 'id' | 'created_at'> & { id?: string; created_at?: string };
        Update: Writable<ApprovalRow>;
        Relationships: [];
      };
      // All three are filled by the worker with a service-role key and read
      // from here. None has an INSERT, UPDATE or DELETE policy, so the browser
      // structurally cannot write one — every change goes through a function.
      heygen_looks: {
        Row: HeyGenLookRow;
        Insert: never;
        Update: never;
        Relationships: [];
      };
      heygen_voices: {
        Row: HeyGenVoiceRow;
        Insert: never;
        Update: never;
        Relationships: [];
      };
      heygen_catalogue: {
        Row: HeyGenCatalogueRow;
        Insert: never;
        Update: never;
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
        // All optional. Null, or omitted, uses the saved settings; a prompt
        // makes the run a described search, scouted on terms read from it.
        Args: { p_budget_minutes?: number | null; p_hashtags_per_run?: number | null; p_prompt?: string | null };
        Returns: TrendRunRow;
      };
      cancel_trend_run: {
        Args: { p_run_id: string };
        Returns: TrendRunRow;
      };
      // Removes a run from the lists, stopping it first if it is still going.
      // Owner-gated in SQL like the others; the row is kept.
      dismiss_trend_run: {
        Args: { p_run_id: string };
        Returns: TrendRunRow;
      };
      approve_idea: {
        // `p_presenter` is the Gate 1 override, and only the presenter lane
        // accepts one. Null keeps the preset's pair. It is validated against
        // the cached catalogue inside the function, because an avatar this
        // account cannot use fails terminally — after the gate, having already
        // spent the review.
        Args: {
          p_idea_id: string;
          p_style_id: string;
          p_note?: string | null;
          p_presenter?: PresenterChoice | null;
        };
        Returns: IdeaRow;
      };
      // The preset default. A function rather than the owner's existing UPDATE
      // on style_presets: `params` is one jsonb column holding the whole lane
      // configuration, and a browser sending a replacement object built from a
      // stale read would silently drop aspect_ratio, resolution and captions.
      set_preset_presenter: {
        Args: { p_preset_id: string; p_avatar_id: string; p_voice_id: string; p_engine?: string | null };
        Returns: StylePresetRow;
      };
      // Both of these record an intention and return; the worker's sweep acts
      // on it within a minute, exactly as `request_trend_run` works.
      request_heygen_catalogue_refresh: {
        Args: Record<never, never>;
        Returns: HeyGenCatalogueRow;
      };
      request_heygen_voice: {
        Args: { p_voice_id: string };
        Returns: HeyGenVoiceRow;
      };
      reject_idea: {
        Args: { p_idea_id: string; p_note?: string | null };
        Returns: IdeaRow;
      };
      decide_production: {
        Args: { p_production_id: string; p_decision: ApprovalDecision; p_note?: string | null };
        Returns: ProductionRow;
      };
      // The pipeline controls. Owner-gated in SQL exactly as the gates are —
      // each raises 42501 for a viewer, and each writes its own event row.
      pause_production: {
        Args: { p_production_id: string; p_note?: string | null };
        Returns: ProductionRow;
      };
      resume_production: {
        Args: { p_production_id: string; p_note?: string | null };
        Returns: ProductionRow;
      };
      retry_production: {
        Args: { p_production_id: string; p_note?: string | null };
        Returns: ProductionRow;
      };
      cancel_production: {
        Args: { p_production_id: string; p_note?: string | null };
        Returns: ProductionRow;
      };
      rerun_production: {
        // Null keeps the style the superseded production used.
        Args: { p_production_id: string; p_style_id?: string | null; p_note?: string | null };
        Returns: ProductionRow;
      };
      rewind_production: {
        Args: { p_production_id: string; p_step: RewindStep; p_note?: string | null };
        Returns: ProductionRow;
      };
      // The script gate. Same shape and the same owner check as the controls
      // above, and for the same reason: there is no server tier, so a
      // `security definer` function is what keeps 'change the row' and 'write
      // the audit row' in one transaction, and what refuses a viewer rather
      // than trusting a disabled button.
      //
      // Saving and approving are separate functions rather than one with a
      // flag, because they differ in what they do to the pipeline: saving
      // persists words and leaves the gate shut, approving hands the row back
      // to the driver. A single endpoint would make 'did that start a render?'
      // a question about an argument.
      save_script: {
        Args: { p_production_id: string; p_script: string };
        Returns: ProductionRow;
      };
      approve_script: {
        // The text goes with the approval, so 'save then approve' is one round
        // trip and one transaction — an owner cannot leave a version behind.
        Args: { p_production_id: string; p_script: string; p_note?: string | null };
        Returns: ProductionRow;
      };
      request_script_redraft: {
        Args: { p_production_id: string; p_note?: string | null };
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
