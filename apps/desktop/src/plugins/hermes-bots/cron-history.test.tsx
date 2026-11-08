import { openCronRun, reconcileCronRunVerdicts } from '@hermes/plugin-sdk'
import type * as HermesSdk from '@hermes/plugin-sdk'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { BOTS_LOCALES } from './i18n'
import { translateBots } from './i18n-test-helper'
import type { RosterRow } from './types'

const { listPersistedSessions, openSession, request, notifyError } = vi.hoisted(() => ({
  listPersistedSessions: vi.fn(),
  openSession: vi.fn(async () => undefined),
  request: vi.fn(),
  notifyError: vi.fn()
}))
vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()
  return {
    ...sdk,
    openCronRun: vi.fn(sdk.openCronRun),
    reconcileCronRunVerdicts: vi.fn(sdk.reconcileCronRunVerdicts),
    host: {
      ...sdk.host,
      listPersistedSessions,
      openSession,
      request,
      notifyError,
      setWorkspaceScope: vi.fn()
    },
    usePluginI18n: () => translateBots
  }
})
vi.mock('./bot-state', () => ({ saveSelectedRosterBot: vi.fn() }))

const { BotCronHistory, cronHistoryOwnerKey, loadBotCronHistory, openBotCronHistoryRun } =
  await import('./cron-history')
type Run = import('./cron-history').BotCronRun
const bot: RosterRow = {
  name: 'radio',
  sourceScoped: true,
  connectionId: 'source-a',
  route: {
    connectionId: 'source-a',
    mode: 'remote',
    profile: 'radio',
    targetProfile: 'backend-radio'
  }
}
const run = (id: string, changes: Partial<Run> = {}): Run => ({
  id,
  title: `Report ${id}`,
  profile: 'backend-radio',
  source: 'cron',
  preview: 'Saved output',
  started_at: 1_794_110_400,
  last_active: 1_794_110_410,
  ended_at: 1_794_110_420,
  message_count: 2,
  tool_call_count: 0,
  input_tokens: 0,
  output_tokens: 0,
  is_active: false,
  model: null,
  ...changes
})
const page = (runs: Run[], total = runs.length) => ({
  sessions: runs,
  total,
  limit: 20,
  offset: 0
})

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(cleanup)

describe('Bot cron history owner and read boundary', () => {
  it('queries the exact source and backend profile, only admitting cron rows for that profile', async () => {
    listPersistedSessions.mockResolvedValueOnce(
      page([run('cron'), run('chat', { source: 'desktop' }), run('other', { profile: 'other-profile' })])
    )
    const result = await loadBotCronHistory(bot, 20)
    expect(listPersistedSessions).toHaveBeenCalledWith(bot.route, {
      profile: 'backend-radio',
      source: 'cron',
      limit: 20,
      offset: 20,
      order: 'recent',
      minMessages: 0
    })
    expect(result.sessions.map(item => item.id)).toEqual(['cron'])
    expect(request).not.toHaveBeenCalled()
  })

  it('keeps same-named bots on different connections and backend aliases distinct', () => {
    const other = {
      ...bot,
      route: { ...bot.route!, connectionId: 'source-b' }
    }
    const alias = {
      ...bot,
      route: { ...bot.route!, targetProfile: 'different-backend' }
    }
    expect(cronHistoryOwnerKey(bot, 'foreground')).not.toBe(cronHistoryOwnerKey(other, 'foreground'))
    expect(cronHistoryOwnerKey(bot, 'foreground')).not.toBe(cronHistoryOwnerKey(alias, 'foreground'))
  })

  it('treats database read failures as failures instead of a successful empty history', async () => {
    listPersistedSessions.mockResolvedValueOnce({
      ...page([]),
      errors: [{ profile: 'backend-radio', error: 'unreadable' }]
    })
    await expect(loadBotCronHistory(bot)).rejects.toThrow()
  })
})

