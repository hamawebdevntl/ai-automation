import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';
import type {
  ApprovalDecision,
  ApprovalRow,
  IdeaRow,
  IdeaStatus,
  ProductionEventRow,
  ProductionRow,
  ProductionStatus,
  RewindStep,
  StylePresetRow,
} from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import { toError } from '@/lib/supabase-error';

export const queueKeys = {
  all: ['queue'] as const,
  stylePresets: () => [...queueKeys.all, 'style-presets'] as const,
  ideas: (status: IdeaStatus) => [...queueKeys.all, 'ideas', status] as const,
  ideaPage: (status: IdeaStatus, page: number, pageSize: number) =>
    [...queueKeys.ideas(status), 'page', page, pageSize] as const,
  idea: (id: string) => [...queueKeys.all, 'idea', id] as const,
  productions: (statuses: readonly ProductionStatus[]) => [...queueKeys.all, 'productions', ...statuses] as const,
  production: (id: string) => [...queueKeys.all, 'production', id] as const,
  approvals: (subjectType: 'idea' | 'production', subjectId: string) =>
    [...queueKeys.all, 'approvals', subjectType, subjectId] as const,
  events: (productionId: string) => [...queueKeys.all, 'events', productionId] as const,
  live: () => [...queueKeys.all, 'live'] as const,
  // Under `live` on purpose, so the table-wide productions subscription that
  // invalidates the live list invalidates this too: a production appearing is
  // exactly what removes an idea from here.
  undispatched: () => [...queueKeys.live(), 'undispatched'] as const,
  productionForIdea: (ideaId: string) => [...queueKeys.all, 'production-for-idea', ideaId] as const,
  // Under `all` on purpose: a run finishing, an approval and a dismissal each
  // invalidate that namespace, and each is exactly what changes this list.
  searchIdeas: (runId: string) => [...queueKeys.all, 'search-ideas', runId] as const,
};

/** Statuses that mean "a person still has to look at this cut". */
export const GATE_2_STATUSES = ['awaiting_review', 'qc_failed'] as const satisfies readonly ProductionStatus[];

/**
 * Everything between Gate 1 and a finished production.
 *
 * Nothing in this app used to read a production in any of these states: the
 * queue asked only for pending ideas and the review page only for the two
 * Gate 2 statuses, so a production that was rendering, retrying or parked
 * appeared nowhere at all. `approved` is here because a production held by
 * `publishing_disabled` keeps that status while holding a finished cut.
 */
export const IN_PROGRESS_STATUSES = [
  'queued',
  'running',
  'publishing',
  'awaiting_review',
  'qc_failed',
  'parked',
  'failed',
  'approved',
] as const satisfies readonly ProductionStatus[];

