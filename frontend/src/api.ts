export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function readCookie(name: string): string | undefined {
  const prefix = `${name}=`;
  const entry = document.cookie.split('; ').find((cookie) => cookie.startsWith(prefix));
  return entry ? decodeURIComponent(entry.slice(prefix.length)) : undefined;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const csrfToken = readCookie('ctm_csrf');
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
  }

  const response = await fetch(path, { ...init, method, headers, credentials: 'include' });
  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get('content-type') ?? '';
  const payload: unknown = contentType.includes('application/json')
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message =
      typeof payload === 'object' && payload !== null && 'detail' in payload
        ? String(payload.detail)
        : typeof payload === 'string' && payload
          ? payload
          : 'No fue posible completar la solicitud.';
    throw new ApiError(message, response.status);
  }
  return payload as T;
}
