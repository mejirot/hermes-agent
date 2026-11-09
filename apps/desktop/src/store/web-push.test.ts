import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ request: vi.fn(), register: vi.fn(), active: vi.fn(), coverage: vi.fn(), capability: vi.fn(), permission: vi.fn() }))
vi.mock('@/lib/browser-notifications', () => ({ browserNotificationPermission: () => api.permission() }))
vi.mock('@/lib/web-push', async importOriginal => ({
  ...await importOriginal<typeof import('@/lib/web-push')>(),
  webPushRequest: api.request, registerWebPushWorker: api.register, activeWebPushWorker: api.active,
  setWebPushCoverage: api.coverage, webPushCapability: api.capability
}))

import { $webPushDevice, disableWebPush, enableWebPush, refreshWebPushStatus, setWebPushPreferences, startWebPushPolling } from './web-push'

const status = { available: true, public_key: 'AQID', device_id: 'device', subscription: { endpoint: 'https://push.example/device' },
  kinds: { cron: true, reply: true, approval: true, input: true }, preview: true }
const subscription = { endpoint: 'https://push.example/device', toJSON: () => ({ endpoint: 'https://push.example/device' }), unsubscribe: vi.fn(async () => true) }
const worker = { postMessage: vi.fn() }
const registration = { active: worker, pushManager: { getSubscription: vi.fn(async () => subscription), subscribe: vi.fn(async () => subscription) }, getNotifications: vi.fn(async () => []) }

beforeEach(() => {
  vi.clearAllMocks()
  api.capability.mockReturnValue('supported'); api.permission.mockReturnValue('granted')
  api.register.mockResolvedValue(registration); api.active.mockResolvedValue(registration); api.request.mockResolvedValue(status)
  registration.pushManager.getSubscription.mockResolvedValue(subscription)
  registration.pushManager.subscribe.mockResolvedValue(subscription)
  $webPushDevice.set({ state: 'ready', busy: false, subscribed: false, status: null })
  vi.stubGlobal('Notification', { permission: 'granted', requestPermission: vi.fn(async () => 'granted') })
  vi.stubGlobal('navigator', { serviceWorker: { getRegistration: vi.fn(async () => registration), addEventListener: vi.fn(), removeEventListener: vi.fn() } })
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('device push subscription', () => {
  it('requires both the server device and this browser’s matching endpoint before suppressing local duplicates', async () => {
    await refreshWebPushStatus()
    expect($webPushDevice.get().subscribed).toBe(true)
    expect(api.coverage).toHaveBeenLastCalledWith(status.kinds)
    api.request.mockResolvedValue({ ...status, subscription: { endpoint: 'https://push.example/another' } })
    await refreshWebPushStatus()
    expect($webPushDevice.get().subscribed).toBe(false)
    expect(api.coverage).toHaveBeenLastCalledWith(null)
  })

  it('registers the existing browser subscription and persists preferences on the authenticated server', async () => {
    await enableWebPush()
    expect(api.request).toHaveBeenCalledWith('/subscription', 'PUT', expect.objectContaining({ subscription: subscription.toJSON() }))
    await setWebPushPreferences({ preview: false })
    expect(api.request).toHaveBeenCalledWith('/preferences', 'PATCH', { preview: false })
  })

  it('does not claim an offline disable succeeded or silently abandon the server grant', async () => {
    $webPushDevice.set({ state: 'ready', busy: false, subscribed: true, status })
    api.request.mockRejectedValue(new Error('offline'))
    await expect(disableWebPush()).rejects.toThrow('offline')
    expect($webPushDevice.get()).toMatchObject({ subscribed: true, state: 'error', busy: false })
    expect(subscription.unsubscribe).not.toHaveBeenCalled()
  })

  it('re-enables after the server device grant is lost by replacing the stale local endpoint', async () => {
    const fresh = { ...subscription, endpoint: 'https://push.example/fresh', toJSON: () => ({ endpoint: 'https://push.example/fresh' }) }
    registration.pushManager.getSubscription.mockResolvedValueOnce(subscription).mockResolvedValueOnce(fresh)
    registration.pushManager.subscribe.mockResolvedValue(fresh)
    api.request.mockImplementation(async (path: string) => path === '/status'
      ? { ...status, device_id: null, subscription: null }
      : { ...status, subscription: { endpoint: fresh.endpoint } })
    await enableWebPush()
    expect(subscription.unsubscribe).toHaveBeenCalledOnce()
    expect(registration.pushManager.subscribe).toHaveBeenCalledWith(expect.objectContaining({ userVisibleOnly: true }))
    expect(api.request).toHaveBeenCalledWith('/subscription', 'PUT', expect.objectContaining({ subscription: fresh.toJSON() }))
    expect($webPushDevice.get()).toMatchObject({ state: 'ready', subscribed: true })
  })

  it('acks only observed events while focused, never all events merely because a tab is open', async () => {
    vi.useFakeTimers()
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
    vi.spyOn(document, 'hasFocus').mockReturnValue(true)
    $webPushDevice.set({ state: 'ready', busy: false, subscribed: true, status })
    api.request.mockImplementation(async (path: string) => path === '/events' ? { events: [{ id: 'seen' }, { id: 'unseen' }] } : { ok: true })
    const stop = startWebPushPolling(event => event.id === 'seen')
    await vi.advanceTimersByTimeAsync(1)
    expect(api.request).toHaveBeenCalledWith('/ack', 'POST', { event_ids: ['seen'] })
    stop()
    vi.clearAllMocks()
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    const stopHidden = startWebPushPolling(() => true)
    await vi.advanceTimersByTimeAsync(700)
    expect(api.request).not.toHaveBeenCalled()
    stopHidden()
  })
})
