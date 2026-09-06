import type { ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { type BoundedField, boundsOf, isWithinBounds } from '@/features/trends/controls';

/**
 * The input, its unit, and the range it is held to.
 *
 * The range is shown rather than merely enforced. These values are refused by
 * a CHECK constraint in Postgres, not by this bundle, so an owner who learns
 * the limit only by being rejected has been handed a puzzle instead of a form.
 * `aria-invalid` marks a value the database would refuse; the card's Save
 * button is what actually blocks on it.
 */
function BoundedInput({
  field,
  value,
  onChange,
  disabled,
  suffix,
}: {
  field: BoundedField;
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  suffix?: string;
}) {
  const bound = boundsOf(field);
  const unit = suffix ?? bound.unit;
  const invalid = !isWithinBounds(field, value);

  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2">
        <Input
          id={field}
          type="number"
          inputMode="decimal"
          className="w-28"
          min={bound.min}
          max={bound.max}
          step={bound.step}
          value={Number.isFinite(value) ? value : ''}
          aria-invalid={invalid}
          aria-describedby={`${field}-range`}
          disabled={disabled}
          onChange={(e) => onChange(e.target.valueAsNumber)}
        />
        {unit && <span className="text-muted-foreground text-sm">{unit}</span>}
      </div>
      <p id={`${field}-range`} className={invalid ? 'text-destructive text-xs' : 'text-muted-foreground text-xs'}>
        {invalid ? 'Outside the allowed range: ' : 'Allowed: '}
        {bound.min}–{bound.max}
        {unit ? ` ${unit}` : ''}
      </p>
    </div>
  );
}

/** One numeric setting: a label, an explanation, and a bounded input. */
export function NumberSetting({
  field,
  label,
  help,
  value,
  onChange,
  disabled,
  suffix,
}: {
  field: BoundedField;
  label: string;
  help?: ReactNode;
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  /** Overrides the unit from the bounds table, for a field shown in other units. */
  suffix?: string;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-start sm:gap-4">
      <div className="space-y-1">
        <Label htmlFor={field}>{label}</Label>
        {help && <p className="text-muted-foreground text-xs">{help}</p>}
      </div>
      <BoundedInput field={field} value={value} onChange={onChange} disabled={disabled} suffix={suffix} />
    </div>
  );
}

/**
 * A setting that can be switched off entirely, where off is null.
 *
 * Both fields using this -- rotation and the run budget -- default to off,
 * because off is what the pipeline did before they existed. A zero would not
 * do: zero tags per run and a zero-minute budget both mean "never scout
 * anything", which is not what switching a feature off should mean.
 */
export function OptionalNumberSetting({
  field,
  label,
  offLabel,
  help,
  value,
  fallback,
  onChange,
  disabled,
}: {
  field: BoundedField;
  label: string;
  /** What null means here, said in words. */
  offLabel: string;
  help?: ReactNode;
  value: number | null;
  /** What to show when it is switched on for the first time. */
  fallback: number;
  onChange: (value: number | null) => void;
  disabled?: boolean;
}) {
  const on = value !== null;
  return (
    <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-start sm:gap-4">
      <div className="space-y-1">
        <Label htmlFor={on ? field : `${field}-toggle`}>{label}</Label>
        {help && <p className="text-muted-foreground text-xs">{help}</p>}
        {!on && <p className="text-muted-foreground text-xs">{offLabel}</p>}
      </div>
      <div className="flex items-start gap-3">
        {on && <BoundedInput field={field} value={value} onChange={onChange} disabled={disabled} />}
        <Switch
          id={`${field}-toggle`}
          aria-label={label}
          checked={on}
          disabled={disabled}
          onCheckedChange={(next) => onChange(next ? fallback : null)}
        />
      </div>
    </div>
  );
}

/** Save, and the reason it is unavailable. Shared by every settings card. */
export function SaveRow({
  isOwner,
  dirty,
  isSaving,
  blocked,
  onSave,
}: {
  isOwner: boolean;
  dirty: boolean;
  isSaving: boolean;
  /** A value the database would refuse. Named so the button is never mutely dead. */
  blocked?: string | null;
  onSave: () => void;
}) {
  if (!isOwner) {
    return (
      <p className="text-muted-foreground text-xs">
        Viewers can read these. Only an owner can change them — enforced by row-level security, not by this page.
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Button type="button" onClick={onSave} disabled={!dirty || isSaving || Boolean(blocked)}>
        {isSaving ? 'Saving…' : 'Save'}
      </Button>
      {blocked ? (
        <span className="text-destructive text-xs">{blocked}</span>
      ) : (
        dirty && <span className="text-muted-foreground text-xs">Unsaved changes</span>
      )}
    </div>
  );
}
