import type { AnyFieldApi } from '@tanstack/react-form';
import type { Label as LabelPrimitive } from 'radix-ui';
import { Slot } from 'radix-ui';
import * as React from 'react';
import { Label } from '@/components/ui/label';
import { cn } from '@/lib/utils';

/**
 * shadcn/ui's form primitives, rebuilt on TanStack Form.
 *
 * The upstream version wraps react-hook-form's `FormProvider` / `Controller`.
 * TanStack Form has no ambient provider — the field API is handed to a render
 * function — so `FormField` takes that field object and republishes it through
 * context, which is what `FormLabel` / `FormControl` / `FormMessage` read for
 * their ids and error state. Usage:
 *
 *   <Form onSubmit={form.handleSubmit}>
 *     <form.Field name="email">
 *       {(field) => (
 *         <FormField field={field}>
 *           <FormItem>
 *             <FormLabel>Email</FormLabel>
 *             <FormControl>
 *               <Input
 *                 value={field.state.value}
 *                 onBlur={field.handleBlur}
 *                 onChange={(event) => field.handleChange(event.target.value)}
 *               />
 *             </FormControl>
 *             <FormMessage />
 *           </FormItem>
 *         </FormField>
 *       )}
 *     </form.Field>
 *   </Form>
 *
 * Field names stay type-checked because `form.Field` is TanStack's own
 * component — the typing lives there, not here.
 */

type FormProps = Omit<React.ComponentProps<'form'>, 'onSubmit'> & {
  /** Usually `form.handleSubmit`. Called after the default submit is cancelled. */
  onSubmit?: () => undefined | Promise<unknown>;
};

function Form({ onSubmit, ...props }: FormProps) {
  return (
    <form
      data-slot="form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        event.stopPropagation();
        void onSubmit?.();
      }}
      {...props}
    />
  );
}

const FormFieldContext = React.createContext<AnyFieldApi | null>(null);

type FormItemContextValue = { id: string };

const FormItemContext = React.createContext<FormItemContextValue | null>(null);

function FormField({ field, children }: { field: AnyFieldApi; children: React.ReactNode }) {
  return <FormFieldContext value={field}>{children}</FormFieldContext>;
}

/** TanStack surfaces standard-schema issues as objects; validators may return plain strings. */
function toMessage(error: unknown): string {
  if (error === null || error === undefined) return '';
  if (typeof error === 'string') return error;
  if (typeof error === 'object' && 'message' in error) return String((error as { message: unknown }).message);
  return String(error);
}

const useFormField = () => {
  const field = React.use(FormFieldContext);
  const item = React.use(FormItemContext);

  if (!field) throw new Error('useFormField should be used within <FormField>');
  if (!item) throw new Error('useFormField should be used within <FormItem>');

  const meta = field.state.meta;
  // Errors exist from the first keystroke; showing them before the field has
  // been touched just shouts at someone who has not typed anything yet.
  const messages = meta.isTouched ? meta.errors.map(toMessage).filter(Boolean) : [];

  return {
    id: item.id,
    name: field.name,
    error: messages[0],
    errors: messages,
    isTouched: meta.isTouched,
    isValidating: meta.isValidating,
    formItemId: `${item.id}-form-item`,
    formDescriptionId: `${item.id}-form-item-description`,
    formMessageId: `${item.id}-form-item-message`,
  };
};

function FormItem({ className, ...props }: React.ComponentProps<'div'>) {
  const id = React.useId();

  return (
    <FormItemContext value={{ id }}>
      <div data-slot="form-item" className={cn('grid gap-2', className)} {...props} />
    </FormItemContext>
  );
}

function FormLabel({ className, ...props }: React.ComponentProps<typeof LabelPrimitive.Root>) {
  const { error, formItemId } = useFormField();

  return (
    <Label
      data-slot="form-label"
      data-error={!!error}
      className={cn('data-[error=true]:text-destructive', className)}
      htmlFor={formItemId}
      {...props}
    />
  );
}

function FormControl({ ...props }: React.ComponentProps<typeof Slot.Root>) {
  const { error, formItemId, formDescriptionId, formMessageId } = useFormField();

  return (
    <Slot.Root
      data-slot="form-control"
      id={formItemId}
      aria-describedby={error ? `${formDescriptionId} ${formMessageId}` : formDescriptionId}
      aria-invalid={!!error}
      {...props}
    />
  );
}

function FormDescription({ className, ...props }: React.ComponentProps<'p'>) {
  const { formDescriptionId } = useFormField();

  return (
    <p
      data-slot="form-description"
      id={formDescriptionId}
      className={cn('text-muted-foreground text-sm', className)}
      {...props}
    />
  );
}

function FormMessage({ className, ...props }: React.ComponentProps<'p'>) {
  const { error, formMessageId } = useFormField();
  const body = error || props.children;

  if (!body) return null;

  return (
    <p data-slot="form-message" id={formMessageId} className={cn('text-destructive text-sm', className)} {...props}>
      {body}
    </p>
  );
}

export { Form, FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage, useFormField };
