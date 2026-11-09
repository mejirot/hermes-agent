import { $clarifyRequests } from '@/store/clarify'
import { sessionApprovalRequests } from '@/store/prompts'
import { knownSessionOwner, ownerLookupSessionRows, sessionMatchesStoredId } from '@/store/session'
import { $focusedStoredSessionId } from '@/store/session-focus'
import { $sessionStates, $sessionTiles } from '@/store/session-states'

import type { WebPushEvent, WebPushTarget } from './web-push'

/** A loaded transcript is not proof that the user saw it. Check the painted card. */
export function pushElementIsVisible(element: Element): boolean {
  if (element.closest('[inert], [aria-hidden="true"], [data-push-room-visible="false"]')) { return false }
  const rect = element.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) { return false }
  for (let current: Element | null = element; current; current = current.parentElement) {
    const style = getComputedStyle(current)
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') { return false }
  }
  if (typeof document.elementFromPoint !== 'function') { return false }
  const x = (Math.max(0, rect.left) + Math.min(innerWidth, rect.right)) / 2
  const y = (Math.max(0, rect.top) + Math.min(innerHeight, rect.bottom)) / 2
  const front = document.elementFromPoint(x, y)
  return Boolean(front && element.contains(front))
}

function painted(selector: string, matches: (element: Element) => boolean): boolean {
  return [...document.querySelectorAll(selector)].some(element => matches(element) && pushElementIsVisible(element))
}

function sessionOwnerMatches(storedId: string, profile: string): boolean {
  const owner = $sessionTiles.get().find(tile => tile.storedSessionId === storedId)?.ownerRoute ??
    knownSessionOwner(ownerLookupSessionRows(), storedId)
  // A browser has one serving-origin connection. Never acknowledge a foreign gateway's tile.
  return typeof owner === 'string' ? owner === profile : owner?.connectionId === 'local' && (owner.targetProfile ?? owner.profile) === profile
}

function roomObserved(event: WebPushEvent): boolean {
  const target = event.target
  return painted('[data-push-room-id]', room => {
    if (room.getAttribute('data-push-room-id') !== target.room_id ||
      room.getAttribute('data-push-profile') !== target.profile || room.getAttribute('data-push-connection') !== 'local') { return false }
    if (event.kind === 'approval' || event.kind === 'input') {
      return [...room.querySelectorAll('[data-push-prompt-id]')].some(card =>
        Boolean(target.prompt_id) && card.getAttribute('data-push-prompt-id') === target.prompt_id &&
        (target.execution_generation === undefined || card.getAttribute('data-push-generation') === String(target.execution_generation)) &&
        (!target.task_id || card.getAttribute('data-task-id') === target.task_id) &&
        (!target.member_id || card.getAttribute('data-member-id') === target.member_id) &&
        pushElementIsVisible(card))
    }
    const messageId = target.message_event_id ?? target.message_id
    return Boolean(messageId) && [...room.querySelectorAll('[data-push-room-message]')].some(message =>
      message.getAttribute('data-push-room-message') === String(messageId) && pushElementIsVisible(message))
  })
}

function sessionObserved(event: WebPushEvent, target: WebPushTarget): boolean {
  const storedId = $focusedStoredSessionId.get()
  const targetSessionId = target.session_id
  if (!storedId || !targetSessionId || !sessionOwnerMatches(storedId, target.profile)) { return false }
  return Object.entries($sessionStates.get()).some(([runtimeId, state]) => {
    if (state.storedSessionId !== storedId) { return false }
    const sameConversation = storedId === targetSessionId || runtimeId === targetSessionId ||
      ownerLookupSessionRows().some(row => sessionMatchesStoredId(row, storedId) && sessionMatchesStoredId(row, targetSessionId))
    if (!sameConversation) { return false }
    if (event.kind === 'approval') {
      const pending = sessionApprovalRequests(runtimeId).get().some(request =>
        request.requestId === target.prompt_id || request.serverRequestId === target.prompt_id)
      return Boolean(target.prompt_id) && pending && painted('[data-push-prompt-id]', element =>
        element.getAttribute('data-push-prompt-id') === target.prompt_id && element.getAttribute('data-push-runtime-id') === runtimeId)
    }
    if (event.kind === 'input') {
      const pending = $clarifyRequests.get()[runtimeId]
      return Boolean(target.prompt_id) && pending?.requestId === target.prompt_id && painted('[data-push-prompt-id]', element =>
        element.getAttribute('data-push-prompt-id') === target.prompt_id && element.getAttribute('data-push-runtime-id') === runtimeId)
    }
    if (target.message_id === undefined) { return false }
    const message = state.messages.find(item => item.role === 'assistant' && !item.pending && !item.hidden &&
      (String(item.rowId) === String(target.message_id) || String(item.persistedTurn?.final_assistant_row_id) === String(target.message_id)))
    return Boolean(message && painted('[data-push-message-id]', element =>
      element.getAttribute('data-push-message-id') === message.id && element.getAttribute('data-push-runtime-id') === runtimeId))
  })
}

/** No text/count/time heuristic: missing stable identity deliberately falls back to notification. */
export function webPushEventWasObserved(event: WebPushEvent): boolean {
  if (document.hidden || !document.hasFocus() || !event.target?.profile) { return false }
  return event.target.room_id ? roomObserved(event) : sessionObserved(event, event.target)
}
