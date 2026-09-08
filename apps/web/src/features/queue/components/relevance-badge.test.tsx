import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { fitOf, RelevanceBadge } from '@/features/queue/components/relevance-badge';

describe('fit', () => {
  it('reads 0–100 into three words, at 75 and 50', () => {
    expect(fitOf(100)).toBe('strong');
    expect(fitOf(75)).toBe('strong');
    expect(fitOf(74)).toBe('good');
    expect(fitOf(50)).toBe('good');
    expect(fitOf(49)).toBe('loose');
    expect(fitOf(0)).toBe('loose');
  });

  it('renders nothing for an idea that was never scored', () => {
    // An ordinary run's ideas were not judged against anything, and an empty
    // badge would say they were.
    const { container } = render(<RelevanceBadge relevance={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the words and the number', () => {
    render(<RelevanceBadge relevance={62} />);
    expect(screen.getByText(/good fit/i)).toBeInTheDocument();
    expect(screen.getByText('62')).toBeInTheDocument();
  });
});
