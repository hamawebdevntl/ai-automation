import { queryOptions } from '@tanstack/react-query';
import type { ProfileRow } from '@/lib/database.types';
import { supabase } from '@/lib/supabase';

/**
 * The signed-in user's profile row, which is where their role lives. Both
 * gates are owner-only; a viewer sees the queue but no decision controls.
 */
export function profileQueryOptions(userId: string | undefined) {
  return queryOptions({
    queryKey: ['profile', userId] as const,
    enabled: Boolean(userId),
    // A role change is rare and arrives via a fresh login anyway.
    staleTime: 5 * 60_000,
    queryFn: async (): Promise<ProfileRow | null> => {
      if (!userId) return null;
      const { data, error } = await supabase.from('profiles').select('*').eq('id', userId).maybeSingle();
      if (error) throw error;
      return data;
    },
  });
}
