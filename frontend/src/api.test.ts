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
});
