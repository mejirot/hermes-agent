import { atom } from 'nanostores'

import { browserNotificationPermission } from '@/lib/browser-notifications'
import {
  activeWebPushWorker, applicationServerKey, DEFAULT_WEB_PUSH_KINDS, registerWebPushWorker,
  setWebPushCoverage, WebPushApiError, webPushCapability, webPushRequest,
  type WebPushEvent, type WebPushKinds, type WebPushStatus
} from '@/lib/web-push'

export type WebPushState = 'loading' | 'ready' | 'unsupported' | 'insecure' | 'denied' | 'unavailable' | 'error' | 'signed-out'
export interface WebPushDeviceState {
  state: WebPushState
  busy: boolean
  subscribed: boolean
  status: WebPushStatus | null
}
export const $webPushDevice = atom<WebPushDeviceState>({ state: 'loading', busy: false, subscribed: false, status: null })

let operation = 0
let started = false
let stopPolling: (() => void) | undefined

function failureState(error: unknown): WebPushState {
  if (error instanceof WebPushApiError && (error.status === 401 || error.status === 403)) { return 'signed-out' }
  return browserNotificationPermission() === 'denied' ? 'denied' : 'error'
}

async function publishStatus(status: WebPushStatus, version: number): Promise<void> {
  const registration = await registerWebPushWorker()
  const local = await registration.pushManager.getSubscription()
  if (version !== operation) { return }
  const subscribed = Boolean(local && status.subscription?.endpoint === local.endpoint && status.device_id)
  const permission = browserNotificationPermission()
  const granted = permission === 'granted'
  setWebPushCoverage(subscribed && granted && status.available ? status.kinds : null)
  $webPushDevice.set({ busy: false, state: permission === 'denied' ? 'denied' : status.available ? 'ready' : 'unavailable', status, subscribed })
}

export async function refreshWebPushStatus(): Promise<void> {
  if ($webPushDevice.get().busy) { return }
  const capability = webPushCapability()
  if (capability !== 'supported') {
    $webPushDevice.set({ busy: false, state: capability, status: null, subscribed: false })
    return
  }
  const version = ++operation
  try {
    const status = await webPushRequest<WebPushStatus>('/status')
    if (version === operation) { await publishStatus(status, version) }
  } catch (error) {
    if (version === operation) {
      setWebPushCoverage(null)
      $webPushDevice.set({ ...$webPushDevice.get(), busy: false, state: failureState(error) })
    }
  }
}

/** Start only from a user gesture; permission must precede the first await. */
export async function enableWebPush(): Promise<void> {
  if (webPushCapability() !== 'supported') { return }
  const permission = Notification.permission === 'default' ? Notification.requestPermission() : Promise.resolve(Notification.permission)
  const version = ++operation
  $webPushDevice.set({ ...$webPushDevice.get(), busy: true })
  let newlyCreated: PushSubscription | null = null
  try {
    if (await permission !== 'granted') { throw new Error('Notification permission is required') }
    const status = await webPushRequest<WebPushStatus>('/status')
    if (!status.available || !status.public_key) { throw new Error('Push is unavailable') }
    const registration = await activeWebPushWorker()
    let subscription = await registration.pushManager.getSubscription()
    // A lost device cookie or expired server grant must not strand this origin's browser endpoint.
    if (subscription && (!status.device_id || subscription.endpoint !== status.subscription?.endpoint)) {
      if (!await subscription.unsubscribe()) { throw new Error('The previous browser subscription could not be removed') }
      subscription = null
    }
    if (!subscription) {
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: applicationServerKey(status.public_key) })
      newlyCreated = subscription
    }
    const saved = await webPushRequest<WebPushStatus>('/subscription', 'PUT', {
      subscription: subscription.toJSON(), kinds: status.kinds ?? DEFAULT_WEB_PUSH_KINDS, preview: status.preview
    })
    if (version === operation) { await publishStatus(saved, version) }
  } catch (error) {
    if (newlyCreated) { await newlyCreated.unsubscribe().catch(() => false) }
    if (version === operation) {
      $webPushDevice.set({ ...$webPushDevice.get(), busy: false, state: failureState(error) })
    }
  }
}

