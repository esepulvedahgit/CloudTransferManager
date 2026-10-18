import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import TransferFileReport from './TransferFileReport';
import type { TransferManifestPage, TransferRecord } from './transferTypes';

const transfer: TransferRecord = {
  id: 'transfer-1',
  public_id: 'TRF-2026-000013',
  status: 'FAILED',
  destination_connection_id: 'destination',
  destination_path: '/Archive',
  progress_percent: 89,
  attempt_count: 2,
  files_total: 3,
  files_completed: 2,
  bytes_total: 110,
  bytes_transferred: 100,
  current_item: null,
  average_speed_bps: null,
  current_speed_bps: null,
  manifest_summary: {
    copied: 1,
    failed: 1,
    skipped: 1,
    pending: 1,
    omitted: 1,
    omitted_bytes: 10,
  },
};

const page: TransferManifestPage = {
  items: [
    {
      id: 'failed-file',
      source_path: '/Curso Pimsleur.rar',
      destination_path: '/Archive/Curso Pimsleur.rar',
      requested_destination_path: '/Archive/Curso Pimsleur.rar',
      rename_reason: null,
      is_dir: false,
      size_bytes: 10,
      status: 'FAILED',
      error_code: 'SOURCE_DOWNLOAD_FAILED',
      verified_by: null,
      can_skip: true,
      skip_unavailable_reason: null,
      can_retry_omission: false,
      omission_kind: null,
      omitted_at: null,
    },
    {
      id: 'explicit-omission',
      source_path: '/restricted.rar',
      destination_path: '/Archive/restricted.rar',
      requested_destination_path: '/Archive/restricted.rar',
      rename_reason: null,
      is_dir: false,
      size_bytes: 10,
      status: 'SKIPPED',
      error_code: 'SOURCE_FILE_RESTRICTED',
      verified_by: null,
      can_skip: false,
      skip_unavailable_reason: null,
      can_retry_omission: true,
      omission_kind: 'USER_ACCEPTED_SOURCE_FAILURE',
      omitted_at: '2026-10-18T14:30:00Z',
    },
    {
      id: 'historical-skip',
      source_path: '/legacy-skip.bin',
      destination_path: '/Archive/legacy-skip.bin',
      requested_destination_path: '/Archive/legacy-skip.bin',
      rename_reason: null,
      is_dir: false,
      size_bytes: 0,
      status: 'SKIPPED',
      error_code: null,
      verified_by: null,
      can_skip: false,
      skip_unavailable_reason: null,
      can_retry_omission: false,
      omission_kind: null,
      omitted_at: null,
    },
  ],
  next_cursor: 'next-page',
  counts: { total: 3, pending: 0, copied: 1, skipped: 2, failed: 0, omitted: 1, omitted_bytes: 10 },
};

function renderReport(overrides: Partial<React.ComponentProps<typeof TransferFileReport>> = {}) {
  const props = {
    transfer,
    pages: [page],
    isLoading: false,
    isError: false,
    hasNextPage: true,
    isFetchingNextPage: false,
    onLoadMore: vi.fn(),
    onSkipAndResume: vi.fn().mockResolvedValue(undefined),
    onRetryOmitted: vi.fn().mockResolvedValue(undefined),
    onDownloadOmissions: vi.fn(),
    onConflictRefresh: vi.fn(),
    ...overrides,
  };
  render(<TransferFileReport {...props} />);
  return props;
}

