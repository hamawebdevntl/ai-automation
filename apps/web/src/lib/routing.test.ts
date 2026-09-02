import { describe, expect, it } from 'vitest';
import { toRoutePath } from '@/lib/routing';

describe('toRoutePath', () => {
  it('passes through a relative in-app path', () => {
    expect(toRoutePath('/review/abc', '/queue')).toBe('/review/abc');
  });

  it('falls back when there is nothing to redirect to', () => {
    expect(toRoutePath(undefined, '/queue')).toBe('/queue');
    expect(toRoutePath(null, '/queue')).toBe('/queue');
    expect(toRoutePath('', '/queue')).toBe('/queue');
  });

  it('refuses a protocol-relative URL, which would be an open redirect', () => {
    expect(toRoutePath('//evil.example/phish', '/queue')).toBe('/queue');
  });

  it('refuses an absolute URL', () => {
    expect(toRoutePath('https://evil.example', '/queue')).toBe('/queue');
    expect(toRoutePath('javascript:alert(1)', '/queue')).toBe('/queue');
  });
});
