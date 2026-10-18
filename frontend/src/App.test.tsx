import { act, cleanup, fireEvent, render, screen, waitFor, waitForElementToBeRemoved, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { mergeTransferState, type Transfer } from './App';
import type { TransferEvaluation } from './ScheduledTransferEvaluation';

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function isTransferCollectionRequest(path: string) {
  return /^\/api\/v1\/transfers(?:\?.*)?$/.test(path);
}

class TestEventSource {
  static instances: TestEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  close = vi.fn();

  constructor(_url: string) {
    TestEventSource.instances.push(this);
  }

  emit(data: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }));
  }
}

async function signIn(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock);
  render(<App />);
  fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
  fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
  await screen.findByRole('heading', { name: /Explorador de archivos/i });
}

async function openAdministration() {
  fireEvent.click(await screen.findByRole('button', { name: /Menú de cuenta de/i }));
  fireEvent.click(await screen.findByRole('menuitem', { name: /Administración/i }));
}

describe('Cloud Transfer Manager', () => {
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    TestEventSource.instances = [];
    document.cookie = 'ctm_csrf=; Max-Age=0; path=/';
  });

  it('shows the sign-in form when there is no session', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"detail":"Autenticación requerida"}', { status: 401 })),
    );

    render(<App />);

    expect(await screen.findByRole('heading', { name: /Cloud Transfer Manager/i })).toBeVisible();
    expect(screen.getByLabelText(/Usuario o correo/i)).toBeVisible();
    expect(screen.getByLabelText(/Contraseña/i)).toBeVisible();
  });

  it('uses the connected-cloud brand mark on the sign-in screen', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"detail":"Autenticación requerida"}', { status: 401 })),
    );

    render(<App />);

    expect(await screen.findByRole('img', { name: 'Cloud Transfer Manager' })).toBeVisible();
  });

  it('shows recognizable logos for MEGA, Google Drive and Seafile on the sign-in page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"detail":"Autenticación requerida"}', { status: 401 })),
    );

    render(<App />);

    expect(await screen.findByRole('heading', { name: /Cloud Transfer Manager/i })).toBeVisible();
    expect(screen.getByRole('img', { name: 'MEGA' })).toBeVisible();
    expect(screen.getByRole('img', { name: 'Google Drive' })).toBeVisible();
    expect(screen.getByRole('img', { name: 'Seafile' })).toBeVisible();
  });

  it('shows the requested date and transfer duration in recent history', async () => {
    const transfer = {
      id: 'transfer-history-1',
      public_id: 'TRF-2026-000001',
      status: 'COMPLETED',
      progress_percent: 100,
      files_total: 1,
      files_completed: 1,
      bytes_total: 1024,
      bytes_transferred: 1024,
      current_item: null,
      average_speed_bps: 16,
      current_speed_bps: null,
      estimated_remaining_seconds: 0,
      source_connection_id: 'source',
      destination_connection_id: 'destination',
      source_paths: ['/report.csv'],
      destination_path: '/',
      conflict_policy: 'KEEP_BOTH',
      send_email_notification: false,
      created_at: '2026-10-11T10:15:00.000Z',
      started_at: '2026-10-11T10:15:10.000Z',
      completed_at: '2026-10-11T10:16:15.000Z',
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [transfer] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);

    expect(await screen.findByText('Duración: 1 min 5 s')).toBeVisible();
    const requestedAt = document.querySelector('time[datetime="2026-10-11T10:15:00.000Z"]');
    expect(requestedAt).toBeVisible();
    expect(requestedAt).toHaveTextContent(/2026/);
  });

  it('shows SMB manifest outcomes instead of labeling resolved files as copied', async () => {
    const transfer = {
      id: 'smb-history-1',
      public_id: 'TRF-2026-000205',
      status: 'COMPLETED_WITH_WARNINGS',
      progress_percent: 99.99,
      files_total: 6,
      files_completed: 4,
      bytes_total: 4096,
      bytes_transferred: 3072,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      source_connection_id: 'nas',
      destination_connection_id: 'cloud',
      source_paths: ['/STORAGE'],
      destination_path: '/Archive',
      manifest_summary: { copied: 2, failed: 1, skipped: 3, pending: 0 },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) {
        return jsonResponse({ items: [
          { id: 'nas', name: 'NAS local', provider_type: 'SMB' },
          { id: 'cloud', name: 'MEGA', provider_type: 'MEGA' },
        ] });
      }
      if (isTransferCollectionRequest(path)) {
        return path.includes('background_only=true') || path.includes('pending_only=true')
          ? jsonResponse({ items: [], total_count: 0 })
          : jsonResponse({ items: [transfer], total_count: 1 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);

    expect(await screen.findByText(/2 copiados · 0 omitidos · 1 con error · 3 SKIPPED históricos/)).toBeVisible();
    expect(screen.getByText('99%')).toBeVisible();
    expect(document.querySelector('[aria-label="Avance 99 por ciento"]')).toBeVisible();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.queryByText(/4 de 6 archivos/)).not.toBeInTheDocument();
  });

  it('shows KEEP_BOTH failures and numbered names separately', async () => {
    const transfer = {
      id: 'smb-history-1',
      public_id: 'TRF-2026-000205',
      status: 'FAILED',
      conflict_policy: 'KEEP_BOTH',
      warning_count: 99,
      progress_percent: 99.99,
      files_total: 6,
      files_completed: 4,
      bytes_total: 4096,
      bytes_transferred: 3072,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      source_connection_id: 'nas',
      destination_connection_id: 'cloud',
      source_paths: ['/STORAGE'],
      destination_path: '/Archive',
      manifest_summary: { copied: 2, failed: 1, skipped: 3, pending: 0, renamed: 7 },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) {
        return jsonResponse({ items: [
          { id: 'nas', name: 'NAS local', provider_type: 'SMB' },
          { id: 'cloud', name: 'MEGA', provider_type: 'MEGA' },
        ] });
      }
      if (isTransferCollectionRequest(path)) {
        return path.includes('background_only=true') || path.includes('pending_only=true')
          ? jsonResponse({ items: [], total_count: 0 })
          : jsonResponse({ items: [transfer], total_count: 1 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);

    expect(await screen.findByText(/2 copiados · 0 omitidos · 1 con error · 3 SKIPPED históricos/)).toBeVisible();
    expect(screen.getByText('99%')).toBeVisible();
    expect(document.querySelector('[aria-label="Avance 99 por ciento"]')).toBeVisible();
    expect(screen.getByText(/7 nombres numerados/)).toBeVisible();
    expect(screen.queryByText(/99 nombres numerados/)).not.toBeInTheDocument();
    expect(screen.queryByText('100%')).not.toBeInTheDocument();
    expect(screen.queryByText(/4 de 6 archivos/)).not.toBeInTheDocument();
  });

  it('keeps scheduled transfers out of history and shows a compact background transfer indicator', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const waiting = {
      id: 'scheduled-waiting',
      public_id: 'TRF-2026-000201',
      status: 'QUEUED',
      progress_percent: 0,
      files_completed: 0,
      files_total: 3,
      bytes_transferred: 0,
      bytes_total: 2048,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: null,
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: 'WAITING_CAPACITY',
    };
    const running = {
      ...waiting,
      id: 'scheduled-running',
      public_id: 'TRF-2026-000202',
      status: 'RUNNING',
      progress_percent: 50,
      bytes_transferred: 1024,
      scheduled_at: '2026-10-20T12:00:00Z',
      wait_reason: null,
    };
    const backgroundItems = [waiting, running];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) {
          return jsonResponse({ items: backgroundItems, total_count: backgroundItems.length });
        }
        if (path.includes('pending_only=true')) {
          return jsonResponse({ items: [waiting], total_count: 1 });
        }
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);

    expect(screen.queryByText(waiting.public_id)).not.toBeInTheDocument();
    expect(screen.queryByText(running.public_id)).not.toBeInTheDocument();
    expect(screen.queryByText(/TRANSFERENCIA ACTIVA/i)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/transfers?exclude_background=true'),
      expect.anything(),
    );
    expect(await screen.findByText('2')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /transferencias en segundo plano/i }));

    expect(await screen.findByText(waiting.public_id)).toBeVisible();
    expect(screen.getByText(running.public_id)).toBeVisible();
    expect(screen.getByText(/En cola: no hay un espacio de ejecución disponible/i)).toBeVisible();
    expect(screen.getByText(/1\.00 KiB de 2\.00 KiB/i)).toBeVisible();
    expect(screen.getByText('50%')).toBeVisible();
  });

  it('moves a completed background transfer to history when its SSE event arrives', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const running = {
      id: 'scheduled-running',
      public_id: 'TRF-2026-000203',
      status: 'RUNNING',
      progress_percent: 84,
      files_completed: 2,
      files_total: 3,
      bytes_transferred: 1024,
      bytes_total: 2048,
      current_item: '/NAS/report.csv',
      average_speed_bps: 1024,
      current_speed_bps: 1024,
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: '2026-10-20T12:00:00Z',
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: null,
    };
    const completed = { ...running, status: 'COMPLETED', progress_percent: 100 };
    let finished = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) {
          const items = finished ? [] : [running];
          return jsonResponse({ items, total_count: items.length });
        }
        if (path.includes('pending_only=true')) return jsonResponse({ items: [], total_count: 0 });
        const items = finished ? [completed] : [];
        return jsonResponse({ items, total_count: items.length });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await waitFor(() => expect(TestEventSource.instances.length).toBeGreaterThan(0));
    expect(screen.getByText('1')).toBeVisible();
    finished = true;
    TestEventSource.instances[0].emit(completed);

    expect(await screen.findByText(completed.public_id)).toBeVisible();
    await waitFor(() => expect(screen.getByRole('button', { name: /0 transferencias en segundo plano/i })).toBeVisible());
    expect(screen.queryByText(/TRANSFERENCIA ACTIVA/i)).not.toBeInTheDocument();
  });

  it('refreshes the scheduled transfer history after 15 seconds when SSE is missed', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const running = {
      id: 'scheduled-polling',
      public_id: 'TRF-2026-000204',
      status: 'RUNNING',
      progress_percent: 90,
      files_completed: 1,
      files_total: 1,
      bytes_transferred: 9,
      bytes_total: 10,
      current_item: '/NAS/report.csv',
      average_speed_bps: 1,
      current_speed_bps: 1,
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: '2026-10-20T12:00:00Z',
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: null,
    };
    const completed = { ...running, status: 'COMPLETED', progress_percent: 100 };
    let finished = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) {
          const items = finished ? [] : [running];
          return jsonResponse({ items, total_count: items.length });
        }
        if (path.includes('pending_only=true')) return jsonResponse({ items: [], total_count: 0 });
        const items = finished ? [completed] : [];
        return jsonResponse({ items, total_count: items.length });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    expect(screen.queryByText(completed.public_id)).not.toBeInTheDocument();
    finished = true;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(await screen.findByText(completed.public_id)).toBeVisible();
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('/api/v1/transfers?background_only=true')
    ))).toBe(true);
  });

  it('keeps cancellation available for a running scheduled transfer in Programaciones', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const running = {
      id: 'scheduled-running-cancellable',
      public_id: 'TRF-2026-000206',
      status: 'RUNNING',
      progress_percent: 35,
      files_completed: 1,
      files_total: 4,
      bytes_transferred: 350,
      bytes_total: 1000,
      current_item: '/NAS/report.csv',
      average_speed_bps: 10,
      current_speed_bps: 10,
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: '2026-10-20T12:00:00Z',
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith(`/transfers/${running.id}/cancel`) && init?.method === 'POST') {
        return jsonResponse({ ...running, status: 'CANCELLING' });
      }
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) return jsonResponse({ items: [running], total_count: 1 });
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Programaciones' }));

    expect(await screen.findByText(running.public_id)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/transfers/${running.id}/cancel`,
      expect.objectContaining({ method: 'POST' }),
    ));
    expect(screen.getByText('Se solicitó cancelar la transferencia.')).toBeVisible();
  });

  it('shows a blocked count in Programaciones and lets the user request another evaluation', async () => {
    const evaluation: TransferEvaluation = {
      status: 'BLOCKED', generation: 1, sequence: 4, files_found: 5001,
      count_complete: false, max_files: 5000, started_at: '2026-10-17T10:00:00Z',
      updated_at: '2026-10-17T10:01:00Z', completed_at: '2026-10-17T10:01:00Z',
      safe_error_code: 'TRANSFER_FILE_LIMIT_EXCEEDED', safe_message: 'Divide la selección.',
    };
    let latestEvaluation = evaluation;
    const transfer = {
      id: 'scheduled-evaluation-row',
      public_id: 'TRF-2026-000208',
      status: 'QUEUED',
      progress_percent: 0,
      files_completed: 0,
      files_total: 0,
      bytes_transferred: 0,
      bytes_total: 0,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      source_connection_id: 'source',
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: '2026-10-20T12:00:00Z',
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: 'BLOCKED_EVALUATION',
      evaluation,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/transfers/capabilities')) {
        return jsonResponse({ max_files_by_source_provider: { default: 5000, SMB: 100000 } });
      }
      if (path.endsWith(`/transfers/${transfer.id}/evaluation`) && init?.method === 'POST') {
        latestEvaluation = {
          ...evaluation, status: 'PENDING', generation: 2, sequence: 0,
          files_found: null, count_complete: false, safe_message: null,
        };
        return jsonResponse(latestEvaluation, 202);
      }
      if (isTransferCollectionRequest(path)) {
        const current = { ...transfer, evaluation: latestEvaluation };
        if (path.includes('background_only=true')) return jsonResponse({ items: [current], total_count: 1 });
        if (path.includes('pending_only=true')) return jsonResponse({ items: [current], total_count: 1 });
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Programaciones' }));

    const row = await screen.findByText(transfer.public_id);
    expect(row.closest('.scheduled-transfer-row')).toBeInTheDocument();
    expect(screen.getByText(/al menos 5\.001 archivos · límite 5\.000/i)).toBeVisible();
    expect(screen.getByText('Divide la selección.')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Cancelar' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Volver a evaluar' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/transfers/${transfer.id}/evaluation`,
      expect.objectContaining({ method: 'POST' }),
    ));
    expect(await screen.findByText(/Solicitud guardada|Se solicitó volver a evaluar/i)).toBeVisible();
    expect(await screen.findByText(/Pendiente de evaluación · cantidad todavía desconocida/i)).toBeVisible();
    expect(screen.getByText(transfer.public_id).closest('.scheduled-transfer-row')).toBeInTheDocument();
  });

  it('orders evaluation snapshots by generation and sequence separately from copy progress', () => {
    const current: Transfer = {
      id: 'merge-transfer', public_id: 'TRF-2026-000209', status: 'RUNNING',
      progress_percent: 50, files_completed: 4, files_total: 8,
      bytes_transferred: 400, bytes_total: 800, current_item: null,
      average_speed_bps: null, current_speed_bps: null,
      destination_connection_id: 'destination', destination_path: '/',
      evaluation: {
        status: 'EVALUATING', generation: 2, sequence: 5, files_found: 100,
        count_complete: false, max_files: 5000, started_at: null, updated_at: null,
        completed_at: null, safe_error_code: null, safe_message: null,
      },
    };
    const stale = mergeTransferState(current, {
      ...current,
      progress_percent: 10,
      bytes_transferred: 50,
      evaluation: { ...current.evaluation!, sequence: 4, files_found: 50 },
    });
    const newGeneration = mergeTransferState(current, {
      ...current,
      progress_percent: 10,
      files_completed: 0,
      bytes_transferred: 0,
      evaluation: { ...current.evaluation!, generation: 3, sequence: 0, files_found: 0 },
    });

    expect(stale.evaluation?.files_found).toBe(100);
    expect(stale.bytes_transferred).toBe(400);
    expect(newGeneration.evaluation?.files_found).toBe(0);
    expect(newGeneration.bytes_transferred).toBe(400);
  });

  it('keeps the newest durable pause state when a stale SSE snapshot arrives', () => {
    const pausing: Transfer = {
      id: 'pause-versioned', public_id: 'TRF-2026-000211', status: 'PAUSING',
      progress_percent: 40, files_completed: 2, files_total: 5,
      bytes_transferred: 400, bytes_total: 1_000, current_item: '/current.rar',
      average_speed_bps: null, current_speed_bps: null,
      destination_connection_id: 'destination', destination_path: '/Backup',
      attempt_count: 2, state_version: 8, pause_supported: true,
      pause_requested_at: '2026-10-18T20:00:00Z',
    };
    const staleRunning = mergeTransferState(pausing, {
      ...pausing,
      status: 'RUNNING',
      current_item: '/next.rar',
      state_version: 7,
    });
    const resumed = mergeTransferState(pausing, {
      ...pausing,
      status: 'RUNNING',
      current_item: '/next.rar',
      pause_requested_at: null,
      paused_at: null,
      state_version: 9,
    });

    expect(staleRunning.status).toBe('PAUSING');
    expect(staleRunning.current_item).toBe('/current.rar');
    expect(resumed.status).toBe('RUNNING');
    expect(resumed.state_version).toBe(9);
  });

  it('does not lose omission warnings when a delayed SSE frame arrives for the same attempt', () => {
    const current: Transfer = {
      id: 'merge-omission', public_id: 'TRF-2026-000210', status: 'RUNNING',
      progress_percent: 75, files_completed: 7, files_total: 9,
      bytes_transferred: 700, bytes_total: 900, current_item: null,
      average_speed_bps: null, current_speed_bps: null,
      destination_connection_id: 'destination', destination_path: '/', attempt_count: 4,
      manifest_summary: { copied: 7, failed: 0, skipped: 1, pending: 1, omitted: 1, omitted_bytes: 10 },
    };
    const delayed = mergeTransferState(current, {
      ...current,
      manifest_summary: { copied: 7, failed: 0, skipped: 0, pending: 2, omitted: 0, omitted_bytes: 0 },
    });

    expect(delayed.manifest_summary?.omitted).toBe(1);
    expect(delayed.manifest_summary?.omitted_bytes).toBe(10);
  });

  it('loads older active transfers after the first 200 scheduled background jobs', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const pending = Array.from({ length: 200 }, (_value, index) => ({
      id: `scheduled-new-${index}`,
      public_id: `TRF-2026-${String(index + 300).padStart(6, '0')}`,
      status: 'SCHEDULED',
      progress_percent: 0,
      files_completed: 0,
      files_total: 1,
      bytes_transferred: 0,
      bytes_total: 4,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      destination_connection_id: 'destination',
      destination_path: '/Archive',
      scheduled_at: '2026-10-20T12:00:00Z',
      execution_mode: 'AUTO',
      depends_on_transfer_id: null,
      wait_reason: 'WAITING_UNTIL',
    }));
    const olderRunning = {
      ...pending[0],
      id: 'scheduled-running-old',
      public_id: 'TRF-2026-000299',
      status: 'RUNNING',
      progress_percent: 55,
      current_item: '/NAS/older.bin',
      wait_reason: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith(`/transfers/${olderRunning.id}/cancel`) && init?.method === 'POST') {
        return jsonResponse({ ...olderRunning, status: 'CANCELLING' });
      }
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) {
          const offset = new URL(path, window.location.origin).searchParams.get('offset');
          const items = offset === '200' ? [olderRunning] : pending;
          return jsonResponse({ items, total_count: 201 });
        }
        if (path.includes('pending_only=true')) return jsonResponse({ items: pending, total_count: 200 });
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Programaciones' }));
    await waitFor(
      () => expect(screen.getByRole('button', { name: 'Cargar más trabajos activos' })).toBeVisible(),
      { timeout: 10_000 },
    );
    fireEvent.click(screen.getByRole('button', { name: 'Cargar más trabajos activos' }));

    await waitFor(() => expect(screen.getByText(olderRunning.public_id)).toBeVisible(), { timeout: 10_000 });
    expect(fetchMock.mock.calls.some(([input]) => (
      String(input).includes('background_only=true&offset=200&page_size=200')
    ))).toBe(true);
    const olderRow = screen.getByText(olderRunning.public_id).closest('.scheduled-transfer-row');
    expect(olderRow).not.toBeNull();
    TestEventSource.instances[0].emit({ ...olderRunning, status: 'RETRYING' });
    await waitFor(() => expect(within(olderRow as HTMLElement).getByText('Reintentando')).toBeVisible());
    fireEvent.click(within(olderRow as HTMLElement).getByRole('button', { name: 'Cancelar' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/transfers/${olderRunning.id}/cancel`,
      expect.objectContaining({ method: 'POST' }),
    ));
  }, 30_000);

  it('schedules a selected transfer with its browser timezone and execution mode', async () => {
    let savedTransfer: Record<string, unknown> | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
        { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
      ] });
      if (path.includes('/connections/source/items')) return jsonResponse({ items: [
        { name: 'archivo.txt', path: '/archivo.txt', is_dir: false, size_bytes: 4, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' },
      ], next_cursor: null, total_count: 1 });
      if (path.includes('/transfers?pending_only=true')) {
        return jsonResponse({ items: savedTransfer ? [savedTransfer] : [], total_count: savedTransfer ? 1 : 0 });
      }
      if (isTransferCollectionRequest(path) && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        savedTransfer = {
          id: 'scheduled-1', public_id: 'TRF-2026-000010', status: 'SCHEDULED',
          source_connection_id: 'source', destination_connection_id: 'destination',
          scheduled_at: body.scheduled_at, schedule_timezone: body.schedule_timezone,
          execution_mode: body.execution_mode, depends_on_transfer_id: body.depends_on_transfer_id,
          wait_reason: 'WAITING_UNTIL', progress_percent: 0, files_completed: 0, files_total: 0,
          bytes_transferred: 0, bytes_total: 0, current_item: null, average_speed_bps: null,
          current_speed_bps: null, estimated_remaining_seconds: null,
        };
        return jsonResponse(savedTransfer, 201);
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: savedTransfer ? [savedTransfer] : [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar archivo.txt' }));
    fireEvent.click(screen.getByRole('button', { name: /Programar transferencia/i }));
    const dateInput = await screen.findByLabelText(/Fecha y hora/i);
    const future = new Date(Date.now() + 5 * 24 * 60 * 60_000);
    const futureLocal = new Date(future.getTime() - future.getTimezoneOffset() * 60_000)
      .toISOString()
      .slice(0, 16);
    fireEvent.change(dateInput, { target: { value: futureLocal } });
    fireEvent.mouseDown(screen.getByLabelText(/Modo de ejecución/i));
    fireEvent.click(await screen.findByRole('option', { name: /Exclusiva/i }));
    fireEvent.click(screen.getByRole('button', { name: /Confirmar programación/i }));

    await waitFor(() => expect(savedTransfer).not.toBeNull());
    const createCall = fetchMock.mock.calls.find(([path, init]) => (
      isTransferCollectionRequest(String(path)) && init?.method === 'POST'
    ));
    expect(createCall).toBeDefined();
    const body = JSON.parse(String(createCall?.[1]?.body)) as Record<string, unknown>;
    expect(body.scheduled_at).toBe(new Date(futureLocal).toISOString());
    expect(body.schedule_timezone).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(body.execution_mode).toBe('EXCLUSIVE');
    const scheduledTab = await screen.findByRole('tab', { name: /Programaciones/i });
    expect(scheduledTab).toBeVisible();
    fireEvent.click(scheduledTab);
    expect(await screen.findByText('A la espera de la hora programada')).toBeVisible();
  });

  it.each(['SSE', 'poll'])('moves a scheduled transfer into history on terminal %s without a stale active card', async (completionSource) => {
    vi.stubGlobal('EventSource', TestEventSource);
    if (completionSource === 'poll') vi.useFakeTimers({ shouldAdvanceTime: true });
    let scheduledTransfer: Record<string, unknown> | null = null;
    let backgroundTransfer: Record<string, unknown> | null = null;
    let completedTransfer: Record<string, unknown> | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
        { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
      ] });
      if (path.includes('/connections/source/items')) return jsonResponse({ items: [
        { name: 'archivo.txt', path: '/archivo.txt', is_dir: false, size_bytes: 4, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' },
      ], next_cursor: null, total_count: 1 });
      if (isTransferCollectionRequest(path) && init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        scheduledTransfer = {
          id: 'scheduled-terminal', public_id: 'TRF-2026-000207', status: 'SCHEDULED',
          source_connection_id: 'source', destination_connection_id: 'destination',
          source_paths: ['/archivo.txt'], destination_path: '/', scheduled_at: body.scheduled_at,
          execution_mode: 'AUTO', wait_reason: 'WAITING_UNTIL', progress_percent: 0,
          files_completed: 0, files_total: 1, bytes_transferred: 0, bytes_total: 4,
          current_item: null, average_speed_bps: null, current_speed_bps: null,
          estimated_remaining_seconds: null,
        };
        backgroundTransfer = { ...scheduledTransfer, status: 'RUNNING', wait_reason: null, current_item: '/archivo.txt' };
        return jsonResponse(scheduledTransfer, 201);
      }
      if (isTransferCollectionRequest(path)) {
        if (path.includes('background_only=true')) {
          const items = backgroundTransfer && !completedTransfer ? [backgroundTransfer] : [];
          return jsonResponse({ items, total_count: items.length });
        }
        if (path.includes('pending_only=true')) {
          const items = scheduledTransfer && !backgroundTransfer ? [scheduledTransfer] : [];
          return jsonResponse({ items, total_count: items.length });
        }
        const items = completedTransfer ? [completedTransfer] : [];
        return jsonResponse({ items, total_count: items.length });
      }
      if (path.includes('/connections/destination/items')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar archivo.txt' }));
    fireEvent.click(screen.getByRole('button', { name: /Programar transferencia/i }));
    const dateInput = await screen.findByLabelText(/Fecha y hora/i);
    const future = new Date(Date.now() + 5 * 24 * 60 * 60_000);
    const futureLocal = new Date(future.getTime() - future.getTimezoneOffset() * 60_000)
      .toISOString()
      .slice(0, 16);
    fireEvent.change(dateInput, { target: { value: futureLocal } });
    fireEvent.click(screen.getByRole('button', { name: /Confirmar programación/i }));
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));

    expect(backgroundTransfer).not.toBeNull();
    completedTransfer = {
      ...(backgroundTransfer ?? {}),
      status: 'COMPLETED', progress_percent: 100, files_completed: 1, bytes_transferred: 4,
    };
    if (completionSource === 'SSE') {
      TestEventSource.instances[0].emit(completedTransfer);
    } else {
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    }

    expect(await screen.findByText('Completada')).toBeVisible();
    expect(screen.queryByText(/TRANSFERENCIA ACTIVA · TRF-2026-000207/)).not.toBeInTheDocument();
  }, 10_000);

  it('signs in and opens the transfer workspace', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) {
        return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      }
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections') || isTransferCollectionRequest(path)) {
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), {
      target: { value: 'admin' },
    });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), {
      target: { value: 'CorrectHorseBatteryStaple123!' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));

    expect(await screen.findByRole('heading', { name: /Explorador de archivos/i })).toBeVisible();
    const header = within(screen.getByRole('banner'));
    expect(header.getByRole('tab', { name: 'Explorador' })).toBeVisible();
    expect(header.getByRole('tab', { name: 'Conexiones' })).toBeVisible();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/connections', expect.anything()));
  });

  it('lets an admin configure a real MEGA connection without exposing credentials in listings', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) {
        return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      }
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections') && init?.method === 'POST') {
        return jsonResponse({
          id: 'mega-1', name: 'MEGA principal', provider_type: 'MEGA', status: 'ACTIVE', credentials_configured: true,
        }, 201);
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        {
          id: 'mock-1', name: 'Archivo de operaciones', provider_type: 'MOCK', status: 'ACTIVE',
          credentials_configured: false, is_global: true, last_tested_at: '2026-10-10T15:30:00Z',
          last_test_status: 'SUCCESS',
        },
      ] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [], total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
    fireEvent.click(await screen.findByRole('tab', { name: /Conexiones/i }));
    expect(await screen.findByRole('heading', { name: /Gestiona tus conexiones/i })).toBeVisible();
    expect(screen.getByText('Archivo de operaciones')).toBeVisible();
    expect(screen.getByText('Simulada')).toBeVisible();
    expect(screen.getByText('Global')).toBeVisible();
    expect(screen.getByText(/Prueba exitosa/i)).toBeVisible();
    fireEvent.click(await screen.findByRole('button', { name: /Agregar conexión/i }));

    expect(await screen.findByRole('dialog')).toBeVisible();
    expect(screen.getByLabelText(/Nombre de conexión/i)).toBeVisible();
    expect(screen.getByLabelText(/Proveedor/i)).toBeVisible();
    expect(screen.getByLabelText(/Usuario de MEGA/i)).toBeVisible();
    expect(screen.getByLabelText(/Contraseña de MEGA/i)).toBeVisible();
  });

  it('creates a Nextcloud connection from its server URL and app password', async () => {
    let createRequest: Record<string, unknown> | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/system/capabilities')) {
        return jsonResponse({ enabled_providers: ['MOCK', 'MEGA', 'GOOGLE_DRIVE', 'SEAFILE', 'SMB'] });
      }
      if (path.endsWith('/connections') && init?.method === 'POST') {
        createRequest = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse({
          id: 'nextcloud-1', name: 'Nube equipo', provider_type: 'NEXTCLOUD', status: 'INACTIVE',
          credentials_configured: true, is_global: true,
        }, 201);
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [], total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
    fireEvent.click(await screen.findByRole('tab', { name: /Conexiones/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Agregar conexión/i }));
    fireEvent.mouseDown(screen.getByRole('combobox', { name: /Proveedor/i }));
    fireEvent.click(await screen.findByRole('option', { name: /Nextcloud/i }));
    fireEvent.change(screen.getByLabelText(/Nombre de conexión/i), { target: { value: 'Nube equipo' } });
    fireEvent.change(screen.getByLabelText(/URL base de Nextcloud/i), {
      target: { value: 'https://cloud.example.test/nextcloud' },
    });
    fireEvent.change(screen.getByLabelText(/Usuario de Nextcloud/i), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText(/Contraseña de aplicación/i), { target: { value: 'application-secret' } });
    fireEvent.click(screen.getByRole('button', { name: /Guardar conexión/i }));

    await waitFor(() => expect(createRequest).toBeDefined());
    expect(createRequest).toMatchObject({
      provider_type: 'NEXTCLOUD',
      credentials: {
        url: 'https://cloud.example.test/nextcloud',
        username: 'alice',
        password: 'application-secret',
      },
    });
    expect(await screen.findByText(/Conexión guardada de forma inactiva/i)).toBeVisible();
  });

  it('creates Seafile with an explicit adapter and sends 2FA only with the connection test', async () => {
    let connections: Array<Record<string, unknown>> = [];
    let createRequest: Record<string, unknown> | undefined;
    let testRequest: { body?: string } | undefined;
    const safeConnection = {
      id: 'seafile-1',
      name: 'Seafile CE',
      provider_type: 'SEAFILE',
      adapter_type: 'SEAFILE_API',
      status: 'INACTIVE',
      credentials_configured: true,
      is_global: true,
      last_tested_at: null,
      last_test_status: null,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections') && init?.method === 'POST') {
        createRequest = JSON.parse(String(init.body)) as Record<string, unknown>;
        connections = [safeConnection];
        return jsonResponse(safeConnection, 201);
      }
      if (path.endsWith('/connections/seafile-1/test') && init?.method === 'POST') {
        testRequest = { body: init.body?.toString() };
        return jsonResponse({ success: true, message: 'Seafile verificado' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: connections });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: /Conexiones/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Agregar conexión/i }));
    const providerSelect = screen.getByRole('combobox', { name: 'Proveedor' });
    fireEvent.mouseDown(providerSelect);
    fireEvent.click(await screen.findByRole('option', { name: 'Seafile' }));

    expect(await screen.findByLabelText(/URL del servidor Seafile/i)).toBeVisible();
    const adapterSelect = screen.getByRole('combobox', { name: /Adaptador Seafile/i });
    expect(adapterSelect).toHaveTextContent('Rclone');
    fireEvent.mouseDown(adapterSelect);
    fireEvent.click(await screen.findByRole('option', { name: /API de Seafile/i }));
    fireEvent.change(screen.getByLabelText(/Nombre de conexión/i), { target: { value: 'Seafile CE' } });
    fireEvent.change(screen.getByLabelText(/URL del servidor Seafile/i), {
      target: { value: 'https://files.example.test' },
    });
    fireEvent.change(screen.getByLabelText(/Usuario de Seafile/i), {
      target: { value: 'operator@example.test' },
    });
    fireEvent.change(screen.getByLabelText(/Contraseña de Seafile/i), {
      target: { value: 'seafile-login-secret' },
    });
    fireEvent.change(screen.getByLabelText(/Biblioteca \(opcional\)/i), {
      target: { value: 'Archive' },
    });
    fireEvent.change(screen.getByLabelText(/Contraseña de biblioteca/i), {
      target: { value: 'library-secret' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Guardar conexión' }));

    await waitFor(() => expect(createRequest).toEqual({
      name: 'Seafile CE',
      provider_type: 'SEAFILE',
      adapter_type: 'SEAFILE_API',
      is_global: true,
      credentials: {
        url: 'https://files.example.test',
        username: 'operator@example.test',
        password: 'seafile-login-secret',
        library: 'Archive',
        library_password: 'library-secret',
      },
    }));
    expect(JSON.stringify(safeConnection)).not.toContain('secret');
    expect(screen.queryByDisplayValue('seafile-login-secret')).not.toBeInTheDocument();
    await waitForElementToBeRemoved(() => screen.queryByRole('dialog', { name: /Agregar conexión/i }));
    expect(await screen.findByText(/Adaptador: API de Seafile/i)).toBeVisible();

    fireEvent.click(screen.getByRole('tab', { name: /Explorador/i }));
    const otpInput = await screen.findByLabelText(/Código 2FA de Seafile/i);
    fireEvent.change(otpInput, { target: { value: '123456' } });
    fireEvent.click(screen.getByRole('button', { name: /Probar conexión/i }));

    await waitFor(() => expect(testRequest?.body).toBe(JSON.stringify({ otp: '123456' })));
    expect(otpInput).toHaveValue('');
    expect(screen.queryByDisplayValue('123456')).not.toBeInTheDocument();
    expect(screen.getByText(/Seafile verificado/i)).toBeVisible();
  });

  it('archives an idle connection only after confirmation and clears explorer selection', async () => {
    let connections = [
      { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: true },
      { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: false },
    ];
    let archiveRequest: RequestInit | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.includes('/connections/source') && init?.method === 'DELETE') {
        archiveRequest = init;
        connections = connections.filter((connection) => connection.id !== 'source');
        return new Response(null, { status: 204 });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: connections });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      if (path.includes('/connections/source/items')) return jsonResponse({
        items: [{ name: 'informe.txt', path: '/informe.txt', is_dir: false, size_bytes: 8, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' }],
        next_cursor: null, total_count: 1,
      });
      if (path.includes('/connections/destination/items')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar informe.txt' }));
    expect(screen.getByText('1 seleccionados')).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: /Conexiones/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Archivar Origen' }));

    expect(await screen.findByRole('dialog', { name: /Archivar conexión/i })).toBeVisible();
    expect(screen.getByText('Proveedor: Simulada')).toBeVisible();
    expect(screen.getByText(/el historial de transferencias se conservará/i)).toBeVisible();
    expect(archiveRequest).toBeUndefined();
    fireEvent.click(screen.getByRole('button', { name: /Confirmar archivado/i }));

    await waitFor(() => expect(archiveRequest?.method).toBe('DELETE'));
    expect(await screen.findByText(/Conexión “Origen” archivada/i)).toBeVisible();
    await waitForElementToBeRemoved(() => screen.queryByRole('dialog', { name: /Archivar conexión/i }));
    fireEvent.click(screen.getByRole('tab', { name: /Explorador/i }));
    expect(await screen.findByText('0 seleccionados')).toBeVisible();
    const sourceSelect = screen.getAllByRole('combobox', { name: 'Conexión' })[0];
    fireEvent.mouseDown(sourceSelect);
    expect(await screen.findByRole('option', { name: 'Destino' })).toBeVisible();
    expect(screen.queryByRole('option', { name: 'Origen' })).not.toBeInTheDocument();
  });

  it('keeps a failed archive confirmation open and disables repeat requests while pending', async () => {
    let resolveArchive: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.includes('/connections/source') && init?.method === 'DELETE') {
        return new Promise<Response>((resolve) => { resolveArchive = resolve; });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: true },
        { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: false },
      ] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: /Conexiones/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Archivar Origen' }));
    fireEvent.click(screen.getByRole('button', { name: /Confirmar archivado/i }));

    const confirmButton = screen.getByRole('button', { name: /Confirmar archivado/i });
    await waitFor(() => expect(confirmButton).toBeDisabled());
    resolveArchive?.(jsonResponse({ detail: 'No se puede archivar mientras haya transferencias activas.' }, 409));
    expect(await screen.findByText(/No se puede archivar mientras haya transferencias activas/i)).toBeVisible();
    expect(screen.getByRole('dialog', { name: /Archivar conexión/i })).toBeVisible();
    expect(screen.getByRole('button', { name: /Confirmar archivado/i })).toBeEnabled();
  });

  it('does not restore an archived connection from stale data when list refresh fails', async () => {
    const connections = [
      { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: true },
      { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false, is_global: false },
    ];
    let connectionListRequests = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.includes('/connections/source') && init?.method === 'DELETE') return new Response(null, { status: 204 });
      if (path.endsWith('/connections')) {
        connectionListRequests += 1;
        if (connectionListRequests > 1) return jsonResponse({ detail: 'Servicio temporalmente no disponible' }, 503);
        return jsonResponse({ items: connections });
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: /Conexiones/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Archivar Origen' }));
    fireEvent.click(screen.getByRole('button', { name: /Confirmar archivado/i }));
    expect(await screen.findByText(/Conexión “Origen” archivada/i)).toBeVisible();
    expect(await screen.findByText(/No se pudieron cargar las conexiones/i)).toBeVisible();
    await waitForElementToBeRemoved(() => screen.queryByRole('dialog', { name: /Archivar conexión/i }));

    fireEvent.click(screen.getByRole('tab', { name: /Explorador/i }));
    const sourceSelect = screen.getAllByRole('combobox', { name: 'Conexión' })[0];
    fireEvent.mouseDown(sourceSelect);
    expect(await screen.findByRole('option', { name: 'Destino' })).toBeVisible();
    expect(screen.queryByRole('option', { name: 'Origen' })).not.toBeInTheDocument();
  });

  it('does not show connection management controls to a regular user', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections') || isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'user' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));

    await screen.findByRole('heading', { name: /Explorador de archivos/i });
    expect(screen.queryByRole('tab', { name: /Conexiones/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Agregar conexión/i })).not.toBeInTheDocument();
  });

  it('summarizes a MEGA route and explains that automatic omission remains disabled', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'MEGA de operaciones', provider_type: 'MEGA', status: 'ACTIVE', credentials_configured: true },
        { id: 'destination', name: 'Drive de respaldo', provider_type: 'GOOGLE_DRIVE', status: 'ACTIVE', credentials_configured: true },
      ] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      if (path.includes('/connections/source/items')) return jsonResponse({
        items: [{ name: 'informe.csv', path: '/informe.csv', is_dir: false, size_bytes: 120, modified_at: null, mime_type: 'text/csv', provider_type: 'GOOGLE_DRIVE' }],
        next_cursor: null,
        total_count: 1,
      });
      if (path.includes('/connections/destination/items')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    const summary = screen.getByRole('group', { name: 'Resumen de transferencia' });
    expect(within(summary).getByRole('heading', { name: 'Prepara tu transferencia' })).toBeVisible();
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar informe.csv' }));

    expect(within(summary).getByRole('heading', { name: 'Listo para transferir' })).toBeVisible();
    expect(within(summary).getByRole('img', { name: 'Transferencia de archivos' })).toBeVisible();
    expect(summary.querySelector('.cloud-network-mark')).not.toBeInTheDocument();
    expect(within(summary).getByText('1 elemento listo para copiar')).toBeVisible();
    expect(within(summary).getByRole('img', { name: 'MEGA' })).toBeVisible();
    expect(within(summary).getByRole('img', { name: 'Google Drive' })).toBeVisible();
    expect(within(summary).getByText('MEGA de operaciones')).toBeVisible();
    expect(within(summary).getByText('Drive de respaldo')).toBeVisible();
    expect(within(summary).getByText('Carpeta raíz')).toBeVisible();
    expect(within(summary).getByText('Conservar ambos · numerar la copia')).toBeVisible();
    expect(within(summary).getByText(/La omisión automática de archivos MEGA está deshabilitada/)).toBeVisible();
    expect(within(summary).getByText('Avisarme por correo al terminar')).toBeVisible();
  });

  it('loads the next explorer page when a directory has more than 50 items', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
        { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
      ] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      if (path.includes('/connections/source/items')) {
        if (path.includes('cursor=50')) return jsonResponse({
          items: [{ name: 'archivo-51.txt', path: '/archivo-51.txt', is_dir: false, size_bytes: 1, modified_at: null, mime_type: null, provider_type: 'MOCK' }],
          next_cursor: null,
          total_count: 51,
        });
        return jsonResponse({
          items: [{ name: 'archivo-1.txt', path: '/archivo-1.txt', is_dir: false, size_bytes: 1, modified_at: null, mime_type: null, provider_type: 'MOCK' }],
          next_cursor: '50',
          total_count: 51,
        });
      }
      if (path.includes('/connections/destination/items')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'user' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/connections/source/items'))).toBe(true));
    expect(await screen.findByText('archivo-1.txt')).toBeVisible();
    fireEvent.click(await screen.findByRole('button', { name: /Cargar más/i }));
    expect(await screen.findByText('archivo-51.txt')).toBeVisible();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('cursor=50'))).toBe(true);
  });

  it.each(['COMPLETED', 'COMPLETED_WITH_WARNINGS', 'FAILED', 'CANCELLED'])(
    'refreshes the open destination folder when a transfer reaches %s',
    async (terminalStatus) => {
      let destinationItemsRequests = 0;
      const transfer = {
        id: 'transfer-1',
        public_id: 'TRF-2026-000001',
        status: 'QUEUED',
        progress_percent: 0,
        files_total: 1,
        files_completed: 0,
        bytes_total: 4,
        bytes_transferred: 0,
        current_item: null,
        average_speed_bps: null,
        current_speed_bps: null,
        estimated_remaining_seconds: null,
        source_connection_id: 'source',
        destination_connection_id: 'destination',
        source_paths: ['/origen.txt'],
        destination_path: '/',
        conflict_policy: 'KEEP_BOTH',
        send_email_notification: false,
      };
      let completedTransfer: typeof transfer | null = null;
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input);
        if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
        if (path.endsWith('/auth/csrf')) {
          document.cookie = 'ctm_csrf=csrf-value; path=/';
          return jsonResponse({ csrf_token: 'csrf-value' });
        }
        if (path.endsWith('/auth/login')) {
          return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
        }
        if (path.endsWith('/connections')) return jsonResponse({ items: [
          { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
          { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
        ] });
        if (isTransferCollectionRequest(path) && init?.method === 'POST') return jsonResponse(transfer, 201);
        if (path.endsWith('/events')) return jsonResponse({ items: [
          { event_type: 'FAILURE_DIAGNOSTIC', message: 'No hay espacio temporal suficiente.' },
        ] });
        if (isTransferCollectionRequest(path)) {
          return jsonResponse({ items: completedTransfer ? [completedTransfer] : [] });
        }
        if (path.includes('/connections/source/items')) return jsonResponse({ items: [
          { name: 'origen.txt', path: '/origen.txt', is_dir: false, size_bytes: 4, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' },
        ], next_cursor: null, total_count: 1 });
        if (path.includes('/connections/destination/items')) {
          destinationItemsRequests += 1;
          if (destinationItemsRequests === 1) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
          return jsonResponse({ items: [
            { name: 'archivo-copiado.txt', path: '/archivo-copiado.txt', is_dir: false, size_bytes: 4, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' },
          ], next_cursor: null, total_count: 1 });
        }
        return jsonResponse({ detail: 'Not found' }, 404);
      });
      vi.stubGlobal('fetch', fetchMock);
      vi.stubGlobal('EventSource', TestEventSource);

      render(<App />);
      fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'user' } });
      fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
      fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));

      fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar origen.txt' }));
      fireEvent.click(screen.getByRole('button', { name: /Iniciar transferencia/i }));
      await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));
      expect(await screen.findByText('origen.txt')).toBeVisible();
      await waitFor(() => expect(destinationItemsRequests).toBe(1));

      const eventSource = TestEventSource.instances[0];
      eventSource.emit({ ...transfer, status: 'RUNNING', progress_percent: 50, bytes_total: 0, bytes_transferred: 4 });
      expect(await screen.findByText(/4 B transferidos · 0\/1 archivos/)).toBeVisible();
      eventSource.emit({
        ...transfer,
        status: 'RUNNING',
        progress_percent: 99.99,
        bytes_transferred: 4,
        current_item: null,
      });
      await waitFor(() => expect(screen.getAllByText('En curso').length).toBeGreaterThan(0));
      expect(screen.getByText('99.9%')).toBeVisible();
      expect(screen.queryByText('100.0%')).not.toBeInTheDocument();
      expect(destinationItemsRequests).toBe(1);

      completedTransfer = {
        ...transfer,
        status: terminalStatus,
        progress_percent: 100,
        files_completed: 1,
        bytes_transferred: 4,
      };
      eventSource.emit(completedTransfer);
      if (terminalStatus === 'COMPLETED') {
        eventSource.emit({ ...transfer, status: 'RUNNING', progress_percent: 55 });
        await waitFor(() => expect(screen.getAllByText('Completada').length).toBeGreaterThan(0));
        expect(screen.getByText('100.0%')).toBeVisible();
      }

      if (terminalStatus === 'FAILED') {
        expect(await screen.findByText('No hay espacio temporal suficiente.')).toBeVisible();
      }

      expect(await screen.findByText('archivo-copiado.txt')).toBeVisible();
      if (terminalStatus === 'COMPLETED') {
        expect(screen.getByText('100.0%')).toBeVisible();
        await new Promise((resolve) => window.setTimeout(resolve, 2_500));
        expect(screen.getByText('100.0%')).toBeVisible();
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        expect(screen.queryByText('100.0%')).not.toBeInTheDocument();
        expect(screen.queryByText(/TRANSFERENCIA ACTIVA · TRF-2026-000001/)).not.toBeInTheDocument();
        expect(screen.getByText('TRF-2026-000001')).toBeVisible();
      }
    },
    10_000,
  );

  it('clears a transfer failure notice when polling confirms that the same transfer completed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const transfer = {
      id: 'transfer-1',
      public_id: 'TRF-2026-000001',
      status: 'QUEUED',
      progress_percent: 0,
      files_total: 1,
      files_completed: 0,
      bytes_total: 4,
      bytes_transferred: 0,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      estimated_remaining_seconds: null,
      source_connection_id: 'source',
      destination_connection_id: 'destination',
      source_paths: ['/origen.txt'],
      destination_path: '/',
      conflict_policy: 'KEEP_BOTH',
      send_email_notification: false,
    };
    let latestTransfer: typeof transfer | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [
        { id: 'source', name: 'Origen', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
        { id: 'destination', name: 'Destino', provider_type: 'MOCK', status: 'ACTIVE', credentials_configured: false },
      ] });
      if (isTransferCollectionRequest(path) && init?.method === 'POST') return jsonResponse(transfer, 201);
      if (path.endsWith('/events')) return jsonResponse({ items: [
        { event_type: 'FAILURE_DIAGNOSTIC', message: 'No se pudo completar la transferencia.' },
      ] });
      if (isTransferCollectionRequest(path)) {
        return jsonResponse({ items: latestTransfer ? [latestTransfer] : [] });
      }
      if (path.includes('/connections/source/items')) return jsonResponse({ items: [
        { name: 'origen.txt', path: '/origen.txt', is_dir: false, size_bytes: 4, modified_at: null, mime_type: 'text/plain', provider_type: 'MOCK' },
      ], next_cursor: null, total_count: 1 });
      if (path.includes('/connections/destination/items')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('EventSource', TestEventSource);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'user' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Seleccionar origen.txt' }));
    fireEvent.click(screen.getByRole('button', { name: /Iniciar transferencia/i }));
    await waitFor(() => expect(TestEventSource.instances).toHaveLength(1));

    const eventSource = TestEventSource.instances[0];
    eventSource.emit({ ...transfer, status: 'RUNNING', progress_percent: 50 });
    eventSource.emit({ ...transfer, status: 'FAILED', progress_percent: 50 });
    expect(await screen.findByText('No se pudo completar la transferencia.')).toBeVisible();

    latestTransfer = {
      ...transfer,
      status: 'COMPLETED',
      progress_percent: 100,
      files_completed: 1,
      bytes_transferred: 4,
    };
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(screen.queryByText('No se pudo completar la transferencia.')).not.toBeInTheDocument();
    expect(screen.getAllByText('Completada').length).toBeGreaterThan(0);
  }, 10_000);

  it('requires the user to change an initial password before normal use', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'newuser', email: 'new@example.test', role: 'USER', must_change_password: true });
      }
      if (path.endsWith('/auth/password')) return jsonResponse({ message: 'Contraseña actualizada' });
      if (path.endsWith('/connections') || isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'newuser' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'Temporary-password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));

    expect(await screen.findByRole('dialog')).toBeVisible();
    expect(screen.getByRole('heading', { name: /Cambia tu contraseña/i })).toBeVisible();
    fireEvent.change(screen.getByLabelText(/Contraseña actual/i), { target: { value: 'Temporary-password123!' } });
    fireEvent.change(screen.getByLabelText(/Nueva contraseña/i), { target: { value: 'New-password456!' } });
    fireEvent.click(screen.getByRole('button', { name: /Actualizar contraseña/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/auth/password', expect.objectContaining({ method: 'POST' })));
  });

  it('lets an administrator provision a user from the administration panel', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/users') && init?.method === 'POST') return jsonResponse({
        id: 'user-2', username: 'operator', email: 'operator@example.test', role: 'USER', is_active: true, must_change_password: true,
      }, 201);
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
    await openAdministration();
    expect(await screen.findByRole('heading', { name: /Administración/i })).toBeVisible();
    fireEvent.change(screen.getByLabelText(/Nombre de usuario/i), { target: { value: 'operator' } });
    fireEvent.change(screen.getByLabelText(/^Correo$/i), { target: { value: 'operator@example.test' } });
    fireEvent.change(screen.getByLabelText(/Contraseña inicial/i), { target: { value: 'Temporary-password123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Crear usuario/i }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/users', expect.objectContaining({ method: 'POST' })));
  });

  it('shows the SMTP subtab inside administration', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/audit-events?limit=20')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await openAdministration();

    expect(await screen.findByRole('heading', { name: /Administración/i })).toBeVisible();
    expect(screen.getByRole('tab', { name: 'General' })).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Correo SMTP' })).toBeVisible();
  });

  it('loads SMTP settings without exposing saved credentials in the form', async () => {
    const safeSettings = {
      host: 'smtp.example.test',
      port: 587,
      use_tls: true,
      from_address: 'notifications@example.test',
      credentials_configured: true,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/settings/smtp')) return jsonResponse(safeSettings);
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/audit-events?limit=20')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await openAdministration();
    fireEvent.click(await screen.findByRole('tab', { name: 'Correo SMTP' }));

    expect(await screen.findByDisplayValue('smtp.example.test')).toBeVisible();
    expect(screen.getByLabelText(/Usuario SMTP/i)).toHaveValue('');
    expect(screen.getByLabelText(/Contraseña SMTP/i)).toHaveValue('');
    expect(screen.getByText(/Credenciales SMTP configuradas/i)).toBeVisible();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/v1/settings/smtp', expect.anything()));
  });

  it('saves SMTP settings and sends blank credentials to preserve the stored values', async () => {
    const safeSettings = {
      host: 'smtp.example.test',
      port: 587,
      use_tls: true,
      from_address: 'notifications@example.test',
      credentials_configured: true,
    };
    let saveBody: Record<string, unknown> | undefined;
    let settingsLoads = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/settings/smtp') && init?.method === 'PUT') {
        saveBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse(safeSettings);
      }
      if (path.endsWith('/settings/smtp')) {
        settingsLoads += 1;
        return jsonResponse(safeSettings);
      }
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/audit-events?limit=20')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await openAdministration();
    fireEvent.click(await screen.findByRole('tab', { name: 'Correo SMTP' }));
    fireEvent.change(await screen.findByLabelText(/Servidor SMTP/i), { target: { value: 'mail.example.test' } });
    fireEvent.click(screen.getByRole('button', { name: /Guardar configuración/i }));

    await waitFor(() => expect(saveBody).toEqual({
      host: 'mail.example.test',
      port: 587,
      use_tls: true,
      from_address: 'notifications@example.test',
      username: '',
      password: '',
    }));
    expect(await screen.findByText(/Configuración SMTP guardada/i)).toBeVisible();
    await waitFor(() => expect(settingsLoads).toBeGreaterThanOrEqual(2));
  });

  it('tests the unsaved SMTP draft without saving it', async () => {
    const safeSettings = {
      host: 'smtp.example.test',
      port: 587,
      use_tls: true,
      from_address: 'notifications@example.test',
      credentials_configured: false,
    };
    let testBody: Record<string, unknown> | undefined;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/settings/smtp/test')) {
        testBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return jsonResponse({ success: true, message: 'Conexión SMTP correcta' });
      }
      if (path.endsWith('/settings/smtp')) return jsonResponse(safeSettings);
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/audit-events?limit=20')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await openAdministration();
    fireEvent.click(await screen.findByRole('tab', { name: 'Correo SMTP' }));
    fireEvent.change(await screen.findByLabelText(/Servidor SMTP/i), { target: { value: 'draft.example.test' } });
    fireEvent.change(screen.getByLabelText(/Usuario SMTP/i), { target: { value: 'draft-user' } });
    fireEvent.change(screen.getByLabelText(/Contraseña SMTP/i), { target: { value: 'draft-password' } });
    fireEvent.click(screen.getByRole('button', { name: /Probar conexión/i }));

    await waitFor(() => expect(testBody).toMatchObject({
      host: 'draft.example.test',
      username: 'draft-user',
      password: 'draft-password',
    }));
    expect(await screen.findByText(/Conexión SMTP correcta/i)).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalledWith('/api/v1/settings/smtp', expect.objectContaining({ method: 'PUT' }));
  });

  it('shows SMTP test loading, success and error states', async () => {
    const safeSettings = {
      host: 'smtp.example.test',
      port: 587,
      use_tls: true,
      from_address: 'notifications@example.test',
      credentials_configured: false,
    };
    let resolveTest: ((response: Response) => void) | undefined;
    let shouldFail = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/settings/smtp/test')) {
        if (shouldFail) return jsonResponse({ success: false, message: 'No se pudo conectar con SMTP' });
        return new Promise<Response>((resolve) => { resolveTest = resolve; });
      }
      if (path.endsWith('/settings/smtp')) return jsonResponse(safeSettings);
      if (path.endsWith('/users')) return jsonResponse({ items: [], total_count: 0 });
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path.endsWith('/audit-events?limit=20')) return jsonResponse({ items: [] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await openAdministration();
    fireEvent.click(await screen.findByRole('tab', { name: 'Correo SMTP' }));
    await screen.findByDisplayValue('smtp.example.test');
    fireEvent.click(screen.getByRole('button', { name: /Probar conexión/i }));

    expect(await screen.findByRole('button', { name: /Probando conexión/i })).toBeDisabled();
    resolveTest?.(jsonResponse({ success: true, message: 'Conexión SMTP correcta' }));
    expect(await screen.findByText(/Conexión SMTP correcta/i)).toBeVisible();

    shouldFail = true;
    fireEvent.click(screen.getByRole('button', { name: /Probar conexión/i }));
    expect(await screen.findByText(/No se pudo conectar con SMTP/i)).toBeVisible();
  });

  it('opens a private NAS from Servicios privados as the transfer source', async () => {
    const smb = {
      id: 'smb-1',
      name: 'NAS Finanzas',
      provider_type: 'SMB',
      status: 'ACTIVE',
      credentials_configured: true,
      is_global: true,
    };
    const drive = {
      id: 'drive-1',
      name: 'Drive',
      provider_type: 'GOOGLE_DRIVE',
      status: 'ACTIVE',
      credentials_configured: true,
      is_global: true,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [smb, drive] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      if (path.includes('/items?')) return jsonResponse({ items: [], next_cursor: null, total_count: 0 });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Servicios privados' }));

    expect(await screen.findByRole('heading', { name: 'Servicios privados', level: 2 })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /Explorar NAS Finanzas/i }));

    expect(await screen.findByRole('heading', { name: 'Explorador de archivos' })).toBeVisible();
    await waitFor(() => {
      const originSelect = screen.getAllByRole('combobox', { name: 'Conexión' })[0];
      expect(originSelect).toHaveTextContent('NAS Finanzas');
    });
  });

  it('lets an admin archive a NAS from Servicios privados after confirmation', async () => {
    const nas = {
      id: 'smb-archive-1',
      name: 'NAS Finanzas',
      provider_type: 'SMB',
      status: 'ACTIVE',
      credentials_configured: true,
      is_global: true,
    };
    let archived = false;
    const archiveRequests: Array<{ path: string; method: string | undefined }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: archived ? [] : [nas] });
      if (path === `/api/v1/connections/${nas.id}` && init?.method === 'DELETE') {
        archiveRequests.push({ path, method: init.method });
        archived = true;
        return new Response(null, { status: 204 });
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Servicios privados' }));

    const archiveButton = await screen.findByRole('button', { name: 'Archivar NAS Finanzas' });
    fireEvent.click(archiveButton);
    expect(await screen.findByRole('dialog', { name: 'Archivar conexión' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Archivar conexión' })).not.toBeInTheDocument());
    expect(archiveRequests).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: 'Archivar NAS Finanzas' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirmar archivado' }));

    expect(await screen.findByText(/Conexión “NAS Finanzas” archivada/)).toBeVisible();
    await waitFor(() => expect(screen.queryByText('NAS Finanzas', { exact: true })).not.toBeInTheDocument());
    expect(archiveRequests).toEqual([{ path: '/api/v1/connections/smb-archive-1', method: 'DELETE' }]);
  });

  it('keeps an NAS visible and shows the error when an active transfer blocks archiving', async () => {
    const nas = {
      id: 'smb-busy-1',
      name: 'NAS Operaciones',
      provider_type: 'SMB',
      status: 'ACTIVE',
      credentials_configured: true,
      is_global: true,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'admin-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [nas] });
      if (path === `/api/v1/connections/${nas.id}` && init?.method === 'DELETE') {
        return jsonResponse({ detail: 'No se puede archivar mientras tenga transferencias activas.' }, 409);
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Servicios privados' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Archivar NAS Operaciones' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirmar archivado' }));

    expect(await screen.findByText('No se puede archivar mientras tenga transferencias activas.')).toBeVisible();
    expect(screen.getByRole('dialog', { name: 'Archivar conexión' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Cancelar' }));
    expect(await screen.findByText('NAS Operaciones', { exact: true })).toBeVisible();
  });

  it('does not offer NAS archiving to regular users', async () => {
    const nas = {
      id: 'smb-user-1',
      name: 'NAS Compartido',
      provider_type: 'SMB',
      status: 'ACTIVE',
      credentials_configured: true,
      is_global: true,
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'user', email: 'user@example.test', role: 'USER' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [nas] });
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Servicios privados' }));

    expect(await screen.findByText('NAS Compartido', { exact: true })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Archivar NAS Compartido' })).not.toBeInTheDocument();
  });

  it('shows the SMB file report and resumes a failed NAS transfer', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const failedTransfer = {
      id: 'smb-transfer',
      public_id: 'TRF-2026-000099',
      status: 'FAILED',
      progress_percent: 50,
      files_total: 2,
      files_completed: 1,
      bytes_total: 2048,
      bytes_transferred: 1024,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      estimated_remaining_seconds: null,
      source_connection_id: 'nas',
      destination_connection_id: 'drive',
      source_paths: ['/Projects'],
      destination_path: '/Archive',
      created_at: '2026-10-12T10:00:00.000Z',
      started_at: '2026-10-12T10:01:00.000Z',
      completed_at: '2026-10-12T10:02:00.000Z',
    };
    let resumed = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) {
        return jsonResponse({ items: [
          { id: 'nas', name: 'NAS Proyectos', provider_type: 'SMB', status: 'ACTIVE', credentials_configured: true },
          { id: 'drive', name: 'Google Drive', provider_type: 'GOOGLE_DRIVE', status: 'ACTIVE', credentials_configured: true },
        ] });
      }
      if (isTransferCollectionRequest(path) && init?.method === 'POST') return jsonResponse(failedTransfer, 201);
      if (path.endsWith('/items?limit=100')) {
        return jsonResponse({
          items: [{
            id: 'manifest-item',
            source_path: '/Projects/report.pdf',
            destination_path: '/Archive/Projects/report.pdf',
            is_dir: false,
            size_bytes: 1024,
            modified_at: null,
            scan_complete: false,
            status: 'FAILED',
            error_code: 'NETWORK',
            verified_by: null,
          }],
          next_cursor: null,
          counts: { total: 1, pending: 0, copied: 0, skipped: 0, failed: 1 },
        });
      }
      if (path.endsWith('/resume') && init?.method === 'POST') {
        resumed = true;
        return jsonResponse({ ...failedTransfer, status: 'QUEUED', attempt_count: 2 }, 202);
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [failedTransfer] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    await screen.findByText('TRF-2026-000099');
    fireEvent.click(screen.getByRole('button', { name: /Ver archivos de TRF-2026-000099/i }));
    expect(await screen.findByText('/Projects/report.pdf')).toBeVisible();
    expect(screen.getByText('NETWORK')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /Reanudar transferencia TRF-2026-000099/i }));

    await waitFor(() => expect(resumed).toBe(true));
    expect(await screen.findByText('La transferencia se reanudó.')).toBeVisible();
  });

  it('omits a confirmed source failure through the audited retry endpoint', async () => {
    vi.stubGlobal('EventSource', TestEventSource);
    const failedTransfer = {
      id: 'mega-transfer',
      public_id: 'TRF-2026-000013',
      status: 'FAILED',
      attempt_count: 2,
      progress_percent: 45,
      files_total: 3,
      files_completed: 1,
      bytes_total: 110,
      bytes_transferred: 100,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      estimated_remaining_seconds: null,
      source_connection_id: 'mega',
      destination_connection_id: 'seafile',
      source_paths: ['/Cursos'],
      destination_path: '/Archive',
      conflict_policy: 'KEEP_BOTH',
      manifest_summary: { copied: 1, failed: 1, skipped: 0, pending: 1, omitted: 0, omitted_bytes: 0 },
    };
    const failedManifest = {
      items: [{
        id: 'mega-manifest-item',
        source_path: '/Cursos/Curso Pimsleur.rar',
        destination_path: '/Archive/Cursos/Curso Pimsleur.rar',
        requested_destination_path: '/Archive/Cursos/Curso Pimsleur.rar',
        rename_reason: null,
        is_dir: false,
        size_bytes: 10,
        modified_at: null,
        scan_complete: true,
        status: 'FAILED',
        error_code: 'MEGA_FILE_RESTRICTED',
        verified_by: null,
        can_skip: true,
        skip_unavailable_reason: null,
        can_retry_omission: false,
        omission_kind: null,
        omitted_at: null,
      }],
      next_cursor: null,
      counts: { total: 3, pending: 1, copied: 1, skipped: 0, failed: 1, omitted: 0, omitted_bytes: 0 },
    };
    let currentTransfer = failedTransfer;
    let skipPayload: Record<string, unknown> | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) {
        return jsonResponse({ items: [
          { id: 'mega', name: 'MEGA', provider_type: 'MEGA', status: 'ACTIVE', credentials_configured: true },
          { id: 'seafile', name: 'Seafile', provider_type: 'SEAFILE', status: 'ACTIVE', credentials_configured: true },
        ] });
      }
      if (path.endsWith('/items?limit=100')) return jsonResponse(failedManifest);
      if (path.endsWith('/items/mega-manifest-item/skip-and-resume') && init?.method === 'POST') {
        skipPayload = JSON.parse(String(init.body)) as Record<string, unknown>;
        currentTransfer = {
          ...failedTransfer,
          status: 'QUEUED',
          attempt_count: 3,
          files_completed: 2,
          manifest_summary: { copied: 1, failed: 0, skipped: 1, pending: 1, omitted: 1, omitted_bytes: 10 },
        };
        return jsonResponse(currentTransfer, 202);
      }
      if (isTransferCollectionRequest(path)) return jsonResponse({ items: [currentTransfer] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(await screen.findByRole('button', { name: /Ver archivos de TRF-2026-000013/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Omitir este archivo y continuar' }));
    const dialog = await screen.findByRole('dialog', { name: /Confirmar omisión del archivo/i });
    expect(within(dialog).getByText(
      'Este archivo no se copiará. Los archivos ya copiados se conservarán y la transferencia continuará con los restantes',
    )).toBeVisible();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Confirmar omisión' }));

    await waitFor(() => expect(skipPayload).not.toBeNull());
    expect(skipPayload).toMatchObject({
      expected_attempt: 2,
      reason: 'USER_ACCEPTED_SOURCE_FAILURE',
      request_id: expect.stringMatching(/[0-9a-f-]{36}/i),
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/transfers/mega-transfer/items/mega-manifest-item/skip-and-resume',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(await screen.findByText(/Archivo omitido\. La transferencia continuará/i)).toBeVisible();
  });

  it('accepts parent omissions for the displayed attempt and leaves dispatch to the queue', async () => {
    const child = {
      id: 'child-transfer',
      public_id: 'TRF-2026-000300',
      status: 'QUEUED',
      attempt_count: 1,
      progress_percent: 0,
      files_total: 1,
      files_completed: 0,
      bytes_total: 10,
      bytes_transferred: 0,
      current_item: null,
      average_speed_bps: null,
      current_speed_bps: null,
      estimated_remaining_seconds: null,
      source_connection_id: 'source',
      destination_connection_id: 'destination',
      source_paths: ['/child'],
      destination_path: '/Archive',
      scheduled_at: null,
      execution_mode: 'AUTO',
      depends_on_transfer_id: 'parent-transfer',
      wait_reason: 'BLOCKED_PARENT_OMISSIONS',
    };
    const parent = {
      ...child,
      id: 'parent-transfer',
      public_id: 'TRF-2026-000299',
      status: 'COMPLETED_WITH_WARNINGS',
      attempt_count: 7,
      depends_on_transfer_id: null,
      wait_reason: null,
      manifest_summary: { copied: 9, failed: 0, skipped: 1, pending: 0, omitted: 1, omitted_bytes: 0 },
    };
    let acceptance: Record<string, unknown> | null = null;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/auth/me')) return jsonResponse({ detail: 'Autenticación requerida' }, 401);
      if (path.endsWith('/auth/csrf')) {
        document.cookie = 'ctm_csrf=csrf-value; path=/';
        return jsonResponse({ csrf_token: 'csrf-value' });
      }
      if (path.endsWith('/auth/login')) {
        return jsonResponse({ id: 'user-1', username: 'admin', email: 'admin@example.test', role: 'ADMIN' });
      }
      if (path.endsWith('/connections')) return jsonResponse({ items: [] });
      if (path === '/api/v1/transfers/parent-transfer') return jsonResponse(parent);
      if (path.endsWith('/accept-parent-omissions') && init?.method === 'POST') {
        acceptance = JSON.parse(String(init.body)) as Record<string, unknown>;
        return jsonResponse(child, 202);
      }
      if (isTransferCollectionRequest(path)) {
        if (path.includes('pending_only=true')) return jsonResponse({ items: [child], total_count: 1 });
        if (path.includes('background_only=true')) return jsonResponse({ items: [child], total_count: 1 });
        return jsonResponse({ items: [], total_count: 0 });
      }
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('tab', { name: 'Programaciones' }));
    fireEvent.click(await screen.findByRole('button', { name: /Revisar omisiones del padre/i }));
    const dialog = await screen.findByRole('dialog', { name: /Aceptar omisiones de la transferencia padre/i });
    expect(within(dialog).getByText(/TRF-2026-000299 terminó con 1 archivo omitido/i)).toBeVisible();
    expect(within(dialog).getByText(/Esta aceptación aplica solo al intento 7/i)).toBeVisible();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Aceptar omisiones del intento 7' }));

    await waitFor(() => expect(acceptance).not.toBeNull());
    expect(acceptance).toMatchObject({
      expected_parent_attempt: 7,
      request_id: expect.stringMatching(/[0-9a-f-]{36}/i),
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/transfers/child-transfer/accept-parent-omissions',
      expect.objectContaining({ method: 'POST' }),
    );
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/dispatch'))).toBe(false);
  });
});