describe('saved runs in the Routines pane', () => {
  it('paginates saved runs and opens an existing run as a Bots tab with the central cron write gate', async () => {
    const rows = Array.from({ length: 20 }, (_, i) => run(String(i)))
    listPersistedSessions.mockResolvedValueOnce(page(rows, 21)).mockResolvedValueOnce(page([run('older')], 21))
    const view = render(<BotCronHistory owner={bot} />)
    await screen.findByText('Report 0')
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    await screen.findByText('Report older')
    expect(listPersistedSessions.mock.calls[1][1].offset).toBe(20)
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    expect(screen.getAllByText('Saved output')).toHaveLength(21)
    expect(view.container.querySelector('time')?.getAttribute('datetime')).toBe(
      new Date(rows[0].started_at * 1000).toISOString()
    )
    fireEvent.click(screen.getByRole('button', { name: /Report older/ }))
    await waitFor(() =>
      expect(openSession).toHaveBeenCalledWith(
        'older',
        expect.objectContaining({
          route: bot.route,
          profile: 'radio',
          intent: 'tab',
          workspaceMode: 'bots',
          workspaceOwnerKey: 'bot:source-a::radio'
        })
      )
    )
    expect(request).not.toHaveBeenCalled()
  })

  it('keeps late reads from the previous Bot out of the new Bot view', async () => {
    let resolveOld!: (value: ReturnType<typeof page>) => void
    listPersistedSessions
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            resolveOld = resolve
          })
      )
      .mockResolvedValueOnce(page([run('new', { profile: 'second-backend' })]))
    const second = {
      ...bot,
      route: {
        ...bot.route!,
        connectionId: 'source-b',
        targetProfile: 'second-backend'
      }
    }
    const view = render(<BotCronHistory key={cronHistoryOwnerKey(bot, null)} owner={bot} />)
    view.rerender(<BotCronHistory key={cronHistoryOwnerKey(second, null)} owner={second} />)
    await screen.findByText('Report new')
    await act(async () => {
      resolveOld(page([run('old')]))
    })
    expect(screen.queryByText('Report old')).toBeNull()
    expect(screen.getByText('Report new')).toBeTruthy()
  })

  it('retains the visible history on a refresh error and offers retry', async () => {
    listPersistedSessions
      .mockResolvedValueOnce(page([run('saved')]))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(page([run('fresh')]))
    render(<BotCronHistory owner={bot} />)
    await screen.findByText('Report saved')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByRole('status')
    expect(screen.getByText('Report saved')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('Report fresh')
    expect(screen.queryByText('Report saved')).toBeNull()
  })

  it('delegates refreshed run rows to the central verdict reconciler', async () => {
    const stopped = run('local-refresh-verdict', { ended_at: null, scheduler_owned: false })
    await openBotCronHistoryRun(bot, stopped)
    expect(openCronRun).toHaveBeenCalledWith(stopped, expect.any(Function))
    const closed = run(stopped.id)
    listPersistedSessions.mockResolvedValueOnce(page([closed]))

    render(<BotCronHistory owner={bot} />)

    await screen.findByText(`Report ${stopped.id}`)
    expect(reconcileCronRunVerdicts).toHaveBeenCalledWith([closed])
  })

  it('distinguishes initial load failure from an empty history', async () => {
    listPersistedSessions.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(page([]))
    render(<BotCronHistory owner={bot} />)
    await screen.findByText('Could not load run history')
    expect(screen.queryByText('No runs yet')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('No runs yet')
  })
})

describe('opening saved cron sessions', () => {
  it('delegates dead never-closed runs to the central gate without creating a chat or calling an agent', async () => {
    const stopped = run('local-zombie', { ended_at: null, scheduler_owned: false })
    await openBotCronHistoryRun(bot, stopped)
    expect(openCronRun).toHaveBeenCalledWith(stopped, expect.any(Function))
    expect(openSession).toHaveBeenCalledTimes(1)
    expect(request).not.toHaveBeenCalled()
  })

  it('refuses a non-cron session or a row from a different profile', async () => {
    await openBotCronHistoryRun(bot, run('chat', { source: 'desktop' }))
    await openBotCronHistoryRun(bot, run('other', { profile: 'other-profile' }))
    expect(openSession).not.toHaveBeenCalled()
  })
})

it('ships history copy in every registered plugin locale', () => {
  for (const messages of Object.values(BOTS_LOCALES)) {
    expect((messages.cron as Record<string, unknown>).historyEmpty).toEqual(expect.any(String))
    expect((messages.cron as Record<string, unknown>).historyFailedLoad).toEqual(expect.any(String))
  }
})
