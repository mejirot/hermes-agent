import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { onBrowserFocusSession, showBrowserNotification } from './browser-notifications'
import { DEFAULT_WEB_PUSH_KINDS, setWebPushCoverage } from './web-push'

const created: { onclick: (() => void) | null; title: string }[] = []

class FakeNotification {
  static permission: NotificationPermission = 'default'
  static requestPermission = vi.fn(async () => FakeNotification.permission)
  onclick: (() => void) | null = null
  constructor(public title: string) {
    created.push(this)
  }
  close() {}
}

describe('showBrowserNotification', () => {
  beforeEach(() => {
    created.length = 0
    FakeNotification.requestPermission.mockClear()
    vi.stubGlobal('Notification', FakeNotification)
    vi.stubGlobal('isSecureContext', true)
  })

  afterEach(() => {
    setWebPushCoverage(null)
    vi.unstubAllGlobals()
  })

  it('never asks for permission from an event (only a click in Settings may)', async () => {
    FakeNotification.permission = 'default'

    expect(await showBrowserNotification({ kind: 'approval', sessionId: 's1', title: 'Approval needed' })).toBe(false)
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled()
    expect(created).toHaveLength(0)
  })

  it('opens the notified session through onFocusSession when clicked', async () => {
    FakeNotification.permission = 'granted'
    const opened: string[] = []
    const off = onBrowserFocusSession(id => opened.push(id))
    const focus = vi.spyOn(window, 'focus').mockImplementation(() => undefined)

    expect(
      await showBrowserNotification({ focusSessionId: 'stored-1', kind: 'approval', sessionId: 'rt-1', title: 'Approval needed' })
    ).toBe(true)
    created[0].onclick?.()

    expect(focus).toHaveBeenCalled()
    expect(opened).toEqual(['stored-1'])
    off()
  })

  it('keeps the explicit local test visible while subscribed conversation events use server preferences', async () => {
    FakeNotification.permission = 'granted'
    setWebPushCoverage(DEFAULT_WEB_PUSH_KINDS)
    expect(await showBrowserNotification({ kind: 'turnDone', sessionId: 'saved', title: 'Reply ready' })).toBe(true)
    expect(created).toHaveLength(0)
    expect(await showBrowserNotification({ kind: 'turnDone', title: 'Test', body: 'Working' })).toBe(true)
    expect(created).toHaveLength(1)
  })
})
