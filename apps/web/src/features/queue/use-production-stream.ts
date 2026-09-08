import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import type { ProductionEventRow, ProductionRow } from '@/lib/database.types';
import { supabase } from '@/lib/supabase';
import type { ProductionWithContext } from './api';
import { queueKeys } from './api';

/**
 * Live updates for a production, over Supabase Realtime.
 *
 * These are the first subscriptions in this app. Everything else here polls,
 * and for the trend runner that is the right shape -- one run at a time, a
 * fifteen-second interval, and a page nobody watches for long. A production is
 * different: the whole point of this screen is that somebody is watching it
 * advance, and a poll interval short enough to feel live is a request every
 * second or two for as long as the tab is open.
 *
 * Row-level security applies to a subscription exactly as it does to a query,
 * so this grants nothing the browser could not already select.
 *
 * The safety net if a socket never connects is the app-wide
 * `refetchOnWindowFocus`, which is on by default in `createQueryClient` -- a
 * missed update is corrected the moment the tab is looked at again. That is
 * deliberately not a second polling path: two mechanisms that both write the
 * cache are two mechanisms to keep in step.
 */
export function useProductionStream(productionId: string | null | undefined) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!productionId) return;

    const channel = supabase
      .channel(`production:${productionId}`)
      .on(
        'postgres_changes',
        { event: 'UPDATE', schema: 'public', table: 'productions', filter: `id=eq.${productionId}` },
        (payload) => {
          const production = payload.new as ProductionRow;

          // Written straight into the cache rather than invalidated: the
          // payload is the whole row -- `replica identity full` is set on
          // `productions` for exactly this -- so a refetch would ask for what
          // has already arrived.
          queryClient.setQueryData(queueKeys.production(productionId), (previous: ProductionWithContext | undefined) =>
            previous ? { ...previous, production } : previous,
          );
          // An uploaded cut has no idea, so there is no per-idea cache entry
          // for it to freshen.
          if (production.idea_id) {
            queryClient.setQueryData(queueKeys.productionForIdea(production.idea_id), production);
          }

          // The lists carry their own joined context, so they are refetched
          // rather than patched -- and a status change is what moves a
          // production between them.
          void queryClient.invalidateQueries({ queryKey: queueKeys.live() });
        },
      )
      .on(
        'postgres_changes',
        {
          event: 'INSERT',
          schema: 'public',
          table: 'production_events',
          filter: `production_id=eq.${productionId}`,
        },
        (payload) => {
          const event = payload.new as ProductionEventRow;
          queryClient.setQueryData(queueKeys.events(productionId), (previous: ProductionEventRow[] | undefined) => {
            if (!previous) return previous;
            // The log is append-only and read oldest-first, but a reconnect can
            // redeliver, so an id already held is dropped rather than doubled.
            if (previous.some((existing) => existing.id === event.id)) return previous;
            return [...previous, event];
          });
        },
      )
      .subscribe();

    return () => {
      void supabase.removeChannel(channel);
    };
  }, [productionId, queryClient]);
}

/**
 * Live updates for the "in production" list on the queue page.
 *
 * Table-wide, because the point is to notice a production the viewer does not
 * yet know about -- one that has just been opened, or has just finished and
 * should leave the list.
 */
export function useLiveProductionsStream() {
  const queryClient = useQueryClient();

  useEffect(() => {
    const channel = supabase
      .channel('productions:live')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'productions' }, () => {
        void queryClient.invalidateQueries({ queryKey: queueKeys.live() });
      })
      .subscribe();

    return () => {
      void supabase.removeChannel(channel);
    };
  }, [queryClient]);
}
