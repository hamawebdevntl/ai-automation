import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { queueKeys } from '@/features/queue/api';
import type { HeyGenCatalogueRow, HeyGenLookRow, HeyGenVoiceRow, StylePresetRow } from '@/lib/database.types';
import { isCatalogueRefreshing } from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import { toError } from '@/lib/supabase-error';

export const presenterKeys = {
  all: ['presenter'] as const,
  looks: () => [...presenterKeys.all, 'looks'] as const,
  voices: () => [...presenterKeys.all, 'voices'] as const,
  catalogue: () => [...presenterKeys.all, 'catalogue'] as const,
};

/**
 * How often to look again while the worker owes an answer.
 *
 * The sweep runs every minute and does nothing on almost every tick; a request
 * from here is what makes it act. There is no row-filtered subscription that
 * would help — three tables change together — so this polls, and only while
 * something is actually in flight.
 */
const REFRESHING_POLL_MS = 3000;

/**
 * The bundle and the database are deployed separately, so a build can reach a
 * schema that has not had this migration applied yet. `42P01` is the missing
 * table, and the honest answer to "which looks can this account use?" against a
 * schema that cannot hold one is "none cached" — which the picker already says
 * well — rather than a red banner over a settings page that otherwise works.
 * The same fallback `trends/api.ts` makes for `trend_settings`.
 */
function isMissingSchema(code: string | undefined): boolean {
  return code === '42P01' || code === '42703';
}

export function looksQueryOptions() {
  return queryOptions({
    queryKey: presenterKeys.looks(),
    queryFn: async (): Promise<HeyGenLookRow[]> => {
      const { data, error } = await supabase.from('heygen_looks').select('*');
      if (error) {
        if (isMissingSchema(error.code)) return [];
        throw toError(error);
      }
      return data ?? [];
    },
  });
}

export function voicesQueryOptions() {
  return queryOptions({
    queryKey: presenterKeys.voices(),
    queryFn: async (): Promise<HeyGenVoiceRow[]> => {
      const { data, error } = await supabase
        .from('heygen_voices')
        .select('*')
        .order('requested_at', { ascending: false });
      if (error) {
        if (isMissingSchema(error.code)) return [];
        throw toError(error);
      }
      return data ?? [];
    },
  });
}

/**
 * The sync row, polled only while it says something is happening.
 *
 * `maybeSingle` rather than `single`: a deployment whose migration has not been
 * pushed has no row, and the picker's own empty state says so far better than
 * a thrown error would.
 */
export function catalogueQueryOptions() {
  return queryOptions({
    queryKey: presenterKeys.catalogue(),
    queryFn: async (): Promise<HeyGenCatalogueRow | null> => {
      const { data, error } = await supabase.from('heygen_catalogue').select('*').limit(1).maybeSingle();
      if (error) {
        if (isMissingSchema(error.code)) return null;
        throw toError(error);
      }
      return data;
    },
    refetchInterval: (query) => (isCatalogueRefreshing(query.state.data ?? null) ? REFRESHING_POLL_MS : false),
  });
}

/** Ask the worker to refill the caches from the live account. */
export function useRefreshCatalogue() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<HeyGenCatalogueRow> => {
      const { data, error } = await supabase.rpc('request_heygen_catalogue_refresh', {});
      if (error) throw toError(error);
      return data;
    },
    onSuccess: (row) => {
      queryClient.setQueryData(presenterKeys.catalogue(), row);
      // Not the looks and voices: they change when the worker answers, and
      // invalidating now would refetch the stale ones it has not replaced yet.
      // The catalogue poll above is what brings them back.
    },
  });
}

/**
 * Add a voice id for the worker to resolve.
 *
 * The voice list is not a mirror of `GET /v3/voices` — 3,089 entries over 62
 * pages, and not containing the cloned voice this account actually uses — so a
 * voice enters the picker by being asked about.
 */
export function useRequestVoice() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (voiceId: string): Promise<HeyGenVoiceRow> => {
      const { data, error } = await supabase.rpc('request_heygen_voice', { p_voice_id: voiceId.trim() });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: presenterKeys.voices() });
      // The function nudges the sweep, so the sync row has changed too and the
      // poll that shows "checking…" hangs off it.
      void queryClient.invalidateQueries({ queryKey: presenterKeys.catalogue() });
    },
  });
}

/**
 * Refetch the caches whenever a refresh finishes.
 *
 * Only the sync row polls, and only while it says something is happening — so
 * without this the moment it stops is the moment nothing looks again, and the
 * empty state's promise that "this page updates on its own" is not kept. There
 * is no realtime subscription to lean on: three tables change together, and the
 * one row that says when they did is already being watched.
 */
export function useCatalogueSync(refreshedAt: string | null | undefined) {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!refreshedAt) return;
    void queryClient.invalidateQueries({ queryKey: presenterKeys.looks() });
    void queryClient.invalidateQueries({ queryKey: presenterKeys.voices() });
  }, [refreshedAt, queryClient]);
}

export interface SetPresetPresenterInput {
  presetId: string;
  avatarId: string;
  voiceId: string;
  /** Null lets HeyGen pick, which is Avatar IV. */
  engine?: string | null;
}

/** Write the pair the presenter lane uses by default. */
export function useSetPresetPresenter() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ presetId, avatarId, voiceId, engine }: SetPresetPresenterInput): Promise<StylePresetRow> => {
      const { data, error } = await supabase.rpc('set_preset_presenter', {
        p_preset_id: presetId,
        p_avatar_id: avatarId,
        p_voice_id: voiceId,
        p_engine: engine ?? null,
      });
      if (error) throw toError(error);
      return data;
    },
    onSuccess: () => {
      // The preset list is what Gate 1 reads to offer the default, so it must
      // not keep serving the pair this call replaced.
      void queryClient.invalidateQueries({ queryKey: queueKeys.stylePresets() });
    },
  });
}
