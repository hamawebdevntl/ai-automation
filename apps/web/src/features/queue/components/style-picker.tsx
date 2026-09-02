import { Clock3Icon, CoinsIcon } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { useOwner } from '@/features/auth/use-owner';
import { RENDER_MODE_LABELS, type StylePresetRow } from '@/lib/database.types';
import { formatCostRange, formatMinutes } from '@/lib/format';
import { cn } from '@/lib/utils';

/**
 * Gate 1 is the last moment before money is spent, which is the whole reason
 * the cost and ETA sit next to the choice rather than behind it.
 */
export function StylePicker({
  presets,
  value,
  onChange,
  disabled,
}: {
  presets: StylePresetRow[];
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
        return (
          <Label
            key={preset.id}
            htmlFor={`style-${preset.id}`}
            className={cn(
              'flex cursor-pointer items-start gap-3 rounded-lg border p-4 transition-colors',
              'hover:bg-accent/50 has-disabled:cursor-not-allowed has-disabled:opacity-60',
              selected && 'border-primary bg-accent/40 ring-1 ring-primary/30',
            )}
          >
            <RadioGroupItem value={preset.id} id={`style-${preset.id}`} className="mt-1" />
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
              </div>
              {preset.description && <p className="text-sm font-normal text-muted-foreground">{preset.description}</p>}
              <div className="flex flex-wrap items-center gap-4 pt-1 text-sm">
                <span className="inline-flex items-center gap-1.5 font-medium tabular-nums">
                  <CoinsIcon className="size-3.5 text-muted-foreground" aria-hidden />
                  {formatCostRange(preset)}
                </span>
                <span className="inline-flex items-center gap-1.5 font-normal text-muted-foreground tabular-nums">
                  <Clock3Icon className="size-3.5" aria-hidden />
                  {formatMinutes(preset.est_minutes)}
                </span>
              </div>
            </div>
          </Label>
        );
      })}
    </RadioGroup>
  );
}
