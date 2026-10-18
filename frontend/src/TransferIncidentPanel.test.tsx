import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import TransferIncidentPanel from './TransferIncidentPanel';

const incident = {
  transfer: {
    id: 'transfer-1',
    public_id: 'TRF-2026-000013',
    status: 'FAILED',
    attempt_count: 3,
    source_connection: { name: 'MEGA principal', provider_type: 'MEGA' },
    destination_connection: { name: 'Seafile cursos', provider_type: 'SEAFILE' },
  },
  item: {
    id: 'item-1',
    source_path: '/Cursos/Idiomas/Curso Pimsleur.rar',
    destination_path: '/Backup/Idiomas/Curso Pimsleur.rar',
    requested_destination_path: '/Backup/Idiomas/Curso Pimsleur.rar',
    status: 'FAILED',
    size_bytes: 686289042,
    error_code: 'MEGACMD_COMMAND_FAILED',
    failure_stage: 'SOURCE_DOWNLOAD',
    omission_kind: null,
    omitted_at: null,
    incident_at: '2026-10-18T12:00:00Z',
  },
  can_skip: true,
  skip_unavailable_reason: null,
  can_retry_omission: false,
};

function renderPanel(onSkip = vi.fn()) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <TransferIncidentPanel onSkip={onSkip} onRetry={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe('TransferIncidentPanel', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows complete routes outside history and confirms the fast skip action', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
      items: [incident],
      counts: { failed: 1, omitted: 0 },
      next_cursor: null,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    const onSkip = vi.fn().mockResolvedValue(undefined);
    renderPanel(onSkip);

    expect(await screen.findByText('/Cursos/Idiomas/Curso Pimsleur.rar')).toBeInTheDocument();
    expect(screen.getByText('/Backup/Idiomas/Curso Pimsleur.rar')).toBeInTheDocument();
    expect(screen.getByText('MEGA principal → Seafile cursos')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Omitir y continuar' }));
    expect(await screen.findByText('Confirmar omisión del archivo')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirmar omisión' }));

    await waitFor(() => expect(onSkip).toHaveBeenCalledWith('transfer-1', 'item-1', 3, expect.any(String)));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/transfer-incidents?kind=failed'),
      expect.objectContaining({ method: 'GET' }),
    );
  });

  it('keeps pagination independent when switching between error and omission tabs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('kind=failed')) {
        return new Response(JSON.stringify({ items: [incident], counts: { failed: 2, omitted: 1 }, next_cursor: 'next-error' }), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({
        items: [{
          ...incident,
          transfer: { ...incident.transfer, status: 'COMPLETED_WITH_WARNINGS' },
          item: { ...incident.item, status: 'SKIPPED', omission_kind: 'USER_ACCEPTED_SOURCE_FAILURE', omitted_at: '2026-10-18T12:00:00Z' },
          can_skip: false,
          can_retry_omission: true,
        }],
        counts: { failed: 2, omitted: 1 },
        next_cursor: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
    renderPanel();

    const errorPanel = await screen.findByText('/Cursos/Idiomas/Curso Pimsleur.rar');
    const errorSection = errorPanel.closest('[data-testid="incident-panel"]');
    expect(errorSection).not.toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: /Omitidos/ }));
    expect(await screen.findByText(/USER_ACCEPTED_SOURCE_FAILURE/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: /Con error/ }));
    expect(await screen.findByRole('button', { name: 'Cargar más incidencias' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cargar más incidencias' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('cursor=next-error'),
      expect.anything(),
    ));
    expect(within(errorSection as HTMLElement).getByText('TRF-2026-000013')).toBeInTheDocument();
    expect(within(errorSection as HTMLElement).getAllByText('/Cursos/Idiomas/Curso Pimsleur.rar')).toHaveLength(1);
  });
});
