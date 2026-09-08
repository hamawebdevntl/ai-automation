import { useQuery } from '@tanstack/react-query';
import { createFileRoute } from '@tanstack/react-router';
import { useOwner } from '@/features/auth/use-owner';
import { clipSourcesQueryOptions } from '@/features/clips/api';
import { SourceCard } from '@/features/clips/components/source-card';
import { UploadRecordingCard } from '@/features/clips/components/upload-recording-card';
import { PageHeader } from '@/features/queue/components/page-header';
import { EmptyQueue, ListSkeleton, QueryError } from '@/features/queue/components/query-state';

export const Route = createFileRoute('/_app/clips')({
  loader: ({ context }) => context.queryClient.ensureQueryData(clipSourcesQueryOptions()),
  component: ClipsPage,
});

/**
 * The clip gate.
 *
 * A third gate, and the argument for it is the one the script gate made: it
 * costs a transcription and one LLM call and no video generation, so discarding
 * a proposed clip is free in a way that discarding a finished cut is not. Ten
 * candidates reviewed here would have been ten renders to find the same answer.
 */
function ClipsPage() {
  const { data: sources, isPending, error } = useQuery(clipSourcesQueryOptions());
  const { isOwner } = useOwner();

  const waiting = (sources ?? []).filter((s) => s.status === 'awaiting_picks').length;

  return (
    <div className="mx-auto w-full max-w-4xl space-y-6">
      <PageHeader
        title="Clips · From your recordings"
        description="Upload something long, and decide which moments are worth making. The transcript and the shortlist are paid for once per recording, not per clip — so discarding one costs nothing."
        count={waiting || undefined}
      />

      <UploadRecordingCard canUpload={isOwner} />

      {error && <QueryError error={error} />}
      {isPending && <ListSkeleton />}

      {sources && sources.length === 0 && (
        <EmptyQueue
          title="No recordings yet"
          description="Upload a talk, a webinar or a call above and the clips worth cutting will appear here."
        />
      )}

      {sources && sources.length > 0 && (
        <ul className="space-y-4">
          {sources.map((source) => (
            <li key={source.id}>
              <SourceCard source={source} canDecide={isOwner} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
