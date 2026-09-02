import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';
import type {
  ApprovalDecision,
  ApprovalRow,
  IdeaRow,
  IdeaStatus,
  ProductionRow,
  ProductionStatus,
  StylePresetRow,
} from '@/lib/database.types';
import { supabase } from '@/lib/supabase';

export const queueKeys = {
  all: ['queue'] as const,
  stylePresets: () => [...queueKeys.all, 'style-presets'] as const,
  ideas: (status: IdeaStatus) => [...queueKeys.all, 'ideas', status] as const,
  idea: (id: string) => [...queueKeys.all, 'idea', id] as const,
  productions: (statuses: readonly ProductionStatus[]) => [...queueKeys.all, 'productions', ...statuses] as const,
  production: (id: string) => [...queueKeys.all, 'production', id] as const,
  approvals: (subjectType: 'idea' | 'production', subjectId: string) =>
    [...queueKeys.all, 'approvals', subjectType, subjectId] as const,
};

/** Statuses that mean "a person still has to look at this cut". */
export const GATE_2_STATUSES = ['awaiting_review', 'qc_failed'] as const satisfies readonly ProductionStatus[];

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
      if (error) throw error;
      return data ?? [];
    },
  });
}

export function ideasQueryOptions(status: IdeaStatus = 'pending') {
  return queryOptions({
    queryKey: queueKeys.ideas(status),
    queryFn: async (): Promise<IdeaRow[]> => {
      const { data, error } = await supabase
        .from('ideas')
        .select('*')
        .eq('status', status)
        .order('created_at', { ascending: false });
      if (error) throw error;
      return data ?? [];
    },
  });
}

export function ideaQueryOptions(id: string) {
  return queryOptions({
    queryKey: queueKeys.idea(id),
    queryFn: async (): Promise<IdeaRow> => {
      const { data, error } = await supabase.from('ideas').select('*').eq('id', id).single();
      if (error) throw error;
      return data;
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
  if (error) throw error;
  if (!productions || productions.length === 0) return [];

  const ideaIds = [...new Set(productions.map((production) => production.idea_id))];
  const [ideasResult, presetsResult] = await Promise.all([
    supabase.from('ideas').select('*').in('id', ideaIds),
    supabase.from('style_presets').select('*'),
  ]);
  if (ideasResult.error) throw ideasResult.error;
  if (presetsResult.error) throw presetsResult.error;

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
      if (error) throw error;

      const [ideaResult, presetResult] = await Promise.all([
        supabase.from('ideas').select('*').eq('id', production.idea_id).maybeSingle(),
        supabase.from('style_presets').select('*').eq('id', production.style_preset_id).maybeSingle(),
      ]);
      if (ideaResult.error) throw ideaResult.error;
      if (presetResult.error) throw presetResult.error;

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
      if (error) throw error;
      return data ?? [];
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
      if (error) throw error;
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
      if (error) throw error;
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
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queueKeys.all });
    },
  });
}
