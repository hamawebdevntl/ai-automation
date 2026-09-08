import { useQuery } from '@tanstack/react-query';
import { PlusIcon, TrashIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect, NativeSelectOption } from '@/components/ui/native-select';
import { useOwner } from '@/features/auth/use-owner';
import { spendRatesQueryOptions, useRemoveSpendRate, useSaveSpendRate } from '@/features/spend/api';
import type { SpendProvider, SpendRateRow, SpendUnit } from '@/lib/database.types';

const PROVIDERS = ['heygen', 'fal', 'mpt'] as const satisfies readonly SpendProvider[];
const UNITS = ['second', 'character', 'clip', 'render'] as const satisfies readonly SpendUnit[];

/** What "per second" and friends mean, in the terms this pipeline bills in. */
const UNIT_BLURB: Record<SpendUnit, string> = {
  second: 'per second of output — how fal bills generation, and how a presenter render is priced.',
  character: 'per character of narration — how the end-to-end lane’s text-to-speech bills.',
  clip: 'per generated clip — a paid footage source fetched per request.',
  render: 'a flat figure per render, whatever its length. Free stock footage is this, at zero.',
};

function rateLabel(rate: Pick<SpendRateRow, 'provider' | 'model'>): string {
  return rate.model ? `${rate.provider} / ${rate.model}` : rate.provider;
}

