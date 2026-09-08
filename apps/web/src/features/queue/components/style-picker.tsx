import { BanIcon, Clock3Icon, CoinsIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { useOwner } from '@/features/auth/use-owner';
import { spendForPreset } from '@/features/spend/api';
import { RENDER_MODE_LABELS, type StylePresetRow, type StylePresetSpendRow } from '@/lib/database.types';
import { formatCostRange, formatMeasuredCost, formatMinutes } from '@/lib/format';
import { cn } from '@/lib/utils';

/**
 * Gate 1 is the last moment before money is spent, which is the whole reason
 * the cost and ETA sit next to the choice rather than behind it.
 *
 * A style whose provider or model is at its spend ceiling is not offered at
 * all, and says which cap was hit and when it comes back. That refusal is not
 * enforced here — `approve_idea` raises on it, because a cap can be reached
 * between this page loading and the click, and because this bundle is static
 * and an old tab would happily offer a style Postgres has stopped allowing.
 * What this does is make the refusal legible before it happens rather than
 * after.
 */
export function StylePicker({
  presets,
  spend,
  value,
  onChange,
  disabled,
}: {
  presets: StylePresetRow[];
  /** From `style_preset_spend`. Absent while the query is in flight, in which
   *  case nothing is marked capped — Postgres is still the one refusing. */
  spend?: StylePresetSpendRow[];
  value: string | null;
  onChange: (styleId: string) => void;
  disabled?: boolean;
}) {
  // Which engine runs is operational detail rather than an editorial choice,
  // so it is shown only to the people who can actually act on it. `video_source`
  // alone is ambiguous now that two presets share the value "fal" and differ in
  // how much of the render it does.
  const { isOwner } = useOwner();

  return (
    <RadioGroup
      value={value ?? ''}
      onValueChange={onChange}
      disabled={disabled}
      className="gap-3"
      aria-label="Production style"
    >
      {presets.map((preset) => {
        const selected = preset.id === value;
        const presetSpend = spendForPreset(spend, preset.id);
        const blocked = presetSpend?.block_reason ?? null;
        const measured = formatMeasuredCost(presetSpend);
        return (
          <Label
            key={preset.id}
            htmlFor={`style-${preset.id}`}
            className={cn(
              'flex items-start gap-3 rounded-lg border p-4 transition-colors',
              'has-disabled:cursor-not-allowed has-disabled:opacity-60',
              blocked ? 'border-destructive/40' : 'cursor-pointer hover:bg-accent/50',
              selected && 'border-primary bg-accent/40 ring-1 ring-primary/30',
            )}
          >
            <RadioGroupItem
              value={preset.id}
              id={`style-${preset.id}`}
              className="mt-1"
              disabled={Boolean(blocked)}
              aria-describedby={blocked ? `style-${preset.id}-blocked` : undefined}
            />
            <div className="flex-1 space-y-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium leading-none">{preset.name}</span>
                <Badge variant="secondary" className="text-[0.7rem] font-normal">
                  {preset.video_source}
                </Badge>
                {isOwner && preset.render_mode !== 'mpt' && (
                  <Badge variant="outline" className="text-[0.7rem] font-normal">
                    {RENDER_MODE_LABELS[preset.render_mode]}
                  </Badge>
                )}
                {blocked && (
                  <Badge variant="destructive" className="gap-1 text-[0.7rem] font-normal">
                    <BanIcon className="size-3" aria-hidden />
                    Capped
                  </Badge>
                )}
              </div>
              {preset.description && <p className="text-sm font-normal text-muted-foreground">{preset.description}</p>}
              <div className="flex flex-wrap items-center gap-4 pt-1 text-sm">
                <span className="inline-flex items-center gap-1.5 font-medium tabular-nums">
                  <CoinsIcon className="size-3.5 text-muted-foreground" aria-hidden />
                  {/* What renders in this style have been billed, once any
                      have. The preset's own range is an estimate — the
                      presenter lane's $1–2 was never a measurement — so a real
                      average beats it. */}
                  {measured ?? formatCostRange(preset)}
                </span>
                <span className="inline-flex items-center gap-1.5 font-normal text-muted-foreground tabular-nums">
                  <Clock3Icon className="size-3.5" aria-hidden />
                  {formatMinutes(preset.est_minutes)}
                </span>
              </div>
              {measured && (
                <p className="text-xs font-normal text-muted-foreground">
                  What renders in this style were actually billed, not an estimate. The estimate was{' '}
                  {formatCostRange(preset)}.
                </p>
              )}
              {blocked && (
                <p id={`style-${preset.id}-blocked`} className="pt-1 text-sm text-destructive">
                  {blocked}
                </p>
              )}
            </div>
          </Label>
        );
      })}
    </RadioGroup>
  );
}