/** Revoke server delivery first; an offline failure remains visible and retryable. */
export async function disableWebPush(): Promise<void> {
  ++operation
  $webPushDevice.set({ ...$webPushDevice.get(), busy: true })
  try {
    await webPushRequest('/subscription', 'DELETE')
    setWebPushCoverage(null)
    const registration = await navigator.serviceWorker.getRegistration()
    const subscription = await registration?.pushManager.getSubscription()
    if (subscription && !await subscription.unsubscribe()) { throw new Error('Browser subscription could not be removed') }
    const notifications = await registration?.getNotifications()
    notifications?.forEach(notification => notification.close())
    $webPushDevice.set({ ...$webPushDevice.get(), busy: false, subscribed: false })
    await refreshWebPushStatus()
  } catch (error) {
    $webPushDevice.set({ ...$webPushDevice.get(), busy: false, state: failureState(error) })
    throw error
  }
}

export async function setWebPushPreferences(patch: { kinds?: WebPushKinds; preview?: boolean }): Promise<void> {
  const version = ++operation
  $webPushDevice.set({ ...$webPushDevice.get(), busy: true })
  try {
    const status = await webPushRequest<WebPushStatus>('/preferences', 'PATCH', patch)
    if (version === operation) { await publishStatus(status, version) }
  } catch (error) {
    if (version === operation) { $webPushDevice.set({ ...$webPushDevice.get(), busy: false, state: failureState(error) }) }
  }
}

export async function sendWebPushTest(): Promise<void> { await webPushRequest('/test', 'POST') }

/** One bounded request at a time. Background tabs do not poll or acknowledge. */
export function startWebPushPolling(observed: (event: WebPushEvent) => boolean): () => void {
  stopPolling?.()
  let stopped = false
  let running = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const poll = async () => {
    const began = Date.now()
    if (stopped || running) { return }
    running = true
    if ($webPushDevice.get().subscribed && !document.hidden && document.hasFocus()) {
      try {
        const { events } = await webPushRequest<{ events: WebPushEvent[] }>('/events')
        const ids = !document.hidden && document.hasFocus() ? events.filter(observed).map(event => event.id).slice(0, 100) : []
        if (ids.length) {
          await webPushRequest('/ack', 'POST', { event_ids: ids })
          const registration = await navigator.serviceWorker.getRegistration()
          registration?.active?.postMessage({ type: 'hermes:push-ack', event_ids: ids })
        }
      } catch (error) {
        if (failureState(error) === 'signed-out') {
          setWebPushCoverage(null)
          $webPushDevice.set({ ...$webPushDevice.get(), subscribed: false, state: 'signed-out' })
        }
      }
    }
    running = false
    if (!stopped) { timer = setTimeout(() => void poll(), Math.max(0, 650 - (Date.now() - began))) }
  }
  const wake = () => { clearTimeout(timer); void poll() }
  const message = (event: MessageEvent) => { if (event.data?.type === 'hermes:push-received') { wake() } }
  navigator.serviceWorker?.addEventListener('message', message)
  document.addEventListener('visibilitychange', wake)
  window.addEventListener('focus', wake)
  void poll()
  const stop = () => {
    stopped = true; clearTimeout(timer)
    navigator.serviceWorker?.removeEventListener('message', message)
    document.removeEventListener('visibilitychange', wake)
    window.removeEventListener('focus', wake)
  }
  stopPolling = stop
  return stop
}

export function startWebPush(): void {
  if (started || webPushCapability() !== 'supported') { return }
  started = true
  void refreshWebPushStatus()
  window.addEventListener('focus', () => void refreshWebPushStatus())
}
