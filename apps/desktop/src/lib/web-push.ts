import { browserPageBasePath } from './browser-settings'
import { hostHas } from './host-capabilities'

export type WebPushKind = 'cron' | 'reply' | 'approval' | 'input'
export type WebPushKinds = Record<WebPushKind, boolean>
export interface WebPushTarget {
  profile: string
  session_id?: string
  room_id?: string
  prompt_id?: string
  admission_id?: string
  generation?: number
  message_id?: number | string
  turn_id?: string
  message_event_id?: string
  job_id?: string
  execution_id?: string
  execution_generation?: number
  session_profile?: string
  task_id?: string
  member_id?: string
}
export interface WebPushEvent {
  id: string
  kind: WebPushKind
  title: string
  body?: string
  target: WebPushTarget
  created_at: number
}
export interface WebPushStatus {
  available: boolean
  public_key: string | null
  device_id: string | null
  subscription: PushSubscriptionJSON | null
  kinds: WebPushKinds
  preview: boolean
  last_status?: number | null
  last_error?: string | null
  last_sent_at?: number | null
}
export const WEB_PUSH_KINDS: readonly WebPushKind[] = ['cron', 'reply', 'approval', 'input']
export const DEFAULT_WEB_PUSH_KINDS: WebPushKinds = { cron: true, reply: true, approval: true, input: true }

export function webPushCapability(): 'supported' | 'insecure' | 'unsupported' {
  if (!hostHas('browserNotifications')) { return 'unsupported' }
  if (!window.isSecureContext) { return 'insecure' }
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
    ? 'supported' : 'unsupported'
}

export class WebPushApiError extends Error {
  constructor(readonly status: number) {
    super(`Notification request failed (${status})`)
    this.name = 'WebPushApiError'
  }
}

/** Device credentials are HttpOnly cookies; none enter URLs, storage or the worker. */
export async function webPushRequest<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch(`${browserPageBasePath()}/api/webapp/push${path}`, {
    cache: 'no-store',
    credentials: 'same-origin',
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(10_000)
  })
  if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
    throw new WebPushApiError(response.status)
  }
  return response.json() as Promise<T>
}

let registrationPromise: Promise<ServiceWorkerRegistration> | undefined

export function registerWebPushWorker(): Promise<ServiceWorkerRegistration> {
  if (!registrationPromise) {
    const base = browserPageBasePath()
    registrationPromise = navigator.serviceWorker.register(`${base}/sw.js`, {
      scope: `${base}/`, updateViaCache: 'none'
    }).catch(error => {
      registrationPromise = undefined
      throw error
    })
  }
  return registrationPromise
}

export async function activeWebPushWorker(): Promise<ServiceWorkerRegistration> {
  const registration = await registerWebPushWorker()
  if (registration.active) { return registration }
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => { cleanup(); reject(new Error('Service worker activation timed out')) }, 15_000)
    const worker = registration.installing ?? registration.waiting
    const cleanup = () => { clearTimeout(timer); worker?.removeEventListener('statechange', changed) }
    const changed = () => {
      if (registration.active) { cleanup(); resolve(registration) }
      else if (worker?.state === 'redundant') { cleanup(); reject(new Error('Service worker installation failed')) }
    }
    worker?.addEventListener('statechange', changed)
    changed()
  })
}

export function applicationServerKey(encoded: string): Uint8Array<ArrayBuffer> {
  const padded = encoded.replace(/-/g, '+').replace(/_/g, '/')
  const bytes = atob(padded.padEnd(Math.ceil(padded.length / 4) * 4, '='))
  return Uint8Array.from(bytes, character => character.charCodeAt(0))
}

// Read by the existing browser bridge without importing stores back into it.
let coveredKinds: WebPushKinds | null = null
export function setWebPushCoverage(kinds: WebPushKinds | null): void { coveredKinds = kinds }
export function serverCoversBrowserNotification(kind?: string): boolean {
  if (!coveredKinds) { return false }
  // The server owns these preferences while subscribed, including a kind switched OFF.
  return kind === 'approval' || kind === 'input' || kind === 'turnDone' || kind === 'turnError'
}

export async function clearLocalWebPush(): Promise<void> {
  setWebPushCoverage(null)
  if (!('serviceWorker' in navigator)) { return }
  const registration = await navigator.serviceWorker.getRegistration(`${browserPageBasePath()}/`)
  const subscription = await registration?.pushManager.getSubscription()
  if (subscription) { await subscription.unsubscribe() }
  const notifications = await registration?.getNotifications()
  notifications?.forEach(notification => notification.close())
}
