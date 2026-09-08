import { useQuery } from '@tanstack/react-query';
import { PlusIcon, TrashIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { Progress } from '@/components/ui/progress';
import { Switch } from '@/components/ui/switch';
import { useOwner } from '@/features/auth/use-owner';
import {
  describeCapViolation,
  spendCapStatusQueryOptions,
  useRemoveSpendCap,
  useSaveSpendCap,
} from '@/features/spend/api';
import type { SpendCapStatusRow, SpendProvider } from '@/lib/database.types';
import { formatUsd } from '@/lib/format';

const PROVIDERS = ['heygen', 'fal', 'mpt'] as const satisfies readonly SpendProvider[];

/** What each provider is, for someone who has not read the pipeline. */
const PROVIDER_BLURB: Record<string, string> = {
  heygen: 'The presenter lane. Pay-as-you-go against a wallet.',
  fal: 'Generated footage, and narration on the end-to-end lane. Billed per second.',
  mpt: 'The standard render. Stock footage is free; a generative source is not.',
};

/** "the provider as a whole", or "provider / model". */
function capLabel(cap: Pick<SpendCapStatusRow, 'provider' | 'model'>): string {
  return cap.model ? `${cap.provider} / ${cap.model}` : cap.provider;
}

/**
 * When a window comes back, in UTC and saying so.
 *
 * Both caps are measured from a UTC boundary — deliberately, so that two people
 * looking at this page see the same figures and the pipeline's idea of "today"
 * does not move when a worker is deployed somewhere else. Rendering the reset
 * in the viewer's own zone would undo that, and would disagree with the
 * sentence `spend_block_reason()` shows at Gate 1, which names UTC outright.
 */
export function formatResetsAt(value: string): string {
  // Spelled out field by field rather than with `dateStyle`/`timeStyle`:
  // Intl refuses to combine either of those with `timeZoneName` and throws
  // `TypeError: Invalid option`, which would take the whole card down rather
  // than merely dropping the label.
  return new Date(value).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  });
}

/** A limit as a number for an input, where null means "no ceiling". */
function toInput(value: number | null): string {
  return value === null ? '' : String(value);
}

