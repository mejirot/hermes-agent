import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router'

import { Button } from '@/components/ui/button'
import { getSession } from '@/hermes'
import { useWebPushCopy } from '@/i18n/web-push'
import { browserPageBasePath } from '@/lib/browser-settings'
import { WebPushApiError, type WebPushEvent, webPushRequest } from '@/lib/web-push'
import { $selectedStoredSessionId } from '@/store/session'
import { $botChatScopes, $sessionTiles } from '@/store/session-states'

import type { OpenSessionWorkspaceScope } from './open-session'
import { sessionRoute } from './routes'

/** Opens only an authenticated saved target. Never resolves the Bot Chat creation registry. */
export async function openWebPushTarget(event: WebPushEvent, navigate: (to: string, options?: { replace?: boolean }) => void, current: () => boolean = () => true): Promise<boolean> {
  const target = event.target
  if (!target?.profile) { return false }
  const routes = await window.hermesDesktop?.getProfileRoutes([target.profile])
  const owner = routes?.find(route => route.connectionId === 'local' && (route.targetProfile ?? route.profile) === target.profile)
  if (!owner) { throw new Error('The notification owner is unavailable') }
  if (!current()) { return false }
  if (target.room_id) {
    const [{ discoverCanonicalGroups }, { registerCanonicalGroup }, { openGroupChat }] = await Promise.all([
      import('@/plugins/hermes-bots/canonical-groups'),
      import('@/plugins/hermes-bots/canonical-group-registry'),
      import('@/plugins/hermes-bots/group-chat-view')
    ])
    const route = { connectionId: owner.connectionId, profile: owner.profile }
    const { rooms } = await discoverCanonicalGroups(route)
    const room = rooms.find(item => item.room_id === target.room_id)
    if (!room) { throw new Error('The notification room no longer exists') }
    if (!current()) { return false }
    const key = registerCanonicalGroup(route, room)
    navigate(sessionRoute($selectedStoredSessionId.get() ?? ''), { replace: true })
    openGroupChat(key, 'key')
    return true
  }
  if (!target.session_id) { return false }
  const row = await getSession(target.session_id, { connectionId: owner.connectionId, profile: owner.targetProfile ?? owner.profile })
  if (!current()) { return false }
  const previous = $sessionTiles.get().find(tile => tile.storedSessionId === row.id) ?? $botChatScopes.get()[row.id]
  const scope: OpenSessionWorkspaceScope = previous ? { ...previous, ownerRoute: owner, workspaceMode: previous.workspaceMode ?? 'sessions' }
    : { ownerRoute: owner, workspaceMode: row.title === 'Bot Chat' ? 'bots' as const : 'sessions' as const,
      workspaceOwnerKey: row.title === 'Bot Chat' ? `${owner.connectionId}::${owner.profile}` : undefined }
  const [{ host }, { resolveExpectHistory }] = await Promise.all([
    import('@/sdk'), import('@/plugins/hermes-bots/canonical-chat')
  ])
  if (!current()) { return false }
  navigate(sessionRoute($selectedStoredSessionId.get() ?? ''), { replace: true })
  await host.openSession(row.id, {
    route: owner,
    profile: owner.profile,
    intent: 'stack',
    awaitHydration: true,
    expectHistory: resolveExpectHistory(row),
    forceResume: true,
    keepAllProfilesScope: true,
    workspaceMode: scope.workspaceMode,
    workspaceOwnerKey: scope.workspaceOwnerKey,
    tabTitle: scope.workspaceTabTitle
  })
  return true
}

export function PushNotificationOpenView() {
  const { eventId } = useParams()
  const navigate = useNavigate()
  const copy = useWebPushCopy()
  const [attempt, setAttempt] = useState(0)
  const [event, setEvent] = useState<WebPushEvent | null>(null)
  const [error, setError] = useState(false)
  const [signedOut, setSignedOut] = useState(false)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let current = true
    setLoading(true); setError(false); setSignedOut(false); setEvent(null)
    void (async () => {
      try {
        if (!eventId || !/^[A-Za-z0-9_-]{1,160}$/.test(eventId)) { throw new Error('Invalid notification') }
        const detail = await webPushRequest<WebPushEvent>(`/notifications/${encodeURIComponent(eventId)}`)
        if (!current) { return }
        setEvent(detail)
        await openWebPushTarget(detail, navigate, () => current)
      } catch (failure) {
        if (current) {
          setError(true)
          setSignedOut(failure instanceof WebPushApiError && (failure.status === 401 || failure.status === 403))
        }
      } finally { if (current) { setLoading(false) } }
    })()
    return () => { current = false }
  }, [attempt, eventId, navigate])

  const signIn = () => {
    const url = new URL(`${browserPageBasePath()}/login`, location.origin)
    url.searchParams.set('next', `${location.pathname}${location.search}${location.hash}`)
    location.assign(url.href)
  }

  return <section className="mx-auto flex w-full max-w-xl flex-col gap-4 p-6" role="status">
    <h1 className="text-lg font-medium">{loading ? copy.opening : error ? copy.openFailed : event?.title ?? copy.details}</h1>
    {!loading && event && <>
      {event.body && <p className="whitespace-pre-wrap break-words text-sm">{event.body}</p>}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-xs">
        <dt>{copy.profile}</dt><dd>{event.target.profile}</dd>
        {event.target.job_id && <><dt>{copy.job}</dt><dd className="break-all">{event.target.job_id}</dd></>}
        {event.target.execution_id && <><dt>{copy.execution}</dt><dd className="break-all">{event.target.execution_id}</dd></>}
      </dl>
      {!event.target.session_id && !event.target.room_id && <p className="text-xs text-(--ui-text-tertiary)">{copy.readOnly}</p>}
    </>}
    {!loading && <div className="flex gap-2">
      {error && <Button onClick={() => signedOut ? signIn() : setAttempt(value => value + 1)} size="sm" variant="outline">{signedOut ? copy.signIn : copy.retry}</Button>}
      <Button onClick={() => navigate('/settings?tab=notifications')} size="sm" variant="text">{copy.settings}</Button>
    </div>}
  </section>
}
