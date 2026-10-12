import { cleanup, fireEvent, render, screen, waitFor, waitForElementToBeRemoved, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  });
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

describe('Cloud Transfer Manager', () => {
  afterEach(() => {
    cleanup();
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
      conflict_policy: 'OVERWRITE_IF_DIFFERENT',
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [transfer] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);

    expect(await screen.findByText('Duración: 1 min 5 s')).toBeVisible();
    const requestedAt = document.querySelector('time[datetime="2026-10-11T10:15:00.000Z"]');
    expect(requestedAt).toBeVisible();
    expect(requestedAt).toHaveTextContent(/2026/);
  });

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
      if (path.endsWith('/connections') || path.endsWith('/transfers')) {
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [], total_count: 0 });
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
      if (path.endsWith('/connections') || path.endsWith('/transfers')) return jsonResponse({ items: [] });
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

  it('summarizes the selected route, providers and destination before transfer', async () => {
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
        { id: 'source', name: 'Drive de operaciones', provider_type: 'GOOGLE_DRIVE', status: 'ACTIVE', credentials_configured: true },
        { id: 'destination', name: 'MEGA de respaldo', provider_type: 'MEGA', status: 'ACTIVE', credentials_configured: true },
      ] });
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
    expect(within(summary).getByRole('img', { name: 'Google Drive' })).toBeVisible();
    expect(within(summary).getByRole('img', { name: 'MEGA' })).toBeVisible();
    expect(within(summary).getByText('Drive de operaciones')).toBeVisible();
    expect(within(summary).getByText('MEGA de respaldo')).toBeVisible();
    expect(within(summary).getByText('Carpeta raíz')).toBeVisible();
    expect(within(summary).getByRole('combobox', { name: 'Si ya existe' })).toBeVisible();
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
        conflict_policy: 'OVERWRITE_IF_DIFFERENT',
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
        if (path.endsWith('/transfers') && init?.method === 'POST') return jsonResponse(transfer, 201);
        if (path.endsWith('/transfers')) {
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
      eventSource.emit({ ...transfer, status: 'RUNNING', progress_percent: 50, current_item: null });
      await waitFor(() => expect(screen.getByText('RUNNING')).toBeVisible());
      expect(destinationItemsRequests).toBe(1);

      completedTransfer = {
        ...transfer,
        status: terminalStatus,
        progress_percent: 100,
        files_completed: 1,
        bytes_transferred: 4,
      };
      eventSource.emit(completedTransfer);

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
      if (path.endsWith('/connections') || path.endsWith('/transfers')) return jsonResponse({ items: [] });
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.change(await screen.findByLabelText(/Usuario o correo/i), { target: { value: 'admin' } });
    fireEvent.change(screen.getByLabelText(/Contraseña/i), { target: { value: 'CorrectHorseBatteryStaple123!' } });
    fireEvent.click(screen.getByRole('button', { name: /Iniciar sesión/i }));
    fireEvent.click(await screen.findByRole('button', { name: /Administración/i }));
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('button', { name: /Administración/i }));

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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('button', { name: /Administración/i }));
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('button', { name: /Administración/i }));
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('button', { name: /Administración/i }));
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
      if (path.endsWith('/transfers')) return jsonResponse({ items: [] });
      return jsonResponse({ detail: 'Not found' }, 404);
    });

    await signIn(fetchMock);
    fireEvent.click(screen.getByRole('button', { name: /Administración/i }));
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
});
