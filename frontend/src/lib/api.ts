export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const res = await fetch(path, { ...options, cache: 'no-store', credentials: 'include', headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'API is unavailable. Please try again.' }));
    const detail = Array.isArray(err.detail)
      ? err.detail.map((item: { msg?: string }) => item.msg || 'Invalid value').join('. ')
      : typeof err.detail === 'string' ? err.detail : 'API request failed';
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}
