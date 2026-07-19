// Fetch-based API client.
// Auth, in precedence order:
//   1. Zitadel OIDC (services/auth.ts): when the SPA is built with
//      VITE_ZITADEL_*, the signed-in user's JWT access token is sent as the
//      Bearer/auth_token credential.
//   2. Shared-token v1: operators put OPENSWEEP_AUTH_TOKEN in localStorage under
//      'opensweep_auth_token' (devtools: localStorage.setItem(...)). Carried as
//      X-OpenSweep-Auth.
// SSE/WS URLs append ?auth_token= because EventSource/WebSocket cannot set
// headers. No token → no header, local dev unchanged.
//
// 401 handling: silent-renew the OIDC token once and retry; a second 401
// means the session is gone, so redirect to interactive sign-in instead of
// leaving a dead app rendering stale data.

import { accessToken, authEnabled, renewToken, signIn } from './auth'

const API_BASE = `${import.meta.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000'}/api/v1`

function authToken(): string {
  const oidc = accessToken()
  if (oidc) return oidc
  try {
    return localStorage.getItem('opensweep_auth_token') || ''
  } catch {
    return ''
  }
}

function withAuthToken(url: string): string {
  const token = authToken()
  if (!token) return url
  return `${url}${url.includes('?') ? '&' : '?'}auth_token=${encodeURIComponent(token)}`
}

export class ApiError extends Error {
  status: number
  detail: string
  traceId: string | null
  /** Raw `detail` payload as returned by the backend. Usually a string, but
   *  dispatch endpoints return an object (e.g. {message, run_uid,
   *  scheduled_agent_uid}) on same-target 409 conflicts — keep it so callers
   *  can link to the blocking run. */
  detailBody: unknown

  constructor(status: number, detail: string, traceId: string | null, detailBody?: unknown) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.traceId = traceId
    this.detailBody = detailBody ?? detail
  }
}

function extractTraceId(res: Response): string | null {
  return res.headers.get('x-request-id') || res.headers.get('x-trace-id')
}

async function handleError(res: Response, _path?: string): Promise<never> {
  const traceId = extractTraceId(res)
  const body = await res.json().catch(() => ({ detail: res.statusText }))
  const rawDetail: unknown = body.detail
  let message = `API error: ${res.status}`
  if (typeof rawDetail === 'string' && rawDetail) {
    message = rawDetail
  } else if (rawDetail && typeof rawDetail === 'object') {
    const m = (rawDetail as Record<string, unknown>).message
    if (typeof m === 'string' && m) message = m
  }
  throw new ApiError(res.status, message, traceId, rawDetail)
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = authToken()
  if (token) headers['X-OpenSweep-Auth'] = token
  return headers
}

// One interactive-sign-in redirect per page: concurrent 401s must not stack
// signinRedirect calls while the browser is already navigating away.
let redirectingToSignIn = false

async function authFetch(path: string, init: Omit<RequestInit, 'headers'>): Promise<Response> {
  // Headers are rebuilt per attempt so the retry carries the renewed token.
  const attempt = () => fetch(`${API_BASE}${path}`, { ...init, headers: authHeaders() })
  let res = await attempt()
  if (res.status === 401 && authEnabled) {
    if (await renewToken()) res = await attempt()
    // Never start an interactive redirect while /auth/callback is mid-code-
    // exchange: navigating away corrupts the OIDC state store and loops the
    // authorize round-trip. The callback view surfaces its own errors.
    const onCallback = window.location.pathname === '/auth/callback'
    if (res.status === 401 && !redirectingToSignIn && !onCallback) {
      redirectingToSignIn = true
      // If the redirect can't even start (Zitadel unreachable), free the
      // flag so a later 401 can retry instead of being silently swallowed.
      signIn(window.location.pathname + window.location.search).catch(() => {
        redirectingToSignIn = false
      })
    }
  }
  if (!res.ok) await handleError(res, path)
  return res
}

export async function apiGet<T = unknown>(path: string): Promise<T> {
  const res = await authFetch(path, {})
  return res.json() as Promise<T>
}

export async function apiPost<T = unknown>(path: string, body?: unknown): Promise<T> {
  const res = await authFetch(path, {
    method: 'POST',
    body: body ? JSON.stringify(body) : undefined,
  })
  return res.json() as Promise<T>
}

export async function apiPatch<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(path, { method: 'PATCH', body: JSON.stringify(body) })
  return res.json() as Promise<T>
}

export async function apiPut<T = unknown>(path: string, body: unknown): Promise<T> {
  const res = await authFetch(path, { method: 'PUT', body: JSON.stringify(body) })
  return res.json() as Promise<T>
}

export async function apiDelete<T = unknown>(path: string): Promise<T | void> {
  const res = await authFetch(path, { method: 'DELETE' })
  const text = await res.text()
  return text ? (JSON.parse(text) as T) : undefined
}

export function wsUrl(path: string): string {
  return withAuthToken(`${API_BASE.replace(/^http/, 'ws')}${path}`)
}

export { API_BASE }
