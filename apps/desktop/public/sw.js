/* Hermes Web Push. Deliberately no fetch handler or offline/auth/transcript cache. */
'use strict'

const safeId = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,160}$/.test(value)
const base = new URL(self.registration.scope)
const eventUrl = id => new URL(`#/push/open/${encodeURIComponent(id)}`, base).href

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()))
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))

self.addEventListener('message', event => {
  if (!event.source || new URL(event.source.url).origin !== self.location.origin) { return }
  if (event.data?.type !== 'hermes:push-ack' || !Array.isArray(event.data.event_ids)) { return }
  const tags = new Set(event.data.event_ids.slice(0, 100).filter(safeId).map(id => `hermes:push:${id}`))
  event.waitUntil((async () => {
    const notifications = await self.registration.getNotifications()
    for (const notification of notifications) { if (tags.has(notification.tag)) { notification.close() } }
  })())
})

self.addEventListener('push', event => {
  event.waitUntil((async () => {
    let payload
    try { payload = event.data?.json() } catch { payload = null }
    const id = safeId(payload?.id) ? payload.id : null
    const title = typeof payload?.title === 'string' ? Array.from(payload.title).slice(0, 160).join('') : 'Hermes'
    const body = typeof payload?.body === 'string' ? Array.from(payload.body).slice(0, 160).join('') : undefined
    // The server owns the pre-send observation window. Every received push remains visible.
    await self.registration.showNotification(title || 'Hermes', {
      body,
      tag: id ? `hermes:push:${id}` : 'hermes:push:unknown',
      icon: new URL('pwa/icon-192.png', base).href,
      badge: new URL('pwa/icon-192.png', base).href,
      data: { event_id: id, url: id ? eventUrl(id) : base.href }
    })
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of windows) { client.postMessage({ type: 'hermes:push-received', event_id: id }) }
  })())
})

self.addEventListener('notificationclick', event => {
  event.notification.close()
  event.waitUntil((async () => {
    const data = event.notification.data
    const id = safeId(data?.event_id) ? data.event_id : null
    const url = id ? eventUrl(id) : base.href
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const client = windows.find(candidate => new URL(candidate.url).origin === self.location.origin && candidate.url.startsWith(base.href))
    if (client) {
      if (data?.local_session_id && typeof data.local_session_id === 'string') {
        client.postMessage({ type: 'hermes:local-focus-session', session_id: data.local_session_id })
      } else { await client.navigate(url) }
      await client.focus()
    } else {
      const target = data?.local_session_id && typeof data.local_session_id === 'string'
        ? new URL(`#/${encodeURIComponent(data.local_session_id)}`, base).href : url
      await self.clients.openWindow(target)
    }
  })())
})
