import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';
import { queueKeys } from '@/features/queue/api';
import type {
  RenderSpendRow,
  SpendCapRow,
  SpendCapStatusRow,
  SpendRateRow,
  SpendUnit,
  StylePresetSpendRow,
} from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import { codeOf, toError } from '@/lib/supabase-error';

export const spendKeys = {
  all: ['spend'] as const,
  caps: () => [...spendKeys.all, 'caps'] as const,
  rates: () => [...spendKeys.all, 'rates'] as const,
  presets: () => [...spendKeys.all, 'presets'] as const,
  production: (id: string) => [...spendKeys.all, 'production', id] as const,
};

/**
 * How long a spend figure may be believed.
 *
 * Short, because these move on their own: a render finishing anywhere in the
 * fleet changes what a cap has left, and nothing in the browser is told about
 * it. Thirty seconds is well inside the time it takes to read an idea and
 * choose a style, so the figure under the Approve button is the one Postgres
 * would use — and if it is not, `approve_idea` refuses and says so, which is
 * the check that actually holds.
 */
const SPEND_STALE_MS = 30_000;

/**
 * Every style with the cap governing it and what its renders have cost.
 *
 * One query for the whole list rather than one per preset: Gate 1 draws all of
 * them at once, and the view is a handful of rows.
 */
export function stylePresetSpendQueryOptions() {
  return queryOptions({
    queryKey: spendKeys.presets(),
    staleTime: SPEND_STALE_MS,
    queryFn: async (): Promise<StylePresetSpendRow[]> => {
      const { data, error } = await supabase.from('style_preset_spend').select('*');
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/** The spend rows for one preset id, or undefined when the query has not landed. */
export function spendForPreset(
  rows: StylePresetSpendRow[] | undefined,
  presetId: string,
): StylePresetSpendRow | undefined {
  return rows?.find((row) => row.style_preset_id === presetId);
}

/** Every cap, with what has been spent against it today and this month. */
export function spendCapStatusQueryOptions() {
  return queryOptions({
    queryKey: spendKeys.caps(),
    staleTime: SPEND_STALE_MS,
    queryFn: async (): Promise<SpendCapStatusRow[]> => {
      const { data, error } = await supabase
        .from('spend_cap_status')
        .select('*')
        .order('provider', { ascending: true })
        .order('model', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * What one production was billed, charge by charge.
 *
 * `productions.cost_actual_usd` is the total and is enough for a list; this is
 * for the production page, where "$2.40" is less use than "$1.20 of video,
 * $1.20 of narration" when the question is why it cost that.
 */
export function productionSpendQueryOptions(productionId: string) {
  return queryOptions({
    queryKey: spendKeys.production(productionId),
    queryFn: async (): Promise<RenderSpendRow[]> => {
      const { data, error } = await supabase
        .from('render_spend')
        .select('*')
        .eq('production_id', productionId)
        .order('spent_at', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

/**
 * What each provider charges, per model.
 *
 * Not decoration beside the caps: a model with no rate cannot render at all.
 * The render step refuses to submit one, because a cost it cannot compute is a
 * cost that would spend against a ceiling without moving it — so this list is
 * the recovery path for that refusal, and the reason it says "add a rate in
 * Settings" rather than naming a table.
 */
export function spendRatesQueryOptions() {
  return queryOptions({
    queryKey: spendKeys.rates(),
    queryFn: async (): Promise<SpendRateRow[]> => {
      const { data, error } = await supabase
        .from('spend_rates')
        .select('*')
        .order('provider', { ascending: true })
        .order('model', { ascending: true });
      if (error) throw toError(error);
      return data ?? [];
    },
  });
}

export interface SaveSpendRateInput {
  provider: string;
  model?: string;
  unit: SpendUnit;
  rateUsd: number;
  note?: string | null;
}

/** Create or change one rate. Upserted on (provider, model), like a cap. */
export function useSaveSpendRate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SaveSpendRateInput): Promise<SpendRateRow> => {
      const { data: auth } = await supabase.auth.getUser();
      const { data, error } = await supabase
        .from('spend_rates')
        .upsert(
          {
            provider: input.provider.trim(),
            model: (input.model ?? '').trim(),
            unit: input.unit,
            rate_usd: input.rateUsd,
            note: input.note?.trim() || null,
            updated_by: auth.user?.id ?? null,
          },
          { onConflict: 'provider,model' },
        )
        .select()
        .single();
      if (error) throw toError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: spendKeys.all });
    },
  });
}

export function useRemoveSpendRate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ provider, model }: { provider: string; model: string }): Promise<void> => {
      const { error } = await supabase.from('spend_rates').delete().eq('provider', provider).eq('model', model);
      if (error) throw toError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: spendKeys.all });
    },
  });
}

// ---------------------------------------------------------------------------
// Editing a cap
// ---------------------------------------------------------------------------
//
// A plain table write rather than an RPC, unlike the gate decisions. Those go
// through security-definer functions because a decision and its audit row must
// land in one transaction; a ceiling is one row with no second effect, so the
// four row policies are the whole rule. An owner check is not duplicated here:
// `is_owner()` is in the policies, so a viewer who reaches this is refused by
// Postgres rather than by this file.

export interface SaveSpendCapInput {
  provider: string;
  /** Empty is the provider as a whole. */
  model?: string;
  dailyLimitUsd: number | null;
  monthlyLimitUsd: number | null;
  isActive?: boolean;
  note?: string | null;
}

/**
 * Create or change one ceiling.
 *
 * An upsert on (provider, model), which is what makes "add a cap for this
 * model" and "raise the cap on this model" the same action from the card's
 * point of view — the difference is whether a row already exists, which is not
 * something the person editing it should have to know.
 */
export function useSaveSpendCap() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SaveSpendCapInput): Promise<SpendCapRow> => {
      const { data: auth } = await supabase.auth.getUser();
      const { data, error } = await supabase
        .from('spend_caps')
        .upsert(
          {
            provider: input.provider.trim(),
            model: (input.model ?? '').trim(),
            daily_limit_usd: input.dailyLimitUsd,
            monthly_limit_usd: input.monthlyLimitUsd,
            is_active: input.isActive ?? true,
            note: input.note?.trim() || null,
            updated_by: auth.user?.id ?? null,
          },
          { onConflict: 'provider,model' },
        )
        .select()
        .single();
      if (error) throw toError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: spendKeys.all });
      // A cap changing changes which styles Gate 1 will accept.
      void queryClient.invalidateQueries({ queryKey: queueKeys.stylePresets() });
    },
  });
}

