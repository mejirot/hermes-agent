import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const state = vi.hoisted(() => ({
  focused: 'saved', owner: { connectionId: 'local', profile: 'writer' },
  sessions: {} as Record<string, unknown>, approvals: [] as unknown[], clarify: {} as Record<string, unknown>
}))
vi.mock('@/store/session-focus', () => ({ $focusedStoredSessionId: { get: () => state.focused } }))
vi.mock('@/store/session', () => ({ knownSessionOwner: () => state.owner, ownerLookupSessionRows: () => [], sessionMatchesStoredId: () => false }))
vi.mock('@/store/session-states', () => ({ $sessionTiles: { get: () => [] }, $sessionStates: { get: () => state.sessions } }))
vi.mock('@/store/prompts', () => ({ sessionApprovalRequests: () => ({ get: () => state.approvals }) }))
vi.mock('@/store/clarify', () => ({ $clarifyRequests: { get: () => state.clarify } }))

import type { WebPushEvent } from './web-push'
import { webPushEventWasObserved } from './web-push-observation'

const event: WebPushEvent = { id: 'a', title: 'Reply', body: 'same words', kind: 'reply', created_at: 1,
  target: { profile: 'writer', session_id: 'saved', message_id: '42' } }
const originalElementFromPoint = document.elementFromPoint

function paint(markup: string): void {
  document.body.innerHTML = markup
  let front: Element | null = null
  Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: vi.fn(() => front) })
  for (const element of document.querySelectorAll('*')) {
    vi.spyOn(element, 'getBoundingClientRect').mockImplementation(() => {
      front = element
      return { width: 200, height: 30, top: 10, bottom: 40, left: 10, right: 210 } as DOMRect
    })
  }
}

beforeEach(() => {
  vi.spyOn(document, 'hasFocus').mockReturnValue(true)
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  state.focused = 'saved'; state.owner = { connectionId: 'local', profile: 'writer' }
  state.sessions = { runtime: { storedSessionId: 'saved', messages: [{ id: 'bubble', role: 'assistant', persistedTurn: { final_assistant_row_id: 42 } }] } }
  state.approvals = []; state.clarify = {}
})
afterEach(() => {
  document.body.innerHTML = ''; vi.restoreAllMocks()
  Object.defineProperty(document, 'elementFromPoint', { configurable: true, value: originalElementFromPoint })
})

describe('observed-content acknowledgements', () => {
  it('requires the exact durable assistant row to be painted in the focused owner conversation', () => {
    paint('<div data-push-message-id="bubble" data-push-runtime-id="runtime">same words</div>')
    expect(webPushEventWasObserved(event)).toBe(true)
    expect(webPushEventWasObserved({ ...event, target: { ...event.target, session_id: 'runtime' } })).toBe(true)
    expect(webPushEventWasObserved({ ...event, target: { ...event.target, session_id: 'sibling-branch' } })).toBe(false)
    expect(webPushEventWasObserved({ ...event, target: { ...event.target, message_id: '41' } })).toBe(false)
    state.owner.profile = 'other'
    expect(webPushEventWasObserved(event)).toBe(false)
    state.owner = { connectionId: 'foreign', profile: 'writer' }
    expect(webPushEventWasObserved(event)).toBe(false)
  })

  it('does not mistake a transcript behind an overlay for observed content', () => {
    paint('<div data-push-message-id="bubble" data-push-runtime-id="runtime">same words</div><aside>Settings</aside>')
    vi.mocked(document.elementFromPoint).mockImplementation(() => document.querySelector('aside'))
    expect(webPushEventWasObserved(event)).toBe(false)
  })

  it('never acknowledges a loaded but hidden reply or an unrelated pending request', () => {
    paint('<div aria-hidden="true"><div data-push-message-id="bubble" data-push-runtime-id="runtime">same words</div></div>')
    expect(webPushEventWasObserved(event)).toBe(false)
    state.approvals = [{ serverRequestId: 'new-prompt' }]
    paint('<article data-push-prompt-id="old-prompt" data-push-runtime-id="runtime"></article>')
    expect(webPushEventWasObserved({ ...event, kind: 'approval', target: { ...event.target, prompt_id: 'old-prompt' } })).toBe(false)
    paint('<article data-push-prompt-id="new-prompt" data-push-runtime-id="runtime"></article>')
    expect(webPushEventWasObserved({ ...event, kind: 'approval', target: { ...event.target, prompt_id: 'new-prompt' } })).toBe(true)
  })

  it('matches Group event IDs and task generation on the room owner rather than member profile', () => {
    paint('<section data-push-room-id="room" data-push-profile="writer" data-push-connection="local"><div data-push-room-message="member-event"></div><div data-push-prompt-id="prompt" data-push-generation="7" data-task-id="task" data-member-id="member"></div></section>')
    const target = { profile: 'writer', room_id: 'room', message_id: 'member-event', session_profile: 'hidden-member' }
    expect(webPushEventWasObserved({ ...event, target })).toBe(true)
    expect(webPushEventWasObserved({ ...event, target: { ...target, profile: 'hidden-member' } })).toBe(false)
    const prompt = { ...target, prompt_id: 'prompt', execution_generation: 7, task_id: 'task', member_id: 'member' }
    expect(webPushEventWasObserved({ ...event, kind: 'approval', target: prompt })).toBe(true)
    expect(webPushEventWasObserved({ ...event, kind: 'approval', target: { ...prompt, execution_generation: 6 } })).toBe(false)
  })
})
