import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({ getSession: vi.fn(), request: vi.fn(), openSession: vi.fn(), hint: vi.fn(),
  groups: vi.fn(), registerGroup: vi.fn(), openGroup: vi.fn(), tiles: [] as unknown[] }))
vi.mock('@/hermes', () => ({ getSession: api.getSession }))
vi.mock('@/lib/web-push', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/web-push')>(), webPushRequest: api.request }))
vi.mock('@/store/session', () => ({ $selectedStoredSessionId: { get: () => 'already-open' }, setSessionOwnerHint: api.hint }))
vi.mock('@/store/session-states', () => ({ $sessionTiles: { get: () => api.tiles }, $botChatScopes: { get: () => ({}) } }))
vi.mock('@/sdk', () => ({ host: { openSession: api.openSession } }))
vi.mock('@/plugins/hermes-bots/canonical-chat', () => ({ resolveExpectHistory: (row: { message_count?: number }) => (row.message_count ?? 1) > 0 }))
vi.mock('@/plugins/hermes-bots/canonical-groups', () => ({ discoverCanonicalGroups: api.groups }))
vi.mock('@/plugins/hermes-bots/canonical-group-registry', () => ({ registerCanonicalGroup: api.registerGroup }))
vi.mock('@/plugins/hermes-bots/group-chat-view', () => ({ openGroupChat: api.openGroup }))

import type { WebPushEvent } from '@/lib/web-push'

import { openWebPushTarget, PushNotificationOpenView } from './push-notification-open'

const event: WebPushEvent = { id: 'abc', kind: 'reply', title: 'Response ready', body: 'Saved reply', created_at: 1,
  target: { profile: 'writer', session_id: 'saved' } }
const owner = { connectionId: 'local', profile: 'writer', targetProfile: 'writer', mode: 'local' as const }
const originalBridge = window.hermesDesktop

beforeEach(() => {
  vi.clearAllMocks()
  api.tiles = []
  window.hermesDesktop = { ...window.hermesDesktop, getProfileRoutes: vi.fn(async () => [owner]) } as Window['hermesDesktop']
  api.getSession.mockResolvedValue({ id: 'saved', title: 'Bot Chat' })
  api.request.mockResolvedValue(event)
})
afterEach(() => { window.hermesDesktop = originalBridge })

describe('notification target opening', () => {
  it('reads the exact saved owner session and uses the existing open door without creating a Bot chat', async () => {
    const navigate = vi.fn()
    expect(await openWebPushTarget(event, navigate)).toBe(true)
    expect(api.getSession).toHaveBeenCalledWith('saved', { connectionId: 'local', profile: 'writer' })
    expect(api.openSession).toHaveBeenCalledWith('saved', expect.objectContaining({ route: owner, workspaceMode: 'bots', awaitHydration: true, forceResume: true, expectHistory: true }))
    expect(api.openGroup).not.toHaveBeenCalled()
  })

  it('forces fresh transcript hydration even when the notification reselects an already mounted tile', async () => {
    api.tiles = [{ storedSessionId: 'saved', workspaceMode: 'bots', workspaceOwnerKey: 'existing-owner', ownerRoute: owner }]
    api.getSession.mockResolvedValue({ id: 'saved', title: 'Bot Chat', message_count: 3 })
    await openWebPushTarget(event, vi.fn())
    expect(api.openSession).toHaveBeenCalledWith('saved', expect.objectContaining({
      awaitHydration: true, forceResume: true, expectHistory: true, workspaceOwnerKey: 'existing-owner', route: owner
    }))
  })

  it('opens an existing canonical room on its owner and never the hidden member session', async () => {
    const room = { room_id: 'room', name: 'Team' }
    api.groups.mockResolvedValue({ rooms: [room] }); api.registerGroup.mockReturnValue('canonical:local:writer:room')
    await openWebPushTarget({ ...event, target: { ...event.target, room_id: 'room', session_profile: 'hidden-member' } }, vi.fn())
    expect(api.groups).toHaveBeenCalledWith({ connectionId: 'local', profile: 'writer' })
    expect(api.openGroup).toHaveBeenCalledWith('canonical:local:writer:room', 'key')
    expect(api.getSession).not.toHaveBeenCalled()
  })

  it('does not focus a target when navigation overtakes a saved-session read', async () => {
    let current = true
    api.getSession.mockImplementation(async () => { current = false; return { id: 'saved' } })
    const navigate = vi.fn()
    expect(await openWebPushTarget(event, navigate, () => current)).toBe(false)
    expect(api.openSession).not.toHaveBeenCalled()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('keeps a no-agent cron notification readable without running the job', async () => {
    api.request.mockResolvedValue({ ...event, kind: 'cron', target: { profile: 'writer', job_id: 'job-one', execution_id: 'execution-one' } })
    render(<MemoryRouter initialEntries={['/push/open/abc']}><Routes><Route path="/push/open/:eventId" element={<PushNotificationOpenView />} /></Routes></MemoryRouter>)
    await waitFor(() => expect(screen.getByText('execution-one')).toBeInTheDocument())
    expect(screen.getByText(/This result has no saved conversation/)).toBeInTheDocument()
    expect(api.getSession).not.toHaveBeenCalled()
    expect(api.openSession).not.toHaveBeenCalled()
  })
})
