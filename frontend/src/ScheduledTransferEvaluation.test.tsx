import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ScheduledTransferEvaluation, { type TransferEvaluation } from './ScheduledTransferEvaluation';

const base: TransferEvaluation = {
  status: 'PENDING',
  generation: 1,
  sequence: 0,
  files_found: null,
  count_complete: false,
  max_files: 5000,
  started_at: null,
  updated_at: null,
  completed_at: null,
  safe_error_code: null,
  safe_message: null,
};

describe('ScheduledTransferEvaluation', () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('does not present an unknown pending count as zero', () => {
    render(<ScheduledTransferEvaluation evaluation={base} onRetry={vi.fn()} retrying={false} />);

    expect(screen.getByText(/cantidad todavía desconocida/i)).toBeVisible();
    expect(screen.queryByText(/0 archivos/i)).not.toBeInTheDocument();
  });

  it('labels transfers without an early evaluation', () => {
    render(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'NOT_REQUIRED', generation: 0 }}
        onRetry={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByText(/Sin evaluación anticipada · se validará al ejecutar/i)).toBeVisible();
  });

  it('shows partial and completed file counts with the limit', () => {
    const { rerender } = render(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'EVALUATING', files_found: 3200, started_at: '2026-10-17T10:00:00Z' }}
        onRetry={vi.fn()}
        retrying={false}
      />,
    );
    expect(screen.getByText(/3\.200 archivos encontrados/i)).toBeVisible();

    rerender(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'APPROVED', files_found: 4180, count_complete: true, completed_at: '2026-10-17T10:10:00Z' }}
        onRetry={vi.fn()}
        retrying={false}
      />,
    );
    expect(screen.getByText(/4\.180 archivos · conteo completo/i)).toBeVisible();
    expect(screen.getByText(/Límite 5\.000/i)).toBeVisible();
  });

  it('explains blocked, empty, failed and parent-waiting evaluations', () => {
    const onRetry = vi.fn();
    const { rerender } = render(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'BLOCKED', files_found: 5001, safe_message: 'Divide la selección.' }}
        onRetry={onRetry}
        retrying={false}
      />,
    );
    expect(screen.getByText(/al menos 5\.001 archivos · límite 5\.000/i)).toBeVisible();
    expect(screen.getByText('Divide la selección.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Volver a evaluar' })).toBeEnabled();

    rerender(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'APPROVED', files_found: 0, count_complete: true }}
        onRetry={onRetry}
        retrying={false}
      />,
    );
    expect(screen.getByText(/0 archivos · conteo completo/i)).toBeVisible();

    rerender(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'ERROR', safe_message: 'No responde el proveedor.' }}
        onRetry={onRetry}
        retrying
      />,
    );
    expect(screen.getByText(/No se pudo evaluar/i)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Solicitando…' })).toBeDisabled();

    rerender(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'WAITING_PARENT' }}
        onRetry={onRetry}
        retrying={false}
      />,
    );
    expect(screen.getByText(/Pendiente de evaluar tras la tarea padre/i)).toBeVisible();
  });

  it('shows a delay notice after sixty seconds without claiming a completion time', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-17T10:02:00Z'));
    render(
      <ScheduledTransferEvaluation
        evaluation={{ ...base, status: 'EVALUATING', files_found: 45, started_at: '2026-10-17T10:00:00Z' }}
        onRetry={vi.fn()}
        retrying={false}
      />,
    );

    expect(screen.getByText(/La revisión continúa; el tiempo depende de las carpetas y del proveedor/i)).toBeVisible();
  });
});