/** An input back to a limit. Empty is no ceiling; a non-number is left alone. */
function fromInput(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * One window of one cap: the ceiling, what is against it, and when it resets.
 *
 * The reset time is shown rather than implied. "Reached" is only useful
 * alongside "and it comes back at midnight UTC" — without that, an owner's only
 * move is to raise the ceiling, which is the wrong instinct when the answer is
 * to wait four hours.
 */
function Window({
  id,
  label,
  limit,
  spent,
  reached,
  resetsAt,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  limit: string;
  spent: number;
  reached: boolean;
  resetsAt: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const ceiling = fromInput(limit);
  const pct = ceiling && ceiling > 0 ? Math.min(100, (spent / ceiling) * 100) : 0;

  return (
    <div className="grid gap-2 sm:grid-cols-[7rem_9rem_1fr] sm:items-center sm:gap-3">
      <Label htmlFor={id} className="text-sm font-normal text-muted-foreground">
        {label}
      </Label>
      <div className="flex items-center gap-1.5">
        <span className="text-muted-foreground text-sm">$</span>
        <Input
          id={id}
          type="number"
          inputMode="decimal"
          min={0}
          step="0.01"
          className="w-28"
          placeholder="no ceiling"
          value={limit}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
      <div className="space-y-1">
        <div className="flex flex-wrap items-baseline gap-x-2 text-xs">
          <span className={reached ? 'font-medium text-destructive tabular-nums' : 'tabular-nums'}>
            {formatUsd(spent)} spent
          </span>
          <span className="text-muted-foreground">
            {ceiling === null ? 'no ceiling' : `of ${formatUsd(ceiling)}`} · resets {formatResetsAt(resetsAt)}
          </span>
        </div>
        {ceiling !== null && ceiling > 0 && (
          <Progress value={pct} className={reached ? '[&>*]:bg-destructive' : undefined} />
        )}
      </div>
    </div>
  );
}

function CapRow({ cap, isOwner }: { cap: SpendCapStatusRow; isOwner: boolean }) {
  const save = useSaveSpendCap();
  const remove = useRemoveSpendCap();
  const [daily, setDaily] = useState(() => toInput(cap.daily_limit_usd));
  const [monthly, setMonthly] = useState(() => toInput(cap.monthly_limit_usd));
  const [active, setActive] = useState(cap.is_active);

  const dailyLimit = fromInput(daily);
  const monthlyLimit = fromInput(monthly);
  const dirty =
    daily !== toInput(cap.daily_limit_usd) || monthly !== toInput(cap.monthly_limit_usd) || active !== cap.is_active;
  const busy = save.isPending || remove.isPending;
  const nothing = dailyLimit === null && monthlyLimit === null;
  const inverted = dailyLimit !== null && monthlyLimit !== null && dailyLimit > monthlyLimit;

  // Mirrored from the CHECK constraints rather than replacing them. Postgres
  // is what refuses; this is so the owner is told before clicking rather than
  // after, which is the same division the trend settings cards use.
  const blocked = nothing
    ? 'A cap needs a daily figure, a monthly one, or both.'
    : inverted
      ? 'The daily figure has to be at most the monthly one, or it can never bind.'
      : null;

  async function onSave() {
    try {
      await save.mutateAsync({
        provider: cap.provider,
        model: cap.model,
        dailyLimitUsd: dailyLimit,
        monthlyLimitUsd: monthlyLimit,
        isActive: active,
        note: cap.note,
      });
      toast.success(`Cap saved for ${capLabel(cap)}`);
    } catch (error) {
      toast.error(describeCapViolation(error) ?? (error instanceof Error ? error.message : 'Could not save the cap'));
    }
  }

  async function onRemove() {
    try {
      await remove.mutateAsync({ provider: cap.provider, model: cap.model });
      toast.success(`Cap removed for ${capLabel(cap)}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not remove the cap');
    }
  }

  const reached = cap.is_active && (cap.daily_reached || cap.monthly_reached);

  return (
    <div className="space-y-3 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="space-y-0.5">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{capLabel(cap)}</span>
            {reached && <Badge variant="destructive">Reached</Badge>}
            {!cap.is_active && <Badge variant="secondary">Off</Badge>}
            {!cap.model && <Badge variant="outline">every model</Badge>}
          </div>
          <p className="text-muted-foreground text-xs">{cap.note ?? PROVIDER_BLURB[cap.provider] ?? ''}</p>
        </div>
        <div className="flex items-center gap-2">
          <Label htmlFor={`active-${cap.provider}-${cap.model}`} className="text-muted-foreground text-xs">
            Enforced
          </Label>
          <Switch
            id={`active-${cap.provider}-${cap.model}`}
            checked={active}
            disabled={!isOwner || busy}
            onCheckedChange={setActive}
          />
        </div>
      </div>

      <Window
        id={`daily-${cap.provider}-${cap.model}`}
        label="Per day"
        limit={daily}
        spent={cap.day_spent_usd}
        reached={cap.is_active && cap.daily_reached}
        resetsAt={cap.daily_resets_at}
        disabled={!isOwner || busy}
        onChange={setDaily}
      />
      <Window
        id={`monthly-${cap.provider}-${cap.model}`}
        label="Per month"
        limit={monthly}
        spent={cap.month_spent_usd}
        reached={cap.is_active && cap.monthly_reached}
        resetsAt={cap.monthly_resets_at}
        disabled={!isOwner || busy}
        onChange={setMonthly}
      />

      {isOwner && (
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" size="sm" onClick={onSave} disabled={!dirty || busy || Boolean(blocked)}>
            {save.isPending ? 'Saving…' : 'Save'}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={onRemove} disabled={busy}>
            <TrashIcon className="size-3.5" />
            Remove
          </Button>
          {blocked ? (
            <span className="text-destructive text-xs">{blocked}</span>
          ) : (
            dirty && <span className="text-muted-foreground text-xs">Unsaved changes</span>
          )}
        </div>
      )}
    </div>
  );
}

function AddCap({ existing }: { existing: SpendCapStatusRow[] }) {
  const save = useSaveSpendCap();
  const [provider, setProvider] = useState<SpendProvider>('heygen');
  const [model, setModel] = useState('');
  const [daily, setDaily] = useState('');
  const [monthly, setMonthly] = useState('');
  const [open, setOpen] = useState(false);

  const duplicate = existing.some((cap) => cap.provider === provider && cap.model === model.trim());
  const nothing = fromInput(daily) === null && fromInput(monthly) === null;

  async function onAdd() {
    try {
      await save.mutateAsync({
        provider,
        model: model.trim(),
        dailyLimitUsd: fromInput(daily),
        monthlyLimitUsd: fromInput(monthly),
      });
      toast.success(`Cap added for ${model.trim() ? `${provider} / ${model.trim()}` : provider}`);
      setModel('');
      setDaily('');
      setMonthly('');
      setOpen(false);
    } catch (error) {
      toast.error(describeCapViolation(error) ?? (error instanceof Error ? error.message : 'Could not add the cap'));
    }
  }

  if (!open) {
    return (
      <Button type="button" variant="outline" size="sm" className="mt-4" onClick={() => setOpen(true)}>
        <PlusIcon className="size-3.5" />
        Add a cap
      </Button>
    );
  }

  return (
    <div className="mt-4 space-y-3 rounded-lg border p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="new-cap-provider">Provider</Label>
          <NativeSelect
            id="new-cap-provider"
            className="w-full"
            value={provider}
            onChange={(event) => setProvider(event.target.value as SpendProvider)}
          >
            {PROVIDERS.map((name) => (
              <NativeSelectOption key={name} value={name}>
                {name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-cap-model">Model</Label>
          <Input
            id="new-cap-model"
            value={model}
            placeholder="every model on this provider"
            onChange={(event) => setModel(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-cap-daily">Per day (USD)</Label>
          <Input
            id="new-cap-daily"
            type="number"
            min={0}
            step="0.01"
            placeholder="no ceiling"
            value={daily}
            onChange={(event) => setDaily(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-cap-monthly">Per month (USD)</Label>
          <Input
            id="new-cap-monthly"
            type="number"
            min={0}
            step="0.01"
            placeholder="no ceiling"
            value={monthly}
            onChange={(event) => setMonthly(event.target.value)}
          />
        </div>
      </div>
      <p className="text-muted-foreground text-xs">
        Leave the model blank to cap the provider as a whole, which counts every model on it. A model named here is the
        tighter cap and is the one reported when both are reached — so “fal at $30 a day, and the expensive model within
        that at $10” is two rows.
      </p>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" size="sm" onClick={onAdd} disabled={save.isPending || duplicate || nothing}>
          {save.isPending ? 'Adding…' : 'Add'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)} disabled={save.isPending}>
          Cancel
        </Button>
        {duplicate && <span className="text-destructive text-xs">That provider and model already has a cap.</span>}
        {!duplicate && nothing && (
          <span className="text-destructive text-xs">Give it a daily or a monthly figure.</span>
        )}
      </div>
    </div>
  );
}

/**
 * What the pipeline is allowed to spend, and what it has.
 *
 * Rows rather than fields, which is what makes a new model's ceiling something
 * an owner can add here rather than a migration. Both windows are shown
 * together on purpose: a monthly figure alone lets a runaway loop burn the
 * month's budget in an hour, and a daily figure alone lets thirty ordinary
 * days add up to a bill nobody agreed to.
 */
export function SpendCapsCard() {
  const { isOwner } = useOwner();
  const { data, isPending, error } = useQuery(spendCapStatusQueryOptions());
  const caps = data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Spend caps</CardTitle>
        <CardDescription>
          A ceiling in dollars per provider and per model, for the day and for the month. A style whose cap is spent
          cannot be chosen at Gate 1, and a production already in flight parks instead of billing.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {error && <p className="text-destructive text-sm">Could not load the caps: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {!isPending && caps.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No caps. Nothing limits what the pipeline can spend, which is the state this page exists to get out of.
          </p>
        )}

        <div className="divide-y">
          {caps.map((cap) => (
            <CapRow key={`${cap.provider}/${cap.model}/${cap.updated_at}`} cap={cap} isOwner={isOwner} />
          ))}
        </div>

        {isOwner ? (
          <AddCap existing={caps} />
        ) : (
          <p className="text-muted-foreground pt-4 text-xs">
            Viewers can read these. Only an owner can change them — enforced by row-level security, not by this page.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
