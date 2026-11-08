/** Saved cron sessions for exactly one Bot owner. Listing never dials an agent. */
import {
  Button,
  Codicon,
  ErrorState,
  host,
  Loader,
  openCronRun,
  PanelEmpty,
  reconcileCronRunVerdicts,
  relativeTime,
  RowButton,
  Tip,
  useI18n
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'

import { saveSelectedRosterBot } from './bot-state'
import { resolveExpectHistory } from './canonical-chat'
import { useBots } from './i18n'
import { displayName, stripPreviewMarkdown } from './labels'
import {
  backendTargetProfile,
  botConnectionRoute,
  botWorkspaceOwnerKey,
  resolveBotConnectionRoute,
  setBotsWorkspaceOwner
} from './routing'
import type { RosterRow } from './types'

type HistoryPage = Awaited<ReturnType<typeof host.listPersistedSessions>>
export type BotCronRun = HistoryPage['sessions'][number]
export const CRON_HISTORY_PAGE_SIZE = 20

/** Includes the immutable connection route and backend alias, not just the Bot name. */
export function cronHistoryOwnerKey(bot: RosterRow, activeConnectionId: null | string) {
  const route = resolveBotConnectionRoute(bot).route

  return JSON.stringify([
    route?.connectionId || bot.connectionId || activeConnectionId || 'local',
    bot.name,
    backendTargetProfile(route, bot.name)
  ])
}

export async function loadBotCronHistory(bot: RosterRow, offset = 0): Promise<HistoryPage> {
  const route = botConnectionRoute(bot)
  const profile = backendTargetProfile(route, bot.name)
  const page = await host.listPersistedSessions(route, {
    profile,
    limit: CRON_HISTORY_PAGE_SIZE,
    offset,
    source: 'cron',
    order: 'recent',
    minMessages: 0
  })

  // A failed DB read is never a successful empty list. The API is already
  // profile/source scoped; validate rows too so an older server cannot leak
  // ordinary conversations or another profile into this view.
  if (page.errors?.length || page.storage?.[profile] === 'corrupt') {
    throw new Error('Cron history could not be read')
  }

  return {
    ...page,
    sessions: page.sessions
      .filter(run => run.id && run.source === 'cron' && (!run.profile || run.profile === profile))
      .slice(0, CRON_HISTORY_PAGE_SIZE)
  }
}

export async function openBotCronHistoryRun(bot: RosterRow, run: BotCronRun): Promise<void> {
  const route = botConnectionRoute(bot)

  if (run.source !== 'cron' || !run.id || (run.profile && run.profile !== backendTargetProfile(route, bot.name))) {
    return
  }

  const ownerKey = botWorkspaceOwnerKey(bot)
  saveSelectedRosterBot(bot)
  setBotsWorkspaceOwner(ownerKey, bot)
  // Reuse the Cron page's write gate, including zombie local-* runs. This
  // opens an existing transcript; no canonical-chat lookup, create or prompt.
  let opened: Promise<void> | undefined
  openCronRun(run, () => {
    opened = host.openSession(run.id, {
      ...(route ? { route } : {}),
      profile: bot.name,
      intent: 'tab',
      awaitHydration: true,
      expectHistory: resolveExpectHistory(run),
      forceResume: true,
      keepAllProfilesScope: true,
      workspaceMode: 'bots',
      workspaceOwnerKey: ownerKey
    })
  })
  await opened
}

interface HistoryState {
  runs: BotCronRun[]
  nextOffset: number
  hasMore: boolean
}

export function BotCronHistory({ owner }: { owner: RosterRow }) {
  // The parent keys this component by the complete owner. Capture the row so
  // roster polling cannot restart reads, or retarget an in-flight click.
  const [bot] = useState(owner)
  const [history, setHistory] = useState<HistoryState>({
    runs: [],
    nextOffset: 0,
    hasMore: false
  })
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState(false)
  const [opening, setOpening] = useState<null | string>(null)
  const generation = useRef(0)
  const b = useBots()
  const { t } = useI18n()

  async function load(append = false) {
    const request = ++generation.current
    const offset = append ? history.nextOffset : 0
    setBusy(true)
    setError(false)

    try {
      const page = await loadBotCronHistory(bot, offset)

      if (request !== generation.current) {
        return
      }

      reconcileCronRunVerdicts(page.sessions)
      setHistory(previous => {
        const merged = new Map((append ? previous.runs : []).map(run => [run.id, run]))

        for (const run of page.sessions) {
          merged.set(run.id, run)
        }

        const nextOffset = offset + page.sessions.length

        return {
          runs: [...merged.values()],
          nextOffset,
          hasMore: page.sessions.length === CRON_HISTORY_PAGE_SIZE && nextOffset < page.total
        }
      })
    } catch {
      if (request === generation.current) {
        setError(true)
      }
    } finally {
      if (request === generation.current) {
        setBusy(false)
      }
    }
  }

  // Invalidate pending requests on cleanup; this ref never mirrors an atom.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    void load()

    return () => {
      generation.current += 1
    }
    // Owner changes remount this component; reads do not follow roster polls.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const open = async (run: BotCronRun) => {
    setOpening(run.id)

    try {
      await openBotCronHistoryRun(bot, run)
    } catch (failure) {
      host.notifyError?.(failure, b.cron.historyOpenFailed)
    } finally {
      setOpening(null)
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex min-w-0 items-center gap-2 px-3 pt-3 pb-2">
        <div className="min-w-0 flex-1">
          <div className="truncate text-xs font-semibold">{displayName(bot)}</div>
        </div>
        <Tip label={t.common.refresh}>
          <Button
            aria-label={t.common.refresh}
            disabled={busy}
            onClick={() => void load()}
            size="icon-xs"
            variant="ghost"
          >
            <Codicon name="refresh" />
          </Button>
        </Tip>
      </div>
      {busy && !history.runs.length ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader label={t.common.loading} />
        </div>
      ) : error && !history.runs.length ? (
        <div className="flex flex-1 items-center justify-center p-3">
          <ErrorState description={b.cron.historyReadFailure} title={b.cron.historyFailedLoad}>
            <Button onClick={() => void load()} size="sm" variant="secondary">
              {t.common.retry}
            </Button>
          </ErrorState>
        </div>
      ) : !history.runs.length ? (
        <PanelEmpty description={b.cron.historyEmpty} icon="history" title={t.cron.noRuns} />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
          {error ? (
            <div className="grid gap-2 px-3 py-2" role="status">
              <p className="text-xs text-(--ui-text-tertiary)">{b.cron.historyReadFailure}</p>
              <Button onClick={() => void load()} size="sm" variant="secondary">
                {t.common.retry}
              </Button>
            </div>
          ) : null}
          <div className="grid gap-1 px-2 py-1">
            {history.runs.map(run => {
              const timestamp = (run.started_at || run.last_active) * 1000

              return (
                <RowButton
                  className="grid min-w-0 gap-1 rounded-md px-2 py-2 text-left hover:bg-(--chrome-action-hover)"
                  disabled={opening !== null}
                  key={run.id}
                  onClick={() => void open(run)}
                >
                  <span className="truncate text-xs font-medium">{run.title || b.cron.historyUntitled}</span>
                  {run.preview ? (
                    <span className="line-clamp-2 break-words text-xs text-(--ui-text-tertiary)">
                      {stripPreviewMarkdown(run.preview)}
                    </span>
                  ) : null}
                  {Number.isFinite(timestamp) && timestamp > 0 ? (
                    <time
                      className="text-[0.65rem] text-(--ui-text-quaternary)"
                      dateTime={new Date(timestamp).toISOString()}
                      title={`${new Date(timestamp).toLocaleString()} · ${relativeTime(timestamp)}`}
                    >
                      {new Date(timestamp).toLocaleString(undefined, {
                        month: 'numeric',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit'
                      })}
                    </time>
                  ) : null}
                </RowButton>
              )
            })}
          </div>
          {busy ? (
            <div className="flex justify-center p-3">
              <Loader label={t.common.loading} />
            </div>
          ) : history.hasMore ? (
            <div className="px-3 py-2">
              <Button onClick={() => void load(true)} size="sm" variant="ghost">
                {t.sidebar.loadMore}
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
