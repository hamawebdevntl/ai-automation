import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { QcReportCard } from '@/features/queue/components/qc-report-card';
import type { QcReport } from '@/lib/database.types';

const PASSING: QcReport = {
  passed: true,
  slideshow_risk: 0.12,
  checks: [
    { key: 'file_integrity', label: 'File integrity', status: 'pass', detail: 'h264 1080x1920' },
    { key: 'audio_levels', label: 'Audio levels', status: 'pass', detail: '-16.2 LUFS' },
  ],
};

const FAILING: QcReport = {
  passed: false,
  slideshow_risk: 0.81,
  checks: [
    { key: 'file_integrity', label: 'File integrity', status: 'pass' },
    { key: 'audio_levels', label: 'Audio levels', status: 'warn', detail: 'quiet for mobile' },
    { key: 'slideshow_risk', label: 'Slideshow risk', status: 'fail', detail: '8 of 10 clips are static' },
  ],
};

describe('QcReportCard', () => {
  it('says plainly that nothing has been checked yet', () => {
    render(<QcReportCard report={null} />);
    expect(screen.getByText(/has not been through the quality check/i)).toBeInTheDocument();
  });

  it('lists each check with its detail', () => {
    render(<QcReportCard report={PASSING} />);

    expect(screen.getByText('passed')).toBeInTheDocument();
    expect(screen.getByText('File integrity')).toBeInTheDocument();
    expect(screen.getByText('-16.2 LUFS')).toBeInTheDocument();
    expect(screen.getByText('0.12')).toBeInTheDocument();
  });

  it('warns before someone approves a cut the pipeline believes is broken', () => {
    render(<QcReportCard report={FAILING} />);

    expect(screen.getByText('failed')).toBeInTheDocument();
    expect(screen.getByText('One check failed')).toBeInTheDocument();
    expect(screen.getByText(/prefer rejecting with a note/i)).toBeInTheDocument();
  });

  it('is explicit that these checks say nothing about whether the claims are true', () => {
    render(<QcReportCard report={PASSING} />);
    expect(screen.getByText(/say nothing about whether the claims are true/i)).toBeInTheDocument();
  });
});
