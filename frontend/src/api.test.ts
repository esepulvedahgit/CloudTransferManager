import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiRequest } from './api';

describe('apiRequest', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    document.cookie = 'ctm_csrf=; Max-Age=0; path=/';
  });

  it('sends session cookies with same-origin requests', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await apiRequest('/api/v1/auth/me');

    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: 'include' });
  });

  it('sends the CSRF cookie value as a header for state-changing requests', async () => {
    document.cookie = 'ctm_csrf=csrf-value; path=/';
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"ok":true}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await apiRequest('/api/v1/transfers', { method: 'POST', body: '{}' });

    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(options.headers).get('X-CSRF-Token')).toBe('csrf-value');
  });

  it('shows a safe retry hint instead of raw HTML for a gateway timeout', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      '<html><title>504 Gateway Time-out</title></html>',
      { status: 504, headers: { 'content-type': 'text/html' } },
    )));

    await expect(apiRequest('/api/v1/transfers', { method: 'POST', body: '{}' }))
      .rejects.toThrow('La solicitud tardó más de lo esperado. Revisa el historial antes de volver a intentarlo.');
  });

  it('extracts a readable message from validation errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: [{ msg: 'Value error, Selecciona como máximo 100 rutas por transferencia.' }] }),
      { status: 422, headers: { 'content-type': 'application/json' } },
    )));

    await expect(apiRequest('/api/v1/transfers', { method: 'POST', body: '{}' }))
      .rejects.toThrow('Selecciona como máximo 100 rutas por transferencia.');
  });
});
