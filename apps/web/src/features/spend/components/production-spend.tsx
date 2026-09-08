import { useQuery } from '@tanstack/react-query';
import { Badge } from '@/components/ui/badge';
import { productionSpendQueryOptions } from '@/features/spend/api';
import type { RenderSpendRow, SpendKind } from '@/lib/database.types';
import { formatUsd } from '@/lib/format';

const KIND_LABELS: Record<SpendKind, string> = {
  render: 'Render',
  tts: 'Narration',
  transcribe: 'Transcription',
  source: 'Footage',
};

/** "heygen", or "fal / fal-ai/ltx-2.3" when the model is what was priced. */
function chargeLabel(row: RenderSpendRow): string {
  const what = KIND_LABELS[row.kind] ?? row.kind;
  return row.model ? `${what} · ${row.provider} / ${row.model}` : `${what} · ${row.provider}`;
}

/**
 * What this production was billed, charge by charge.
 *
 * `cost_actual_usd` on the row is the total and is what a list shows. This is
 * the breakdown, which is the useful thing when the question is *why* it cost
 * that: a fal end-to-end reel bills twice — the video model and the TTS model —
 * so one figure hides which half was expensive.
 *
 * `source` is shown rather than smoothed over. "$1.40, measured" and "$1.40, we
 * think" are different claims, and a derived figure that could pass for a
 * measured one would make the whole ledger untrustworthy.
 */
export function ProductionSpend({ productionId }: { productionId: string }) {
  const { data, isPending, error } = useQuery(productionSpendQueryOptions(productionId));
  const rows = data ?? [];

  if (isPending || error || rows.length === 0) {
    // Nothing recorded yet is the normal state before a render is submitted,
    // and it is not worth a row of its own: the estimate above already says
    // what the style is expected to cost.
    return null;
  }

  const total = rows.reduce((sum, row) => sum + Number(row.amount_usd), 0);

  return (
    <div className="space-y-1.5 border-t pt-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Billed</span>
        <span className="text-sm font-medium tabular-nums">{formatUsd(total)}</span>
      </div>
      <ul className="space-y-1">
        {rows.map((row) => (
          <li key={row.id} className="flex flex-wrap items-baseline justify-between gap-2 text-xs">
            <span className="flex flex-wrap items-center gap-1.5 text-muted-foreground">
              {chargeLabel(row)}
              <Badge variant={row.source === 'reported' ? 'secondary' : 'outline'} className="font-normal">
                {row.source === 'reported' ? 'measured' : 'estimated'}
              </Badge>
            </span>
            <span className="tabular-nums">{formatUsd(Number(row.amount_usd))}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
