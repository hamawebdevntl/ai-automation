import { Link } from '@tanstack/react-router';
import { Badge } from '@/components/ui/badge';
import { diagnoseRun, formatStageValue, settingLabel } from '@/features/trends/controls';
import type { TrendRejectionStage, TrendRunRow } from '@/lib/database.types';
import { formatMinutes } from '@/lib/format';

/**
 * What a run actually did, stage by stage.
 *
 * The point of this table is the zeroes. A breakdown listing only the filters
 * that fired reads as an accusation and invites loosening whichever one is
 * named; listing all of them reads as a funnel, and shows the owner that the
 * filter they were about to loosen was not the one rejecting anything.
 *
 * The video stages sum to `Seen`, which is why they are indented under it and
 * why the duplicate stage — counted in ideas, not videos — is separated out
 * below rather than added to the column.
 */
export function RunBreakdown({ run }: { run: TrendRunRow }) {
  const report = run.rejections;
  if (!report) return null;

  const videoStages = report.stages.filter((s) => s.level === 'video');
  const ideaStages = report.stages.filter((s) => s.level === 'idea');
  const diagnosis = diagnoseRun(run);

  return (
    <div className="space-y-3 text-sm">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[22rem] border-collapse text-left">
          <tbody>
            <tr className="border-b">
              <th scope="row" className="py-1.5 pr-4 font-medium">
                Videos seen
              </th>
              <td className="py-1.5 pr-4 text-right tabular-nums">{report.seen.toLocaleString()}</td>
              <td className="text-muted-foreground py-1.5 text-xs">before any filter</td>
            </tr>

            {videoStages.map((stage) => (
              <StageRow key={stage.key} stage={stage} highlight={diagnosis.culprit?.key === stage.key} />
            ))}

            <tr className="border-t">
              <th scope="row" className="py-1.5 pr-4 font-medium">
                Reached drafting
              </th>
              <td className="py-1.5 pr-4 text-right tabular-nums">{report.surfaced.toLocaleString()}</td>
              <td className="text-muted-foreground py-1.5 text-xs">signals worth surfacing</td>
            </tr>

            <tr>
              <th scope="row" className="text-muted-foreground py-1.5 pr-4 font-normal">
                Ideas drafted
              </th>
              <td className="text-muted-foreground py-1.5 pr-4 text-right tabular-nums">{report.drafted}</td>
              <td />
            </tr>

            {ideaStages.map((stage) => (
              <StageRow key={stage.key} stage={stage} highlight={diagnosis.culprit?.key === stage.key} />
            ))}

            <tr className="border-t">
              <th scope="row" className="py-1.5 pr-4 font-medium">
                Added to the queue
              </th>
              <td className="py-1.5 pr-4 text-right tabular-nums">{report.inserted}</td>
              <td />
            </tr>
          </tbody>
        </table>
      </div>

      <ul className="text-muted-foreground space-y-1 text-xs">
        <li>
          Scouted {report.hashtags_scouted.length} hashtag
          {report.hashtags_scouted.length === 1 ? '' : 's'}
          {report.hashtags_configured && report.hashtags_configured > report.hashtags_scouted.length ? (
            <> of {report.hashtags_configured}, rotating</>
          ) : null}
          {report.hashtags_scouted.length > 0 && <>: {report.hashtags_scouted.map((t) => `#${t}`).join(', ')}</>}
        </li>

        {report.budget_exhausted && (
          <li>
            The run hit its time budget and stopped early
            {report.hashtags_skipped > 0 && <>, leaving {report.hashtags_skipped} hashtag(s) unscouted</>}. Ideas were
            drafted from what it had found by then.
          </li>
        )}

        {report.failed_hashtags.length > 0 && (
          <li className="text-destructive">
            {report.failed_hashtags.length} feed{report.failed_hashtags.length === 1 ? '' : 's'} raised an error:{' '}
            {report.failed_hashtags.map((f) => `#${f.hashtag}`).join(', ')}. That is usually the session being blocked
            rather than a bad hashtag — the delay between requests is under{' '}
            <Link to="/settings" className="underline underline-offset-4">
              Run length and pacing
            </Link>
            .
          </li>
        )}

        {report.seen > 0 && report.inserted === 0 && (
          <li>
            {report.seen.toLocaleString()} videos were fetched, so the scraper is working — this is the filters, not a
            broken run.
          </li>
        )}
      </ul>
    </div>
  );
}

function StageRow({ stage, highlight }: { stage: TrendRejectionStage; highlight: boolean }) {
  const value = formatStageValue(stage);
  return (
    <tr className={highlight ? 'bg-muted/50' : undefined}>
      <th scope="row" className="text-muted-foreground py-1.5 pr-4 pl-3 font-normal">
        {stage.label}
      </th>
      <td className="py-1.5 pr-4 text-right tabular-nums">
        {stage.dropped ? `−${stage.dropped.toLocaleString()}` : 0}
      </td>
      <td className="text-muted-foreground py-1.5 text-xs">
        {stage.setting ? (
          <>
            {settingLabel(stage.setting)}
            {value ? `: ${value}` : ''}
          </>
        ) : (
          'not a setting'
        )}
      </td>
    </tr>
  );
}

/** A one-line verdict, for places with no room for the table. */
export function RunVerdict({ run }: { run: TrendRunRow }) {
  const diagnosis = diagnoseRun(run);
  return (
    <div className="space-y-1">
      <p className="font-medium">{diagnosis.headline}</p>
      <p className="text-muted-foreground text-xs">{diagnosis.detail}</p>
    </div>
  );
}

/** Where the run came from and how long it took, for the run history. */
export function RunOrigin({ run }: { run: TrendRunRow }) {
  const minutes =
    run.started_at && run.finished_at
      ? Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 60000)
      : null;
  return (
    <span className="flex flex-wrap items-center gap-2">
      <Badge variant="secondary" className="font-normal">
        {run.trigger === 'schedule' ? 'Scheduled' : 'Started by hand'}
      </Badge>
      {minutes !== null && <span className="text-muted-foreground text-xs">took {formatMinutes(minutes)}</span>}
    </span>
  );
}
