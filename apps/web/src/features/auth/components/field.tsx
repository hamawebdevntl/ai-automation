import type { AnyFieldApi } from '@tanstack/react-form';
import { FormControl, FormDescription, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form';
import { Input } from '@/components/ui/input';

/**
 * A single-line text field wired to a TanStack Form field. Enough of a wrapper
 * to keep the auth screens short without hiding the field API.
 */
export function TextField({
  field,
  label,
  description,
  type = 'text',
  autoComplete,
  placeholder,
  disabled,
}: {
  field: AnyFieldApi;
  label: string;
  description?: string;
  type?: 'text' | 'email' | 'password';
  autoComplete?: string;
  placeholder?: string;
  disabled?: boolean;
}) {
  return (
    <FormField field={field}>
      <FormItem>
        <FormLabel>{label}</FormLabel>
        <FormControl>
          <Input
            type={type}
            name={field.name}
            value={(field.state.value as string | undefined) ?? ''}
            autoComplete={autoComplete}
            placeholder={placeholder}
            disabled={disabled}
            onBlur={field.handleBlur}
            onChange={(event) => field.handleChange(event.target.value)}
          />
        </FormControl>
        {description && <FormDescription>{description}</FormDescription>}
        <FormMessage />
      </FormItem>
    </FormField>
  );
}