export interface ProductionWithContext {
  production: ProductionRow;
  idea: IdeaRow | null;
  style: StylePresetRow | null;
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function stylePresetsQueryOptions() {
  return queryOptions({
    queryKey: queueKeys.stylePresets(),
    // Presets change when someone edits the pipeline, not during a session.
    staleTime: 10 * 60_000,
    queryFn: async (): Promise<StylePresetRow[]> => {
      const { data, error } = await supabase
        .from('style_presets')
        .select('*')
        .eq('is_active', true)
        .order('sort_order', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/** How many ideas are shown at once. */
export const IDEAS_PER_PAGE = 10;

export interface IdeaPage {
  ideas: IdeaRow[];
  /** Total matching rows, not the number on this page. */
  total: number;
  pageCount: number;
}

/**
 * One page of the queue.
 *
 * Paged in Postgres rather than in the browser. A trend run adds up to ten
 * ideas at a time and the owner can now ask for more on demand, so "fetch them
 * all and slice" gets slower exactly as the feature gets used. `count: exact`
 * costs a second scan, which at these row counts is cheaper than shipping
 * pages nobody looks at.
 *
 * A page past the end returns no rows rather than an error -- the queue shrinks
 * under you every time an idea is approved, and landing on a page that has
 * since emptied should not look like a failure. `pageCount` is what the UI
 * uses to send you back.
 */
export function ideaPageQueryOptions(status: IdeaStatus = 'pending', page = 1, pageSize = IDEAS_PER_PAGE) {
  const from = (page - 1) * pageSize;
  return queryOptions({
    queryKey: queueKeys.ideaPage(status, page, pageSize),
    // Without this the list flashes an empty state on every page change, which
    // reads as "the queue emptied" rather than "the next page is loading".
    placeholderData: (previous) => previous,
    queryFn: async (): Promise<IdeaPage> => {
      const { data, error, count } = await supabase
        .from('ideas')
        .select('*', { count: 'exact' })
        .eq('status', status)
        .order('created_at', { ascending: false })
        .range(from, from + pageSize - 1);
      if (error) throw toError(error);
      const total = count ?? 0;
      return { ideas: data ?? [], total, pageCount: Math.max(1, Math.ceil(total / pageSize)) };
    },
  });
}

export function ideaQueryOptions(id: string) {
  return queryOptions({
    queryKey: queueKeys.idea(id),
    queryFn: async (): Promise<IdeaRow> => {
      const { data, error } = await supabase.from('ideas').select('*').eq('id', id).single();
      if (error) throw toError(error);
      return data;
    },
  });
}

/** More than any run can insert (`ideas_per_run` tops out at 25), so a guard rather than a page. */
export const SEARCH_IDEAS_LIMIT = 50;

/**
 * The pending ideas one described search produced, best fit first.
 *
 * Unpaged: a run inserts at most `ideas_per_run`. Pending only, so Review and
 * Dismiss mean what they mean everywhere else; the header quotes the run's own
 * `inserted` for the total. Relevance is null on ideas from a run that had no
 * prompt, hence `nullsFirst: false`, and `created_at` breaks ties the way the
 * main queue orders.
 */
export function searchIdeasQueryOptions(runId: string) {
  return queryOptions({
    queryKey: queueKeys.searchIdeas(runId),
    queryFn: async (): Promise<IdeaRow[]> => {
      const { data, error } = await supabase
        .from('ideas')
        .select('*')
        .eq('trend_run_id', runId)
        .eq('status', 'pending')
        .order('relevance', { ascending: false, nullsFirst: false })
        .order('created_at', { ascending: false })
        .limit(SEARCH_IDEAS_LIMIT);
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * Productions plus the idea and style they came from.
 *
 * Deliberately three round trips rather than one PostgREST embed: the
 * hand-written `Database` type carries no relationship metadata, so an embedded
 * select would not be type-checked. At queue sizes — tens of rows a day — the
 * extra requests cost nothing and the shape stays honest.
 */
async function loadProductionsWithContext(statuses: readonly ProductionStatus[]): Promise<ProductionWithContext[]> {
  const { data: productions, error } = await supabase
    .from('productions')
    .select('*')
    .in('status', statuses)
    .order('created_at', { ascending: false });
  if (error) throw toError(error);
  if (!productions || productions.length === 0) return [];

  const ideaIds = [...new Set(productions.map((production) => production.idea_id))];
  const [ideasResult, presetsResult] = await Promise.all([
    supabase.from('ideas').select('*').in('id', ideaIds),
    supabase.from('style_presets').select('*'),
  ]);
  if (ideasResult.error) throw toError(ideasResult.error);
  if (presetsResult.error) throw toError(presetsResult.error);

  const ideasById = new Map((ideasResult.data ?? []).map((idea) => [idea.id, idea]));
  const presetsById = new Map((presetsResult.data ?? []).map((preset) => [preset.id, preset]));

  return productions.map((production) => ({
    production,
    idea: ideasById.get(production.idea_id) ?? null,
    style: presetsById.get(production.style_preset_id) ?? null,
  }));
}

export function reviewQueueQueryOptions(statuses: readonly ProductionStatus[] = GATE_2_STATUSES) {
  return queryOptions({
    queryKey: queueKeys.productions(statuses),
    queryFn: () => loadProductionsWithContext(statuses),
  });
}

export function productionQueryOptions(id: string) {
  return queryOptions({
    queryKey: queueKeys.production(id),
    queryFn: async (): Promise<ProductionWithContext> => {
      const { data: production, error } = await supabase.from('productions').select('*').eq('id', id).single();
      if (error) throw toError(error);

      const [ideaResult, presetResult] = await Promise.all([
        supabase.from('ideas').select('*').eq('id', production.idea_id).maybeSingle(),
        supabase.from('style_presets').select('*').eq('id', production.style_preset_id).maybeSingle(),
      ]);
      if (ideaResult.error) throw toError(ideaResult.error);
      if (presetResult.error) throw toError(presetResult.error);

      return { production, idea: ideaResult.data, style: presetResult.data };
    },
  });
}

export function approvalsQueryOptions(subjectType: 'idea' | 'production', subjectId: string) {
  return queryOptions({
    queryKey: queueKeys.approvals(subjectType, subjectId),
    queryFn: async (): Promise<ApprovalRow[]> => {
      const { data, error } = await supabase
        .from('approvals')
        .select('*')
        .eq('subject_type', subjectType)
        .eq('subject_id', subjectId)
        .order('created_at', { ascending: false });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * A production's step log, oldest first.
 *
 * This is the record `run_state` cannot be: `run_state` is one mutable column
 * that every step overwrites, so it says where a production is and never how
 * it got there. Ordered ascending because it is read as a narrative.
 */
export function productionEventsQueryOptions(productionId: string) {
  return queryOptions({
    queryKey: queueKeys.events(productionId),
    queryFn: async (): Promise<ProductionEventRow[]> => {
      const { data, error } = await supabase
        .from('production_events')
        .select('*')
        .eq('production_id', productionId)
        .order('created_at', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * Ideas that were approved and never dispatched.
 *
 * The one state that used to appear in no list at all. An approved idea leaves
 * the pending queue at once, but it only reaches the live list when a worker
 * opens a production for it — so with no worker running, every approval simply
 * vanished. These are the ideas in that gap.
 *
 * Two round trips rather than an anti-join: the hand-written `Database` type
 * carries no relationship metadata, so an embedded select would not be typed.
 * Fifty approved ideas is far more than this queue holds at once.
 */
export function undispatchedIdeasQueryOptions() {
  return queryOptions({
    queryKey: queueKeys.undispatched(),
    queryFn: async (): Promise<IdeaRow[]> => {
      const { data: ideas, error } = await supabase
        .from('ideas')
        .select('*')
        .eq('status', 'approved')
        .order('decided_at', { ascending: false })
        .limit(50);
      if (error) throw toError(error);
      if (!ideas || ideas.length === 0) return [];

      const { data: productions, error: productionsError } = await supabase
        .from('productions')
        .select('idea_id')
        .in(
          'idea_id',
          ideas.map((idea) => idea.id),
        );
      if (productionsError) throw toError(productionsError);

      const dispatched = new Set((productions ?? []).map((production) => production.idea_id));
      return ideas.filter((idea) => !dispatched.has(idea.id));
    },
  });
}

/** Every production the pipeline still has work to do on, or has stopped on. */
export function liveProductionsQueryOptions() {
  return queryOptions({
    queryKey: queueKeys.live(),
    queryFn: () => loadProductionsWithContext(IN_PROGRESS_STATUSES),
  });
}

/**
 * The production for one idea, if it has one yet.
 *
 * Returns null rather than throwing for the window between approving an idea
 * and the dispatcher opening its production -- five seconds at the default
 * poll, and a state the UI has to be able to draw.
 *
 * `maybeSingle` is wrong here despite looking right: an idea can have more
 * than one production once it has been re-run, and the superseded rows stay.
 * The newest is the live one.
 */
export function productionForIdeaQueryOptions(ideaId: string) {
  return queryOptions({
    queryKey: queueKeys.productionForIdea(ideaId),
    queryFn: async (): Promise<ProductionRow | null> => {
      const { data, error } = await supabase
        .from('productions')
        .select('*')
        .eq('idea_id', ideaId)
        .order('created_at', { ascending: false })
        .limit(1);
      if (error) throw toError(error);
      return data?.[0] ?? null;
    },
  });
}

// ---------------------------------------------------------------------------
// Gate decisions
// ---------------------------------------------------------------------------
//
// Every decision goes through a Postgres function rather than an UPDATE. There
// is no server tier here, so those functions are what keep "record the
// decision" and "write the audit row" from coming apart, and what stops a
// viewer from approving anything.

export interface ApproveIdeaInput {
  ideaId: string;
  styleId: string;
  note?: string;
}

export function useApproveIdea() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ ideaId, styleId, note }: ApproveIdeaInput): Promise<IdeaRow> => {
      const { data, error } = await supabase.rpc('approve_idea', {
        p_idea_id: ideaId,
        p_style_id: styleId,
        p_note: note?.trim() || null,
      });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (idea) => {
      queryClient.setQueryData(queueKeys.idea(idea.id), idea);
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

export interface RejectIdeaInput {
  ideaId: string;
  note?: string;
}

export function useRejectIdea() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ ideaId, note }: RejectIdeaInput): Promise<IdeaRow> => {
      const { data, error } = await supabase.rpc('reject_idea', {
        p_idea_id: ideaId,
        p_note: note?.trim() || null,
      });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (idea) => {
      queryClient.setQueryData(queueKeys.idea(idea.id), idea);
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

export interface DecideProductionInput {
  productionId: string;
  decision: ApprovalDecision;
  note?: string;
}

export function useDecideProduction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ productionId, decision, note }: DecideProductionInput): Promise<ProductionRow> => {
      const { data, error } = await supabase.rpc('decide_production', {
        p_production_id: productionId,
        p_decision: decision,
        p_note: note?.trim() || null,
      });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

// ---------------------------------------------------------------------------
// Driving a production
// ---------------------------------------------------------------------------
//
// Six controls, each an owner-gated Postgres function for the same reason the
// gate decisions are: there is no server tier, so a `security definer` function
// is what keeps "change the row" and "write the audit row" in one transaction,
// and what refuses a viewer rather than trusting the button to be disabled.
//
// They also all refuse a row the worker currently holds, instead of racing the
// driver for `run_state`. `availableControls` in `pipeline-steps.ts` mirrors
// those guards so the owner is told why rather than refused after clicking.

export interface ControlInput {
  productionId: string;
  note?: string;
}

export interface RerunInput extends ControlInput {
  /** Omitted keeps the style the superseded production used. */
  styleId?: string | null;
}

export interface RewindInput extends ControlInput {
  step: RewindStep;
}

/**
 * Everything a control touches, invalidated together.
 *
 * A control changes the row, appends an event, and can move a production
 * between the queue's live list and the review queue -- so the whole `queue`
 * namespace goes. These are tens of rows; the alternative is a list of keys
 * that silently goes stale the next time one is added.
 */
function useControlMutation<TInput extends ControlInput>(run: (input: TInput) => Promise<ProductionRow>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (production) => {
      queryClient.setQueryData(queueKeys.production(production.id), (previous: ProductionWithContext | undefined) =>
        previous ? { ...previous, production } : previous,
      );
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

export function usePauseProduction() {
  return useControlMutation(async ({ productionId, note }: ControlInput) => {
    const { data, error } = await supabase.rpc('pause_production', {
      p_production_id: productionId,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

export function useResumeProduction() {
  return useControlMutation(async ({ productionId, note }: ControlInput) => {
    const { data, error } = await supabase.rpc('resume_production', {
      p_production_id: productionId,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

export function useRetryProduction() {
  return useControlMutation(async ({ productionId, note }: ControlInput) => {
    const { data, error } = await supabase.rpc('retry_production', {
      p_production_id: productionId,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

export function useCancelProduction() {
  return useControlMutation(async ({ productionId, note }: ControlInput) => {
    const { data, error } = await supabase.rpc('cancel_production', {
      p_production_id: productionId,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

/** Opens a *new* production. The returned row is the replacement, not the original. */
export function useRerunProduction() {
  return useControlMutation(async ({ productionId, styleId, note }: RerunInput) => {
    const { data, error } = await supabase.rpc('rerun_production', {
      p_production_id: productionId,
      p_style_id: styleId ?? null,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

export function useRewindProduction() {
  return useControlMutation(async ({ productionId, step, note }: RewindInput) => {
    const { data, error } = await supabase.rpc('rewind_production', {
      p_production_id: productionId,
      p_step: step,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

// ---------------------------------------------------------------------------
// The script gate
// ---------------------------------------------------------------------------
//
// Three functions rather than one with a flag, because they differ in what they
// do to the *pipeline* rather than in what they write. Saving persists words and
// leaves the gate shut; approving hands the row back to the driver and is the
// one thing here that lets a render start; redrafting spends an LLM call and
// throws away the current text. Collapsing them would make "did that start a
// render?" a question about an argument.
//
// All three are owner-gated in Postgres, refuse a production whose render has
// already been submitted, and write their own `production_events` row — so the
// script gate is as traceable as every other human action on a production.

export interface SaveScriptInput {
  productionId: string;
  script: string;
}

export interface ApproveScriptInput extends SaveScriptInput {
  note?: string;
}

/**
 * Everything a script write touches.
 *
 * The row itself is set directly so the editor stops showing a stale approval
 * the instant the mutation returns, and the rest of the namespace is
 * invalidated because a script change moves the production between the queue's
 * live list, the idea page and the production page.
 */
function useScriptMutation<TInput extends { productionId: string }>(run: (input: TInput) => Promise<ProductionRow>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: run,
    onSuccess: (production) => {
      queryClient.setQueryData(queueKeys.production(production.id), (previous: ProductionWithContext | undefined) =>
        previous ? { ...previous, production } : previous,
      );
      queryClient.setQueryData(queueKeys.productionForIdea(production.idea_id), production);
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}

/** Persist the words without opening the gate. An edit un-approves. */
export function useSaveScript() {
  return useScriptMutation(async ({ productionId, script }: SaveScriptInput) => {
    const { data, error } = await supabase.rpc('save_script', {
      p_production_id: productionId,
      p_script: script,
    });
    if (error) throw toError(error);
    return data;
  });
}

/**
 * Approve the script, and let the render start.
 *
 * The text goes with the approval so that "save then approve" is one round trip
 * and one transaction — an owner who edits and approves cannot leave a version
 * behind, and there is no window in which the gate is open on words other than
 * the ones on screen.
 */
export function useApproveScript() {
  return useScriptMutation(async ({ productionId, script, note }: ApproveScriptInput) => {
    const { data, error } = await supabase.rpc('approve_script', {
      p_production_id: productionId,
      p_script: script,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}

/** Ask the pipeline for a different draft. One LLM call; no video is generated. */
export function useRedraftScript() {
  return useScriptMutation(async ({ productionId, note }: ControlInput) => {
    const { data, error } = await supabase.rpc('request_script_redraft', {
      p_production_id: productionId,
      p_note: note?.trim() || null,
    });
    if (error) throw toError(error);
    return data;
  });
}
