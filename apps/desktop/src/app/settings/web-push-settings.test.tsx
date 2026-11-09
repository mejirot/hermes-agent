import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'

const actions = vi.hoisted(() => ({ enable: vi.fn(), disable: vi.fn(), refresh: vi.fn(), preferences: vi.fn(), test: vi.fn() }))
vi.mock('@/store/web-push', async () => {
  const { atom } = await import('nanostores')
  return {
    $webPushDevice: atom({ state: 'ready', subscribed: false, busy: false, status: null }),
    enableWebPush: actions.enable, disableWebPush: actions.disable, refreshWebPushStatus: actions.refresh,
    setWebPushPreferences: actions.preferences, sendWebPushTest: actions.test
  }
})

import { $webPushDevice } from '@/store/web-push'

import { WebPushSettings } from './web-push-settings'

const status = { available: true, public_key: 'key', device_id: 'device', subscription: { endpoint: 'https://push.example/sub' }, preview: true,
  kinds: { cron: true, reply: true, approval: true, input: true } }

beforeEach(() => {
  vi.clearAllMocks()
  $webPushDevice.set({ state: 'ready', subscribed: false, busy: false, status: null })
})

it('asks for opt-in from the device button rather than on settings mount', () => {
  render(<WebPushSettings />)
  expect(actions.enable).not.toHaveBeenCalled()
  expect(screen.getByRole('button', { name: 'Send push test' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Enable on this device' }))
  expect(actions.enable).toHaveBeenCalledOnce()
})

it('persists the preview switch and exposes exactly the four server event preferences', () => {
  $webPushDevice.set({ state: 'ready', subscribed: true, busy: false, status })
  render(<WebPushSettings />)
  for (const label of ['Scheduled job results', 'Bot and Group replies in Hermes', 'Approval needed', 'Question needs an answer']) {
    expect(screen.getByRole('switch', { name: label })).toBeEnabled()
  }
  fireEvent.click(screen.getByRole('switch', { name: 'Show a short preview' }))
  expect(actions.preferences).toHaveBeenCalledWith({ preview: false })
  expect(screen.getByRole('button', { name: 'Disable on this device' })).toBeEnabled()
})