/**
 * Remove a ceiling entirely.
 *
 * Switching one off is usually the better move — `is_active` keeps the numbers
 * on record — so this is for a cap on a model that no longer exists, where the
 * row is just clutter that nothing can ever bind on.
 */
export function useRemoveSpendCap() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ provider, model }: { provider: string; model: string }): Promise<void> => {
      const { error } = await supabase.from('spend_caps').delete().eq('provider', provider).eq('model', model);
      if (error) throw toError(error);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: spendKeys.all });
      void queryClient.invalidateQueries({ queryKey: queueKeys.stylePresets() });
    },
  });
}

/**
 * A CHECK constraint refusing a cap, said as a sentence.
 *
 * The same reasoning as `describeConstraintViolation` for the trend settings:
 * the bounds live in Postgres because this app ships as a static bundle and a
 * guard written here is one an old tab can skip. The cost is a `23514` naming
 * a constraint rather than a field, so this is the translation.
 */
export function describeCapViolation(error: unknown): string | null {
  if (codeOf(error) !== '23514') return null;
  const message = toError(error).message;
  const named = Object.entries(CAP_CONSTRAINT_SENTENCES).find(([constraint]) => message.includes(constraint));
  return named ? named[1] : 'One of these figures is outside the range the database will accept.';
}

const CAP_CONSTRAINT_SENTENCES: Record<string, string> = {
  spend_caps_needs_a_limit: 'A cap needs a daily figure, a monthly one, or both — otherwise it caps nothing.',
  spend_caps_daily_within_monthly: 'The daily figure has to be at most the monthly one, or it can never bind.',
  spend_caps_provider_check: 'A cap needs a provider.',
  render_spend_amount_usd_check: 'A charge cannot be negative.',
};

/**
 * The code `approve_idea` raises when the chosen style is at its ceiling.
 *
 * Gate 1 disables a capped style, but the button being disabled is a courtesy:
 * a cap can be reached between the page loading and the click, and it is
 * Postgres that refuses. `program_limit_exceeded` is distinguishable from the
 * function's other refusals — a viewer (42501), an unknown preset (22023), an
 * idea that is no longer pending (P0002) — so the message can name the cap
 * rather than saying "the database refused".
 */
export const SPEND_CAP_REFUSED = '54000';

export function isSpendCapRefusal(error: unknown): boolean {
  return codeOf(error) === SPEND_CAP_REFUSED;
}
