import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { applicationServerKey, DEFAULT_WEB_PUSH_KINDS, serverCoversBrowserNotification, setWebPushCoverage, WebPushApiError, webPushRequest } from './web-push'

vi.mock('./browser-settings', () => ({ browserPageBasePath: () => '/hermes' }))

describe('Web Push browser boundary', () => {
  beforeEach(() => setWebPushCoverage(null))
  afterEach(() => { vi.unstubAllGlobals(); setWebPushCoverage(null) })

  it('sends settings with same-origin cookies and no token-bearing URL or cache', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ ok: true }), { headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetch)
    await webPushRequest('/preferences', 'PATCH', { preview: false })
    expect(fetch).toHaveBeenCalledWith('/hermes/api/webapp/push/preferences', expect.objectContaining({
      cache: 'no-store', credentials: 'same-origin', method: 'PATCH', body: '{"preview":false}'
    }))
  })

  it('does not interpret a login HTML redirect as a successful API response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>login</html>', { headers: { 'content-type': 'text/html' } })))
    await expect(webPushRequest('/status')).rejects.toBeInstanceOf(WebPushApiError)
  })

  it('suppresses only kinds covered by a live server subscription', () => {
    expect(serverCoversBrowserNotification('approval')).toBe(false)
    setWebPushCoverage({ ...DEFAULT_WEB_PUSH_KINDS, reply: false })
    expect(serverCoversBrowserNotification('approval')).toBe(true)
    expect(serverCoversBrowserNotification('turnDone')).toBe(true)
    expect(serverCoversBrowserNotification('plugin')).toBe(false)
    expect([...applicationServerKey('AQID-_8')]).toEqual([1, 2, 3, 251, 255])
  })
})
