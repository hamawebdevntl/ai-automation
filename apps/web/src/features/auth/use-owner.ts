import { useQuery } from '@tanstack/react-query';
import { useAuth } from '@/features/auth/auth-context';
import { profileQueryOptions } from '@/features/auth/queries';

export interface OwnerState {
  isOwner: boolean;
  isLoading: boolean;
  role: 'owner' | 'viewer' | null;
  displayName: string | null;
}

/**
 * Whether the signed-in user may pass a gate.
 *
 * This is a *presentation* check — it decides whether the approve and reject
 * controls are shown. The rule is actually enforced in Postgres: the gate
 * functions raise `42501` for anyone who is not an owner, so a viewer who
 * calls one anyway gets refused by the database, not by this hook.
 */
export function useOwner(): OwnerState {
  const { user } = useAuth();
  const { data, isPending } = useQuery(profileQueryOptions(user?.id));

  return {
    isOwner: data?.role === 'owner',
    isLoading: Boolean(user) && isPending,
    role: data?.role ?? null,
    displayName: data?.display_name ?? user?.email ?? null,
  };
}
