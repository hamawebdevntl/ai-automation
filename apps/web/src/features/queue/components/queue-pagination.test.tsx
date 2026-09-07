import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

// The controls are router links; the router itself is not what is under test.
vi.mock('@tanstack/react-router', () => ({
  Link: ({ children, to, search, ...rest }: { children: React.ReactNode; to: string; search?: { page?: number } }) => (
    <a href={`${to}?page=${search?.page}`} {...rest}>
      {children}
    </a>
  ),
}));

const { QueuePagination, pageWindow } = await import('@/features/queue/components/queue-pagination');

describe('pageWindow', () => {
  it('shows every page when they all fit', () => {
    expect(pageWindow(1, 3)).toEqual([1, 2, 3]);
  });

  it('keeps a constant width rather than shrinking at the ends', () => {
    // A strip that changes width as you page through it makes the Next button
    // move under the pointer.
    expect(pageWindow(1, 20)).toHaveLength(5);
    expect(pageWindow(20, 20)).toHaveLength(5);
    expect(pageWindow(10, 20)).toHaveLength(5);
  });

  it('centres on the current page in the middle of a long list', () => {
    expect(pageWindow(10, 20)).toEqual([8, 9, 10, 11, 12]);
  });

  it('runs forwards at the start and backwards at the end', () => {
    expect(pageWindow(1, 20)).toEqual([1, 2, 3, 4, 5]);
    expect(pageWindow(20, 20)).toEqual([16, 17, 18, 19, 20]);
  });

  it('never invents a page', () => {
    for (const total of [1, 2, 7, 13]) {
      for (let page = 1; page <= total; page++) {
        const window = pageWindow(page, total);
        expect(Math.min(...window)).toBeGreaterThanOrEqual(1);
        expect(Math.max(...window)).toBeLessThanOrEqual(total);
        expect(window).toContain(page);
      }
    }
  });
});

describe('QueuePagination', () => {
  it('renders nothing when there is only one page', () => {
    const { container } = render(<QueuePagination page={1} pageCount={1} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('states the position in words for small screens', () => {
    render(<QueuePagination page={2} pageCount={5} />);
    expect(screen.getByText('Page 2 of 5')).toBeInTheDocument();
  });

  it('marks the current page for assistive technology', () => {
    render(<QueuePagination page={3} pageCount={5} />);
    expect(screen.getByLabelText('Page 3')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByLabelText('Page 2')).not.toHaveAttribute('aria-current');
  });

  it('does not link past either end', () => {
    // Rendered as a span rather than an anchor, so there is nothing to click
    // and nothing for the keyboard to land on.
    const first = render(<QueuePagination page={1} pageCount={5} />);
    expect(first.container.querySelector('a[href*="page=0"]')).toBeNull();
    first.unmount();

    const last = render(<QueuePagination page={5} pageCount={5} />);
    expect(last.container.querySelector('a[href*="page=6"]')).toBeNull();
  });

  it('links to the neighbouring pages from the middle', () => {
    const { container } = render(<QueuePagination page={3} pageCount={5} />);
    expect(container.querySelector('a[href="/queue?page=2"]')).not.toBeNull();
    expect(container.querySelector('a[href="/queue?page=4"]')).not.toBeNull();
  });
});
