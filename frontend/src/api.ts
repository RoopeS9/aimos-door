export type Json = Record<string, any>

// The door answers the first request after an idle auto-reset with
// X-Demo-Idle-Reset: 1. The console registers one listener for it.
let idleResetListener: (() => void) | null = null
export function onIdleReset(listener: (() => void) | null) { idleResetListener = listener }

export async function api(path: string, body?: unknown, accessCode = '', extraHeaders: Record<string, string> = {}): Promise<any> {
  const headers: Record<string, string> = { Accept: 'application/json', ...extraHeaders }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (accessCode) headers['X-Demo-Access-Code'] = accessCode
  const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' })
  const idleReset = response.headers.get('X-Demo-Idle-Reset') === '1'
  if (idleReset) idleResetListener?.()
  const content = await response.text()
  let value: any
  try { value = JSON.parse(content) } catch { value = { detail: response.ok ? content : 'The door could not complete this request.' } }
  if (!response.ok) {
    const detail = typeof value.detail === 'string' ? value.detail : value.detail?.message || value.message || `Request failed (${response.status}).`
    const error = new Error(detail) as Error & { status: number; data: Json; idleReset?: boolean }
    error.status = response.status
    error.data = value
    // The request met a freshly reset demo; the console reloads instead of reporting it.
    if (idleReset) error.idleReset = true
    throw error
  }
  return value
}

export const money = (n: unknown) => new Intl.NumberFormat('en-FI', { style: 'currency', currency: 'EUR', maximumFractionDigits: 2 }).format(Number(n || 0))
export const amount = (n: unknown) => new Intl.NumberFormat('en-FI', { maximumFractionDigits: 3 }).format(Number(n || 0))
export const prettyDate = (value?: string) => value ? new Date(value.includes('T') ? value : `${value}T12:00:00`).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) : 'Not yet confirmed'
export const readable = (value: unknown): string => typeof value === 'string' ? value.replaceAll('_', ' ') : value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value)
