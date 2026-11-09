import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { useWebPushCopy } from '@/i18n/web-push'
import { Bell } from '@/lib/icons'
import { WEB_PUSH_KINDS } from '@/lib/web-push'
import { notify } from '@/store/notifications'
import {
  $webPushDevice, disableWebPush, enableWebPush, refreshWebPushStatus,
  sendWebPushTest, setWebPushPreferences
} from '@/store/web-push'

import { ListRow, SectionHeading, ToggleRow } from './primitives'

export function WebPushSettings() {
  const copy = useWebPushCopy()
  const device = useStore($webPushDevice)
  const status = device.status
  const actionable = device.state !== 'unsupported' && device.state !== 'insecure'
  useEffect(() => { void refreshWebPushStatus() }, [])

  const disable = async () => {
    try { await disableWebPush() }
    catch { notify({ kind: 'error', message: copy.disableFailed }) }
  }
  const test = async () => {
    try { await sendWebPushTest(); notify({ kind: 'info', message: copy.testSent }) }
    catch { notify({ kind: 'error', message: copy.testFailed }) }
  }

  return <section aria-label={copy.title} className="mt-5">
    <SectionHeading icon={Bell} title={copy.title} />
    <p className="mb-2 text-xs text-(--ui-text-tertiary)">{copy.description}</p>
    <ListRow action={<div className="flex flex-wrap gap-2">
        {actionable && <Button disabled={device.busy || device.state === 'loading' || (!device.subscribed && device.state === 'unavailable')}
          onClick={() => void (device.subscribed ? disable() : enableWebPush())} size="sm" variant="outline">
          {device.subscribed ? copy.disable : copy.enable}
        </Button>}
        {actionable && <Button disabled={device.busy} onClick={() => void refreshWebPushStatus()} size="sm" variant="text">{copy.retry}</Button>}
      </div>} description={device.subscribed && device.state === 'ready' ? copy.states.subscribed : copy.states[device.state]} title={copy.title} />
    {WEB_PUSH_KINDS.map(kind => <ToggleRow checked={status?.kinds[kind] ?? true}
      disabled={!device.subscribed || device.busy} key={kind} label={copy.kinds[kind]}
      onChange={enabled => status && void setWebPushPreferences({ kinds: { ...status.kinds, [kind]: enabled } })} />)}
    <ToggleRow checked={status?.preview ?? true} description={copy.previewDescription} disabled={!device.subscribed || device.busy}
      label={copy.preview} onChange={preview => void setWebPushPreferences({ preview })} />
    <Button className="mt-3" disabled={!device.subscribed || device.busy} onClick={() => void test()} size="sm" variant="outline">{copy.test}</Button>
    {status?.last_error && <p className="mt-2 text-xs text-destructive" role="alert">{copy.deliveryFailed}</p>}
    <p className="mt-3 text-xs text-(--ui-text-tertiary)">{copy.hint}</p>
    <p className="mt-2 text-xs text-(--ui-text-tertiary)">{copy.browserHint}</p>
  </section>
}