describe('TransferFileReport', () => {
  afterEach(cleanup);

  it('preserves the assigned source and destination paths and paginated loading', () => {
    const props = renderReport();

    expect(screen.getByText('/Curso Pimsleur.rar')).toBeVisible();
    expect(screen.getByText('/Archive/Curso Pimsleur.rar')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /Cargar más resultados/i }));
    expect(props.onLoadMore).toHaveBeenCalledOnce();
  });

  it('shows global copied and failed counts instead of only the loaded page', () => {
    renderReport({
      transfer: {
        ...transfer,
        manifest_summary: {
          ...transfer.manifest_summary!,
          copied: 4316,
          failed: 17,
        },
      },
    });

    expect(screen.getByText(/4316 copiados · 1 omitidos · 17 con error/i)).toBeVisible();
  });

  it('requires the exact confirmation before skipping and continuing', async () => {
    const onSkipAndResume = vi.fn().mockResolvedValue(undefined);
    renderReport({ onSkipAndResume });

    const failedRow = screen.getByTestId('manifest-row-failed-file');
    fireEvent.click(within(failedRow).getByRole('button', { name: 'Omitir este archivo y continuar' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText(
      'Este archivo no se copiará. Los archivos ya copiados se conservarán y la transferencia continuará con los restantes',
    )).toBeVisible();
    expect(within(dialog).getByText('/Curso Pimsleur.rar')).toBeVisible();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirmar omisión' }));

    await waitFor(() => expect(onSkipAndResume).toHaveBeenCalledOnce());
    expect(onSkipAndResume.mock.calls[0][0]).toBe('failed-file');
    expect(onSkipAndResume.mock.calls[0][1]).toMatch(/[0-9a-f-]{36}/i);
  });

  it('shows persistent omission warning and retries only selected explicit omissions', async () => {
    const onRetryOmitted = vi.fn().mockResolvedValue(undefined);
    const onDownloadOmissions = vi.fn();
    renderReport({ onRetryOmitted, onDownloadOmissions });

    expect(screen.getByText(/1 archivo omitido · 10 bytes/i)).toBeVisible();
    expect(screen.getByRole('button', { name: /Descargar CSV de omisiones/i })).toBeVisible();
    expect(screen.queryByRole('checkbox', { name: /legacy-skip/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: /restricted.rar/i }));
    const retry = screen.getByRole('button', { name: /Reintentar seleccionados/i });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);

    await waitFor(() => expect(onRetryOmitted).toHaveBeenCalledOnce());
    expect(onRetryOmitted.mock.calls[0][0]).toEqual(['explicit-omission']);
    expect(onRetryOmitted.mock.calls[0][1]).toMatch(/[0-9a-f-]{36}/i);
    fireEvent.click(screen.getByRole('button', { name: /Descargar CSV de omisiones/i }));
    expect(onDownloadOmissions).toHaveBeenCalledOnce();
  });

  it('keeps zero-byte omissions visible as warnings and downloadable rows', () => {
    const zeroByteTransfer = {
      ...transfer,
      status: 'RUNNING',
      manifest_summary: { ...transfer.manifest_summary!, omitted_bytes: 0 },
    };
    const zeroBytePage: TransferManifestPage = {
      ...page,
      items: page.items.map((item) => item.id === 'explicit-omission' ? { ...item, size_bytes: 0 } : item),
      counts: { ...page.counts, omitted_bytes: 0 },
    };
    renderReport({ transfer: zeroByteTransfer, pages: [zeroBytePage] });

    expect(screen.getByText('Archivos omitidos durante la transferencia')).toBeVisible();
    expect(screen.getByText(/1 archivo omitido · 0 bytes/i)).toBeVisible();
    expect(screen.getByRole('button', { name: /Descargar CSV de omisiones/i })).toBeVisible();
  });

  it('refreshes stale state after a conflict and reuses request id after a network failure', async () => {
    const request = vi.fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockRejectedValueOnce(Object.assign(new Error('attempt changed'), { status: 409 }));
    const onConflictRefresh = vi.fn();
    renderReport({ onRetryOmitted: request, onConflictRefresh });
    fireEvent.click(screen.getByRole('checkbox', { name: /restricted.rar/i }));
    const retry = screen.getByRole('button', { name: /Reintentar seleccionados/i });
    fireEvent.click(retry);
    await screen.findByRole('alert');
    fireEvent.click(retry);

    await waitFor(() => expect(onConflictRefresh).toHaveBeenCalledOnce());
    expect(request.mock.calls[0][1]).toBe(request.mock.calls[1][1]);
  });

  it('disables repeated actions while the request is pending', async () => {
    let resolveAction: (() => void) | undefined;
    const onRetryOmitted = vi.fn(() => new Promise<void>((resolve) => { resolveAction = resolve; }));
    renderReport({ onRetryOmitted });
    fireEvent.click(screen.getByRole('checkbox', { name: /restricted.rar/i }));
    const retry = screen.getByRole('button', { name: /Reintentar seleccionados/i });
    fireEvent.click(retry);

    await waitFor(() => expect(onRetryOmitted).toHaveBeenCalledOnce());
    expect(retry).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /restricted.rar/i })).toBeDisabled();
    await act(async () => resolveAction?.());
    await waitFor(() => expect(screen.queryByRole('button', { name: /Reintentar seleccionados/i })).not.toBeInTheDocument());
  });
});
