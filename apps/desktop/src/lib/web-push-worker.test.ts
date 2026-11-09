// @vitest-environment node
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

import { describe, expect, it, vi } from 'vitest'

const source = readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8')

function worker() {
  const handlers: Record<string, (event: unknown) => void> = {}
  const showNotification = vi.fn(async () => undefined)
  const openWindow = vi.fn(async () => undefined)
  const notifications = [{ tag: 'hermes:push:seen', close: vi.fn() }, { tag: 'hermes:push:other', close: vi.fn() }]
  const client = { url: 'https://hermes.example/', postMessage: vi.fn(), navigate: vi.fn(async () => undefined), focus: vi.fn(async () => undefined) }

  const self = {
    location: { origin: 'https://hermes.example' },
    registration: { scope: 'https://hermes.example/', showNotification, getNotifications: async () => notifications },
    clients: { matchAll: vi.fn(async () => [client]), openWindow, claim: async () => undefined },
    skipWaiting: async () => undefined,
    addEventListener: (name: string, handler: (event: unknown) => void) => { handlers[name] = handler }
  }

  vm.runInNewContext(source, { self, URL, Set })

  const dispatch = async (name: string, event: Record<string, unknown>) => {
    let completion: Promise<unknown> | undefined
    handlers[name]({ ...event, waitUntil: (pending: Promise<unknown>) => { completion = pending } })
    await completion
  }

  return { dispatch, handlers, showNotification, client, openWindow, notifications }
}

describe('PWA service worker', () => {
  it('shows received pushes with an event tag and a bounded preview, without any fetch/cache handler', async () => {
    const app = worker()
    await app.dispatch('push', { data: { json: () => ({ v: 1, id: 'abc', title: 'Hermes', body: 'a'.repeat(180), url: 'https://attacker.example/' }) } })
    expect(app.showNotification).toHaveBeenCalledWith('Hermes', expect.objectContaining({ tag: 'hermes:push:abc', body: 'a'.repeat(160), data: { event_id: 'abc', url: 'https://hermes.example/#/push/open/abc' } }))
    expect(app.handlers.fetch).toBeUndefined()
  })

  it('always displays a received push even after an ack, while a same-origin ack may close an existing card', async () => {
    const app = worker()
    await app.dispatch('message', { source: { url: 'https://attacker.example/' }, data: { type: 'hermes:push-ack', event_ids: ['wrong'] } })
    expect(app.notifications[0].close).not.toHaveBeenCalled()
    await app.dispatch('message', { source: { url: 'https://hermes.example/' }, data: { type: 'hermes:push-ack', event_ids: ['seen'] } })
    await app.dispatch('push', { data: { json: () => ({ id: 'seen' }) } })
    expect(app.showNotification).toHaveBeenCalledTimes(1)
    expect(app.notifications[0].close).toHaveBeenCalledOnce()
    expect(app.notifications[1].close).not.toHaveBeenCalled()
  })

  it('opens only the authenticated event route on a click, ignoring payload URLs', async () => {
    const app = worker()
    await app.dispatch('notificationclick', { notification: { close: vi.fn(), data: { event_id: 'event', url: 'javascript:alert(1)' } } })
    expect(app.client.navigate).toHaveBeenCalledWith('https://hermes.example/#/push/open/event')
    expect(app.client.focus).toHaveBeenCalled()
    expect(app.openWindow).not.toHaveBeenCalled()
  })
})
