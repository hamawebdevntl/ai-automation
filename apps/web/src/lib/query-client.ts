import { QueryClient } from '@tanstack/react-query';

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The queue is a shared work list — two people may be looking at it at
        // once, so stale data is worse here than an extra request.
        staleTime: 15_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          // A row-level-security refusal will never succeed on retry.
          const code = (error as { code?: string } | null)?.code;
          if (code === '42501' || code === 'PGRST301') return false;
          return failureCount < 2;
        },
      },
      mutations: {
        retry: 0,
      },
    },
  });
}
