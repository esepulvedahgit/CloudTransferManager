import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MetricsDashboard from './MetricsDashboard';
import { metricsConnections, metricsFixture, metricsUser } from './test-fixtures/metrics';
import type { MetricsUser } from './metricsModel';

vi.mock('@mui/x-charts', () => ({
  BarChart: () => <div>Gráfico de proveedores</div>,
  LineChart: () => <div>Gráfico diario</div>,
  PieChart: () => <div>Gráfico de tamaños</div>,
}));

function renderDashboard(fetchDashboard: () => Promise<Response>, user: MetricsUser = metricsUser) {
  const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((path) => (
    String(path).startsWith('/api/v1/users')
      ? Promise.resolve(json({ items: [metricsUser] }))
      : fetchDashboard()
  ));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0, gcTime: 0 } } });
  const view = render(<QueryClientProvider client={client}><MetricsDashboard user={user} connections={metricsConnections} /></QueryClientProvider>);
  return { ...view, fetchMock, client };
}

function json(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'Content-Type': 'application/json' } });
}

function select(label: string, option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: label }));
  fireEvent.click(screen.getByRole('option', { name: option }));
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('Métricas: filtros, cargas y datos dinámicos', () => {
  it('keeps filters available during the first load and removes test data controls', () => {
    renderDashboard(() => new Promise<Response>(() => {}));
    expect(screen.getByRole('combobox', { name: 'Período' })).toBeInTheDocument();
    expect(screen.getByLabelText('Cargando métricas')).toBeInTheDocument();
    expect(screen.queryByText('Incluir datos de prueba')).not.toBeInTheDocument();
  });

  it('can retry an initial failure without losing the filters', async () => {
    let failed = true;
    renderDashboard(() => Promise.resolve(failed ? json({ detail: 'Servicio temporalmente no disponible' }, 503) : json(metricsFixture())));
    expect(await screen.findByText('No se pudieron cargar las métricas.')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Período' })).toBeInTheDocument();
    failed = false;
    fireEvent.click(screen.getByRole('button', { name: 'Reintentar' }));
    expect(await screen.findByText('Archivos copiados')).toBeInTheDocument();
  });

  it('applies a group of filters once and passes an abort signal for obsolete requests', async () => {
    const load = vi.fn(() => Promise.resolve(json(metricsFixture())));
    const { fetchMock } = renderDashboard(load);
    await screen.findByText('Archivos copiados');
    select('Nube de origen', 'MEGA');
    expect(load).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Aplicar filtros' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('source_provider=MEGA'), expect.objectContaining({ signal: expect.any(AbortSignal) })));
    expect(screen.queryByText('Incluir datos de prueba')).not.toBeInTheDocument();
    select('Nube de destino', 'Todas');
    expect(screen.queryByRole('option', { name: 'Mock' })).not.toBeInTheDocument();
  });

  it('validates dates inline while retaining the applied results', async () => {
    const load = vi.fn(() => Promise.resolve(json(metricsFixture())));
    renderDashboard(load);
    await screen.findByText('Archivos copiados');
    select('Período', 'Personalizado');
    fireEvent.change(screen.getByLabelText('Desde'), { target: { value: '2026-10-20' } });
    fireEvent.change(screen.getByLabelText('Hasta'), { target: { value: '2026-10-18' } });
    expect(screen.getByText('La fecha desde debe ser anterior o igual a la fecha hasta.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Aplicar filtros' })).toBeDisabled();
    expect(screen.getByText('Archivos copiados')).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText('Desde'), { target: { value: '2026-10-01' } });
    expect(screen.getByRole('button', { name: 'Aplicar filtros' })).toBeEnabled();
  });

  it('preserves the last results on a refresh failure and marks health as stale', async () => {
    let failed = false;
    renderDashboard(() => Promise.resolve(failed ? json({ detail: 'No disponible' }, 503) : json(metricsFixture())));
    await screen.findByText('Archivos copiados');
    failed = true;
    fireEvent.click(screen.getByRole('button', { name: 'Actualizar métricas' }));
    expect(await screen.findByText(/No se pudo actualizar/)).toBeInTheDocument();
    expect(screen.getByText('Archivos copiados')).toBeInTheDocument();
    expect(screen.getByText('Sin actualizar')).toBeInTheDocument();
    expect(screen.queryByText('Operativa')).not.toBeInTheDocument();
  });

  it('counts transfers with warnings without summing overlapping file categories', async () => {
    renderDashboard(() => Promise.resolve(json(metricsFixture())));
    const card = await screen.findByLabelText('Transferencias con advertencias');
    expect(within(card).getByText('2')).toBeInTheDocument();
    expect(within(card).queryByText('42')).not.toBeInTheDocument();
  });

  it('does not show a successful platform when a queue has lost its consumer', async () => {
    const data = metricsFixture();
    data.system.queues[2].consumer_ready = false;
    renderDashboard(() => Promise.resolve(json(data)));
    expect(await screen.findByText('Con incidencias')).toBeInTheDocument();
    expect(screen.getByText('Sin consumidor')).toBeInTheDocument();
  });

  it('keeps the visible results while a refresh is pending and then updates them', async () => {
    let finishRefresh: (response: Response) => void = () => {};
    let loaded = false;
    renderDashboard(() => {
      if (!loaded) { loaded = true; return Promise.resolve(json(metricsFixture())); }
      return new Promise(resolve => { finishRefresh = resolve; });
    });
    const card = await screen.findByLabelText('Archivos copiados');
    expect(within(card).getByText('121')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Actualizar métricas' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Actualizar métricas' })).toBeDisabled());
    expect(within(card).getByText('121')).toBeInTheDocument();
    const next = metricsFixture(); next.summary.files.copied = 122;
    await act(async () => finishRefresh(json(next)));
    expect(await within(card).findByText('122')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Actualizar métricas' })).toBeEnabled();
  });

  it('hides admin health and user filters for a regular user', async () => {
    const { fetchMock } = renderDashboard(() => Promise.resolve(json(metricsFixture())), { ...metricsUser, role: 'USER' });
    await screen.findByText('Archivos copiados');
    expect(screen.queryByText('Salud de la plataforma')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Usuario' })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes('/users'))).toBe(false);
  });
});