function RateRow({ rate, isOwner }: { rate: SpendRateRow; isOwner: boolean }) {
  const save = useSaveSpendRate();
  const remove = useRemoveSpendRate();
  const [value, setValue] = useState(String(rate.rate_usd));
  const [unit, setUnit] = useState<SpendUnit>(rate.unit);

  const parsed = Number(value.trim());
  const valid = value.trim() !== '' && Number.isFinite(parsed) && parsed >= 0;
  const dirty = value !== String(rate.rate_usd) || unit !== rate.unit;
  const busy = save.isPending || remove.isPending;
  const id = `rate-${rate.provider}-${rate.model}`;

  async function onSave() {
    try {
      await save.mutateAsync({
        provider: rate.provider,
        model: rate.model,
        unit,
        rateUsd: parsed,
        note: rate.note,
      });
      toast.success(`Rate saved for ${rateLabel(rate)}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not save the rate');
    }
  }

  async function onRemove() {
    try {
      await remove.mutateAsync({ provider: rate.provider, model: rate.model });
      toast.success(`Rate removed for ${rateLabel(rate)}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not remove the rate');
    }
  }

  return (
    <div className="space-y-2 py-3">
      <div className="space-y-0.5">
        <Label htmlFor={id} className="font-medium">
          {rateLabel(rate)}
        </Label>
        {rate.note && <p className="text-muted-foreground text-xs">{rate.note}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-muted-foreground text-sm">$</span>
        <Input
          id={id}
          type="number"
          inputMode="decimal"
          min={0}
          step="0.000001"
          className="w-32"
          value={value}
          disabled={!isOwner || busy}
          onChange={(event) => setValue(event.target.value)}
        />
        <NativeSelect
          aria-label={`Unit for ${rateLabel(rate)}`}
          value={unit}
          disabled={!isOwner || busy}
          onChange={(event) => setUnit(event.target.value as SpendUnit)}
        >
          {UNITS.map((name) => (
            <NativeSelectOption key={name} value={name}>
              per {name}
            </NativeSelectOption>
          ))}
        </NativeSelect>
        {isOwner && (
          <>
            <Button type="button" size="sm" onClick={onSave} disabled={!dirty || !valid || busy}>
              {save.isPending ? 'Saving…' : 'Save'}
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={onRemove} disabled={busy}>
              <TrashIcon className="size-3.5" />
              Remove
            </Button>
          </>
        )}
      </div>
      {!valid && <p className="text-destructive text-xs">A rate has to be a number, and cannot be negative.</p>}
    </div>
  );
}

function AddRate({ existing }: { existing: SpendRateRow[] }) {
  const save = useSaveSpendRate();
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState<SpendProvider>('fal');
  const [model, setModel] = useState('');
  const [unit, setUnit] = useState<SpendUnit>('second');
  const [value, setValue] = useState('');

  const parsed = Number(value.trim());
  const valid = value.trim() !== '' && Number.isFinite(parsed) && parsed >= 0;
  const duplicate = existing.some((rate) => rate.provider === provider && rate.model === model.trim());

  async function onAdd() {
    try {
      await save.mutateAsync({ provider, model: model.trim(), unit, rateUsd: parsed });
      toast.success(`Rate added for ${model.trim() ? `${provider} / ${model.trim()}` : provider}`);
      setModel('');
      setValue('');
      setOpen(false);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not add the rate');
    }
  }

  if (!open) {
    return (
      <Button type="button" variant="outline" size="sm" className="mt-4" onClick={() => setOpen(true)}>
        <PlusIcon className="size-3.5" />
        Add a rate
      </Button>
    );
  }

  return (
    <div className="mt-4 space-y-3 rounded-lg border p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label htmlFor="new-rate-provider">Provider</Label>
          <NativeSelect
            id="new-rate-provider"
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
          <Label htmlFor="new-rate-model">Model</Label>
          <Input
            id="new-rate-model"
            value={model}
            placeholder="the provider as a whole"
            onChange={(event) => setModel(event.target.value)}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-rate-unit">Billed</Label>
          <NativeSelect
            id="new-rate-unit"
            className="w-full"
            value={unit}
            onChange={(event) => setUnit(event.target.value as SpendUnit)}
          >
            {UNITS.map((name) => (
              <NativeSelectOption key={name} value={name}>
                per {name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="new-rate-value">Rate (USD)</Label>
          <Input
            id="new-rate-value"
            type="number"
            min={0}
            step="0.000001"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </div>
      </div>
      <p className="text-muted-foreground text-xs">{UNIT_BLURB[unit]}</p>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" size="sm" onClick={onAdd} disabled={!valid || duplicate || save.isPending}>
          {save.isPending ? 'Adding…' : 'Add'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={() => setOpen(false)} disabled={save.isPending}>
          Cancel
        </Button>
        {duplicate && <span className="text-destructive text-xs">That provider and model already has a rate.</span>}
      </div>
    </div>
  );
}

/**
 * What each provider charges.
 *
 * Not decoration beside the caps, and not only for the sake of a tidier
 * estimate. A model with no rate here *cannot render*: the render step refuses
 * to submit one, because a cost it cannot compute is a cost that would spend
 * against a ceiling without moving it. The realistic way into that state is a
 * preset pointed at a model nobody has priced — a premium fal tier, say, which
 * the plan costs at $1,500–5,400 a month — so this list is the recovery path,
 * and the reason a parked production can say "add a rate in Settings" rather
 * than naming a table nobody can reach.
 *
 * HeyGen is the exception and can render without one: it reports a wallet, so a
 * presenter render is priced from the delta and the rate here is only the
 * fallback for when the balance cannot be read twice.
 */
export function SpendRatesCard() {
  const { isOwner } = useOwner();
  const { data, isPending, error } = useQuery(spendRatesQueryOptions());
  const rates = data ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Provider rates</CardTitle>
        <CardDescription>
          What a render costs, so it can be counted against a cap. A model with no rate here cannot be rendered at all —
          the pipeline parks rather than spending money it could not account for.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {error && <p className="text-destructive text-sm">Could not load the rates: {error.message}</p>}
        {isPending && <p className="text-muted-foreground text-sm">Loading…</p>}

        {!isPending && rates.length === 0 && (
          <p className="text-muted-foreground text-sm">
            No rates. Nothing but the presenter lane can render until at least one is added.
          </p>
        )}

        <div className="divide-y">
          {rates.map((rate) => (
            <RateRow key={`${rate.provider}/${rate.model}/${rate.updated_at}`} rate={rate} isOwner={isOwner} />
          ))}
        </div>

        {isOwner ? (
          <AddRate existing={rates} />
        ) : (
          <p className="text-muted-foreground pt-4 text-xs">
            Viewers can read these. Only an owner can change them — enforced by row-level security, not by this page.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
