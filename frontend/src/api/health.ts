export type HealthState =
  | { kind: 'checking' }
  | { kind: 'connected'; version: string }
  | { kind: 'unavailable' }

interface HealthResponse {
  status: 'ok'
  version: string
}

function isHealthResponse(value: unknown): value is HealthResponse {
  return typeof value === 'object' && value !== null &&
    'status' in value && value.status === 'ok' &&
    'version' in value && typeof value.version === 'string' && value.version.trim().length > 0
}

/** Check the real application endpoint; never infer business readiness. */
export async function getHealth(request: typeof fetch = fetch): Promise<HealthState> {
  try {
    const response = await request('/api/v1/health', {
      cache: 'no-store',
      signal: AbortSignal.timeout(5000),
    })
    if (!response.ok) return { kind: 'unavailable' }
    const body: unknown = await response.json()
    if (!isHealthResponse(body)) return { kind: 'unavailable' }
    return { kind: 'connected', version: body.version }
  } catch {
    // Network errors, timeout or invalid JSON are visible as a failed connection.
    return { kind: 'unavailable' }
  }
}
